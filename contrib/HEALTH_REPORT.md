# Contrib Health Report — 2026-06-18

## Overview

| Metric | Count |
|--------|-------|
| Files audited | 8 Python + 1 markdown doc |
| Total LOC | ~1,350 |
| **Blocker** | 1 |
| **Critical** | 2 |
| **High** | 4 |
| **Medium** | 6 |
| **Low / Style** | 5 |

---

## BLOCKER — Must Fix Before Production

### B1. `runner.py:172-220` — **Monkey-patch race condition destroys response_schema**

**File:** `contrib/multilingual/runner.py`  
**Lines:** 172-218  
**Severity:** BLOCKER

**What it does:**
```python
_saved_base = _Base.response_schema       # Thread A: saves None (already patched by Thread B)
_saved_meta = _Meta.response_schema
_Base.response_schema = None
_Meta.response_schema = None
try:
    result = graph.invoke(state)           # synchronous, blocks this thread
    ...
finally:
    _Base.response_schema = _saved_base   # Thread A: restores None (the WRONG value!)
    _Meta.response_schema = _saved_meta
```

**The race (4 threads in ThreadPoolExecutor):**

```
T0: LLMAnalyzerBase.response_schema = LLMAnalysisResult (original)
T1: Thread A saves original → sets None → graph.invoke(skill_1) [blocked]
T2: Thread B saves None       → sets None → graph.invoke(skill_2) [blocked]
T3: Thread A finishes → restores original ✓
T4: Thread B finishes → restores None     ← PERMANENTLY DESTROYS original
T5: All future threads: schema = None (mostly fine for this run, but state is corrupted)
```

**Worse — meta_analyzer created late in LangGraph:**
```
Thread B is inside graph.invoke(), past the fan-out phase.
Thread A finishes, restores MetaAnalyzerResult.
Thread B now creates LLMMetaAnalyzer instance → with_structured_output(MetaAnalyzerResult).
DeepSeek returns 400 → httpx connection pool corrupted → cleanup_result hangs.
```

This is the **root cause** of the 3 known symptom chains:

1. **Sporadic 400 errors** — when a meta_analyzer instance is created after another thread restored the schema
2. **cleanup_result hang** — corrupted httpx connection pool from 400 responses
3. **Non-deterministic behavior** — depends on thread timing, which is why `--no-llm` (no LLM → no 400 → no hang) always works and LLM path sometimes works / sometimes hangs

**Recommended fix:**
```python
# Option A: Thread-local override (safe, no global state)
import threading
_thread_local = threading.local()

def run_one(...):
    token = object()  # unique sentinel
    _thread_local.suppress_response_schema = token
    try:
        ...
    finally:
        _thread_local.suppress_response_schema = None
```
But this requires patching `LLMAnalyzerBase.__init__` to check the thread-local flag.

**Option B (better): Make `response_schema` an instance attribute via constructor injection.**
This is the cleanest approach but requires changes to `LLMAnalyzerBase`, which is in `src/` (not `contrib/`). The zero-intrusion constraint makes this harder.

**Option C (pragmatic, safest for now): Serialize the monkey-patch with a lock.**
```python
_patch_lock = threading.Lock()

def run_one(...):
    with _patch_lock:
        _saved_base = _Base.response_schema
        _saved_meta = _Meta.response_schema
        _Base.response_schema = None
        _Meta.response_schema = None
    try:
        result = graph.invoke(state)
        ...
    finally:
        with _patch_lock:
            _Base.response_schema = _saved_base
            _Meta.response_schema = _saved_meta
        cleanup_result(result)
```
Wait, this doesn't work either — if Thread B is waiting for the lock while Thread A is inside `graph.invoke()`, Thread B will block on the lock. The lock must NOT be held during `graph.invoke()`. So the lock only protects the save/restore, not the window during invoke. This means Thread B could still save None after Thread A already set it.

**Option D (actually correct): Reference count.**
```python
_patch_refcount = 0
_patch_lock = threading.Lock()

def run_one(...):
    with _patch_lock:
        if _patch_refcount == 0:
            _saved_base = _Base.response_schema
            _saved_meta = _Meta.response_schema
            _Base.response_schema = None
            _Meta.response_schema = None
        _patch_refcount += 1
    try:
        result = graph.invoke(state)
        ...
    finally:
        with _patch_lock:
            _patch_refcount -= 1
            if _patch_refcount == 0:
                _Base.response_schema = _saved_base
                _Meta.response_schema = _saved_meta
        cleanup_result(result)
```
But `_saved_base` is a local variable — each thread has its own. The first thread to decrement to 0 restores using ITS saved value. If that's the original, great. But which thread saved the original? Only the first thread. The refcount approach works because only the first thread (refcount 0→1) saves, and only the last thread (refcount 1→0) restores using the SAME saved value.

