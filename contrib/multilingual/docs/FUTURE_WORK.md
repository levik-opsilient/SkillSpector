# Future Work — Known Limitations & Suggested Directions

> Honest assessment of what the current version does not yet cover,
> and where a motivated contributor could take it next.

---

## 1. API Key Pool Coverage

**Current state:** Only the gap-fill analyzer routes through `ApiKeyPool`. Graph-internal LLM calls (SSD, SDI, SQP, meta-analyzer) use the single-key path via `get_chat_model()`. This means N parallel workers share a single API key for the bulk of LLM work.

**Impact:** With `--workers 4`, the single key receives concurrent requests from all four skills' internal analyzers, occasionally triggering rate limits. The pool's 10-key failover currently only protects gap-fill.

**Suggested direction:** Patch `LLMAnalyzerBase.__init__` to route `get_chat_model()` through the pool when `SKILLSPECTOR_API_KEYS` is configured. Requires solving the pool-visibility problem (the pool instance must be reachable from the patched `__init__` without global state).

---

## 2. Checkpoint / Resume

**Current state:** A batch scan that fails at skill 847 of 1000 loses all progress. There is no intermediate state written to disk.

**Impact:** Large repositories require restarting from scratch after any failure.

**Suggested direction:** Write per-skill results to a `_batch_checkpoint.jsonl` as each skill completes (before the aggregated report). On restart, skip skills already in the checkpoint. The file doubles as a progress log.

---

## 3. Language Detection Coverage

**Current state:** Unicode script-ratio detection supports four languages (en, zh, ja, ko). Japanese text with high kanji density and low kana frequency can be misclassified as Chinese. Mixed-language skills take a majority vote with no confidence score.

**Impact:** Non-CJK languages (Arabic, Hindi, Cyrillic) are classified as English and lose non-English gap-fill coverage.

**Suggested direction:**
- Add Cyrillic script range (U+0400–U+04FF) → `ru` / `uk`
- Add Arabic script range (U+0600–U+06FF) → `ar`
- Add Devanagari range (U+0900–U+097F) → `hi`
- Return confidence scores alongside language tags for mixed-content skills
- Consider a `--confidence-threshold` flag to control when gap-fill is applied

---

## 4. Output Formats

**Current state:** Terminal (Rich), JSON, and Markdown. Upstream SkillSpector also supports SARIF.

**Impact:** Teams using SARIF-based CI tooling (GitHub Code Scanning, Azure DevOps) cannot ingest batch results directly.

**Suggested direction:** Add `-f sarif` output. SARIF's `runs[].results[].locations[].physicalLocation` maps cleanly to SkillSpector's `Finding.location` / `file` / `start_line` model. Batch-level metadata can live in `runs[].properties`.

Additionally, a **diff mode** (`--diff report1.json report2.json`) that shows which skills changed score between two scans would help teams track security drift over time.

---

## 5. Automated Testing

**Current state:** All verification has been manual — running the 23-skill fixture suite and inspecting terminal output. There are no unit tests for any of the 8 contrib modules.

**Impact:** Refactoring any module risks silent breakage. Language detection accuracy has no baseline measurement.

**Suggested direction:**
- **Unit tests** for pure functions: `detect_language()`, `_strip_markdown_fences()`, `_sanitize_meta_finding()`, `is_language_compatible()`
- **Integration tests** with `--no-llm` against `tests/fixtures/`: verify 23/23 skills complete, exit code matches expectation, JSON output schema is valid
- **Mocked LLM tests** for `GapFillAnalyzer.parse_response()`, `_patched_base_parse()`, `_patched_meta_parse()`
- **Language detection accuracy** benchmark against a curated set of real multi-language skill files

---

## 6. Non-English Gap-Fill Quality Baseline

**Current state:** Gap-fill correctness has been verified by manual inspection of LLM output during development. No systematic ground-truth comparison exists for non-English skills.

**Impact:** We know gap-fill *produces findings*, but we have not measured false-positive rate or recall against known vulnerabilities in non-English skills.

**Suggested direction:** Build a small non-English fixture set (zh/ja/ko skills with known vulnerabilities across the 8 gap-fill rules). Run gap-fill against this set and measure precision/recall. Publish the results as a confidence baseline for users.

---

## Summary

| # | Area | Status | Next Step |
|---|------|--------|-----------|
| 1 | Pool coverage | Gap-fill only | Route graph-internal calls through pool |
| 2 | Checkpoint | None | JSONL progress log + skip-on-restart |
| 3 | Language detection | 4 languages, no confidence | Add Cyrillic/Arabic/Devanagari; return confidence |
| 4 | Output formats | Terminal/JSON/Markdown | Add SARIF + diff mode |
| 5 | Testing | Manual only | Unit + integration + mocked LLM tests |
| 6 | Gap-fill baseline | Not measured | Non-English fixture set + precision/recall |

All six are additive — none require breaking changes to the current API. A contributor can pick one area and ship independently.
