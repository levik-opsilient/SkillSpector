# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""API Key Pool — multi-key scheduler with rate-limit-aware retry.

Provides a K8s-scheduler-style resource pool for LLM API keys.  When a key
hits rate-limit (HTTP 429), the pool marks it as ``rate_limited`` with
exponential backoff, switches to an idle key, and retries transparently.
This keeps worker throughput stable without the caller knowing which key
is in use.

Integration point
-----------------
Wrap a LangChain ``BaseChatModel`` with :class:`PooledChatModel` to give
it transparent access to the key pool.  The wrapper is API-compatible with
the models returned by :func:`skillspector.llm_utils.get_chat_model` and
can be used wherever a standard ``BaseChatModel`` is expected.

Configuration
-------------
Multi-key mode (recommended for batch scans)::

    export SKILLSPECTOR_API_KEYS="
      sk-or-xxx1|https://api.openai.com/v1|gpt-5.4
      sk-or-xxx2|https://api.openai.com/v1|gpt-5.4
    "

Single-key mode (backward-compatible — no pool needed)::

    export OPENAI_API_KEY=sk-or-xxx1

When ``SKILLSPECTOR_API_KEYS`` is not set, :func:`create_api_key_pool_from_env`
returns ``None`` and the caller should fall back to the single-key provider path.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from skillspector.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Multi-key configuration env var (pipe-delimited: key|base_url|model)
_API_KEYS_ENV = "SKILLSPECTOR_API_KEYS"

# How many times to retry on rate-limit before giving up
_MAX_RATE_LIMIT_RETRIES = 5

# Exponential backoff base (seconds) for consecutive 429s on a single key
_BACKOFF_BASE_S = 30.0

# Maximum backoff cap (seconds) — 5 minutes
_BACKOFF_CAP_S = 300.0


# ---------------------------------------------------------------------------
# ApiKey — single key tracked by the pool
# ---------------------------------------------------------------------------


@dataclass
class ApiKey:
    """A single API key with scheduling metadata.

    Attributes
    ----------
    key :
        API key string (e.g. ``"sk-or-xxx"``).
    base_url :
        Optional base URL override for the provider endpoint.
    model :
        Model label to use with this key.
    status :
        Current scheduling state: ``"idle"`` (available), ``"in_use"``
        (assigned to a caller), or ``"rate_limited"`` (cooling down after
        a 429 response).
    rate_limited_until :
        Monotonic timestamp when this key becomes eligible again after a
        429.  Only meaningful when *status* is ``"rate_limited"``.
    consecutive_429 :
        Count of consecutive rate-limit hits.  Used to compute the next
        backoff duration via :math:`30 \\times 2^n` seconds, capped at 300.
    total_requests :
        Cumulative request count served by this key.  Used for
        least-loaded scheduling.
    """

    key: str
    base_url: str | None
    model: str
    status: Literal["idle", "in_use", "rate_limited"] = "idle"
    rate_limited_until: float = 0.0
    consecutive_429: int = 0
    total_requests: int = 0


# ---------------------------------------------------------------------------
# ApiKeyPool — multi-key scheduler
# ---------------------------------------------------------------------------


