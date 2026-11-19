"""Mirror the source tool's global instructions (off unless `mirror_instructions = true`).

- Claude Code: ~/.claude/CLAUDE.md
- Codex:       ~/.codex/AGENTS.md
- Cursor:      ~/.cursor/rules/agent-sync-instructions.mdc (a rule that always applies; Cursor keeps the "User
               Rules" of its settings screen in its own database, so a global rule file is used instead)
In CLAUDE.md and AGENTS.md the copy goes between two marker lines and everything else in the file is kept, so each
tool can still have notes of its own. Cursor can't be the source: set `source` to claude or codex.
"""
from __future__ import annotations

import re
from pathlib import Path

BEGIN = "<!-- agent-sync: begin (mirrored from {src}; edit the source, not this block) -->"
END = "<!-- agent-sync: end -->"
BLOCK = re.compile(r"\n*<!-- agent-sync: begin.*?<!-- agent-sync: end -->\n?", re.S)


def files(home: Path) -> dict[str, Path]:
    return {"claude": home / ".claude/CLAUDE.md", "codex": home / ".codex/AGENTS.md",
            "cursor": home / ".cursor/rules/agent-sync-instructions.mdc"}


def own_text(text: str) -> str:
    """A file's text without the block agent-sync put there."""
    return BLOCK.sub("\n", text).strip()


def rendered(tool: str, current: str, body: str, src: Path) -> str:
    if tool == "cursor":
        return ("---\ndescription: Global instructions, mirrored by agent-sync from " + src.name +
                "\nalwaysApply: true\n---\n" + body + "\n")
    block = BEGIN.format(src=src.name) + "\n" + body + "\n" + END + "\n"
    mine = own_text(current)
    return (mine + "\n\n" if mine else "") + block


def mirror(home: Path, source: str, dry: bool = False) -> list[str]:
    if source == "cursor":
        return ["skip  Cursor can't be the source of global instructions; set source to claude or codex"]
    paths = files(home)
    src = paths[source]
    if not src.exists():
        return [f"skip  no {src}"]
    body = own_text(src.read_text())
    if not body:
        return [f"skip  {src} is empty"]
    changes = []
    for tool, path in paths.items():
        if tool == source:
            continue
        current = path.read_text() if path.exists() else ""
        new = rendered(tool, current, body, src)
        if new == current:
            continue
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new)
        changes.append(f"{'would update' if dry else 'updated'} {path}")
    return changes
