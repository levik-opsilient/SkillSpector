# Multilingual Batch Scanner for SkillSpector

Scans **directories** of AI agent skills in parallel, with automatic language
detection and targeted LLM gap-fill for non-English skills.  Zero changes to
upstream `src/skillspector/`.

## What it does

```
python -m contrib.multilingual.batch_scan ./skills/ -f terminal --workers 7
```

1. Finds all `SKILL.md`-containing directories under the input root
2. Detects language per skill (en / zh / ja / ko)
3. Runs the full SkillSpector graph pipeline per skill in parallel
4. For non-English skills, applies LLM gap-fill for 8 vulnerability rules
   that English-keyword static patterns cannot detect
5. Produces an aggregated report sorted by risk score

## Quickstart

### Prerequisites

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install SkillSpector in development mode
pip install -e .

# Copy and edit the environment template
cp contrib/multilingual/.env.example .env
```

The `.env` file needs these keys (see `.env.example` for the full template):

| Variable | Required | Purpose |
|----------|----------|---------|
| `SKILLSPECTOR_PROVIDER` | Yes | `openai` for DeepSeek/OpenAI-compatible |
| `SKILLSPECTOR_MODEL` | Yes | e.g. `deepseek-v4-flash` |
| `OPENAI_API_KEY` | For single-key | Standard OpenAI-compatible key |
| `OPENAI_BASE_URL` | For single-key | e.g. `https://api.deepseek.com/v1` |
| `SKILLSPECTOR_API_KEYS` | For multi-key | Pipe-delimited: `key\|base_url\|model`, one per line |

> **⚠️ Parallel LLM scanning requires multiple API keys.** With `--workers 4`
> and 1 key, you hit rate limits immediately.  Configure at least as many keys
> as workers — 10 keys for `--workers 8` is safe.  The ApiKeyPool handles
> automatic failover when a key is rate-limited.  If you only have 1 key, use
> `--workers 1` or `--no-llm`.

### Static-only (fast, no API keys needed)

```bash
python -m contrib.multilingual.batch_scan ./skills/ --no-llm
```

### Full LLM scan

```bash
python -m contrib.multilingual.batch_scan ./skills/ -f terminal --workers 7
```

### Test with built-in fixtures

```bash
python -m contrib.multilingual.batch_scan ./tests/fixtures/ -f terminal --workers 8
```

23 skills designed to exercise every detection rule.

## Output formats

| Format | Flag | Use case |
|--------|------|----------|
| Terminal (Rich) | `-f terminal` (default) | Human review |
| JSON | `-f json -o report.json` | CI pipelines |
| Markdown | `-f markdown -o report.md` | PR comments |

### Example: terminal output (23 fixtures, 8 workers)

```
SkillSpector Batch Scan — 23 skill(s) in ./tests/fixtures  (8 workers, 10 API keys)

  [1/23] malicious_skill → 100/100 CRITICAL (14 issue(s))
  [8/23] sdi/sdi1_mismatch → 97/100 CRITICAL (6 issue(s))
  [11/23] sdi/sdi4_divergence → 100/100 CRITICAL (8 issue(s))
  [19/23] ssd/ssd1_semantic_injection → 100/100 CRITICAL (4 issue(s))
  [5/23] mcp_poisoned_tool → 100/100 CRITICAL (16 issue(s))

╭──────────────────────────────────────────────────────────────────╮
│ SkillSpector Batch Scan Report                                   │
╰────────────────── v2.2.3  |  Multilingual Enhanced ──────────────╯

Total: 23 skill(s) scanned

                Skills by Risk Score (23 completed)
┏━━━━━━━━━━━━━━━━━━━━┳━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Skill              ┃ LR ┃   Score ┃ Severity ┃ Issues ┃ Lang ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ chef-assistant     │ ✓  │ 100/100 │ CRITICAL │     14 │ en   │
│ reаd_data          │ ✓  │ 100/100 │ CRITICAL │     16 │ en   │
│ ...                │    │         │          │        │      │
│ safe-greeting      │ ✓  │   0/100 │ LOW      │      0 │ en   │
│ code-reviewer      │ ✓  │   0/100 │ LOW      │      0 │ en   │
└────────────────────┴────┴─────────┴──────────┴────────┴──────┘

15 skill(s) with HIGH or CRITICAL risk — review immediately
6 skill(s) with LOW risk — likely safe
```

