#!/usr/bin/env python3
"""Batch scanner for SkillSpector — lightweight external tool.

Runs SkillSpector's static analyzers across a directory of skills and
produces a single aggregated report (terminal / JSON / Markdown).  Zero
changes to SkillSpector source — imports the same ``graph`` that
``skillspector scan`` uses.

Usage::

    python batch_scan.py ./skills/ --no-llm
    python batch_scan.py ./skills/ --no-llm -f json -o batch-report.json
    python batch_scan.py ./skills/ --no-llm -f markdown -o batch-report.md
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from skillspector import __version__ as _skillspector_version
from skillspector.graph import graph
from skillspector.logging_config import set_level

# ═══════════════════════════════════════════════════════════════════
#  Skill discovery
# ═══════════════════════════════════════════════════════════════════


def discover_skills(root: Path) -> list[Path]:
    """Recursively find all skill directories under *root*.

    A directory is considered a skill if it directly contains a
    ``SKILL.md`` file.  The root directory itself is never treated as
    a skill.
    """
    skills: list[Path] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        if skill_dir == root:
            continue
        skills.append(skill_dir)
    return skills


# ═══════════════════════════════════════════════════════════════════
#  Graph helpers
# ═══════════════════════════════════════════════════════════════════


def _scan_state(skill_dir: Path, use_llm: bool) -> dict[str, object]:
    """Build initial graph state for a single skill directory."""
    return {
        "input_path": str(skill_dir),
        "output_format": "json",
        "use_llm": use_llm,
    }


def _cleanup_result(result: dict[str, object]) -> None:
    """Remove temp directory created by graph, if any."""
    temp_dir = result.get("temp_dir_for_cleanup")
    if temp_dir and isinstance(temp_dir, str):
        shutil.rmtree(temp_dir, ignore_errors=True)


def _entry_from_result(
    result: dict[str, object], skill_dir: Path, root: Path
) -> dict[str, object]:
    """Build a single batch entry from a ``graph.invoke()`` result.

    Uses the same field shape as the single-scan JSON report so the
    batch output is consistent with SkillSpector's native format.
    """
    findings = result.get("filtered_findings", result.get("findings", []))
    manifest = result.get("manifest") or {}
    component_metadata = result.get("component_metadata") or []
    skill_name = (manifest.get("name") or skill_dir.name) if manifest else skill_dir.name

    try:
        rel_path = str(skill_dir.relative_to(root))
    except ValueError:
        rel_path = str(skill_dir)

    source_group = rel_path.split("/")[0] if "/" in rel_path else "."

    return {
        "skill": {
            "name": skill_name,
            "source": rel_path,
            "source_group": source_group,
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
            for c in component_metadata
        ],
        "issues": [f.to_dict() for f in findings],
    }


# ═══════════════════════════════════════════════════════════════════
#  Report generation
# ═══════════════════════════════════════════════════════════════════


def _format_terminal(results: list[dict[str, object]]) -> str:
    """Generate a Rich terminal summary table for the batch."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        # Fallback: plain-text summary (no Rich installed standalone)
        lines: list[str] = []
        for r in _sorted_results(results):
            risk = r.get("risk_assessment", {})
            skill = r.get("skill", {})
            lines.append(
                f"  {skill.get('name', '?'):40s} "
                f"{risk.get('score', 0):>3}/100 {risk.get('severity', 'LOW'):<8s}"
            )
        return "\n".join(lines)

    capture = Console(record=True, force_terminal=True, width=80, file=StringIO())
    total = len(results)

    critical = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "CRITICAL"
    )
    high = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "HIGH"
    )
    medium = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "MEDIUM"
    )
    low_count = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "LOW"
    )
    errs = sum(1 for r in results if r.get("error"))

    capture.print()
    capture.print(
        Panel(
            "[bold]SkillSpector Batch Scan Report[/bold]",
            subtitle=f"v{_skillspector_version}",
        )
    )
    capture.print()

    completed = total - errs
    capture.print(f"[bold]Total:[/bold] {total} skill(s) scanned")
    if errs:
        capture.print(f"[red]Errors:[/red] {errs}")
    capture.print()

    # ── Source-group breakdown ──────────────────────────────────
    from collections import defaultdict

    group_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    )
    for r in results:
        group = r.get("skill", {}).get("source_group", ".")
        sev = r.get("risk_assessment", {}).get("severity", "LOW")
        group_stats[group]["total"] += 1
        if sev in group_stats[group]:
            group_stats[group][sev] += 1

    if len(group_stats) > 1:
        capture.print("[bold]Source Breakdown:[/bold]")
        for group in sorted(group_stats):
            st = group_stats[group]
            parts = [f"  {group:<30s} {st['total']:>4d} skills"]
            if st["CRITICAL"]:
                parts.append(f"[bold red]{st['CRITICAL']} CRITICAL[/bold red]")
            if st["HIGH"]:
                parts.append(f"[red]{st['HIGH']} HIGH[/red]")
            if st["MEDIUM"]:
                parts.append(f"[yellow]{st['MEDIUM']} MEDIUM[/yellow]")
            capture.print(", ".join(parts))
        capture.print()

    severity_colors: dict[str, str] = {
        "LOW": "green",
        "MEDIUM": "yellow",
        "HIGH": "red",
        "CRITICAL": "bold red",
        "ERROR": "red",
    }

    table = Table(title=f"Skills by Risk Score ({completed} completed)")
    table.add_column("Skill", style="cyan")
    table.add_column("Score", justify="right")
    table.add_column("Severity")
    table.add_column("Issues", justify="right")

    for r in _sorted_results(results):
        skill = r.get("skill", {})
        risk = r.get("risk_assessment", {})
        name = skill.get("name", "?")
        score = risk.get("score", 0)
        sev = risk.get("severity", "LOW")
        color = severity_colors.get(sev, "")
        issues = len(r.get("issues", []))

        if r.get("error"):
            table.add_row(str(name), "ERR", "[red]ERROR[/red]", "—")
        else:
            table.add_row(
                str(name),
                f"[{color}]{score}/100[/{color}]",
                f"[{color}]{sev}[/{color}]",
                str(issues),
            )
    capture.print(table)
    capture.print()

    if critical + high > 0:
        capture.print(
            f"[bold red]{critical + high} skill(s)[/bold red] "
            "with HIGH or CRITICAL risk — review immediately"
        )
    if medium > 0:
        capture.print(
            f"[yellow]{medium} skill(s)[/yellow] "
            "with MEDIUM risk — review before installing"
        )
    if low_count > 0:
        capture.print(
            f"[green]{low_count} skill(s)[/green] with LOW risk — likely safe"
        )
    capture.print()

    return capture.export_text()


