"""Copy Claude Code's home-project memory and link to it from enabled tools."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

from . import backups, pending, writes
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


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest(marker: Path, dest: Path) -> dict[str, str]:
    """The files agent-sync copied into the memory folder, with the digest of what it wrote. An older release left
    the marker empty and owned every note in the folder, so those notes count as its copies once."""
    try:
        text = marker.read_text()
    except OSError:
        return {}
    try:
        files = json.loads(text).get("files") if text.strip() else None
    except (ValueError, AttributeError):
        files = None
    if isinstance(files, dict):
        return {name: value for name, value in files.items() if isinstance(name, str) and isinstance(value, str)}
    return {path.name: digest(path.read_bytes()) for path in dest.glob("*.md")
            if path.is_file() and not path.is_symlink()}


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
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(home))       # how Claude Code names a project's folder
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
    index_text = ""
    if index is not None:
        lines = index.decode().splitlines(keepends=True)
        secrets = secret_rows(lines)
        index_text = "".join(line for row, line in enumerate(lines) if row not in secrets
                             and not any(unquote(target) in private_notes for target in LINK_TARGET.findall(line)))
        source_files["MEMORY.md"] = index_text.encode()

    owned = manifest(marker, dest)
    written = dict(owned)

    def mine(path: Path) -> bool:
        """agent-sync wrote this file and nobody changed it since."""
        return path.name in owned and path.is_file() and digest(path.read_bytes()) == owned[path.name]

    copied = False
    for name, data in source_files.items():
        path = dest / name
        if not path.is_symlink() and path.is_file() and path.read_bytes() == data:
            written[name] = digest(data)
            continue
        if path.exists() or path.is_symlink():
            if name not in owned:
                messages.append(f"WARN  {path} is not a copy agent-sync made; left alone")
                continue
            key = f"memory:{name}"
            if not mine(path) and (not pending.due(key, f"your edits to memory copy {path} (Claude Code's note "
                                                         "changed; its new text replaces them)") or dry):
                continue
            pending.removed(key)
        copied = True
        if not dry:
            writes.update(path, lambda _, data=data: data, new_mode=0o600)
            written[name] = digest(data)
    for path in sorted(dest.glob("*.md")):
        if path.name in source_files or path.name not in owned or not (path.is_symlink() or path.is_file()):
            continue                                        # a file agent-sync did not write is never touched
        key = f"memory:{path.name}"
        if path.name in private_notes and mine(path):
            messages.append(f"{'would remove' if dry else 'removed'} {path}: the note is private now (backed up)")
            if not dry:
                backups.save(path)
                path.unlink()
                written.pop(path.name, None)
            continue
        reason = "the note is private now" if path.name in private_notes else "no longer in Claude Code's memory"
        edited = "" if mine(path) else "; you edited this copy"
        if pending.due(key, f"memory copy {path} ({reason}{edited})") and not dry:
            backups.save(path)
            path.unlink()
            written.pop(path.name, None)
            pending.removed(key)
            copied = True
    if copied or not exists:
        messages.append(f"{action} memory copy ({len(source_files)} files)")
    if not dry and (written != owned or not marker.is_file() or not marker.read_text().strip()):
        writes.update_text(marker, lambda _: json.dumps({"files": dict(sorted(written.items()))}, indent=2) + "\n")

    index_text = LINK.sub(r"\1~/.agents/memory/\2", index_text).strip("\r\n")
    section = "\n".join([MEMORY_START, "## Memory (kept by Claude Code)", GUIDANCE, index_text,
                         MEMORY_END]) + "\n" if index_text.strip() else ""
    for path, readers in grouped_paths(locations).items():
        if "claude" in readers or path.resolve() == claude.resolve():   # AGENTS.md may be a link to CLAUDE.md
            continue
        tool = readers[0]

        def change(current: str, tool=tool) -> str:
            body = split_front(current)[1] if tool == "cursor" else current
            if match := SECTION.search(body):
                replaced = body[:match.start()] + section + SECTION.sub("", body[match.end():])
            elif not section:
                return current
            else:
                separator = "" if not body or body.endswith("\n\n") else "\n" if body.endswith("\n") else "\n\n"
                replaced = body + separator + section
            return rendered(tool, current, replaced, claude)

        try:
            if dry:
                current = path.read_text() if path.exists() else ""
                changed = change(current) != current
            else:
                changed = writes.update_text(path, change)
        except writes.Raced as raced:
            messages.append(f"WARN  {raced}")
            continue
        if changed:
            messages.append(f"{action} memory index in {path}")
    return messages