class ApiKeyPool:
    """Thread-safe pool of API keys with K8s-scheduler-style allocation.

    The pool tracks each key's state (idle / in_use / rate_limited), handles
    automatic recovery of rate-limited keys after their backoff expires, and
    performs least-loaded scheduling among idle keys.

    Usage::

        pool = ApiKeyPool([ApiKey("sk-a", ...), ApiKey("sk-b", ...)])
        key = pool.acquire()          # blocks until a key is available
        try:
            llm_call(key)
            pool.release(key, success=True)
        except RateLimitError:
            pool.release(key, success=False)
            key = pool.acquire()      # will pick a different key
    """

    def __init__(self, keys: list[ApiKey]) -> None:
        if not keys:
            raise ValueError("ApiKeyPool requires at least one key")
        self._keys = list(keys)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._rate_limits_hit: int = 0
        self._retry_successes: int = 0

    # -- Public API -----------------------------------------------------------

    def acquire(self, timeout: float | None = None) -> ApiKey:
        """Acquire an available key, blocking if all are in use or rate-limited.

        Scheduling priority:

        1. **Recovered keys** — rate-limited keys whose backoff has expired
           are promoted back to ``idle``.
        2. **Idle keys** — pick the one with the fewest ``total_requests``
           (least-loaded scheduling).
        3. **Block** — if no idle key exists, wait for the earliest
           rate-limited key to recover (or until *timeout* seconds pass).

        Parameters
        ----------
        timeout :
            Maximum seconds to wait.  ``None`` means wait indefinitely.

        Returns
        -------
        ApiKey
            An allocated key with ``status == "in_use"``.

        Raises
        ------
        RuntimeError
            If *timeout* expires before a key becomes available.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None

        with self._condition:
            while True:
                now = time.monotonic()

                # Step 1: recover rate-limited keys whose backoff has expired
                self._recover_expired_keys(now)

                # Step 2: find an idle key (least-loaded)
                idle_keys = [k for k in self._keys if k.status == "idle"]
                if idle_keys:
                    key = min(idle_keys, key=lambda k: k.total_requests)
                    key.status = "in_use"
                    key.total_requests += 1
                    logger.debug(
                        "Pool: allocated key ending …%s (requests=%d)",
                        key.key[-8:],
                        key.total_requests,
                    )
                    return key

                # Step 3: all keys busy — compute wait time
                wait_for = self._next_available_in(now)
                if wait_for is None:
                    # No rate-limited keys either — all in_use, no recovery
                    # expected.  Wait for a release signal.
                    remaining = self._remaining_timeout(deadline)
                    if remaining is not None and remaining <= 0:
                        raise RuntimeError(
                            "ApiKeyPool: timed out waiting for available key"
                        )
                    self._condition.wait(timeout=remaining)
                    continue

                # Some keys are rate-limited — wait for the earliest recovery
                remaining = self._remaining_timeout(deadline)
                if remaining is not None and wait_for > remaining:
                    raise RuntimeError(
                        "ApiKeyPool: timed out waiting for available key "
                        f"(next recovery in {wait_for:.1f}s)"
                    )
                logger.debug(
                    "Pool: all keys busy, waiting %.1fs for recovery", wait_for
                )
                self._condition.wait(timeout=min(wait_for, remaining or wait_for))

    def release(self, key: ApiKey, *, success: bool = True) -> None:
        """Return a key to the pool.

        Parameters
        ----------
        key :
            The key previously obtained from :meth:`acquire`.
        success :
            ``True`` if the API call succeeded; ``False`` if it failed with
            a rate-limit error (HTTP 429).  On failure the key is placed in
            ``rate_limited`` state with exponential backoff.
        """
        with self._condition:
            if success:
                key.status = "idle"
                key.consecutive_429 = 0
                logger.debug("Pool: released key ending …%s (ok)", key.key[-8:])
            else:
                key.consecutive_429 += 1
                backoff = min(
                    _BACKOFF_BASE_S * (2 ** (key.consecutive_429 - 1)),
                    _BACKOFF_CAP_S,
                )
                key.rate_limited_until = time.monotonic() + backoff
                key.status = "rate_limited"
                self._rate_limits_hit += 1
                logger.warning(
                    "Pool: key ending …%s rate-limited for %.0fs "
                    "(consecutive=%d)",
                    key.key[-8:],
                    backoff,
                    key.consecutive_429,
                )
            self._condition.notify_all()

    def record_retry_success(self) -> None:
        """Increment the retry-success counter for reporting."""
        with self._lock:
            self._retry_successes += 1

    @property
    def rate_limits_hit(self) -> int:
        """Total number of 429 responses encountered across all keys."""
        with self._lock:
            return self._rate_limits_hit

    @property
    def retry_successes(self) -> int:
        """Total number of successful retries after a key switch."""
        with self._lock:
            return self._retry_successes

    @property
    def keys_active(self) -> int:
        """Number of keys currently in ``in_use`` state."""
        with self._lock:
            return sum(1 for k in self._keys if k.status == "in_use")

    @property
    def keys_configured(self) -> int:
        """Total number of keys in the pool."""
        return len(self._keys)

    def snapshot(self) -> dict[str, object]:
        """Return a snapshot dict suitable for report metadata."""
        with self._lock:
            return {
                "keys_configured": len(self._keys),
                "keys_active": sum(1 for k in self._keys if k.status == "in_use"),
                "keys_rate_limited": sum(
                    1 for k in self._keys if k.status == "rate_limited"
                ),
                "keys_idle": sum(1 for k in self._keys if k.status == "idle"),
                "rate_limits_hit": self._rate_limits_hit,
                "retry_successes": self._retry_successes,
            }

    # -- Internal -------------------------------------------------------------

    def _recover_expired_keys(self, now: float) -> None:
        """Promote rate-limited keys whose backoff has expired to idle."""
        for k in self._keys:
            if k.status == "rate_limited" and now >= k.rate_limited_until:
                k.status = "idle"
                k.consecutive_429 = 0
                logger.info(
                    "Pool: key ending …%s recovered (backoff expired)", k.key[-8:]
                )

    def _next_available_in(self, now: float) -> float | None:
        """Seconds until the earliest rate-limited key recovers, or ``None``."""
        rate_limited = [k for k in self._keys if k.status == "rate_limited"]
        if not rate_limited:
            return None
        earliest = min(k.rate_limited_until for k in rate_limited)
        return max(0.0, earliest - now)

    @staticmethod
    def _remaining_timeout(deadline: float | None) -> float | None:
        """Seconds remaining until *deadline*, or ``None`` if no deadline."""
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())


# ---------------------------------------------------------------------------
# PooledChatModel — transparent key-switching wrapper
# ---------------------------------------------------------------------------


class PooledChatModel:
    """LangChain-compatible chat model wrapper with transparent key switching.

    Each :meth:`invoke` / :meth:`ainvoke` call acquires a key from the pool,
    builds a :class:`~langchain_openai.ChatOpenAI` instance on the fly, and
    releases the key when done.  On rate-limit errors the wrapper releases
    the key with ``success=False``, picks a different key, and retries.

    The caller does not need to know which API key is in use — the pool
    handles scheduling transparently.

    Parameters
    ----------
    pool :
        An :class:`ApiKeyPool` with at least one configured key.
    max_tokens :
        ``max_completion_tokens`` passed to each ``ChatOpenAI`` instance.
    timeout :
        Request timeout in seconds passed to each ``ChatOpenAI`` instance.
    max_retries :
        Maximum number of key-switch retries on rate-limit errors before
        giving up.
    """

    def __init__(
        self,
        pool: ApiKeyPool,
        *,
        max_tokens: int = 4096,
        timeout: float = 30.0,
        max_retries: int = _MAX_RATE_LIMIT_RETRIES,
    ) -> None:
        self._pool = pool
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._max_retries = max_retries

    # -- Public API -----------------------------------------------------------

    def invoke(self, prompt: str) -> object:
        """Synchronous invoke with automatic key switching on rate-limit.

        Parameters
        ----------
        prompt :
            The prompt string to send to the LLM.

        Returns
        -------
        object
            LangChain ``BaseMessage`` response from the LLM.

        Raises
        ------
        RuntimeError
            If all retries are exhausted due to rate-limit errors.
        """
        return self._invoke_with_retry(prompt)

    async def ainvoke(self, prompt: str) -> object:
        """Async invoke with automatic key switching on rate-limit.

        Parameters
        ----------
        prompt :
            The prompt string to send to the LLM.

        Returns
        -------
        object
            LangChain ``BaseMessage`` response from the LLM.

        Raises
        ------
        RuntimeError
            If all retries are exhausted due to rate-limit errors.
        """
        return await self._ainvoke_with_retry(prompt)

    # -- Internal -------------------------------------------------------------

    def _invoke_with_retry(self, prompt: str) -> object:
        """Sync retry loop — acquire key, call LLM, release, retry on 429."""
        last_exception: Exception | None = None

        for attempt in range(self._max_retries + 1):
            key = self._pool.acquire()
            llm = self._build_llm(key)
            try:
                result = llm.invoke(prompt)
                self._pool.release(key, success=True)
                return result
            except Exception as exc:
                if self._is_rate_limit(exc) and attempt < self._max_retries:
                    self._pool.release(key, success=False)
                    self._pool.record_retry_success()
                    logger.debug(
                        "PooledChatModel: rate-limited, retrying "
                        "(attempt %d/%d)",
                        attempt + 1,
                        self._max_retries,
                    )
                    continue
                self._pool.release(key, success=True)
                last_exception = exc
                raise

        raise RuntimeError(
            f"PooledChatModel: exhausted {self._max_retries} retries "
            "due to rate-limit errors"
        ) from last_exception

    async def _ainvoke_with_retry(self, prompt: str) -> object:
        """Async retry loop — acquire key, call LLM, release, retry on 429."""
        last_exception: Exception | None = None

        for attempt in range(self._max_retries + 1):
            key = self._pool.acquire()
            llm = self._build_llm(key)
            try:
                result = await llm.ainvoke(prompt)
                self._pool.release(key, success=True)
                return result
            except Exception as exc:
                if self._is_rate_limit(exc) and attempt < self._max_retries:
                    self._pool.release(key, success=False)
                    self._pool.record_retry_success()
                    logger.debug(
                        "PooledChatModel: rate-limited, retrying "
                        "(attempt %d/%d)",
                        attempt + 1,
                        self._max_retries,
                    )
                    continue
                self._pool.release(key, success=True)
                last_exception = exc
                raise

        raise RuntimeError(
            f"PooledChatModel: exhausted {self._max_retries} retries "
            "due to rate-limit errors"
        ) from last_exception

    def _build_llm(self, key: ApiKey):
        """Build a fresh :class:`~langchain_openai.ChatOpenAI` for *key*.

        Uses :class:`httpx.Timeout` so ``connect`` and ``read`` deadlines
        are independent — a hung server that accepts the TCP handshake but
        never sends a response byte is cut off at ``connect + timeout``
        instead of blocking the worker thread forever.
        """
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        try:
            import httpx
            _timeout = httpx.Timeout(self._timeout, connect=8.0)
        except ImportError:
            _timeout = self._timeout

        return ChatOpenAI(
            model=key.model,
            base_url=key.base_url,
            api_key=SecretStr(key.key),
            max_completion_tokens=self._max_tokens,
            timeout=_timeout,
        )

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        """Detect rate-limit errors from common LLM provider SDKs.

        Checks for ``openai.RateLimitError`` (if available) and falls back
        to inspecting the error message for HTTP 429 indicators.
        """
        # Try explicit OpenAI exception class
        try:
            import openai

            if isinstance(exc, openai.RateLimitError):
                return True
        except ImportError:
            pass

        # Fallback: inspect error string for rate-limit patterns
        message = str(exc).lower()
        for marker in ("429", "rate limit", "rate_limit", "too many requests"):
            if marker in message:
                return True

        return False


# ---------------------------------------------------------------------------
# Factory — create pool from environment
# ---------------------------------------------------------------------------


def create_api_key_pool_from_env() -> ApiKeyPool | None:
    """Build an :class:`ApiKeyPool` from environment variables.

    Reads ``SKILLSPECTOR_API_KEYS`` — a newline- or semicolon-delimited list
    of ``key|base_url|model`` entries::

        export SKILLSPECTOR_API_KEYS="
          sk-or-xxx1|https://api.openai.com/v1|gpt-5.4
          sk-or-xxx2|https://api.openai.com/v1|gpt-5.4
        "

    Also supports a fallback format where multiple keys are specified via
    sequentially numbered env vars ``OPENAI_API_KEY``, ``OPENAI_API_KEY_2``,
    ``OPENAI_API_KEY_3`` etc.

    Returns
    -------
    ApiKeyPool or None
        ``None`` when no multi-key configuration is detected, signaling the
        caller to use the single-key provider path from ``skillspector``.
    """
    keys: list[ApiKey] = []

    # Primary: SKILLSPECTOR_API_KEYS (newline- or semicolon-delimited)
    raw = os.environ.get(_API_KEYS_ENV, "").strip()
    if raw:
        for line in raw.replace(";", "\n").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 1:
                continue
            key_str = parts[0].strip()
            base_url = parts[1].strip() if len(parts) > 1 else None
            model = parts[2].strip() if len(parts) > 2 else "gpt-5.4"
            keys.append(ApiKey(key=key_str, base_url=base_url, model=model))

    # Fallback: OPENAI_API_KEY + OPENAI_API_KEY_2, _3, ...
    if not keys:
        base = os.environ.get("OPENAI_API_KEY", "").strip()
        base_url = os.environ.get("OPENAI_BASE_URL", None)
        if base:
            keys.append(ApiKey(key=base, base_url=base_url, model="gpt-5.4"))
        # Sequentially numbered keys
        for idx in range(2, 10):
            extra = os.environ.get(f"OPENAI_API_KEY_{idx}", "").strip()
            if not extra:
                break
            keys.append(ApiKey(key=extra, base_url=base_url, model="gpt-5.4"))

    if len(keys) <= 1:
        # Single key — no pool needed; caller uses normal provider path
        return None

    logger.info(
        "ApiKeyPool: created pool with %d keys (multi-key mode)", len(keys)
    )
    return ApiKeyPool(keys)
