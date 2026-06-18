# Quickstart Guide

## Prerequisites

```bash
# Activate the virtual environment
source .venv/bin/activate

# Verify SkillSpector works
skillspector scan ./tests/fixtures/malicious_skill/ --no-llm
```

Set up API keys for LLM mode (`.env` at repo root).  Copy the template:

```bash
cp contrib/multilingual/.env.example .env
# Edit .env with your actual keys
```

> ⚠️ **Parallel LLM scanning requires multiple API keys.**  Each worker thread
> issues LLM calls concurrently.  With 1 key and 4 workers, you will hit rate
> limits (HTTP 429) almost immediately.  **Configure at least as many keys as
> workers** — 10 keys for `--workers 8` is a safe ratio.  The built-in
> ApiKeyPool handles automatic failover when a key is rate-limited.
>
> If you only have 1 key, use `--workers 1` for LLM mode, or `--no-llm` for
> static-only mode (no API keys needed at all).

```bash
# Single key — use --workers 1 only
OPENAI_API_KEY=sk-or-xxxxxxxxxxxxxxxxxxxxxxxx

# Multi-key pool — required for --workers >= 2
# Format: key|base_url|model, one per line or semicolon-delimited
SKILLSPECTOR_API_KEYS="
sk-or-xxx1|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx2|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx3|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx4|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx5|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx6|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx7|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx8|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx9|https://api.deepseek.com/v1|deepseek-v4-flash
sk-or-xxx10|https://api.deepseek.com/v1|deepseek-v4-flash
"

# Active provider
SKILLSPECTOR_PROVIDER=openai
SKILLSPECTOR_MODEL=deepseek-v4-flash
```

## Basic Usage

### Static-only batch (fastest, no API keys needed)

```bash
python -m contrib.multilingual.batch_scan ./skills/ --no-llm
```

Scans all skills in `./skills/`, terminal output, 4 workers. ~0.1s per skill.

### Full LLM batch

```bash
python -m contrib.multilingual.batch_scan ./skills/ -f terminal --workers 4
```

Same but with LLM semantic analysis. ~5-30s per skill depending on file count.

### Test with the built-in fixtures

```bash
# Static mode (sub-second)
python -m contrib.multilingual.batch_scan ./tests/fixtures/ -f terminal --workers 4 --no-llm

# LLM mode (~3 min with 7 workers)
python -m contrib.multilingual.batch_scan ./tests/fixtures/ -f terminal --workers 7
```

23 skills, designed to test every detection rule.

## Output Formats

```bash
# Terminal (default) — human-readable table with colors
python -m contrib.multilingual.batch_scan ./skills/ -f terminal

# JSON — machine-readable, good for CI pipelines
python -m contrib.multilingual.batch_scan ./skills/ -f json -o report.json

# Markdown — good for PR comments, docs
python -m contrib.multilingual.batch_scan ./skills/ -f markdown -o report.md
```

### Example: Terminal Output (fixture scan with 8 workers)

```
$ python -m contrib.multilingual.batch_scan ./tests/fixtures/ -f terminal --workers 8

SkillSpector Batch Scan — 23 skill(s) in ./tests/fixtures  (8 workers, 10 API keys)

  [7/23] safe_skill → 0/100 LOW (0 issue(s))
  [8/23] sdi/sdi1_mismatch → 97/100 CRITICAL (6 issue(s))
  [3/23] mcp_mismatched_skill → 100/100 CRITICAL (9 issue(s))
  [1/23] malicious_skill → 100/100 CRITICAL (14 issue(s))
  [11/23] sdi/sdi4_divergence → 100/100 CRITICAL (8 issue(s))
  [19/23] ssd/ssd1_semantic_injection → 100/100 CRITICAL (4 issue(s))
  [5/23] mcp_poisoned_tool → 100/100 CRITICAL (16 issue(s))

╭──────────────────────────────────────────────────────────────────╮
│ SkillSpector Batch Scan Report                                   │
╰────────────────── v2.2.3  |  Multilingual Enhanced ──────────────╯

Total: 23 skill(s) scanned

Source Breakdown:
  .                                 7 skills, 5 CRITICAL, 1 MEDIUM
  sdi                               5 skills, 4 CRITICAL, 1 MEDIUM
  sqp                               6 skills, 1 CRITICAL, 1 HIGH
  ssd                               5 skills, 3 CRITICAL, 1 HIGH

                Skills by Risk Score (23 completed)
┏━━━━━━━━━━━━━━━━━━━━┳━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Skill              ┃ LR ┃   Score ┃ Severity ┃ Issues ┃ Lang ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ chef-assistant     │ ✓  │ 100/100 │ CRITICAL │     14 │ en   │
│ friendly-greeter   │ ✓  │ 100/100 │ CRITICAL │      9 │ en   │
│ reаd_data          │ ✓  │ 100/100 │ CRITICAL │     16 │ en   │
│ deploy-service     │ ✓  │ 100/100 │ CRITICAL │      5 │ en   │
│ onboarding-guide   │ ✓  │ 100/100 │ CRITICAL │      9 │ en   │
│ ...                │    │         │          │        │      │
│ safe-greeting      │ ✓  │   0/100 │ LOW      │      0 │ en   │
│ code-reviewer      │ ✓  │   0/100 │ LOW      │      0 │ en   │
└────────────────────┴────┴─────────┴──────────┴────────┴──────┘

15 skill(s) with HIGH or CRITICAL risk — review immediately
2 skill(s) with MEDIUM risk — review before installing
6 skill(s) with LOW risk — likely safe
```