def _format_json(results: list[dict[str, object]]) -> str:
    """Generate a JSON batch report."""
    entries: list[dict[str, object]] = []
    for r in _sorted_results(results):
        skill = r.get("skill", {})
        entry: dict[str, object] = {
            "skill": {
                "name": skill.get("name"),
                "source": skill.get("source"),
                "source_group": skill.get("source_group"),
                "scanned_at": skill.get("scanned_at"),
            },
            "risk_assessment": r.get("risk_assessment", {}),
            "components": r.get("components", []),
            "issues": r.get("issues", []),
        }
        if r.get("error"):
            entry["error"] = r["error"]
        entries.append(entry)

    data: dict[str, object] = {
        "batch": {
            "scanned_at": datetime.now(UTC).isoformat(),
            "total_skills": len(results),
        },
        "skills": entries,
        "metadata": {
            "skillspector_version": _skillspector_version,
        },
    }
    return json.dumps(data, indent=2)


def _format_markdown(results: list[dict[str, object]]) -> str:
    """Generate a Markdown batch report."""
    lines: list[str] = []
    total = len(results)

    lines.append("# SkillSpector Batch Scan Report\n")
    lines.append(f"**Skills scanned:** {total}  ")
    lines.append(
        f"**Scanned at:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}  \n"
    )

    critical = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "CRITICAL"
    )
    high = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "HIGH"
    )
    medium = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "MEDIUM"
    )
    low_count = sum(
        1 for r in results if r.get("risk_assessment", {}).get("severity") == "LOW"
    )

    lines.append("## Summary\n")
    lines.append("| Severity | Count |")
    lines.append("|----------|-------|")
    lines.append(f"| 🔴 CRITICAL | {critical} |")
    lines.append(f"| 🔴 HIGH | {high} |")
    lines.append(f"| 🟡 MEDIUM | {medium} |")
    lines.append(f"| 🟢 LOW | {low_count} |")
    lines.append("")

    lines.append("## Skills by Risk Score\n")
    lines.append("| Skill | Score | Severity | Issues |")
    lines.append("|-------|-------|----------|--------|")
    for r in _sorted_results(results):
        skill = r.get("skill", {})
        risk = r.get("risk_assessment", {})
        name = skill.get("name", "?")
        score = risk.get("score", 0)
        sev = risk.get("severity", "LOW")
        issues = len(r.get("issues", []))

        if r.get("error"):
            lines.append(f"| `{name}` | ERR | ERROR | — |")
        else:
            lines.append(f"| `{name}` | {score}/100 | {sev} | {issues} |")
    lines.append("")

    # ── Issue details for HIGH / CRITICAL ──────────────────────
    high_critical = [
        r for r in _sorted_results(results)
        if r.get("risk_assessment", {}).get("severity") in ("HIGH", "CRITICAL")
        and not r.get("error")
    ]
    if high_critical:
        severity_emoji = {"HIGH": "🔴", "CRITICAL": "🔴"}
        lines.append("## 🔴 HIGH / CRITICAL Issue Details\n")
        for r in high_critical:
            skill = r.get("skill", {})
            risk = r.get("risk_assessment", {})
            name = skill.get("name", "?")
            lines.append(
                f"### {name} — {risk.get('score', 0)}/100 "
                f"{risk.get('severity', 'HIGH')}\n"
            )
            for issue in r.get("issues", []):
                sev = (issue.get("severity") or "LOW").upper()
                emoji = severity_emoji.get(sev, "")
                loc_start = issue.get("location", {}).get("start_line", "?")
                loc_file = issue.get("location", {}).get("file", "")
                lines.append(
                    f"- **{emoji} {issue.get('id', '?')}**: "
                    f"{issue.get('explanation', issue.get('message', ''))}"
                )
                lines.append(f"  - Location: `{loc_file}:{loc_start}`")
                lines.append(
                    f"  - Confidence: {issue.get('confidence', 0):.0%}"
                )
                rem = issue.get("remediation")
                if rem:
                    lines.append(f"  - Remediation: {rem}")
                lines.append("")
        lines.append("")

    lines.append(f"\n*Generated by SkillSpector v{_skillspector_version}*")
    return "\n".join(lines)


