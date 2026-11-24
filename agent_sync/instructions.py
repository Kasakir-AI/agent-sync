"""Mirror the source tool's global instructions (off unless `mirror_instructions = true`).

- Claude Code: ~/.claude/CLAUDE.md
- Codex:       ~/.codex/AGENTS.md
- Cursor:      ~/.cursor/rules/agent-sync-instructions.mdc (a rule that always applies; Cursor keeps the "User
               Rules" of its settings screen in its own database, so a global rule file is used instead)
In CLAUDE.md and AGENTS.md the source's lines are merged in, the way `agent-sync review` keeps a version: a line the
source changed is replaced, a new one is added, and a line only the target has stays as that tool's own note, unless
the source had it at the last sync and has since deleted it. The lines last synced are recorded with review's
baselines. Earlier versions wrote a marked block instead; a run folds that block into the merged text.
Cursor can't be the source: set `source` to claude or codex.
"""
from __future__ import annotations

import re
from pathlib import Path

END = "<!-- agent-sync: end -->"
KEY = "instructions:Global instructions"   # its entry in review's baselines
BLOCK = re.compile(r"\n*<!-- agent-sync: begin.*?<!-- agent-sync: end -->\n?", re.S)

FRONT = re.compile(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)", re.M | re.S)


def split_front(text: str) -> tuple[str, str]:
    match = FRONT.match(text)
    return (match[0], text[match.end():]) if match else ("", text)


def files(home: Path) -> dict[str, Path]:
    return {"claude": home / ".claude/CLAUDE.md", "codex": home / ".codex/AGENTS.md",
            "cursor": home / ".cursor/rules/agent-sync-instructions.mdc"}


def own_text(text: str) -> str:
    """A file's text without the block agent-sync put there."""
    return BLOCK.sub("\n", text).strip()


def rendered(tool: str, current: str, body: str, src: Path, synced: frozenset[str] = frozenset()) -> str:
    from .review import merge, removed_lines  # review builds on this module, so it is imported here, not at the top
    if tool == "cursor":
        _, existing = split_front(current)
        if own_text(existing) == body.strip():
            return current
        return ("---\ndescription: Global instructions, mirrored by agent-sync from " + src.name +
                "\nalwaysApply: true\n---\n" + body + "\n")
    mine = own_text(current)
    if not mine:
        return body + "\n"
    merged = merge(mine + "\n", body, frozenset(removed_lines(mine, body, synced)))
    return current if merged.strip() == current.strip() else merged


def mirror(home: Path, source: str, dry: bool = False, baselines: Path | None = None) -> list[str]:
    if source == "cursor":
        return ["skip  Cursor can't be the source of global instructions; set source to claude or codex"]
    from . import review
    paths = files(home)
    src = paths[source]
    if not src.exists():
        return [f"skip  no {src}"]
    body = own_text(src.read_text())
    if not body:
        return [f"skip  {src} is empty"]
    stored = review.load_baselines(baselines) if baselines else {}
    entry = stored.get(KEY) if isinstance(stored.get(KEY), dict) else {}
    lines = entry.get("lines")
    valid = isinstance(lines, list) and all(isinstance(line, str) for line in lines)
    synced = frozenset(lines) if valid else frozenset()
    changes = []
    for tool, path in paths.items():
        if tool == source:
            continue
        current = path.read_text() if path.exists() else ""
        new = rendered(tool, current, body, src, synced)
        if new == current:
            continue
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new)
        changes.append(f"{'would update' if dry else 'updated'} {path}")
    if baselines and not dry and entry.get("lines") != review.keys(body):
        review.save_baselines(baselines, {**stored, KEY: {**entry, "lines": review.keys(body)}})
    return changes
