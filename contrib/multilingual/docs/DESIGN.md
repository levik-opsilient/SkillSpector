# Design — Multilingual Batch Scanner

> Built against SkillSpector v2.2.3.  This contrib module has its own
> independent versioning; the upstream version is noted for compatibility
> reference only.

## Architecture

```
CLI
 │  python -m contrib.multilingual.batch_scan ./skills/ --workers 7
 │
 ▼
batch_scan.py :: main()
 ├─ discover skills (recursive SKILL.md finder)
 ├─ detect language (Unicode script-ratio, per skill)
 ├─ create API pool (optional, 10-key scheduler)
 ├─ ThreadPoolExecutor(max_workers=N)
 │   ├─ Thread A: skill_1 → graph.invoke() + gap-fill
 │   ├─ Thread B: skill_2 → graph.invoke() + gap-fill
 │   └─ ...
 ├─ collect results, sort by risk score
 └─ report (terminal / JSON / Markdown)
```

### Per-skill flow

```
run_one(skill_dir)
 ├─ scan_state()          # build initial LangGraph state
 ├─ graph.invoke(state)   # upstream pipeline (unchanged)
 │   ├─ build_context     # file cache, manifest
 │   ├─ 20 analyzers      # fan-out (15 static + 5 LLM)
 │   └─ meta_analyzer     # LLM verification + enrich
 ├─ entry_from_result()   # extract + annotate
 └─ cleanup_result()      # shutil.rmtree → subprocess fallback
```

## Three-layer concurrency

```
Layer 3 — batch_scan.py:        ThreadPoolExecutor(max_workers=N)  [CONTRIB]
Layer 2 — llm_analyzer_base:    asyncio.Semaphore(10)               [UPSTREAM]
Layer 1 — graph.py:             20 analyzers fan-out                [UPSTREAM]
```

Each layer is unaware of the others.  The graph doesn't know it's being called
concurrently; the workers don't know the graph fans out internally.

## Why ThreadPoolExecutor

- ProcessPoolExecutor hangs on macOS (spawn mode reimports LangGraph per child)
- `graph.invoke()` is a pure function — same state → same result, no shared state
- Each thread operates on its own state dict, isolated from other threads

## The 7 import-time patches

All patches execute at module import (`runner.py`) — before any thread starts.
Each wraps an upstream constructor to inject behavior without modifying
`src/skillspector/`.

| # | Target | Mechanism | Why |
|---|--------|-----------|-----|
| 1 | `LLMAnalyzerBase.__init__` | `self.response_schema = None` (instance attr) | Disable structured output; instance-isolated |
| 2 | `LLMAnalyzerBase.parse_response` | `json.loads` → Pydantic validate | Handle raw string (no `response_format`) |
| 3 | `LLMMetaAnalyzer.parse_response` | Same + sanitize null/`"none"` | LLM output quirks |
| 4 | `LLMAnalyzerBase.build_prompt` | Append JSON output instruction | Model needs format hint |
| 5 | `LLMMetaAnalyzer.build_prompt` | Same | Same |
| 6 | `ChatOpenAI.__init__` | `httpx.Timeout(connect=8s, read=30s)` | Prevent hung connections |
| 7 | `asyncio.run` | Exception handler: drop `Event loop is closed` | Suppress cleanup noise |

### Why instance attributes (Patch 1 is the key insight)

The original approach mutated `LLMAnalyzerBase.response_schema` (class attribute,
shared by all threads).  Race: Thread A restores the original value while
Thread B is still creating instances → `with_structured_output()` fires → 400.

The fix: `self.response_schema = None` writes to the instance `__dict__`.
Python MRO finds the instance attribute before the class attribute.  Each
analyzer instance gets its own `None` — zero shared state, zero races.

### Why `ChatOpenAI.__init__` (Patch 6 pipeline)

httpx defaults: `connect=5.0`, `read=None` (infinite).  A TCP connection that
is accepted but never sends a response byte blocks the worker thread forever.
ThreadPoolExecutor cannot kill threads.

