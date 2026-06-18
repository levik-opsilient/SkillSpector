"""Graph invocation helpers for batch scanning.

Thin wrappers over ``skillspector.graph.graph`` — build initial state,
invoke the graph, and transform the raw result dict into a structured
batch entry suitable for downstream reporting.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from skillspector.graph import graph

from .annotation import annotate_findings


def scan_state(skill_dir: Path, use_llm: bool) -> dict[str, object]:
    """Build the initial LangGraph state for a single skill directory."""
    return {
        "input_path": str(skill_dir),
        "output_format": "json",
        "use_llm": use_llm,
    }


def cleanup_result(result: dict[str, object]) -> None:
    """Remove the temporary directory created by the graph, if any."""
    temp_dir = result.get("temp_dir_for_cleanup")
    if temp_dir and isinstance(temp_dir, str):
        shutil.rmtree(temp_dir, ignore_errors=True)


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
    # Disable structured output for graph-internal LLM calls.  DeepSeek
    # and some providers don't support response_format; requesting it
    # causes a 400 that corrupts the HTTP connection pool.  Both the
    # base class and the meta-analyzer subclass set their own schema.
    from skillspector.llm_analyzer_base import LLMAnalyzerBase as _Base
    from skillspector.nodes.meta_analyzer import LLMMetaAnalyzer as _Meta
    _saved_base = _Base.response_schema
    _saved_meta = _Meta.response_schema
    _Base.response_schema = None
    _Meta.response_schema = None
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
        _Base.response_schema = _saved_base
        _Meta.response_schema = _saved_meta
        if result is not None:
            cleanup_result(result)


def _rel_name(skill_dir: Path, root: Path) -> str:
    """Best-effort relative name for display in progress lines."""
    try:
        return str(skill_dir.relative_to(root))
    except ValueError:
        return skill_dir.name
