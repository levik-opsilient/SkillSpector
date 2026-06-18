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

"""Graph invocation helpers for batch scanning.

Thin wrappers over ``skillspector.graph.graph`` — build initial state,
invoke the graph, and transform the raw result dict into a structured
batch entry suitable for downstream reporting.

Thread-safety note
------------------
The module-level patches below run at import time (before any threads
start).  They inject ``response_schema = None`` as an *instance attribute*
inside ``__init__``, which Python MRO resolves before the class-level
``response_schema``.  Each analyzer instance gets its own ``None`` in
``self.__dict__`` — no shared state, no race.

The ``parse_response`` patches handle raw-string responses (JSON parsed
manually) so that providers without structured-output support (e.g.
DeepSeek direct API) work correctly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from skillspector.graph import graph
from skillspector.llm_analyzer_base import LLMAnalyzerBase, LLMAnalysisResult
from skillspector.logging_config import get_logger
from skillspector.nodes.meta_analyzer import LLMMetaAnalyzer, MetaAnalyzerResult

from .annotation import annotate_findings

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# HTTP timeout — stop hung connections from blocking workers forever
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT_REQUEST_TIMEOUT = 30.0  # total request ceiling
_DEFAULT_CONNECT_TIMEOUT = 8.0   # TCP / TLS handshake

# ═══════════════════════════════════════════════════════════════════════════
# Module-level patches (import time — before any thread starts)
# ═══════════════════════════════════════════════════════════════════════════

# -- Patch 1: inject response_schema=None as instance attribute ------------
_original_base_init = LLMAnalyzerBase.__init__


def _patched_base_init(self, base_prompt, model):
    """Set response_schema=None on the instance dict BEFORE original init.

    Python MRO finds the instance attribute first, so the class-level
    ``response_schema = LLMAnalysisResult`` is never reached.  Each
    instance has its own ``None`` — no shared mutable state.
    """
    self.response_schema = None
    _original_base_init(self, base_prompt, model)


LLMAnalyzerBase.__init__ = _patched_base_init


# -- Patch 2: LLMAnalyzerBase.parse_response handles raw JSON --------------
_original_base_parse = LLMAnalyzerBase.parse_response


def _patched_base_parse(self, response, batch):
    """Parse raw LLM text into Findings via manual JSON + Pydantic."""
    if isinstance(response, LLMAnalysisResult):
        return _original_base_parse(self, response, batch)
    text = _strip_markdown_fences(str(response))
    try:
        data = json.loads(text)
        result = LLMAnalysisResult.model_validate(data)
        return [f.to_finding(batch.file_path) for f in result.findings]
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning(
            "LLMAnalyzerBase.parse_response: invalid JSON for %s: %s",
            batch.file_label,
            exc,
        )
        return []


LLMAnalyzerBase.parse_response = _patched_base_parse


# -- Patch 3: LLMMetaAnalyzer.parse_response handles raw JSON ---------------
# Also sanitizes LLM quirks: null string fields, "none" impact value.
_original_meta_parse = LLMMetaAnalyzer.parse_response


def _sanitize_meta_finding(d: dict) -> dict:
    """Fix common LLM output quirks that break downstream consumers."""
    # LLM sometimes emits null for optional string fields
    for key in ("remediation", "explanation"):
        if d.get(key) is None:
            d[key] = ""
    # LLM sometimes emits "none" which is not in the literal enum
    if d.get("impact") not in ("critical", "high", "medium", "low"):
        d["impact"] = "low"
    return d


def _patched_meta_parse(self, response, batch):
    """Parse raw LLM text into meta-analyzer dicts via manual JSON + Pydantic."""
    if isinstance(response, MetaAnalyzerResult):
        return _original_meta_parse(self, response, batch)
    text = _strip_markdown_fences(str(response))
    try:
        data = json.loads(text)
        result = MetaAnalyzerResult.model_validate(data)
        items = []
        for f in result.findings:
            d = _sanitize_meta_finding(f.model_dump())
            d["_file"] = batch.file_path
            items.append(d)
        return items
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning(
            "LLMMetaAnalyzer.parse_response: invalid JSON for %s: %s",
            batch.file_label,
            exc,
        )
        return []


LLMMetaAnalyzer.parse_response = _patched_meta_parse


# -- Patch 4: append JSON output format to base prompt ---------------------
# Without with_structured_output(), the LLM receives no JSON format
# instruction.  We append it so the model responds with parseable JSON
# instead of natural language.
_JSON_OUTPUT_INSTRUCTION = (
    "\n\nRespond with ONLY a JSON object (no markdown, no explanation):\n"
    '{"findings": [{"rule_id": "...", "message": "...", '
    '"severity": "LOW|MEDIUM|HIGH|CRITICAL", "start_line": 1, '
    '"end_line": null, "confidence": 0.0-1.0, '
    '"explanation": "...", "remediation": "..."}]}\n'
    "If no issues found, return: {\"findings\": []}"
)

_original_base_build_prompt = LLMAnalyzerBase.build_prompt


def _patched_base_build_prompt(self, batch, **kwargs):
    prompt = _original_base_build_prompt(self, batch, **kwargs)
    return prompt + _JSON_OUTPUT_INSTRUCTION


LLMAnalyzerBase.build_prompt = _patched_base_build_prompt


# -- Patch 5: append JSON format to meta-analyzer prompt -----------------------
_original_meta_build_prompt = LLMMetaAnalyzer.build_prompt

_META_JSON_PROMPT = (
    "\n\nRespond with ONLY a JSON object (no markdown):\n"
    '{"findings": [{"pattern_id": "...", "is_vulnerability": true|false, '
    '"confidence": 0.0-1.0, "intent": "malicious|negligent|benign", '
    '"impact": "critical|high|medium|low", '
    '"explanation": "...", "remediation": "..."}], '
    '"overall_assessment": {"risk_level": "LOW|MEDIUM|HIGH|CRITICAL", '
    '"summary": "..."}}\n'
    'Rules: never use null — use "" for empty strings. '
    'Never use "none" for impact — use "low" for negligible. '
    'If no findings: {"findings": [], '
    '"overall_assessment": {"risk_level": "LOW", "summary": "No issues found"}}'
)


def _patched_meta_build_prompt(self, batch, **kwargs):
    prompt = _original_meta_build_prompt(self, batch, **kwargs)
    return prompt + _META_JSON_PROMPT


LLMMetaAnalyzer.build_prompt = _patched_meta_build_prompt


# -- Patch 6: enforce HTTP-level timeouts on all ChatOpenAI instances ------
# ChatOpenAI stores timeout internally and caches the OpenAI client inside
# __init__.  Patching after __init__ (e.g. via get_chat_model) is too late
# — the cached client keeps the original timeout.  Instead we inject the
# timeout via __init__ kwargs so it flows into every root_client / async_client
# from the start.
try:
    import httpx
    from langchain_openai import ChatOpenAI as _ChatOpenAI

    _original_chatopenai_init = _ChatOpenAI.__init__

    def _patched_chatopenai_init(self, **kwargs):
        # ``timeout`` is the Pydantic alias for ``request_timeout``.
        # When both keys are present, Pydantic v2 prefers the alias,
        # so we must overwrite the alias — not the canonical name.
        kwargs["timeout"] = httpx.Timeout(
            _DEFAULT_REQUEST_TIMEOUT,
            connect=_DEFAULT_CONNECT_TIMEOUT,
        )
        _original_chatopenai_init(self, **kwargs)

    _ChatOpenAI.__init__ = _patched_chatopenai_init
except ImportError:
    pass


# -- Patch 7: silence "Event loop is closed" noise from httpx cleanup ------
# httpx.AsyncClient internally schedules connection-close tasks.  When
# asyncio.run() tears down the event loop before those tasks finish, they
# fail with RuntimeError("Event loop is closed") and asyncio prints the
# full traceback to stderr.  The error is harmless — the connections are
# already dead — so we suppress the noise without touching any other
# exception path.
import asyncio as _asyncio

_original_asyncio_run = _asyncio.run


def _patched_asyncio_run(main, *, debug=None, loop_factory=None):
    def _make_quiet_loop():
        loop = (loop_factory or _asyncio.new_event_loop)()
        def _handler(loop, context):
            exc = context.get("exception")
            if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
                return  # httpx cleanup after loop teardown — harmless
            loop.default_exception_handler(context)
        loop.set_exception_handler(_handler)
        return loop
    return _original_asyncio_run(main, debug=debug, loop_factory=_make_quiet_loop)


_asyncio.run = _patched_asyncio_run


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    return text.strip()


def scan_state(skill_dir: Path, use_llm: bool) -> dict[str, object]:
    """Build the initial LangGraph state for a single skill directory."""
    return {
        "input_path": str(skill_dir),
        "output_format": "json",
        "use_llm": use_llm,
    }


def _is_windows() -> bool:
    return os.name == "nt"


def cleanup_result(result: dict[str, object]) -> None:
    """Remove the temporary directory created by the graph, if any.

    Uses ``shutil.rmtree`` first (cross-platform).  Falls back to a
    platform-specific subprocess command with a 10-second timeout when
    the tree contains dangling file handles (e.g. stale asyncio HTTP
    connections after a provider error).
    """
    temp_dir = result.get("temp_dir_for_cleanup")
    if not temp_dir or not isinstance(temp_dir, str):
        return
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        try:
            if _is_windows():
                # rmdir /s removes directory tree; /q suppresses confirmation
                subprocess.run(
                    ["cmd", "/c", "rmdir", "/s", "/q", temp_dir],
                    timeout=10,
                    capture_output=True,
                    shell=False,
                )
            else:
                subprocess.run(
                    ["rm", "-rf", temp_dir],
                    timeout=10,
                    capture_output=True,
                )
        except Exception:
            pass


# Number of English-keyword static rules that lose recall for non-English skills.
# These 25 rules are documented in annotation._ENGLISH_KEYWORD_RULES.
_ENGLISH_KEYWORD_RULE_COUNT = 25


def entry_from_result(
    result: dict[str, object],
    skill_dir: Path,
    root: Path,
    *,
    detected_language: str = "en",
    gap_fill_applied: bool = False,
    gap_fill_findings: int = 0,
) -> dict[str, object]:
    """Convert a raw ``graph.invoke()`` result into a batch-report entry.

    Extracts findings, manifest metadata, component metadata, and builds
    the canonical ``skill / risk_assessment / components / issues`` shape
    used by report formatters.  Adds ``source_group``, ``language``,
    ``scan_mode``, and ``enhancements`` fields for provenance tracking
    and comparability with the standard single-skill scan.

    Parameters
    ----------
    result :
        Raw dict returned by ``graph.invoke(state)``.
    skill_dir :
        The skill directory that was scanned.
    root :
        Root directory for relative-path computation.
    detected_language :
        Language detected for this skill (``"en"``, ``"zh"``, etc.).
    gap_fill_applied :
        ``True`` when the gap-fill LLM pass has been applied.
    gap_fill_findings :
        Number of gap-fill findings appended to the issues list.
    """
    findings = result.get("filtered_findings", result.get("findings", []))
    manifest = result.get("manifest") or {}
    component_metadata = result.get("component_metadata") or []
    skill_name = (
        (manifest.get("name") or skill_dir.name) if manifest else skill_dir.name
    )

    try:
        rel_path = str(skill_dir.relative_to(root))
    except ValueError:
        rel_path = str(skill_dir)

    source_group = rel_path.split("/")[0] if "/" in rel_path else "."

    raw_issues: list[dict[str, object]]
    if findings and hasattr(findings[0], "to_dict"):
        raw_issues = [f.to_dict() for f in findings]  # type: ignore[union-attr]
    elif findings:
        raw_issues = list(findings)  # type: ignore[assignment]
    else:
        raw_issues = []

    issues = annotate_findings(raw_issues, detected_language)
    is_non_en = detected_language != "en"

    return {
        "skill": {
            "name": skill_name,
            "source": rel_path,
            "source_group": source_group,
            "language": detected_language,
            "scanned_at": datetime.now(UTC).isoformat(),
        },
        "risk_assessment": {
            "score": result.get("risk_score", 0),
            "severity": result.get("risk_severity", "LOW"),
            "recommendation": (result.get("risk_recommendation") or "SAFE").replace(
                "_", " "
            ),
        },
        "components": [
            {
                "path": c.get("path"),
                "type": c.get("type"),
                "lines": c.get("lines"),
                "executable": c.get("executable"),
                "size_bytes": c.get("size_bytes"),
            }
            for c in component_metadata  # type: ignore[union-attr]
        ],
        "issues": issues,
        "scan_mode": "multilingual-enhanced",
        "enhancements": {
            "gap_fill_applied": gap_fill_applied,
            "gap_fill_findings": gap_fill_findings,
            "english_keyword_rules_skipped": (
                _ENGLISH_KEYWORD_RULE_COUNT if is_non_en else 0
            ),
        },
    }


def run_one(
    skill_dir: Path,
    root: Path,
    *,
    use_llm: bool,
    detected_language: str = "en",
    gap_fill_applied: bool = False,
    gap_fill_findings: int = 0,
) -> tuple[dict[str, object], str | None]:
    """Scan a single skill through the full graph pipeline.

    Parameters
    ----------
    skill_dir :
        Path to the skill directory.
    root :
        Root directory for relative-path computation in reports.
    use_llm :
        Passed through to the graph as ``state["use_llm"]``.
    detected_language :
        Language tag for annotation and reporting.
    gap_fill_applied :
        ``True`` when the caller has applied gap-fill (set by
        :func:`~.batch_scan._scan_skill` after the graph returns).
    gap_fill_findings :
        Number of gap-fill findings appended post-graph.

    Returns
    -------
    ``(entry, error_message_or_None)`` — on success *error_message*
    is ``None``; on failure *entry* is a stub error entry and
    *error_message* carries the exception text.
    """
    result = None
    try:
        state = scan_state(skill_dir, use_llm=use_llm)
        result = graph.invoke(state)
        entry = entry_from_result(
            result,
            skill_dir,
            root,
            detected_language=detected_language,
            gap_fill_applied=gap_fill_applied,
            gap_fill_findings=gap_fill_findings,
        )
        return entry, None
    except Exception as exc:
        rel_name = _rel_name(skill_dir, root)
        error_entry: dict[str, object] = {
            "skill": {
                "name": rel_name,
                "source": str(skill_dir),
                "source_group": rel_name.split("/")[0] if "/" in rel_name else ".",
                "language": detected_language,
                "scanned_at": datetime.now(UTC).isoformat(),
            },
            "risk_assessment": {
                "score": 0,
                "severity": "ERROR",
                "recommendation": "ERROR",
            },
            "components": [],
            "issues": [],
            "scan_mode": "multilingual-enhanced",
            "enhancements": {
                "gap_fill_applied": False,
                "gap_fill_findings": 0,
                "english_keyword_rules_skipped": 0,
            },
            "error": str(exc),
        }
        return error_entry, str(exc)
    finally:
        if result is not None:
            cleanup_result(result)


def _rel_name(skill_dir: Path, root: Path) -> str:
    """Best-effort relative name for display in progress lines."""
    try:
        return str(skill_dir.relative_to(root))
    except ValueError:
        return skill_dir.name