The fix injects `httpx.Timeout` via the `timeout` Pydantic alias **before**
the internal OpenAI client is cached.  `ChatOpenAI`'s Pydantic model defines
`request_timeout` as the canonical field name with `timeout` as its alias
(`populate_by_name=True`).  When both the alias and canonical name appear in
`**kwargs`, Pydantic v2 prefers the alias — so we overwrite `kwargs["timeout"]`
directly rather than setting `kwargs["request_timeout"]`.  This ensures the
``httpx.Timeout(connect=8s, read=30s)` value flows into every `root_client`
and `async_client` from their first instantiation.

## DeepSeek compatibility

DeepSeek's API does not support `response_format` (structured output).
Upstream calls `with_structured_output()` unconditionally.  Without patches,
this returns HTTP 400, corrupting the httpx connection pool.

The fix chain:
1. Patch 1 disables `with_structured_output()` → raw text responses
2. Patches 4/5 append JSON format instructions to every prompt
3. Patches 2/3 parse raw JSON strings manually with Pydantic validation

## Language detection

Unicode script-ratio heuristic, zero additional dependencies (uses `unicodedata`
from stdlib, already imported by upstream).

```
CJK Unified (0x4E00–0x9FFF)    → zh  (≥10% of alpha chars)
Hiragana + Katakana            → ja  (≥5%)
Hangul Syllables (0xAC00–0xD7AF) → ko  (≥10%)
Otherwise                       → en
```

Aggregated per file by majority vote.  Known limitation: Japanese text with
high kanji and low kana density misclassifies as Chinese.

## Gap-fill

When a skill is non-English, 25 English-keyword static rules lose recall.
17 are covered by SSD/SDI/SQP (semantic analyzers).  8 have no equivalent:

**P5** (harmful content), **P6–P8** (system prompt leakage),
**MP1–MP3** (memory poisoning), **RA1–RA2** (rogue agent).

`GapFillAnalyzer` extends `LLMAnalyzerBase` with a language-aware prompt,
runs via `ApiKeyPool` for key failover, and appends findings to the graph result.

## API Pool

Kubernetes-scheduler-inspired design:

```
acquire → pick least-loaded idle key
release(success=True)  → mark idle
release(success=False) → mark rate_limited, backoff 30s × 2^n (cap 300s)
acquire after 429      → picks different key automatically
```

## cleanup_result resilience

```python
try:
    shutil.rmtree(temp_dir, ignore_errors=True)
except Exception:
    subprocess.run(["rm", "-rf", temp_dir], timeout=10, capture_output=True)
```

`shutil.rmtree` blocks on macOS when the directory contains files with
dangling fd (e.g., from corrupted httpx connections).  The subprocess
fallback runs outside the Python process and is unaffected.  Platform
detection (`os.name`) selects `rm -rf` on Unix or `rmdir /s /q` on
Windows.

## Per-skill timeout (90s)

A skill that takes >90s is marked TIMEOUT and skipped.  Other workers continue.
HTTP-level timeouts (Patch 6) prevent most hangs from reaching the 90s ceiling.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All safe |
| 1 | ≥1 skill HIGH or CRITICAL |
| 2 | Scan errors |

## File layout

```
contrib/multilingual/
├── __init__.py          # package init + dotenv preload
├── batch_scan.py        # CLI + ThreadPoolExecutor
├── runner.py            # graph wrapper + 7 patches
├── discovery.py         # SKILL.md finder (24 lines)
├── detection.py         # language detection (77 lines)
├── annotation.py        # finding compatibility labels (86 lines)
├── gap_fill.py          # GapFillAnalyzer (~290 lines)
├── api_pool.py          # ApiKeyPool + PooledChatModel (~570 lines)
├── reports.py           # Terminal / JSON / Markdown (~400 lines)
├── .env.example         # configuration template
└── docs/
    ├── README.md        # user-facing guide
    └── DESIGN.md        # this file
```
