"""Every write to a tool's settings, instructions or memory goes through here: stage a temp file beside the target
with its mode, fsync it, back up the original, and rename into place only when the file still holds the bytes the
change was planned from. Also writes the version chosen in review (or by upkeep's MCP repair) to the other tools."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_IMODE

from . import backups, instructions, mcp, review, roles, skills, tomledit
from .config import Config
from .registry import TOOLS
from .review import (
    Content,
    Link,
    Origin,
    Scan,
    Version,
    as_text,
    current,
    fixed,
    leftovers,
    read_text,
    shown,
    skill_digest,
    skill_files,
    tool_names,
)
from .textmerge import keys, merge


@dataclass(frozen=True)
class Step:
    key: str
    tools: tuple[str, ...]
    path: Path
    action: str
    winner: Version
    marker: str = ""
    drop: dict[Path, frozenset[str]] = field(default_factory=dict)   # file -> its own lines to remove


@dataclass(frozen=True)
class Edit:
    key: str
    tools: tuple[str, ...]
    target: Path
    action: str
    path: Path
    before: Content | None
    after: Content
    winner: str = ""
    removed: tuple[str, ...] = ()
    create_mode: int | None = None


Reader = Callable[[Path], Content | None]


class Raced(ValueError):
    """A file changed again while agent-sync was writing it."""

    def __init__(self, path: Path):
        super().__init__(f"{path} changed while agent-sync was updating it; will retry next run")


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def stage(target: Path, data: bytes, mode: int) -> Path:
    """A synced temp copy of `data` beside `target`, with `mode`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.agent-sync-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(name, mode)
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


def update(path: Path, plan: Callable[[bytes | None], bytes | None], *, new_mode: int = 0o644,
           check: Callable[[], None] | None = None, follow_symlinks: bool = True) -> bool:
    """Write plan(current bytes) to `path` (through a link, to its target). Returns whether it wrote. When the file
    changes between reading and renaming, plans once more from the new bytes; when it changes again, raises Raced."""
    for _ in range(2):
        target = Path(os.path.realpath(path))
        if not follow_symlinks and (path.is_symlink() or target != path.parent.resolve() / path.name):
            raise Raced(path)
        before = read_bytes(target)
        after = plan(before)
        if after is None or after == before:
            return False
        mode = S_IMODE(target.stat().st_mode) if before is not None else new_mode
        temp = stage(target, after, mode)
        try:
            backups.save(target)
            if check is not None:
                check()
            if read_bytes(target) == before and Path(os.path.realpath(path)) == target:
                temp.replace(target)
                return True
        finally:
            temp.unlink(missing_ok=True)
    raise Raced(path)


def update_text(path: Path, change: Callable[[str], str], *, new_mode: int = 0o644, empty: str = "") -> bool:
    """`update` for a UTF-8 text file; a missing file reads as `empty`."""
    return update(path, lambda data: change(empty if data is None else data.decode()).encode(), new_mode=new_mode)


def checked_path(home: Path, path: Path) -> Path:
    try:
        real = path.resolve()
    except RuntimeError as exc:
        raise ValueError(f"cannot resolve {path}: {exc}") from exc
    if not real.is_relative_to(home.resolve()):
        raise ValueError(f"cannot write outside the home folder: {path}")
    return real


class ChoiceError(ValueError):
    """A choice that does not fit the settings as scanned."""