This is the correct pattern.

---

## CRITICAL — Severe Impact

### C1. `runner.py:28-32` — **cleanup_result has no timeout → hangs forever**

**File:** `contrib/multilingual/runner.py`  
**Lines:** 28-32  
**Severity:** CRITICAL

```python
def cleanup_result(result: dict[str, object]) -> None:
    temp_dir = result.get("temp_dir_for_cleanup")
    if temp_dir and isinstance(temp_dir, str):
        shutil.rmtree(temp_dir, ignore_errors=True)
```

`shutil.rmtree` can block indefinitely when the temp dir contains files with open handles (from asyncio HTTP connections left dangling after a 400 error from DeepSeek). `ignore_errors=True` only suppresses exceptions — it does NOT add timeout protection. A blocked `rmtree` call blocks the entire ThreadPool worker forever.

**This is the symptom you observed** — LLM path "completes" but never finishes because one worker is stuck in `rmtree`.

**Fix:**
```python
import subprocess
import shutil

def cleanup_result(result: dict[str, object]) -> None:
    temp_dir = result.get("temp_dir_for_cleanup")
    if temp_dir and isinstance(temp_dir, str):
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            # Fallback: force-remove via subprocess with timeout
            subprocess.run(
                ["rm", "-rf", temp_dir],
                timeout=10,
                capture_output=True,
            )
```

Better yet, use `subprocess` as the primary path and keep `shutil.rmtree` as a Windows fallback, since subprocess-based removal isn't affected by Python-level file handle leaks.

### C2. `runner.py:172` — **No thread-safe guarantee for monkey-patch**

See **B1** above — this is the same issue, listed separately because it has both a correctness dimension (B1) and a safety dimension (C2). The non-thread-safe class-attribute mutation is undefined behavior in Python's memory model.

---

## HIGH — Likely to Cause Problems

### H1. `gap_fill.py:281` — **Bare `except ValueError: raise` swallows all other exceptions**

**File:** `contrib/multilingual/gap_fill.py`  
**Line:** 281  
**Severity:** HIGH

```python
def run_gap_fill(...) -> list[Finding]:
    try:
        analyzer = GapFillAnalyzer(...)
        batches = analyzer.get_batches(...)
        results = analyzer.run_batches(batches, language=language)
        return analyzer.collect_findings(results)
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("Gap-fill analysis failed: %s", exc)
        return []
```

The `except ValueError: raise` line re-raises `ValueError` while silently swallowing ALL other exceptions (including `TypeError`, `AttributeError`, `RuntimeError`). This means:
- A bug in `get_batches` (e.g., `NoneType` error) → silently returns `[]`
- A bug in `run_batches` → silently returns `[]`
- A corrupted model config → silently returns `[]`

The user never knows gap-fill silently failed. This pattern masks real bugs.

**Fix:** Log ALL exceptions at warning level, not just non-ValueError. Or better, only catch specific known-recoverable exceptions.

### H2. `batch_scan.py:340-360` — **RuntimeError retry swallows the original exception**

**File:** `contrib/multilingual/batch_scan.py`  
**Lines:** 340-360  
**Severity:** HIGH

```python
except RuntimeError:
    try:
        new_future = executor.submit(...)
        entry, error_msg, rel_name = new_future.result(timeout=300)
    except Exception:
        errors += 1
        ...
        continue
```

The outer `except RuntimeError` catches ALL RuntimeErrors, not just the expected "event loop closed" crash. If a genuine RuntimeError occurs (e.g., from the API pool), it triggers an unnecessary retry that wastes 300 seconds.

**Fix:** Check the exception message:
```python
except RuntimeError as exc:
    if "event loop" not in str(exc).lower():
        raise  # genuine error, don't retry
```

### H3. `reports.py:389` — **`float(None)` would crash the Markdown report**

**File:** `contrib/multilingual/reports.py`  
**Line:** 389  
**Severity:** HIGH

```python
conf = issue.get("confidence", 0)
lines.append(f"  - Confidence: {float(conf):.0%}")
```

If `issue["confidence"]` exists but is `None`, then `conf = None` (`.get("confidence", 0)` returns the stored value, not the default, when the key exists). `float(None)` → `TypeError`, crashing the entire report generation.

**Fix:** `float(issue.get("confidence") or 0)` or `float(conf if conf is not None else 0)`.

