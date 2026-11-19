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

ROLE = re.compile(r"^[a-z][a-z ,&-]*:\s*\S")
FRONT_KEYS = ("description:", "alwaysApply:", "globs:")


def files(home: Path) -> dict[str, Path]:
    return {"claude": home / ".claude/rules/pstack-models.md",
            "cursor": home / ".cursor/rules/pstack-models.mdc",
            "codex": home / ".agents/pstack-models.md"}


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


def mirror(home: Path, source: str, dry: bool = False) -> list[str]:
    paths = files(home)
    src = paths[source]
    if not src.exists():
        return [f"skip  no {src}"]
    _, roles, _ = split(src.read_text())
    if not roles:
        return [f"WARN  no role lines in {src}"]
    changes = []
    for tool, path in paths.items():
        if tool == source or not path.exists():
            continue
        header, old, rest = split(path.read_text())
        if old == roles:
            continue
        new = "\n".join(header + roles) + ("\n" + rest if rest else "") + "\n"
        if not dry:
            path.write_text(new.replace("\n\n\n", "\n\n"))
        changes.append(f"{'would update' if dry else 'updated'} {path}")
    return changes