def _sorted_results(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return results sorted by risk score descending."""
    return sorted(
        results,
        key=lambda x: x.get("risk_assessment", {}).get("score", 0),
        reverse=True,
    )


# ═══════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════


def main() -> None:
    try:
        from rich.console import Console
    except ImportError:
        Console = None  # type: ignore[assignment]  # noqa: N806

    c = Console() if Console is not None else None

    def _print(*args: object, **kwargs: object) -> None:
        """Print via Rich when available, otherwise plain print."""
        if c:
            c.print(*args, **{k: v for k, v in kwargs.items() if k != "file"})
        else:
            msg = " ".join(str(a) for a in args)
            file = kwargs.get("file")
            if file:
                print(msg, file=file)
            else:
                print(msg)

    parser = argparse.ArgumentParser(
        description="Batch-scan a directory of AI agent skills with SkillSpector.",
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing skill subdirectories (each with a SKILL.md).",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("terminal", "json", "markdown"),
        default="terminal",
        help="Output format (default: terminal).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write report to FILE (default: stdout).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Skip LLM analysis — static patterns only (recommended for batch).",
    )
    parser.add_argument(
        "-V",
        "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG-level logging (shows per-skill graph details).",
    )
    args = parser.parse_args()

    if args.verbose:
        set_level("DEBUG")

    root = args.input_dir.resolve()
    if not root.is_dir():
        _print(f"[red]Error:[/red] {root} is not a directory", file=sys.stderr)
        sys.exit(2)

    skill_dirs = discover_skills(root)
    if not skill_dirs:
        _print(
            "[yellow]No skills found.[/yellow] Each skill must be a subdirectory "
            "containing a SKILL.md file.",
            file=sys.stderr,
        )
        sys.exit(2)

    _print(f"\n[bold]SkillSpector Batch Scan[/bold] — "
           f"{len(skill_dirs)} skill(s) in [dim]{root}[/dim]\n")

    results: list[dict[str, object]] = []
    errors = 0
    has_high_risk = False

    _sev_colors: dict[str, str] = {
        "LOW": "green",
        "MEDIUM": "yellow",
        "HIGH": "red",
        "CRITICAL": "bold red",
        "ERROR": "red",
    }

    for i, skill_dir in enumerate(skill_dirs, 1):
        try:
            rel_name = str(skill_dir.relative_to(root))
        except ValueError:
            rel_name = skill_dir.name
        result = None
        try:
            state = _scan_state(skill_dir, use_llm=not args.no_llm)
            result = graph.invoke(state)
            entry = _entry_from_result(result, skill_dir, root)
            results.append(entry)

            score = result.get("risk_score", 0)
            severity = result.get("risk_severity", "LOW")
            findings = result.get("filtered_findings", result.get("findings", []))

            if score > 50:
                has_high_risk = True

            color = _sev_colors.get(severity, "")
            _print(
                f"  [{i}/{len(skill_dirs)}] [cyan]{rel_name}[/cyan] → "
                f"[{color}]{score}/100 {severity}[/{color}] "
                f"({len(findings)} issue(s))"
            )

        except Exception as exc:
            errors += 1
            results.append({
                "skill": {
                    "name": rel_name,
                    "source": str(skill_dir),
                    "source_group": rel_name.split("/")[0] if "/" in rel_name else ".",
                    "scanned_at": datetime.now(UTC).isoformat(),
                },
                "risk_assessment": {
                    "score": 0,
                    "severity": "ERROR",
                    "recommendation": "ERROR",
                },
                "components": [],
                "issues": [],
                "error": str(exc),
            })
            _print(
                f"  [{i}/{len(skill_dirs)}] [cyan]{rel_name}[/cyan] → "
                f"[red]ERROR: {exc}[/red]"
            )
        finally:
            if result is not None:
                _cleanup_result(result)

    # ── output ──────────────────────────────────────────────────
    fmt = args.format
    if fmt == "terminal":
        report_body = _format_terminal(results)
    elif fmt == "json":
        report_body = _format_json(results)
    else:  # markdown
        report_body = _format_markdown(results)

    if args.output:
        args.output.write_text(report_body, encoding="utf-8")
        _print(f"\n[green]Batch report saved to:[/green] {args.output}")
    else:
        if fmt == "terminal":
            _print(report_body)
        else:
            sys.stdout.write(report_body + "\n")

    if errors:
        sys.exit(2)
    if has_high_risk:
        sys.exit(1)


if __name__ == "__main__":
    main()