### H4. `api_pool.py:396-457` — **60 lines of duplicated sync/async retry logic**

**File:** `contrib/multilingual/api_pool.py`  
**Lines:** 403-457  
**Severity:** HIGH (maintainability)

`_invoke_with_retry` and `_ainvoke_with_retry` are ~30 lines each, identical except for `llm.invoke(prompt)` vs `await llm.ainvoke(prompt)`. Any bug fix in one must be manually mirrored to the other. Already observed: both methods have the same `record_retry_success` ordering bug (see M2).

---

## MEDIUM — Should Be Addressed

### M1. `detection.py:55-60` — **Language classification order creates Japanese→Chinese misclassification risk**

**File:** `contrib/multilingual/detection.py`  
**Lines:** 55-60  
**Severity:** MEDIUM

```python
if kana / alpha > _KANA_THRESHOLD:    # checked first
    return "ja"
if hangul / alpha > _HANGUL_THRESHOLD:  # checked second
    return "ko"
if cjk / alpha > _CJK_THRESHOLD:        # checked third
    return "zh"
```

A Japanese document heavy on kanji (CJK characters) with few kana characters will be classified as Chinese. This is a known limitation of script-ratio detection. Acceptable for a heuristic, but should be documented.

### M2. `api_pool.py:417` — **`record_retry_success` counted even when retry hasn't succeeded yet**

**File:** `contrib/multilingual/api_pool.py`  
**Line:** 417  
**Severity:** MEDIUM

```python
if self._is_rate_limit(exc) and attempt < self._max_retries:
    self._pool.release(key, success=False)
    self._pool.record_retry_success()  # Counted BEFORE the retry outcome
    ...
    continue
```

The counter is incremented when a retry is ATTEMPTED, not when it succeeds. If the retry also fails (another 429), it's still counted as a "success". The method name and docstring (`record_retry_success`) are misleading — it should be `record_retry_attempt` or the increment should move to after a successful retry.

### M3. `batch_scan.py:309-310` — **Double file I/O for non-English skills**

**File:** `contrib/multilingual/batch_scan.py`  
**Lines:** 309-310 + 151  
**Severity:** MEDIUM (performance)

Language detection reads all files in the main thread (`_resolve_language`), then gap-fill re-reads the same files inside the worker thread (`_read_skill_files` on line 151). For a skill with 50 files, this is 50 unnecessary `read_text` calls.

**Fix:** Pass the already-read `file_cache` from `_resolve_language` through to `_scan_skill` instead of re-reading.

### M4. `__init__.py:23-28` + `batch_scan.py:37-43` — **Double dotenv loading**

**File:** `contrib/multilingual/__init__.py` + `contrib/multilingual/batch_scan.py`  
**Severity:** MEDIUM (fragility)

Both files load `.env` with `override=True`. This is idempotent but fragile:
- If someone changes one but not the other, behavior diverges
- `find_dotenv(usecwd=True)` searches from cwd upward; running from a different directory might find a different `.env` or none

**Fix:** Load only in `__init__.py`, add a comment in `batch_scan.py` explaining it's already loaded by the package import.

### M5. `reports.py:40` — **StringIO-based Rich capture fragile across Rich versions**

**File:** `contrib/multilingual/reports.py`  
**Line:** 40  
**Severity:** MEDIUM

```python
capture = Console(record=True, force_terminal=True, width=80, file=StringIO())
```

This works with Rich 14.x but `Console(record=True, file=StringIO())` has had subtle behavior changes across Rich versions. On some versions, `export_text()` returns empty string when `file` is set to a non-TTY.

**Fix:** Use `Console(record=True)` without `file=`, then `capture.export_text()` to get the output. Or use `rich.console.Capture` context manager.

### M6. `gap_fill.py:197-202` — **Markdown fence stripping can't handle ````json` fences**

**File:** `contrib/multilingual/gap_fill.py`  
**Lines:** 197-202  
**Severity:** MEDIUM

```python
if text.startswith("```"):
    first_nl = text.find("\n")
    if first_nl != -1:
        text = text[first_nl + 1:]
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3].rstrip()
```

This only handles exactly ```` ``` ```` (3 backticks). If the LLM outputs ```` ```json ```` (common), the first line is ```` ```json```` — after `first_nl` split, it drops that line correctly. But the closing check only looks for exactly ```` ``` ```` at the end. If the LLM outputs ```` ```json ```` at the end, it won't match. Unlikely but possible.

---

## LOW — Style / Polish