def plan(home: Path, scan: Scan, choices: dict[str, str | dict], source: str) -> list[Step]:
    """List the target paths for the selected versions without writing. A choice is a version id, or
    {"version": id, "remove": {file as shown: [lines]}}: the lines only that file has which go. A file not named
    there loses the lines deleted from the chosen version since the last sync."""
    groups = {group.key: group for group in scan.groups}
    steps = []
    for key, choice in choices.items():
        group = groups.get(key)
        if group is None:
            raise ValueError(f"unknown setting: {key}")
        if choice == "leave":
            continue
        if group.status == "local":
            raise ChoiceError(group.note)
        if isinstance(choice, str):
            chosen, asked = choice, {}
        else:
            chosen, asked = choice.get("version"), dict(choice.get("remove", {}))
        winner = next((version for version in group.versions if version.id == chosen), None)
        if winner is None:
            raise ValueError(f"unknown version for {key}")
        if fixed(winner):
            raise ValueError(f"{key} comes with a plugin and can't be copied to the other tools")
        targets: dict[Path, set[str]] = {}
        drop: dict[Path, frozenset[str]] = {}
        same = {version.path.resolve() for version in group.versions if version.digest == winner.digest}
        for version in group.versions:
            if version.id == winner.id or version.origin == Origin.PLUGIN:
                continue
            if (version.digest != winner.digest or
                    (group.kind == "instructions" and "cursor" not in version.tools
                     and version.path.resolve() != winner.path.resolve()
                     and instructions.BLOCK.search(read_text(version.path)))):
                targets.setdefault(checked_path(home, version.path), set()).update(version.tools)
                for path, lines in leftovers(group, version, winner).items():
                    wanted = asked.pop(shown(home, path), None)
                    wanted = {line for line, gone in lines if gone} if wanted is None else set(wanted)
                    if extra := wanted - {line for line, _ in lines}:
                        raise ChoiceError(f"{shown(home, path)} has no line of its own reading {min(extra)!r}")
                    if wanted:
                        drop[path] = frozenset(wanted)
        if asked := [path for path, lines in asked.items() if lines]:
            raise ChoiceError(f"{min(asked)} has no lines of its own to remove for {group.name}")
        location = review.KINDS[group.kind].location
        for tool in group.missing:
            destination = location(home, group.name, tool)
            if destination is None:
                continue
            path = checked_path(home, destination)
            if path not in same:
                targets.setdefault(path, set()).add(tool)
        source_path = next((version.path for version in group.versions if source in version.tools), None)
        source_folder = (home / TOOLS[source].skills_dir).resolve()
        for path, tools in targets.items():
            exists = path.exists()
            marker = (f"{skills.COPY_MARK}{source_path}\n"
                      if group.kind == "skill" and not exists and source_path and path.parent != source_folder else "")
            steps.append(Step(key, tuple(tool for tool in TOOLS if tool in tools), path,
                              "replace" if exists else "create", winner, marker, drop))
    return steps


def write_text(rewrite: Callable[[Step, str], str], merged: bool = True) -> Callable[[Step, Reader], dict]:
    def write(step: Step, read: Reader) -> dict[Path, tuple[Content, str]]:
        before = read(step.path)
        text = as_text(before) if before is not None else ""
        if text is None:
            raise ValueError(f"not a text file: {step.path}")
        return {step.path: (rewrite(step, text).encode(), step.winner.body if merged else "")}
    return write


def instruction_text(step: Step, text: str) -> str:
    body, drop = step.winner.body, step.drop.get(step.path, frozenset())
    tool = step.tools[0]
    rest = instructions.split_front(text)[1] if tool == "cursor" else text
    rest = instructions.own_text(rest)
    merged = merge(rest, body, drop, protected=instructions.local_rows(rest.splitlines()))
    return instructions.rendered(tool, text, merged, step.winner.path)


def rule_text(step: Step, text: str) -> str:
    front, rest = instructions.split_front(text)
    if not front and "cursor" in step.tools and step.action == "create":
        name = step.key.split(":", 1)[1]
        front = f"---\ndescription: {name}\nalwaysApply: true\n---\n"
    return front + merge(rest, step.winner.body, step.drop.get(step.path, frozenset()))


def role_text(step: Step, text: str) -> str:
    header, old, rest = roles.split(text)
    lines = merge("\n".join(old), step.winner.body, step.drop.get(step.path, frozenset())).splitlines()
    if lines == old:
        return text
    updated = "\n".join(header + lines) + ("\n" + rest if rest else "") + "\n"
    return updated.replace("\n\n\n", "\n\n")


def write_skill(step: Step, read: Reader) -> dict[Path, tuple[Content, str]]:
    """Merge the winner's text files, add its other files and keep everything only the target has."""
    winner = skill_files(step.winner.path)
    if skill_digest(winner) != step.winner.digest:
        raise ValueError(f"selected skill changed: {step.winner.path}")
    out: dict[Path, tuple[Content, str]] = {}
    marker = step.path / skills.MARKER
    if step.marker and not step.path.exists():
        out[marker] = (step.marker.encode(), "")
    for name, data in winner.items():
        path = step.path / name
        if Path(os.path.realpath(path)) != path or (path.exists() and not path.is_file()):
            continue
        before, theirs = read(path), as_text(data)
        mine = as_text(before)
        if before is None:
            out[path] = (data, theirs or "")
        elif mine is not None and theirs is not None:
            out[path] = (merge(mine, theirs, step.drop.get(path, frozenset())).encode(), theirs)
        elif not isinstance(data, Link):
            out[path] = (data, "")
    mark = read(marker)
    if (isinstance(mark, bytes) and mark.startswith(skills.COPY_MARK.encode())
            and not mark.startswith(skills.PLUGIN_MARK.encode())):
        merged = skill_files(step.path)
        merged.update((os.fspath(path.relative_to(step.path)), data) for path, (data, _) in out.items())
        if merged != winner:
            return {marker: (skills.REVIEW_MARK.encode() + mark, ""), **out}
    return out


MCP_KEYS = ("command", "args", "env", "url")


