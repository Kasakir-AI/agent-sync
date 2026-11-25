"""Copy Claude Code's home-project memory and link to it from enabled tools."""
from __future__ import annotations

import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from .instructions import MEMORY_END, MEMORY_START, files, rendered, split_front
from .registry import grouped_paths

MARKER = ".agent-sync"
SECTION = re.compile(rf"(?ms)^[ \t]*{MEMORY_START}[ \t]*\r?\n.*?^[ \t]*{MEMORY_END}[ \t]*(?:\r?\n|\Z)")
LINK = re.compile(r"(?m)^([ \t]*-[ \t]+\[[^\n]*?\]\()([^/\n()]+\.md)(\))")
GUIDANCE = ("Read the matching file in ~/.agents/memory/ when an entry applies. "
            "These are facts and preferences about the user and their projects.")


def replace_file(path: Path, data: bytes) -> None:
    with NamedTemporaryFile(dir=path.parent, prefix=".agent-sync-", delete=False) as temp:
        staged = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def mirror(home: Path, dry: bool, *, enabled) -> list[str]:
    locations = files(home, enabled)
    if "claude" not in locations:
        return []
    slug = "".join(c if c.isalnum() else "-" for c in str(home))
    source = home / ".claude/projects" / slug / "memory"
    if not source.is_dir():
        return []

    dest = home / ".agents/memory"
    marker = dest / MARKER
    exists = dest.exists()
    if (dest.is_symlink() or not dest.resolve().is_relative_to(home.resolve())
            or (exists and (marker.is_symlink() or not marker.is_file()))):
        return [f"WARN  {dest} is not an agent-sync-owned memory folder"]
    if not exists and not dry:
        dest.mkdir(mode=0o700, parents=True)
        marker.touch()

    messages = []
    action = "would update" if dry else "updated"
    source_files = {path.name: path.read_bytes() for path in sorted(source.glob("*.md"))
                    if path.is_file() and not path.is_symlink()}
    copied = False
    for name, data in source_files.items():
        path = dest / name
        if not path.is_symlink() and path.is_file() and path.read_bytes() == data:
            continue
        copied = True
        if not dry:
            replace_file(path, data)
    for path in dest.glob("*.md"):
        if path.name not in source_files and (path.is_symlink() or path.is_file()):
            copied = True
            if not dry:
                path.unlink()
    if copied or not exists:
        messages.append(f"{action} memory copy ({len(source_files)} files)")

    index = source_files.get("MEMORY.md", b"").decode()
    index = LINK.sub(r"\1~/.agents/memory/\2\3", index).strip("\r\n")
    section = "\n".join(filter(None, [MEMORY_START, "## Memory (kept by Claude Code)", GUIDANCE, index,
                                      MEMORY_END])) + "\n"
    for path, readers in grouped_paths(locations).items():
        if "claude" in readers:
            continue
        current = path.read_text() if path.exists() else ""
        tool = readers[0]
        body = split_front(current)[1] if tool == "cursor" else current
        if match := SECTION.search(body):
            replaced = body[:match.start()] + section + SECTION.sub("", body[match.end():])
        else:
            separator = "" if not body or body.endswith("\n\n") else "\n" if body.endswith("\n") else "\n\n"
            replaced = body + separator + section
        updated = rendered(tool, current, replaced, locations["claude"])
        if updated != current:
            if not dry:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(updated)
            messages.append(f"{action} memory index in {path}")
    return messages