### L1. `batch_scan.py:137-139` — **Dead comment, actual warning is on line 367**

The comment on lines 136-139 describes a warning that isn't emitted there. The real warning is 230 lines later. Confusing for future readers.

### L2. `reports.py:273` — **`languages_detected` dict comprehension iterates results twice**

Minor performance concern, but the dict comprehension on line 273-276 iterates all results to count per language, while the same data was already partially collected on lines 254-264. Could be unified.

### L3. `annotation.py:58-68` — **`_ENGLISH_KEYWORD_RULES` defined but only used for documentation**

The frozenset `_ENGLISH_KEYWORD_RULES` is defined on lines 46-55 with a docstring saying "listed for documentation." It's never referenced in any logic — `is_language_compatible` computes compatibility via set exclusion (`rule_id in _SEMANTIC_RULES | _CODE_RULES | _GAP_FILL_RULES`). This is consistent but the unused frozenset should have a comment explicitly stating it's reference-only.

### L4. `detection.py:52-53` — **`alpha == 0` returns "en" — should maybe return "unknown"**

If a file has zero letter characters (e.g., a binary file or purely numeric), classifying it as English is a silent default. Consider returning `None` or `"unknown"` and letting the caller decide.

### L5. `runner.py:87-92` — **`hasattr(findings[0], "to_dict")` fragile for mixed-type lists**

If `findings` contains objects of different types (some with `to_dict`, some without), only the first element is checked. In practice this doesn't happen because the graph always returns homogeneous lists, but the pattern is fragile.

---

## Root Cause Analysis — Why So Many Problems?

The problems cluster around 3 architectural tensions:

### 1. Zero-Intrusion Constraint vs. DeepSeek Reality

The rule "don't modify `src/skillspector/`" forced the monkey-patch approach. `LLMAnalyzerBase` uses `response_schema` as a class attribute read at `__init__` time, and `with_structured_output()` is called unconditionally when the schema is non-None. The clean fix — making `response_schema` injectable via constructor or environment variable — would require a one-line change in the base class:

```python
# In LLMAnalyzerBase.__init__:
schema_override = os.environ.get("SKILLSPECTOR_FORCE_RAW_LLM")
self._effective_schema = None if schema_override else self.response_schema
```

But this violates zero-intrusion. The monkey-patch is the price paid for that constraint.

### 2. LangGraph's asyncio.run() in ThreadPoolExecutor

LangGraph internally uses `asyncio.run()` for parallel LLM calls. When running inside a `ThreadPoolExecutor` worker thread, each `asyncio.run()` creates and destroys an event loop. If an HTTP connection from a 400 error isn't cleanly closed, the event loop shutdown leaves dangling resources that block filesystem operations on macOS (observed as `shutil.rmtree` hang).

This is a known Python/asyncio sharp edge on macOS — `asyncio` + `httpx` + thread pools + file cleanup is a toxic combination.

### 3. DeepSeek's Missing `response_format` Support

Every problem traces back to this: DeepSeek's API doesn't support `response_format` with structured output schemas. This is the first domino:

```
No response_format → with_structured_output() 400
  → monkey-patch needed (B1)
  → meta_analyzer race condition (B1)
  → httpx connection corruption
  → cleanup_result hang (C1)
  → gap_fill raw string parser needed (M6)
```

If DeepSeek supported `response_format`, none of these problems would exist.

---

## Priority Action Plan

| Order | Issue | Effort | Impact |
|-------|-------|--------|--------|
| 1 | **B1**: Fix monkey-patch with refcount | ~20 lines | Unblocks LLM path |
| 2 | **C1**: Timeout-protect cleanup_result | ~10 lines | Prevents hang |
| 3 | **H4**: Deduplicate invoke/ainvoke | ~30 lines | Prevents future bugs |
| 4 | **H1**: Fix gap_fill exception swallowing | ~5 lines | Don't hide bugs |
| 5 | **H2**: Narrow RuntimeError retry | ~5 lines | Don't retry real errors |
| 6 | **H3**: Fix float(None) crash | ~5 lines | Markdown report safety |
| 7 | **M3**: Eliminate double file I/O | ~15 lines | Perf improvement |
| 8 | **M1-M6**: Remaining medium issues | ~30 lines | Polish |

**Total estimated effort:** ~120 lines of changes across 6 files.

---

## Files NOT Needing Changes

- `annotation.py` — Clean, well-structured, correct logic
- `discovery.py` — Minimal, correct, no issues found
- `api_pool.py` — Well-designed core (acquire/release/scheduling), only the wrapper has duplication