def codex_mcp_text(text: str, name: str, winner: dict | None) -> str:
    current = tomllib.loads(text).get("mcp_servers", {})
    if winner is None:
        table = ("mcp_servers", name)
        updated = text
        for section in reversed(tomledit.sections(text)):
            if section.parts[:2] == table:
                end = section.body
                if section.keys:
                    eol = text.find("\n", section.keys[-1][3])
                    end = len(text) if eol < 0 else eol + 1
                updated = updated[:section.start] + updated[end:]
        updated = tomledit.remove_value(updated, ("mcp_servers",), name)
        updated = tomledit.remove_inline_entry(updated, None, "mcp_servers", name)
        expected = {key: value for key, value in current.items() if key != name}
        if tomllib.loads(updated).get("mcp_servers", {}) != expected:
            raise ValueError(f"could not remove MCP server {name!r} from its TOML table")
        return updated
    server = current.get(name, {})
    settings = {key: value for key, value in server.items() if key not in MCP_KEYS}
    if tomledit.has_value(text, None, "mcp_servers"):
        updated = tomledit.set_value(text, None, "mcp_servers", {**current, name: {**settings, **winner}})
    elif tomledit.has_value(text, ("mcp_servers",), name):
        updated = tomledit.set_value(text, ("mcp_servers",), name, {**settings, **winner})
    elif name not in current:
        updated = text + mcp.codex_block(name, winner)
    else:
        updated = text
        table = ("mcp_servers", name)
        for key in MCP_KEYS:
            if key == "env":
                updated = tomledit.remove_value(updated, table, key)
                updated = tomledit.set_table(updated, (*table, key), winner.get(key, {}))
            elif key in winner:
                updated = tomledit.set_value(updated, table, key, winner[key])
            else:
                updated = tomledit.remove_value(updated, table, key)
    parsed = tomllib.loads(updated)["mcp_servers"][name]
    if parsed != {**settings, **winner}:
        raise ValueError(f"could not update the MCP server {name!r} in the settings file")
    return updated


def json_mcp_text(text: str, name: str, winner: dict | None, *, aliases: tuple[str, ...] = (),
                  typed: bool = True) -> str:
    data = json.loads(text) if text.strip() else {}
    servers = data.get("mcpServers") or {}
    if winner is None:
        servers.pop(name, None)
    else:
        entry = {key: value for key, value in (servers.get(name) or {}).items()
                 if key not in (*MCP_KEYS, *aliases) or key in winner}
        entry.update(winner)
        if typed:
            entry["type"] = "stdio" if "command" in winner else "http"
        servers[name] = entry
    data["mcpServers"] = servers
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def gemini_mcp_text(text: str, name: str, winner: dict) -> str:
    return json_mcp_text(text, name, mcp.gemini_entry(winner), aliases=("httpUrl",), typed=False)


def antigravity_mcp_text(text: str, name: str, winner: dict) -> str:
    return json_mcp_text(text, name, winner, aliases=("serverUrl",), typed=False)


MCP_WRITERS = {
    "claude-json": json_mcp_text,
    "codex-toml": codex_mcp_text,
    "cursor-json": json_mcp_text,
    "gemini-json": gemini_mcp_text,
    "antigravity-json": antigravity_mcp_text,
}


def mcp_text(step: Step, text: str) -> str:
    fmt = TOOLS[step.tools[0]].mcp_format
    return MCP_WRITERS[fmt](text, step.winner.key, json.loads(step.winner.body))


def edits(steps: list[Step]) -> list[Edit]:
    """Every file the steps change, with its content before and after; steps on one settings file chain."""
    pending: dict[Path, tuple[Step, Content, str]] = {}
    originals: dict[Path, Content | None] = {}

    def read(path: Path) -> Content | None:
        if path in pending:
            return pending[path][1]
        if path not in originals:
            originals[path] = current(path)
        return originals[path]

    for step in steps:
        kind = step.key.split(":", 1)[0]
        for path, (after, winner) in WRITERS[kind](step, read).items():
            if path in pending and pending[path][0].key != step.key and kind != "mcp":
                raise ValueError(f"more than one selection would write {path}")
            read(path)
            pending[path] = (step, after, winner)
    changes = []
    for path, (step, after, winner) in pending.items():
        before = originals[path]
        if after != before:
            left = set(keys(as_text(after) or ""))
            removed = tuple(line for line in dict.fromkeys(keys(as_text(before) or ""))
                            if line in step.drop.get(path, ()) and line not in left)
            create_mode = None
            new_skill_file = before is None and step.key.startswith("skill:") and path.name != skills.MARKER
            if new_skill_file and isinstance(after, bytes):
                source = step.winner.path / path.relative_to(step.path)
                create_mode = S_IMODE(source.lstat().st_mode)
            changes.append(Edit(step.key, step.tools, step.path, step.action, path, before, after, winner, removed,
                                create_mode=create_mode))
    return changes


