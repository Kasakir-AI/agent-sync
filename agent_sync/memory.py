"""Copy Claude Code's home-project memory and link to it from enabled tools."""
from __future__ import annotations

import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import unquote

from . import backups, pending
from .instructions import MEMORY_END, MEMORY_START, files, rendered, split_front
from .privacy import secret_rows, secret_values, sensitive
from .registry import grouped_paths

MARKER = ".agent-sync"
SECTION = re.compile(rf"(?ms)^[ \t]*{MEMORY_START}[ \t]*\r?\n.*?^[ \t]*{MEMORY_END}[ \t]*(?:\r?\n|\Z)")
FILENAME = r"([^/<>()[\]#\r\n]+\.md)(?=>?[\s)#])"
LINK = re.compile(rf"(?m)^([ \t]*-[ \t]+\[[^\n]*?\]\(<?)(?:\./)?{FILENAME}")
LINK_TARGET = re.compile(rf"\]\(<?(?:[^\s<>()]*/)?{FILENAME}")
NAME = re.compile(r"""^["']?name["']?:[ \t]*(?:"([^"]+)"|'([^']+)'|([^#\r\n]+))""", re.M)
PRIVATE = r"""["']?private["']?[ \t]*:[ \t]*true[ \t]*(?:#[^\r\n]*)?\r?$"""
METADATA = re.compile(r"""^["']?metadata["']?:[ \t]*(?:#[^\r\n]*)?\r?\n((?:[ \t]+[^\n]*\n|\r?\n)*)""", re.M)
INLINE_PRIVATE = re.compile(
    r"""^["']?metadata["']?:[ \t]*\{(?:[^{}\r\n]*?,)?[ \t]*["']?private["']?[ \t]*:[ \t]*true[ \t]*[,}]""",
    re.I | re.M,
)
GUIDANCE = ("Read the matching file in ~/.agents/memory/ when an entry applies. "
            "These are facts and preferences about the user and their projects.")


def replace_file(path: Path, data: bytes) -> None:
    backups.save(path)
    with NamedTemporaryFile(dir=path.parent, prefix=".agent-sync-", delete=False) as temp:
        staged = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def note_privacy(filename: str, text: str, private_memory=(), share_memory=()) -> tuple[str, list[str], list[str]]:
    front, _ = split_front(text.removeprefix("\ufeff"))
    stem = Path(filename).stem
    match = NAME.search(front)
    name = next(value for value in match.groups() if value is not None).strip() if match else stem
    aliases = {name, stem}
    private = bool(re.search("^" + PRIVATE, front, re.I | re.M) or INLINE_PRIVATE.search(front))
    if metadata := METADATA.search(front):
        if indent := re.search(r"(?m)^([ \t]+)[^\s#]", metadata[1]):
            private |= bool(re.search("^" + indent[1] + PRIVATE, metadata[1], re.I | re.M))
    kinds = secret_values(text) + secret_values(filename)
    kinds = list(dict.fromkeys(kinds))
    reasons = []
    if private:
        reasons.append("marked private")
    if aliases.intersection(private_memory):
        reasons.append("listed in private_memory")
    if kinds:
        reasons.extend(f"contains a secret ({kind})" for kind in kinds)
    elif not reasons and not aliases.intersection(share_memory):
        reasons.extend(sensitive(text))
    if secret_values(name):
        name = stem if not secret_values(stem) else "[name withheld]"
    return name, reasons, kinds


def mirror(home: Path, dry: bool, *, enabled, private_memory=(), share_memory=()) -> list[str]:
    locations = files(home, enabled)
    claude = files(home, ("claude",))["claude"]
    if not locations:
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
    index = source_files.pop("MEMORY.md", None)
    private_notes = {}
    for filename, data in source_files.items():
        name, reasons, kinds = note_privacy(filename, data.decode(), private_memory, share_memory)
        if reasons:
            private_notes[filename] = f"{name} ({', '.join(reasons)})"
        if kinds:
            messages.append(f"WARN  memory note {name} contains what looks like a secret ({', '.join(kinds)}); "
                            "it stays in Claude Code only. Consider moving the secret out of memory.")
    if private_notes:
        messages.insert(0, f"kept {len(private_notes)} memory notes private to Claude Code: "
                        f"{', '.join(private_notes.values())}. To share one, add it to share_memory")
        for filename in private_notes:
            del source_files[filename]
    if index is not None:
        lines = index.decode().splitlines(keepends=True)
        secrets = secret_rows(lines)
        index = "".join(line for row, line in enumerate(lines) if row not in secrets
                        and not any(unquote(target) in private_notes for target in LINK_TARGET.findall(line)))
        source_files["MEMORY.md"] = index.encode()
    else:
        index = ""
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
            if path.name in private_notes:
                messages.append(f"{'would remove' if dry else 'removed'} {path}: the note is private now "
                                "(backed up)")
                if not dry:
                    backups.save(path)
                    path.unlink()
                continue
            key = f"memory:{path.name}"
            if pending.due(key, f"memory copy {path} (no longer in Claude Code's memory)") and not dry:
                backups.save(path)
                path.unlink()
                pending.removed(key)
                copied = True
    if copied or not exists:
        messages.append(f"{action} memory copy ({len(source_files)} files)")

    index = LINK.sub(r"\1~/.agents/memory/\2", index).strip("\r\n")
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
        updated = rendered(tool, current, replaced, claude)
        if updated != current:
            if not dry:
                path.parent.mkdir(parents=True, exist_ok=True)
                backups.save(path, follow=True)
                path.write_text(updated)
            messages.append(f"{action} memory index in {path}")
    return messages