**Columns:** `LR` = Language Reliability — ✓ for English (full coverage), ⚠ for non-English (gap-fill applied).

### Example: JSON Output (excerpt)

```json
{
  "batch": {
    "scanned_at": "2026-06-19T01:20:00+00:00",
    "total_skills": 23,
    "scan_mode": "multilingual-enhanced",
    "enhancements": {
      "language_detection": "unicode-script-ratio",
      "gap_fill_applied": 0,
      "gap_fill_findings": 0
    }
  },
  "skills": [
    {
      "skill": {
        "name": "malicious_skill",
        "source": "malicious_skill",
        "source_group": ".",
        "language": "en",
        "scanned_at": "2026-06-19T01:20:05+00:00"
      },
      "risk_assessment": {
        "score": 100,
        "severity": "CRITICAL",
        "recommendation": "DO NOT INSTALL"
      },
      "issues": [
        {
          "id": "E1",
          "message": "Skill executes shell commands without user consent",
          "severity": "CRITICAL",
          "confidence": 1.0,
          "language_compatible": true
        }
      ],
      "scan_mode": "multilingual-enhanced",
      "enhancements": {
        "gap_fill_applied": false,
        "gap_fill_findings": 0,
        "english_keyword_rules_skipped": 0
      }
    }
  ]
}
```

### Example: Static-Only vs LLM Comparison

Same 23 fixtures, same 4 workers:

| Skill | `--no-llm` | LLM mode | Delta |
|-------|-----------|----------|-------|
| `ssd1_semantic_injection` | 0/100 (0) | 100/100 (4) | Static blind to semantic injection |
| `ssd3_nl_exfiltration` | 0/100 (0) | 60/100 (3) | Static blind to NL exfiltration |
| `ssd4_narrative_deception` | 10/100 (1) | 100/100 (9) | Static nearly blind |
| `sdi4_divergence` | 13/100 (2) | 100/100 (8) | Static severely underestimates |
| `sqp2_missing_warnings` | 26/100 (2) | 58/100 (3) | Static underestimates |
| `safe_skill` | 0/100 (0) | 0/100 (0) | Correct — no false positive |
| `ssd_clean` | 0/100 (0) | 0/100 (0) | Correct — no false positive |

**Conclusion:** LLM semantic analyzers (SSD/SDI/SQP) catch vulnerabilities that static English-keyword patterns miss entirely. Clean skills remain clean — no false-positive inflation.

## Tuning Workers

| Scenario | --workers | Why |
|----------|-----------|-----|
| Free-tier API key | 1 | Avoid 429 rate limits |
| Paid basic tier | 4 (default) | Good balance |
| Enterprise / multi-key | 7-10 | Maximize throughput |
| Debugging | 1 | Sequential output, easier to read |

```bash
# Single worker for debugging
python -m contrib.multilingual.batch_scan ./skills/ --workers 1 -V

# Verbose mode shows debug logs
python -m contrib.multilingual.batch_scan ./skills/ --workers 4 -V
```

## Language Options

```bash
# Auto-detect (default) — uses Unicode script ratio
python -m contrib.multilingual.batch_scan ./skills/ --lang auto

# Force a specific language
python -m contrib.multilingual.batch_scan ./skills/ --lang zh

# Available: auto, en, zh, ja, ko
```

For non-English skills, the scanner automatically applies LLM gap-fill for 8 vulnerability rules that static English-keyword patterns cannot detect.

```bash
# Disable LLM requirement for non-English (results may be incomplete)
python -m contrib.multilingual.batch_scan ./skills/ --no-require-llm --no-llm
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All skills safe (no HIGH/CRITICAL) |
| 1 | At least one skill has HIGH or CRITICAL risk |
| 2 | Scan errors occurred (timeouts, crashes) |

Useful for CI:

```bash
python -m contrib.multilingual.batch_scan ./skills/ -f json -o report.json
if [ $? -eq 0 ]; then
    echo "All clean"
fi
```

## Quick Comparison: Upstream vs Batch

```bash
# Upstream — scan one skill
skillspector scan ./skills/my-skill/ -f json -o upstream.json

# Batch — scan all skills
python -m contrib.multilingual.batch_scan ./skills/ -f json -o batch.json

# Diff the results for any skill
# batch.json.skills[*].scan_mode = "multilingual-enhanced"
# batch.json.skills[*].enhancements = {...}
```

Key differences in batch output:
- `scan_mode: "multilingual-enhanced"` — provenance marker
- `enhancements.gap_fill_applied` — true if LLM gap-fill was used
- `enhancements.english_keyword_rules_skipped` — count of static rules bypassed
- `skill.language` — detected language tag

## Troubleshooting

### "No LLM API key configured"
Either set up `.env` with API keys, or use `--no-llm` for static-only mode.

### Connection errors during LLM scan
The scanner has built-in HTTP timeouts (8s connect, 30s read). Failed skills are marked as errors and other workers continue. Reduce `--workers` if rate limits appear.

### "Event loop is closed" warnings
Harmless. Suppressed by Patch 7. Does not affect results.

### Skills timing out (90s limit)
A skill that takes >90s is marked as timeout and skipped. Increase `--workers` to overlap more skills, or check network connectivity to the LLM provider.

### WARNING: model_info token limit
Harmless. Add your model to `model_registry.yaml` if you want accurate token budgeting. Otherwise a 128K default is used.