**LR column:** Language Reliability. ✓ = English (full static + LLM coverage).
⚠ = non-English (gap-fill applied, 8 extra rules covered).

### LLM vs static comparison (same 23 fixtures, 8 workers)

| Skill | `--no-llm` | LLM mode | What LLM caught |
|-------|-----------|----------|-----------------|
| `ssd1_semantic_injection` | 0/100 (0) | **100/100** (4) | Semantic injection invisible to static |
| `ssd2_novel_phrasing` | 0/100 (0) | **100/100** (3) | Novel phrasing bypasses keyword match |
| `ssd3_nl_exfiltration` | 0/100 (0) | **60/100** (3) | NL-veiled data exfiltration |
| `ssd4_narrative_deception` | 10/100 (1) | **100/100** (9) | Deceptive narrative framing |
| `sdi4_divergence` | 13/100 (2) | **100/100** (8) | Intent-behavior mismatch |
| `sdi1_mismatch` | 52/100 (4) | **97/100** (6) | +2 additional LLM findings |
| `sdi3_scope_creep` | 71/100 (3) | **100/100** (9) | Hidden scope expansion |
| `sqp2_missing_warnings` | 26/100 (2) | **58/100** (3) | Missing safety guardrails |
| `malicious_skill` | 100/100 (6) | 100/100 **(14)** | +8 additional LLM findings |
| `mcp_poisoned_tool` | 100/100 (8) | 100/100 **(16)** | +8 additional LLM findings |
| `safe_skill` | 0/100 (0) | **0/100** (0) | Clean stays clean ✓ |
| `ssd_clean` | 0/100 (0) | **0/100** (0) | Clean stays clean ✓ |

**Key insight:** LLM semantic analyzers (SSD/SDI/SQP) catch entire vulnerability
categories that English-keyword static patterns miss completely.  Clean skills
remain clean — no false-positive inflation.  For skills already flagged by
static rules, LLM finds 2–8 additional issues per skill.

## Tuning `--workers`

| Scenario | Workers | Peak concurrent LLM requests |
|----------|---------|------------------------------|
| Free-tier API key | 1 | 10–15 |
| Paid basic | 4 (default) | 25–40 |
| Enterprise / multi-key | 7–10 | 50–80 |
| Debugging | 1 + `-V` | Sequential, easy to read |

## Language options

```bash
--lang auto    # Unicode script-ratio detection (default)
--lang zh      # Force Chinese
--lang ja      # Force Japanese
--lang ko      # Force Korean
--lang en      # Force English (skip gap-fill)
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All safe (no HIGH/CRITICAL) |
| 1 | ≥1 skill has HIGH or CRITICAL risk |
| 2 | Scan errors occurred |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "No LLM API key configured" | Set up `.env` or use `--no-llm` |
| Connection errors / 429 | Reduce `--workers` |
| Skills timing out (90s) | Check network; the scanner skips and continues |
| "Event loop is closed" | Harmless, suppressed |
| model_info token limit warning | Harmless, 128K default used |

## Known Limitations

1. **Graph-internal LLM calls don't route through ApiKeyPool.**  SSD/SDI/SQP/meta
   share a single key.  Pool failover protects gap-fill only.
2. **No checkpoint/resume.**  A failure at skill 847 of 1000 loses all progress.
3. **Language detection covers 4 scripts.**  Arabic, Hindi, Cyrillic are
   classified as English and lose gap-fill coverage.
4. **No SARIF output.**  Upstream supports it; this contrib adds terminal/JSON/Markdown.
5. **No automated tests.**  All verification has been manual against `tests/fixtures/`.
6. **Gap-fill quality not benchmarked for non-English.**  No ground-truth comparison exists.

See `DESIGN.md` for architecture details and `FUTURE_WORK.md` for suggested directions.