def apply(home: Path, steps: list[Step], backup_dir: Path | None = None) -> list[str]:
    """Write only what changes: stage each file beside its target, back up the originals, then rename into place.
    (`remember` then records the result for the next review.)"""
    return apply_edits(home, edits(steps), backup_dir)


def apply_edits(home: Path, changes: list[Edit], backup_dir: Path | None = None, *,
                check: Callable[[], None] | None = None) -> list[str]:
    """Stage every file, back up the originals, then rename each into place; raises Raced, before renaming any, when
    one no longer holds what the edits were planned from."""
    staged: list[tuple[Edit, Path]] = []
    try:
        for edit in changes:
            if checked_path(home, edit.path) != edit.path:
                raise ValueError(f"target moved: {edit.path}")
            if isinstance(edit.after, Link):
                edit.path.parent.mkdir(parents=True, exist_ok=True)
                temp = edit.path.with_name(f".{edit.path.name}.agent-sync-{secrets.token_hex(4)}")
                temp.symlink_to(edit.after.target)
            else:
                mode = (S_IMODE(edit.path.stat().st_mode) if edit.before is not None
                        else edit.create_mode if edit.create_mode is not None
                        else 0o600 if edit.key.startswith("mcp") else 0o644)
                temp = stage(edit.path, edit.after, mode)
            staged.append((edit, temp))
        for edit, _ in staged:
            backups.save(edit.path)
            if backup_dir is not None and edit.before is not None:
                backups.copy(edit.path, home.resolve(), backup_dir)
        for edit, _ in staged:
            if current(edit.path) != edit.before:
                raise Raced(edit.path)
        if check is not None:
            check()
        for edit, temp in staged:
            temp.replace(edit.path)
    finally:
        for _, temp in staged:
            if temp.is_symlink() or temp.exists():
                temp.unlink()
    targets = {edit.target: edit.action for edit in changes}
    return [f"{'created' if action == 'create' else 'updated'} {shown(home, target)}"
            for target, action in targets.items()]


backup_folder = backups.new_folder


def repair(home: Path, cfg: Config, dry: bool = False) -> list[str]:
    """Bring every copy of an MCP server up to its newest working version. A tool that owns a server (Codex's
    node_repl) rewrites its own copy on every app update; copies made from it before then point at an older version,
    or at files that no longer exist, and would stay so, as the MCP mirror never changes an existing server. Acts
    when the working copies agree, or differ only in version numbers; anything else is left for review."""
    scan_now = review.scan(home, cfg, ["mcp"])
    choices, why = {}, {}
    for group in scan_now.groups:
        working = [version for version in group.versions if not version.missing and not fixed(version)]
        if not working:
            continue
        best = working[0] if len({v.digest for v in working}) == 1 else review.newest(working)
        behind = [version for version in group.versions if not fixed(version) and best
                  and version.digest != best.digest]
        if not behind:
            continue
        choices[group.key] = best.id
        names = tool_names(sorted({tool for version in behind for tool in version.tools}, key=tuple(TOOLS).index))
        if all(version.missing for version in behind):
            why[group.key] = (f"MCP server {group.name}: {names} pointed at files that no longer exist; "
                              f"now {tool_names(best.tools)}'s version")
        else:
            older = next(version for version in behind if not version.missing)
            why[group.key] = (f"MCP server {group.name}: {names} had an older version; now "
                              f"{tool_names(best.tools)}'s version ({review.first_version(best, older)})")
    steps = [step for step in plan(home, scan_now, choices, cfg.source) if step.action == "replace"]
    if not steps:
        return scan_now.problems
    if dry:
        return scan_now.problems + [f"would update {shown(home, step.path)} ({why[step.key]})" for step in steps]

    def check() -> None:
        for path, (target, before) in scan_now.mcp_originals.items():
            if path.resolve() != target or read_bytes(target) != before:
                raise Raced(path)

    check()
    if not apply_edits(home, edits(steps), check=check):
        return scan_now.problems
    return scan_now.problems + [f"updated {shown(home, step.path)} ({why[step.key]})" for step in steps]


def repair_mcp(home: Path, cfg: Config, dry: bool = False) -> list[str]:
    """`repair` once more from the new state when a file changed under it."""
    try:
        return repair(home, cfg, dry)
    except Raced:
        try:
            return repair(home, cfg, dry)
        except Raced as raced:
            return [f"WARN  {raced}"]


WRITERS = {
    "instructions": write_text(instruction_text),
    "rule": write_text(rule_text),
    "roles": write_text(role_text),
    "skill": write_skill,
    "mcp": write_text(mcp_text, merged=False),
}
