"""Copy Claude Code's home-project memory and link to it from enabled tools."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

from . import backups, pending, writes
from .config import Config
from .instructions import MEMORY_END, MEMORY_START, files, rendered, split_front
from .privacy import secret_rows, secret_values, sensitive
from .registry import TOOLS, grouped_paths, inside

MARKER = ".agent-sync"
CHOICE = "memory-sharing"
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


def manifest(marker: Path) -> dict[str, str] | None:
    """The note digests agent-sync wrote, or None for a malformed manifest."""
    try:
        files = json.loads(marker.read_text()).get("files")
    except FileNotFoundError:
        return {}
    except (ValueError, AttributeError):
        return None
    if not isinstance(files, dict) or any(Path(name).name != name or not name.endswith(".md")
                                         or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                                         for name, value in files.items()):
        return None
    return files


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


def notes(home: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(home))
    return home / ".claude/projects" / slug / "memory"


def question(cfg: Config) -> str | None:
    others = [TOOLS[tool].name for tool in cfg.agents if tool != "claude"]
    if cfg.mirror_memory is not None or not others:
        return None
    if not any(path.is_file() and not path.is_symlink() for path in notes(cfg.home).glob("*.md")):
        return None
    names = others[0] if len(others) == 1 else " and ".join(others) if len(others) == 2 else (
        ", ".join(others[:-1]) + ", and " + others[-1])
    return (f"Share Claude Code's memory notes with {names}? "
            "Notes about credentials, keys and personal details stay in Claude Code.")


def mirror(home: Path, dry: bool, *, enabled, private_memory=(), share_memory=(), sharing=True) -> list[str]:
    locations = files(home, enabled)
    claude = files(home, ("claude",))["claude"]
    source = notes(home)
    if not locations or not any(tool != "claude" for tool in locations):
        pending.incomplete()
        return []

    messages = [] if sharing else mirror_index(locations, claude, "", dry)
    dest = home / ".agents/memory"
    marker = dest / MARKER
    exists = dest.exists()
    if (dest.is_symlink() or not inside(home, dest)
            or (exists and (marker.is_symlink() or not marker.is_file()))):
        if not sharing:
            return messages
        pending.incomplete()
        messages = [f"WARN  {dest} is not an agent-sync-owned memory folder"]
        return messages if source.is_dir() else messages + mirror_index(locations, claude, "", dry)
    resolved_dest = dest.resolve()

    action = "would update" if dry else "updated"
    source_files = {path.name: path.read_bytes() for path in sorted(source.glob("*.md"))
                    if path.is_file() and not path.is_symlink()} if sharing else {}
    index = source_files.pop("MEMORY.md", None)
    private_notes, chosen = {}, set()        # chosen: notes you listed in share_memory, by name or file name
    for filename, data in source_files.items():
        name, reasons, kinds = note_privacy(filename, data.decode(), private_memory, share_memory)
        if {name, Path(filename).stem} & set(share_memory):
            chosen.add(filename)
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
        index_text = "".join(line for row, line in enumerate(lines)
                             if row not in secrets and line.startswith("-")
                             and (link := LINK.match(line)) and unquote(link[2]) in source_files
                             and all(unquote(target) in source_files for target in LINK_TARGET.findall(line))
                             and (not sensitive(line) or unquote(link[2]) in chosen))
        source_files["MEMORY.md"] = index_text.encode()

    owned = manifest(marker)
    malformed = owned is None
    if malformed:
        messages.append(f"WARN  malformed memory manifest {marker}; existing notes are treated as unowned")
        if not sharing:
            return messages
        owned = {}
    written = dict(owned)

    def check_dest() -> None:
        if dest.is_symlink() or dest.resolve() != resolved_dest:
            raise writes.Raced(dest)

    def skip_link(path: Path) -> bool:
        if not path.is_symlink():
            return False
        messages.append(f"WARN  skipped symlink {path}")
        pending.incomplete()
        return True

    copied = False
    for name, data in source_files.items():
        path = dest / name
        if skip_link(path):
            continue
        before = writes.read_bytes(path) if path.is_file() else None
        if before == data:
            continue
        expected = digest(before) if before is not None else None
        key = f"memory:{name}"
        if before is not None or path.exists():
            if name not in owned:
                messages.append(f"WARN  {path} is not a copy agent-sync made; left alone")
                continue
            if expected != owned[name] and (not pending.due(key, f"your edits to memory copy {path} (Claude Code's "
                                                                 "note changed; its new text replaces them)") or dry):
                continue
        if not dry:
            def replace(current: bytes | None, expected=expected, path=path, data=data) -> bytes:
                if (digest(current) if current is not None else None) != expected:
                    raise writes.Raced(path)
                return data

            try:
                check_dest()
                dest.mkdir(mode=0o700, parents=True, exist_ok=True)
                writes.update(path, replace, check=check_dest, new_mode=0o600, follow_symlinks=False)
            except writes.Raced as raced:
                messages.append(f"WARN  {raced}")
                pending.incomplete()
                continue
            written[name] = digest(data)
            pending.removed(key)
        copied = True
    for path in sorted(dest.glob("*.md")):
        if path.name in source_files or path.name not in owned or skip_link(path) or not path.is_file():
            continue                                        # a file agent-sync did not write is never touched
        before = path.read_bytes()
        key = f"memory:{path.name}"
        private = path.name in private_notes
        edited = digest(before) != owned[path.name]
        reason = "the note is private now" if private else "no longer in Claude Code's memory"
        if not sharing:
            reason = "memory sharing is off"
        if (edited or (sharing and not private)) and not pending.due(
                key, f"memory copy {path} ({reason}{'; you edited this copy' if edited else ''})"):
            continue
        if not dry:
            check_dest()
            backups.save(path)
            check_dest()
            if skip_link(path) or writes.read_bytes(path) != before:
                messages.append(f"WARN  {writes.Raced(path)}")
                pending.incomplete()
                continue
            path.unlink()
            written.pop(path.name, None)
            pending.removed(key)
        if not edited and (private or not sharing):
            messages.append(f"{'would remove' if dry else 'removed'} {path}: {reason} (backed up)")
        elif not dry:
            copied = True
    written = {name: value for name, value in written.items()
               if (dest / name).exists() or (dest / name).is_symlink()}
    if copied:
        messages.append(f"{action} memory copy ({len(source_files)} files)")
    if not dry and (written != owned or malformed):
        data = (json.dumps({"files": dict(sorted(written.items()))}, indent=2) + "\n").encode()
        check_dest()
        writes.update(marker, lambda _: data, check=check_dest, new_mode=0o600, follow_symlinks=False)

    return messages + mirror_index(locations, claude, index_text, dry) if sharing else messages


def mirror_index(locations: dict[str, Path], claude: Path, index_text: str, dry: bool) -> list[str]:
    messages = []
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
                def plan(data: bytes | None) -> bytes | None:
                    if data is None and not section:
                        return None
                    return change((data or b"").decode()).encode()

                changed = writes.update(path, plan)
        except writes.Raced as raced:
            messages.append(f"WARN  {raced}")
            pending.incomplete()
            continue
        if changed:
            messages.append(f"{'would update' if dry else 'updated'} memory index in {path}")
    return messages
