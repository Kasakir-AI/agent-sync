"""Write the version chosen in review (or by upkeep's MCP repair) to the other tools: plan the target files, merge
the chosen text into each, stage and back up, then rename into place."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from stat import S_IMODE

from . import instructions, mcp, review, roles, skills, tomledit
from .config import TOOLS, Config
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
        source_folder = skills.folders(home)[source].resolve()
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
    if "cursor" in step.tools:
        front, rest = instructions.split_front(text)
        if not front:
            return instructions.rendered("cursor", "", merge(rest, body, drop).rstrip("\n"), step.winner.path)
        return front + merge(rest, body, drop)
    return merge(instructions.BLOCK.sub("\n", text), body, drop)

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

def codex_mcp_text(text: str, name: str, winner: dict) -> str:
    current = tomllib.loads(text).get("mcp_servers", {})
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

def mcp_text(step: Step, text: str) -> str:
    winner = json.loads(step.winner.body)
    name = step.winner.key
    if "codex" in step.tools:
        return codex_mcp_text(text, name, winner)
    data = json.loads(text) if text else {}
    servers = data.get("mcpServers") or {}
    entry = servers.get(name) or {}
    keep = {key: value for key, value in entry.items() if key not in MCP_KEYS and key != "type"}
    servers[name] = {**keep, **winner, "type": "stdio" if "command" in winner else "http"}
    data["mcpServers"] = servers
    return json.dumps(data, indent=2) + "\n"

def edits(steps: list[Step]) -> list[Edit]:
    """Every file the steps change, with its content before and after; steps on one settings file chain."""
    pending: dict[Path, tuple[Step, Content, str]] = {}

    def read(path: Path) -> Content | None:
        return pending[path][1] if path in pending else current(path)

    for step in steps:
        kind = step.key.split(":", 1)[0]
        for path, (after, winner) in WRITERS[kind](step, read).items():
            if path in pending and pending[path][0].key != step.key and kind != "mcp":
                raise ValueError(f"more than one selection would write {path}")
            pending[path] = (step, after, winner)
    changes = []
    for path, (step, after, winner) in pending.items():
        before = current(path)
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

def apply(home: Path, steps: list[Step], backup_dir: Path) -> list[str]:
    """Write only what changes: stage each file beside its target, back up the originals, then rename into place.
    (`remember` then records the result for the next review.)"""
    changes = edits(steps)
    staged: list[tuple[Edit, Path]] = []
    try:
        for edit in changes:
            if checked_path(home, edit.path) != edit.path:
                raise ValueError(f"target moved: {edit.path}")
            edit.path.parent.mkdir(parents=True, exist_ok=True)
            temp = edit.path.with_name(f".{edit.path.name}.agent-sync-{secrets.token_hex(4)}")
            staged.append((edit, temp))
            if isinstance(edit.after, Link):
                temp.symlink_to(edit.after.target)
            else:
                temp.write_bytes(edit.after)
                if edit.before is not None:
                    shutil.copymode(edit.path, temp)
                elif edit.create_mode is not None:
                    temp.chmod(edit.create_mode)
        for edit, _ in staged:
            dest = backup_dir / edit.path.relative_to(home.resolve())
            if edit.before is not None and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(edit.path, dest, follow_symlinks=False)
        for edit, temp in staged:
            temp.replace(edit.path)
    finally:
        for _, temp in staged:
            if temp.is_symlink() or temp.exists():
                temp.unlink()
    targets = {edit.target: edit.action for edit in changes}
    return [f"{'created' if action == 'create' else 'updated'} {shown(home, target)}"
            for target, action in targets.items()]

def backup_folder(root: Path, prefix: str) -> Path:
    """A new, private folder for the originals of one apply."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now()
    while True:
        backup = root / f"{prefix}-{stamp:%Y%m%d-%H%M%S}"
        try:
            backup.mkdir(mode=0o700)
        except FileExistsError:
            stamp += timedelta(seconds=1)
        else:
            return backup

def repair_mcp(home: Path, cfg: Config, dry: bool = False) -> list[str]:
    """Replace each MCP server copy that points at files that no longer exist with the working version, when every
    working copy agrees on it. A tool that owns a server (Codex's node_repl) updates its own copy; copies made
    from it before then go stale and would otherwise stay so, as the MCP mirror never changes an existing server."""
    scan_now = review.scan(home, cfg, ["mcp"])
    choices, why = {}, {}
    for group in scan_now.groups:
        working = [version for version in group.versions if not version.missing and not fixed(version)]
        stale = [version for version in group.versions if version.missing]
        if group.kind == "mcp" and stale and working and len({version.digest for version in working}) == 1:
            choices[group.key] = working[0].id
            broken = tool_names(sorted({tool for version in stale for tool in version.tools}, key=TOOLS.index))
            why[group.key] = (f"MCP server {group.name}: {broken} pointed at files that no longer exist; "
                              f"now {tool_names(working[0].tools)}'s version")
    steps = [step for step in plan(home, scan_now, choices, cfg.source) if step.action == "replace"]
    if not steps:
        return []
    if dry:
        return [f"would update {shown(home, step.path)} ({why[step.key]})" for step in steps]
    backup = backup_folder(cfg.state_dir / "backups", "repair")
    if not apply(home, steps, backup):
        backup.rmdir()
        return []
    return [f"updated {shown(home, step.path)} ({why[step.key]})" for step in steps] + [
        f"originals backed up in {shown(home, backup)}"]


WRITERS = {
    "instructions": write_text(instruction_text),
    "rule": write_text(rule_text),
    "roles": write_text(role_text),
    "skill": write_skill,
    "mcp": write_text(mcp_text, merged=False),
}
