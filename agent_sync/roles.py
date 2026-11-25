"""Mirror pstack's per-role model lines ("feature, refactoring: <model>") from the source tool's settings file.

Files (as pstack's /setup-pstack writes them):
- Claude Code: ~/.claude/rules/pstack-models.md
- Cursor:      ~/.cursor/rules/pstack-models.mdc (front matter, then the roles)
- Codex:       ~/.agents/pstack-models.md
Only the role lines are copied. Everything after them (each tool's own notes on how to run each model) is kept.
"""
from __future__ import annotations

import re
from pathlib import Path

from .registry import DEFAULT_AGENTS, grouped_paths, paths

ROLE = re.compile(r"^[a-z][a-z ,&-]*:\s*\S")
FRONT_KEYS = ("description:", "alwaysApply:", "globs:")


def files(home: Path, enabled=DEFAULT_AGENTS) -> dict[str, Path]:
    return paths(home, enabled, "roles")


def is_role(line: str) -> bool:
    return bool(ROLE.match(line)) and not line.startswith(FRONT_KEYS)


def split(text: str) -> tuple[list[str], list[str], str]:
    """(header lines, role lines, rest). The role block ends at the first blank line or heading after a role."""
    lines = text.splitlines()
    header, roles, i = [], [], 0
    while i < len(lines) and not is_role(lines[i]):
        header.append(lines[i]); i += 1
    while i < len(lines) and (is_role(lines[i]) or (lines[i].startswith("#") and not roles)):
        (roles if is_role(lines[i]) else header).append(lines[i]); i += 1
    rest = "\n".join(lines[i:])
    return header, roles, rest


def mirror(home: Path, source: str, dry: bool = False, *, enabled=DEFAULT_AGENTS) -> list[str]:
    locations = files(home, enabled)
    src = locations[source]
    if not src.exists():
        return [f"skip  no {src}"]
    from . import writes  # writes builds on this module

    _, roles, _ = split(src.read_text())
    if not roles:
        return [f"WARN  no role lines in {src}"]
    changes = []
    for path in grouped_paths(locations):
        if path == src:
            continue

        def change(current: str, path=path) -> str:
            if not current and path.suffix == ".mdc":
                current = "---\ndescription: pstack model roles\nalwaysApply: true\n---\n"
            header, old, rest = split(current)
            if old == roles:
                return current
            return ("\n".join(header + roles) + ("\n" + rest if rest else "") + "\n").replace("\n\n\n", "\n\n")

        if dry:
            current = path.read_text() if path.exists() else ""
            if change(current) == current:
                continue
        else:
            try:
                if not writes.update_text(path, change):
                    continue
            except writes.Raced as raced:
                changes.append(f"WARN  {raced}")
                continue
        changes.append(f"{'would update' if dry else 'updated'} {path}")
    return changes
