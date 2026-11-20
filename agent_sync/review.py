"""Compare user settings across the three tools."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path

from . import instructions, mcp, roles, skills, tomledit
from .config import TOOLS, Config, save_setting

TOOL_LABELS = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}


class Origin(StrEnum):
    YOURS = "yours"
    EDITED_COPY = "edited-copy"
    COPY = "copy"
    PACKAGE = "package"
    PLUGIN = "plugin"
    SETTINGS = "settings"


ORIGIN_LABELS = {
    Origin.YOURS: "your edit",
    Origin.EDITED_COPY: "agent-sync copy, edited since",
    Origin.COPY: "copied by agent-sync",
    Origin.PACKAGE: "package update",
    Origin.PLUGIN: "from a Claude Code plugin",
    Origin.SETTINGS: "in the tool's settings file",
}
RANK = {Origin.YOURS: 3, Origin.EDITED_COPY: 3, Origin.PACKAGE: 2,
        Origin.PLUGIN: 2, Origin.SETTINGS: 1, Origin.COPY: 0}
DATED = {Origin.YOURS, Origin.EDITED_COPY}


@dataclass(frozen=True)
class Version:
    id: str
    tools: tuple[str, ...]
    path: Path
    key: str
    body: str
    digest: str
    modified: float
    lines: int
    origin: Origin
    note: str


@dataclass
class Group:
    kind: str
    name: str
    versions: list[Version]
    missing: tuple[str, ...]
    matched_by: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def status(self) -> str:
        if len({version.digest for version in self.versions}) > 1:
            return "differ"
        return "missing" if self.missing else "same"


@dataclass(frozen=True)
class Plugin:
    id: str
    enabled: bool = True
    skills: int = 0
    shared: bool = False


@dataclass
class Scan:
    groups: list[Group]
    plugins: dict[str, dict[str, list[Plugin]]]
    managed: dict[str, int]


@dataclass(frozen=True)
class Step:
    key: str
    tools: tuple[str, ...]
    path: Path
    action: str
    winner: Version
    marker: str = ""


@dataclass(frozen=True)
class Kind:
    scan: Callable[[Path, Config, dict], Iterator[tuple[str, Version]]]
    location: Callable[[Path, str, str], Path | None]
    write: Callable[[Step, Path, Path], None]


RULES = {"claude": (".claude/rules", ".md"), "cursor": (".cursor/rules", ".mdc")}
MCP_FILES = {"claude": ".claude.json", "codex": ".codex/config.toml", "cursor": ".cursor/mcp.json"}
SKILL_IGNORES = {skills.MARKER, ".DS_Store"}


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def normalized(body: str) -> str:
    text = "\n".join(line.rstrip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def make_version(kind: str, name: str, tools: tuple[str, ...], path: Path, body: str, origin: Origin,
                 note: str = "", *, key: str = "", digest: str = "", modified: float | None = None) -> Version:
    identity = json.dumps((kind, name, os.fspath(path), key)).encode()
    return Version(
        hashlib.sha1(identity).hexdigest()[:10], tools, path, key, body,
        digest or hashlib.sha256(normalized(body).encode()).hexdigest(),
        path.stat().st_mtime if modified is None else modified, len(body.splitlines()), origin, note,
    )


def text_version(kind: str, name: str, tool: str, path: Path, origin: Origin) -> Version:
    body = instructions.BLOCK.sub("\n", instructions.split_front(read_text(path))[1])
    note = "mirrored global instructions" if origin == Origin.COPY else ""
    return make_version(kind, name, (tool,), path, body, origin, note)


def scan_instructions(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    name = "Global instructions"
    for tool, path in instructions.files(home).items():
        if path.is_file():
            origin = Origin.COPY if tool == "cursor" else Origin.YOURS
            yield name, text_version("instructions", name, tool, path, origin)


def rule_location(home: Path, name: str, tool: str) -> Path | None:
    if tool in RULES:
        folder, suffix = RULES[tool]
        return home / folder / (name + suffix)
    return None


def scan_rules(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    for tool, (folder, suffix) in RULES.items():
        for path in sorted((home / folder).glob("*" + suffix)):
            name = path.stem
            if path.is_file() and name not in {"pstack-models", "agent-sync-instructions"}:
                yield name, text_version("rule", name, tool, path, Origin.YOURS)


def scan_roles(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    name = "pstack model roles"
    for tool, path in roles.files(home).items():
        if path.is_file():
            _, lines, _ = roles.split(read_text(path))
            origin = Origin.COPY if cfg.mirror_roles and tool != cfg.source else Origin.YOURS
            note = f"mirrored from {TOOL_LABELS[cfg.source]}" if origin == Origin.COPY else ""
            yield name, make_version("roles", name, (tool,), path, "\n".join(lines), origin, note)


def skill_tree(path: Path) -> tuple[str, float]:
    digest = hashlib.sha256()
    newest = 0.0
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(set(dirs) - SKILL_IGNORES)
        for name in sorted((set(files) - SKILL_IGNORES) | set(dirs)):
            entry = Path(root) / name
            if entry.is_symlink():
                data = b"link\0" + os.fsencode(os.readlink(entry))
            elif entry.is_file():
                data = b"file\0" + entry.read_bytes()
            else:
                continue
            digest.update(os.fsencode(entry.relative_to(path)) + b"\0")
            digest.update(hashlib.sha256(data).digest())
            newest = max(newest, entry.lstat().st_mtime)
    return digest.hexdigest(), newest


def skill_version(home: Path, name: str, tools: tuple[str, ...], path: Path) -> Version:
    digest, modified = skill_tree(path)
    marker = path / skills.MARKER
    note = ""
    origin = Origin.YOURS
    if marker.is_file():
        marker_text = read_text(marker)
        source = marker_text.split("\n", 1)[0].removeprefix("copied by agent-sync from ")
        if source.startswith(f"{home}/"):
            source = "~/" + source.removeprefix(f"{home}/")
        note = f"copied from {source}" if source else ""
        if marker_text.startswith(skills.PLUGIN_MARK):
            origin = Origin.PLUGIN
        else:
            origin = Origin.EDITED_COPY if modified > marker.stat().st_mtime + 2 else Origin.COPY
    return make_version("skill", name, tools, path, read_text(path / "SKILL.md"), origin, note,
                        digest=digest, modified=modified)


def scan_skills(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    locations = {home / ".claude/skills": ("claude",), home / ".agents/skills": ("codex", "cursor"),
                 home / ".codex/skills": ("codex",), home / ".cursor/skills": ("cursor",)}
    folders: dict[tuple[str, Path], set[str]] = {}
    for root, tools in locations.items():
        for path in sorted(root.iterdir()) if root.is_dir() else ():
            if not path.name.startswith(".") and path.name not in locked and (path / "SKILL.md").is_file():
                folders.setdefault((path.name, path.resolve()), set()).update(tools)
    for (name, path), tools in folders.items():
        yield name, skill_version(home, name, tuple(tool for tool in TOOLS if tool in tools), path)


def portable(server: dict) -> dict | None:
    if not isinstance(server, dict):
        return None
    if server.get("command") and (not isinstance(server.get("args") or [], list)
                                  or not isinstance(server.get("env") or {}, dict)):
        return None
    try:
        value = mcp.portable(server)
    except (TypeError, ValueError):
        return None
    if value is None:
        return None
    if "url" in value:
        return value if isinstance(value["url"], str) else None
    env = value.get("env", {})
    if (isinstance(value["command"], str) and all(isinstance(arg, str) for arg in value["args"])
            and all(isinstance(key, str) and isinstance(val, str) for key, val in env.items())):
        return value
    return None


def scan_mcp(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    for tool, filename in MCP_FILES.items():
        for name, server in sorted(mcp.read(home, tool).items()):
            if (value := portable(server)) is not None:
                body = json.dumps(value, indent=2, sort_keys=True)
                yield name, make_version("mcp", name, (tool,), home / filename, body, Origin.SETTINGS, key=name)



def group_tools(group: Group) -> set[str]:
    return {tool for version in group.versions for tool in version.tools}


def pair_similar(groups: list[Group]) -> None:
    candidates = [group for group in groups if group.kind in {"instructions", "rule"}]
    bodies = {version.id: normalized(version.body) for group in candidates for version in group.versions}

    def similarity(left: Group, right: Group) -> float:
        best = 0.0
        for a in left.versions:
            for b in right.versions:
                if not bodies[a.id] or not bodies[b.id]:
                    continue
                left, right = bodies[a.id], bodies[b.id]
                if left == right:
                    return 1.0
                match = SequenceMatcher(None, left, right, autojunk=len(left) * len(right) > 16_000_000)
                if match.quick_ratio() >= max(0.6, best):
                    best = max(best, match.ratio())
        return best

    for group in candidates[:]:
        if group.kind != "rule" or group not in candidates:
            continue
        matches = [(similarity(group, other), other) for other in candidates if other is not group
                   and (len(group_tools(group)) == 1 or (other.kind == "instructions"
                        and len(group_tools(other)) == 1))]
        ratio, target = max(matches, key=lambda pair: pair[0], default=(0.0, group))
        if ratio >= 0.6:
            target.versions.extend(group.versions)
            target.matched_by = f"content {ratio:.0%} alike"
            candidates.remove(group)
            groups.remove(group)


def scan_plugins(home: Path, cfg: Config) -> dict[str, dict[str, list[Plugin]]]:
    plugins: dict[str, dict[str, list[Plugin]]] = {}

    def add(tool: str, plugin: Plugin) -> None:
        name = plugin.id.split("@", 1)[0]
        plugins.setdefault(name, {}).setdefault(tool, []).append(plugin)

    for pid, root in skills.plugins(home).items():
        shared = pid in cfg.plugin_skills or pid.split("@", 1)[0] in cfg.plugin_skills
        add("claude", Plugin(pid, skills=len(skills.plugin_skill_dirs(root)), shared=shared))
    config = home / MCP_FILES["codex"]
    if config.is_file():
        for pid, entry in sorted(tomllib.loads(read_text(config)).get("plugins", {}).items()):
            enabled = entry.get("enabled", True) if isinstance(entry, dict) else bool(entry)
            add("codex", Plugin(pid, enabled=enabled))
    cursor = home / ".cursor/plugins"
    for path in sorted((cursor / "cache").glob("*/*")):
        if path.is_dir():
            add("cursor", Plugin(f"{path.name}@{path.parent.name}"))
    for path in sorted((cursor / "local").glob("*")):
        if path.is_dir():
            add("cursor", Plugin(path.name))
    return dict(sorted(plugins.items()))


def scan(home: Path, cfg: Config) -> Scan:
    """Read the user settings and count the files kept in step by package updates."""
    locked = skills.locked(home)
    groups = []
    for kind, entry in KINDS.items():
        by_name: dict[str, list[Version]] = {}
        for name, version in entry.scan(home, cfg, locked):
            by_name.setdefault(name, []).append(version)
        groups.extend(Group(kind, name, versions, ()) for name, versions in sorted(by_name.items()))
    pair_similar(groups)
    for group in groups:
        have = group_tools(group)
        location = KINDS[group.kind].location
        group.missing = tuple(tool for tool in TOOLS if tool not in have and location(home, group.name, tool))
    managed = Counter(meta.get("source") or "Unknown package" for meta in locked.values())
    patterns = {"claude": ".claude/agents/*.md", "codex": ".codex/agents/*.toml",
                 "cursor": ".cursor/plugins/local/*-agents/agents/*.md"}
    for tool, pattern in patterns.items():
        count = sum(path.is_file() for path in home.glob(pattern))
        if count:
            managed[f"{TOOL_LABELS[tool]} agents"] = count
    return Scan(groups, scan_plugins(home, cfg), dict(sorted(managed.items())))


def preferred(versions: list[Version], source: str) -> Version:
    rank = max(RANK[version.origin] for version in versions)
    candidates = [version for version in versions if RANK[version.origin] == rank]
    if candidates[0].origin in DATED:
        latest = max(version.modified for version in candidates)
        candidates = [version for version in candidates if latest - version.modified <= 60]
    return max(candidates, key=lambda version: (source in version.tools, version.lines))


def recommend(group: Group, source: str) -> tuple[Version, list[str]]:
    """Pick by origin, then meaningful edit date, source and length."""
    winner = preferred(group.versions, source)
    others = [version for version in group.versions if version.id != winner.id]
    if not others:
        return winner, ["the only version found"]
    runner_up = preferred(others, source)
    if RANK[winner.origin] != RANK[runner_up.origin]:
        lead = {
            Origin.YOURS: "your own edit",
            Origin.EDITED_COPY: "this copy has your edits",
            Origin.PACKAGE: "from a package update",
            Origin.PLUGIN: "from a plugin",
            Origin.SETTINGS: "in the tool's settings file",
        }[winner.origin]
        other = {
            Origin.COPY: "a copy agent-sync made",
            Origin.SETTINGS: "in the tool's settings file",
            Origin.PACKAGE: "from a package update",
            Origin.PLUGIN: "from a plugin",
        }[runner_up.origin]
        return winner, [f"{lead}; the other is {other}"]
    gap = winner.modified - runner_up.modified
    if winner.origin in DATED and gap > 60:
        unit, seconds = next((unit, seconds) for unit, seconds in (("day", 86400), ("hour", 3600), ("minute", 60))
                             if gap >= seconds)
        count = int(gap // seconds)
        dates = [datetime.fromtimestamp(version.modified) for version in (winner, runner_up)]
        when = " vs ".join(f"{date:%b} {date.day}" for date in dates)
        return winner, [f"edited {count} {unit}{'s' if count != 1 else ''} later ({when})"]
    if source in winner.tools and source not in runner_up.tools:
        return winner, [f"from your source of truth ({TOOL_LABELS[source]})"]
    if winner.lines > runner_up.lines:
        return winner, [f"longer: {winner.lines} lines vs {runner_up.lines}"]
    return winner, ["tied on origin, source and length"]


def cautions(version: Version, winner: Version) -> list[str]:
    if version.origin == Origin.COPY and version.modified > winner.modified:
        return ["newer only because agent-sync rewrote it"]
    if version.origin == Origin.PACKAGE:
        return ["date is the last package update, not an edit"]
    if version.origin == Origin.SETTINGS:
        return ["date is when the settings file last changed"]
    return []


def checked_path(home: Path, path: Path) -> Path:
    try:
        real = path.resolve()
    except RuntimeError as exc:
        raise ValueError(f"cannot resolve {path}: {exc}") from exc
    if not real.is_relative_to(home.resolve()):
        raise ValueError(f"cannot write outside the home folder: {path}")
    return real


def plan(home: Path, scan: Scan, choices: dict[str, str], source: str) -> list[Step]:
    """List the target paths for the selected versions without writing."""
    groups = {group.key: group for group in scan.groups}
    steps = []
    for key, choice in choices.items():
        group = groups.get(key)
        if group is None:
            raise ValueError(f"unknown setting: {key}")
        if choice == "leave":
            continue
        winner = next((version for version in group.versions if version.id == choice), None)
        if winner is None:
            raise ValueError(f"unknown version for {key}")
        targets: dict[Path, set[str]] = {}
        same = {version.path.resolve() for version in group.versions if version.digest == winner.digest}
        for version in group.versions:
            if version.digest != winner.digest:
                targets.setdefault(checked_path(home, version.path), set()).update(version.tools)
        location = KINDS[group.kind].location
        for tool in group.missing:
            path = checked_path(home, location(home, group.name, tool))
            if path not in same:
                targets.setdefault(path, set()).add(tool)
        source_path = next((version.path for version in group.versions if source in version.tools), None)
        source_folder = skills.folders(home)[source].resolve()
        for path, tools in targets.items():
            exists = path.exists()
            marker = (f"copied by agent-sync from {source_path}\n"
                      if group.kind == "skill" and not exists and source_path and path.parent != source_folder else "")
            steps.append(Step(key, tuple(tool for tool in TOOLS if tool in tools), path,
                              "replace" if exists else "create", winner, marker))
    return steps


def write_text(rewrite: Callable[[Step, str], str]) -> Callable[[Step, Path, Path], None]:
    def write(step: Step, current: Path, staged: Path) -> None:
        text = read_text(current) if current.is_file() else ""
        updated = rewrite(step, text)
        if text != updated or not current.exists():
            staged.write_bytes(updated.encode())
    return write


def instruction_text(step: Step, text: str) -> str:
    body = step.winner.body.rstrip("\r\n")
    if "cursor" in step.tools:
        front, _ = instructions.split_front(text)
        if front:
            return front + body + "\n"
        if step.path.suffix == ".mdc" or not text:
            return instructions.rendered("cursor", "", body, step.winner.path)
    return body + "\n"


def rule_text(step: Step, text: str) -> str:
    front, _ = instructions.split_front(text)
    if not front and "cursor" in step.tools and step.action == "create":
        name = step.key.split(":", 1)[1]
        front = f"---\ndescription: {name}\nalwaysApply: true\n---\n"
    return front + step.winner.body.rstrip("\r\n") + "\n"


def role_text(step: Step, text: str) -> str:
    header, _, rest = roles.split(text)
    updated = "\n".join(header + step.winner.body.splitlines()) + ("\n" + rest if rest else "") + "\n"
    return updated.replace("\n\n\n", "\n\n")


def write_skill(step: Step, current: Path, staged: Path) -> None:
    if current.is_dir() and skill_tree(current)[0] == step.winner.digest:
        return
    if skill_tree(step.winner.path)[0] != step.winner.digest:
        raise ValueError(f"selected skill changed: {step.winner.path}")
    shutil.copytree(step.winner.path, staged, symlinks=True, ignore=shutil.ignore_patterns(*SKILL_IGNORES))
    marker = current / skills.MARKER
    if marker.is_file() or marker.is_symlink():
        shutil.copy2(marker, staged / skills.MARKER, follow_symlinks=False)
    elif step.marker:
        (staged / skills.MARKER).write_text(step.marker)


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


KINDS = {
    "instructions": Kind(scan_instructions, lambda home, name, tool: instructions.files(home)[tool],
                         write_text(instruction_text)),
    "rule": Kind(scan_rules, rule_location, write_text(rule_text)),
    "roles": Kind(scan_roles, lambda home, name, tool: None, write_text(role_text)),
    "skill": Kind(scan_skills, lambda home, name, tool: skills.folders(home)[tool] / name, write_skill),
    "mcp": Kind(scan_mcp, lambda home, name, tool: home / MCP_FILES[tool], write_text(mcp_text)),
}


def apply(home: Path, steps: list[Step], backup_dir: Path) -> list[str]:
    """Prepare all changes, back up existing targets and rename each replacement into place."""
    by_path: dict[Path, list[Step]] = {}
    for step in steps:
        if checked_path(home, step.path) != step.path:
            raise ValueError(f"target moved: {step.path}")
        by_path.setdefault(step.path, []).append(step)

    with ExitStack() as stack:
        prepared = {}
        for path, changes in by_path.items():
            if len(changes) > 1 and any(not change.key.startswith("mcp:") for change in changes):
                raise ValueError(f"more than one selection would write {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".agent-sync-", dir=path.parent)))
            staged = temporary / "new"
            for step in changes:
                kind = step.key.split(":", 1)[0]
                KINDS[kind].write(step, staged if staged.exists() else path, staged)
            if staged.exists():
                prepared[path] = staged

        for path, staged in prepared.items():
            if path.exists():
                dest = backup_dir / path.relative_to(home.resolve())
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    if path.is_dir():
                        shutil.copytree(path, dest, symlinks=True)
                    else:
                        shutil.copy2(path, dest, follow_symlinks=False)
                shutil.copymode(path, staged)

        changes = []
        for path, staged in prepared.items():
            if checked_path(home, path) != path:
                raise ValueError(f"target moved: {path}")
            exists = path.exists()
            previous = staged.with_name("old")
            if exists and path.is_dir():
                path.rename(previous)
            try:
                staged.replace(path)
            except OSError:
                if previous.exists():
                    previous.rename(path)
                raise
            changes.append(f"{'replaced' if exists else 'created'} {path}")
    return changes


def set_plugin_skills(home: Path, ids: list[str]) -> None:
    save_setting(home, "plugin_skills", ids)
