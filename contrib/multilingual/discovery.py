"""Skill discovery — recursively find skill directories under a root path.

A directory is a skill if it directly contains a ``SKILL.md`` file.
The root directory itself is never treated as a skill.
"""

from __future__ import annotations

from pathlib import Path


def discover_skills(root: Path) -> list[Path]:
    """Recursively find all skill directories under *root*.

    Returns a list of ``Path`` objects sorted alphabetically by path.
    Each path points to a directory that contains a ``SKILL.md`` file.
    """
    skills: list[Path] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        if skill_dir == root:
            continue
        skills.append(skill_dir)
    return skills
