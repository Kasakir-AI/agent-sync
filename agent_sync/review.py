"""Compare user settings across the three tools: read every version of each setting, judge how they differ and
recommend one. Line comparison is in textmerge, and writing the chosen version is in writes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path

from . import instructions, mcp, roles, skills
from .config import Config
from .registry import DEFAULT_AGENTS, TOOLS, grouped_paths, paths
from .textmerge import (
    extends,
    keys,
    load_baselines,
    meaningful,
    normalized,
    own_lines,
    removed_lines,
    reworded,
    rows,
    save_baselines,
)


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
    scoped_rule: bool = False
    missing: tuple[str, ...] = ()    # an MCP server's: the absolute paths it names that don't exist


@dataclass
class Group:
    kind: str
    name: str
    versions: list[Version]
    missing: tuple[str, ...]
    matched_by: str = ""
    status: str = ""
    note: str = ""
    synced: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)  # see `baselines`
    confirmed: dict[str, frozenset[str]] = field(default_factory=dict)  # file -> own lines the user chose to keep

    @property
    def key(self) -> str:
        if self.scoped_rule:
            return f"{self.kind}:{self.name}/{self.versions[0].id}"
        return f"{self.kind}:{self.name}"

    @property
    def scoped_rule(self) -> bool:
        return self.versions[0].scoped_rule


@dataclass(frozen=True)
class Plugin:
    id: str
    enabled: bool = True
    skills: int = 0


@dataclass
class Scan:
    groups: list[Group]
    plugins: dict[str, dict[str, list[Plugin]]]
    managed: dict[str, int]


@dataclass(frozen=True)
class Link:
    target: str


Content = bytes | Link


@dataclass(frozen=True)
class Kind:
    scan: Callable[[Path, Config, dict], Iterator[tuple[str, Version]]]
    location: Callable[[Path, str, str], Path | None]


RULES = {"claude": (".claude/rules", ".md"), "cursor": (".cursor/rules", ".mdc")}
EXTRA_SKILL_ROOTS = {"codex": ".codex/skills", "cursor": ".cursor/skills"}
SKILL_IGNORES = {skills.MARKER, ".DS_Store"}


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def as_text(data: Content | None) -> str | None:
    if isinstance(data, bytes) and b"\0" not in data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def make_version(kind: str, name: str, tools: tuple[str, ...], path: Path, body: str, origin: Origin,
                 note: str = "", *, key: str = "", digest: str = "", modified: float | None = None,
                 scoped_rule: bool = False, missing: tuple[str, ...] = ()) -> Version:
    identity = json.dumps((kind, name, os.fspath(path), key)).encode()
    return Version(
        hashlib.sha1(identity).hexdigest()[:10], tools, path, key, body,
        digest or hashlib.sha256(normalized(body).encode()).hexdigest(),
        path.stat().st_mtime if modified is None else modified, len(body.splitlines()), origin, note, scoped_rule,
        missing,
    )


def is_scoped_rule(tool: str, front: str) -> bool:
    if tool == "claude":
        return bool(re.search(r"""(?m)^[ \t]*["']?paths["']?[ \t]*:""", front))
    fields = r"""(?m)^[ \t]*["']?(?:globs|alwaysApply)["']?[ \t]*:"""
    always = r"""(?m)^[ \t]*["']?alwaysApply["']?[ \t]*:[ \t]*(?i:true)[ \t]*(?:#[^\n]*)?\r?$"""
    return bool(re.search(fields, front) and not re.search(always, front))


def text_version(kind: str, name: str, tool: str, path: Path, origin: Origin) -> Version:
    front, body = instructions.split_front(read_text(path))
    body = instructions.BLOCK.sub("\n", body)
    note = "mirrored global instructions" if origin == Origin.COPY else ""
    return make_version(kind, name, (tool,), path, body, origin, note,
                        scoped_rule=kind == "rule" and is_scoped_rule(tool, front))


def scan_instructions(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    name = "Global instructions"
    for path, readers in grouped_paths(instructions.files(home, cfg.agents)).items():
        if path.is_file():
            _, body = instructions.split_front(read_text(path))
            origin = Origin.COPY if "cursor" in readers else Origin.YOURS
            note = "mirrored global instructions" if origin == Origin.COPY else ""
            yield name, make_version("instructions", name, readers, path, instructions.BLOCK.sub("\n", body),
                                     origin, note)


def rule_location(home: Path, name: str, tool: str) -> Path | None:
    if tool in RULES:
        folder, suffix = RULES[tool]
        path = home / folder / (name + suffix)
        if not path.exists() and not path.is_symlink():
            return path
    return None


def scan_rules(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    for tool, (folder, suffix) in RULES.items():
        if tool not in cfg.agents:
            continue
        for path in sorted((home / folder).glob("*" + suffix)):
            name = path.stem
            if path.is_file() and name not in {"pstack-models", "agent-sync-instructions"}:
                yield name, text_version("rule", name, tool, path, Origin.YOURS)


def scan_roles(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    name = "pstack model roles"
    for path, readers in grouped_paths(roles.files(home, cfg.agents)).items():
        if path.is_file():
            _, lines, _ = roles.split(read_text(path))
            origin = Origin.COPY if cfg.mirror_roles and cfg.source not in readers else Origin.YOURS
            note = f"mirrored from {TOOLS[cfg.source].name}" if origin == Origin.COPY else ""
            yield name, make_version("roles", name, readers, path, "\n".join(lines), origin, note)


def current(path: Path) -> Content | None:
    if path.is_symlink():
        return Link(os.readlink(path))
    return path.read_bytes() if path.is_file() else None


def skill_files(path: Path) -> dict[str, Content]:
    """Every file and link in a skill folder by relative path, without the marker and .DS_Store."""
    files = {}
    for root, dirs, names in os.walk(path):
        dirs[:] = sorted(set(dirs) - SKILL_IGNORES)
        for name in sorted((set(names) - SKILL_IGNORES) | set(dirs)):
            entry = Path(root) / name
            if (data := current(entry)) is not None:
                files[os.fspath(entry.relative_to(path))] = data
    return files


def skill_digest(files: dict[str, Content]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        raw = b"link\0" + os.fsencode(data.target) if isinstance(data, Link) else b"file\0" + data
        digest.update(os.fsencode(name) + b"\0" + hashlib.sha256(raw).digest())
    return digest.hexdigest()


def skill_extends(longer: dict[str, Content], base: dict[str, Content]) -> bool:
    def covers(name: str, data: Content) -> bool:
        mine, theirs = as_text(longer.get(name)), as_text(data)
        return longer.get(name) == data or (mine is not None and theirs is not None and extends(mine, theirs))
    return all(covers(name, data) for name, data in base.items())


def skill_version(home: Path, name: str, tools: tuple[str, ...], path: Path) -> Version:
    files = skill_files(path)
    digest = skill_digest(files)
    modified = max((path / name).lstat().st_mtime for name in files) if files else 0.0
    marker = path / skills.MARKER
    note = ""
    origin = Origin.YOURS
    if marker.is_file():
        marker_text = read_text(marker)
        reviewed = marker_text.startswith(skills.REVIEW_MARK)
        marker_text = marker_text.removeprefix(skills.REVIEW_MARK)
        source = marker_text.split("\n", 1)[0].removeprefix(skills.COPY_MARK)
        if source.startswith(f"{home}/"):
            source = "~/" + source.removeprefix(f"{home}/")
        note = f"copied from {source}" if source else ""
        if marker_text.startswith(skills.PLUGIN_MARK):
            origin = Origin.PLUGIN
        else:
            origin = Origin.EDITED_COPY if reviewed or modified > marker.stat().st_mtime + 2 else Origin.COPY
    return make_version("skill", name, tools, path, read_text(path / "SKILL.md"), origin, note,
                        digest=digest, modified=modified)


def skill_folders(home: Path, enabled=DEFAULT_AGENTS) -> dict[tuple[str, Path], set[str]]:
    """(name, real folder) -> the tools that read it, for every skill folder in the tools' skill folders."""
    folders: dict[tuple[str, Path], set[str]] = {}
    roots = list(grouped_paths(skills.folders(home, enabled)).items())
    roots.extend((home / root, (tool,)) for tool, root in EXTRA_SKILL_ROOTS.items() if tool in enabled)
    for root, tools in roots:
        for path in sorted(root.iterdir()) if root.is_dir() else ():
            if not path.name.startswith(".") and (path / "SKILL.md").is_file():
                folders.setdefault((path.name, path.resolve()), set()).update(tools)
    return folders


def plugin_of(path: Path) -> str:
    """The plugin a skill copy came from, or "" when agent-sync did not copy it from a plugin."""
    if not skills.is_plugin_copy(path):
        return ""
    return read_text(path / skills.MARKER).removeprefix(skills.PLUGIN_MARK).split(":", 1)[0].strip() or "unknown"


def scan_skills(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    for (name, path), tools in skill_folders(home, cfg.agents).items():
        if name not in locked and not plugin_of(path):
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


def plugin_servers(root: Path) -> Iterator[tuple[Path, str, dict]]:
    """(file, name, server) for the MCP servers a Claude Code plugin brings: its .mcp.json and plugin.json."""
    manifest = root / ".claude-plugin/plugin.json"
    sources = [root / ".mcp.json", manifest]
    for path in sources:
        try:
            data = json.loads(read_text(path))
        except (OSError, ValueError):
            continue
        servers = data.get("mcpServers", data if path != manifest else None) if isinstance(data, dict) else None
        if isinstance(servers, str) and path == manifest:
            sources.append(root / servers)
        elif isinstance(servers, dict):
            for name, server in sorted(servers.items()):
                yield path, name, server


ABSOLUTE = re.compile(r"(?<![\w/.~-])/(?!/)[^\s:\"',;{}()\[\]]+")


def missing_paths(server: dict) -> tuple[str, ...]:
    """The paths a stdio server names whose program or folder is gone: its command, or a path in its arguments or
    environment whose folder no longer exists. That is a copy made before the tool that owns the server moved it
    (an app update renames a versioned folder). A missing file in a folder that exists may be one the server
    creates, such as a log, so it does not count."""
    if "command" not in server:
        return ()
    found = []
    for text in [server["command"], *server["args"], *server.get("env", {}).values()]:
        if text.startswith("/") and os.path.exists(text):
            continue
        found += text.split(os.pathsep) if text.startswith("/") else ABSOLUTE.findall(text)
    return tuple(path for path in dict.fromkeys(found) if path.startswith("/") and not os.path.exists(path)
                 and (path == server["command"] or not os.path.isdir(os.path.dirname(path))))


VERSION = re.compile(r"\d+(?:\.\d+)+")
VERSION_FOLDER = re.compile(r"(?<=/)\d+(?:\.\d+)+(?=/|$)")


def app_versions(version: Version) -> tuple[str, list[str]]:
    """A stdio server's settings with its app version numbers taken out, and those numbers in order. Only where an
    app update puts them: a whole folder name in a path (…/browser/26.1120.41243/…) or a setting named *VERSION*.
    A dotted number anywhere else, such as an address, stays part of the settings; a remote server has none."""
    server, found = json.loads(version.body), []
    if "command" not in server:
        return version.body, []

    def take(text: str, whole: bool = False) -> str:
        if whole and VERSION.fullmatch(text):
            found.append(text)
            return "#"
        return VERSION_FOLDER.sub(lambda match: found.append(match[0]) or "#", text)

    rest = {"command": take(server["command"]), "args": [take(arg) for arg in server["args"]],
            "env": {key: take(value, "VERSION" in key.upper()) for key, value in server.get("env", {}).items()}}
    return json.dumps(rest, sort_keys=True), found


def newest(versions: list[Version]) -> Version | None:
    """When copies of a server differ only in app version numbers, as an app's own server does after an update
    (Codex's node_repl), the one with the highest; None otherwise."""
    if len({version.digest for version in versions}) < 2:
        return None
    parts = {version.id: app_versions(version) for version in versions}
    if len({rest for rest, _ in parts.values()}) > 1:
        return None
    def numbers(version: Version) -> list[tuple[int, ...]]:
        return [tuple(int(part) for part in found.split(".")) for found in parts[version.id][1]]
    best = max(versions, key=numbers)
    return best if len({v.digest for v in versions if numbers(v) == numbers(best)}) == 1 else None


def first_version(newer: Version, older: Version) -> str:
    """The first app version number where `newer` differs from `older`."""
    return next(a for a, b in zip(app_versions(newer)[1], app_versions(older)[1], strict=True) if a != b)


def scan_mcp(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    for path, readers in grouped_paths(paths(home, cfg.agents, "mcp")).items():
        for name, server in sorted(mcp.read(home, readers[0]).items()):
            if (value := portable(server)) is not None:
                body = json.dumps(value, indent=2, sort_keys=True)
                yield name, make_version("mcp", name, readers, path, body, Origin.SETTINGS, key=name,
                                         missing=missing_paths(value))
    for pid, root in (skills.plugins(home) if "claude" in cfg.agents else {}).items():
        for path, name, server in plugin_servers(root):
            if (value := portable(server)) is not None:
                body = json.dumps(value, indent=2, sort_keys=True)
                body = body.replace("${CLAUDE_PLUGIN_ROOT}", json.dumps(str(root))[1:-1])
                yield name, make_version("mcp", name, ("claude",), path, body, Origin.PLUGIN,
                                         f"from plugin {pid}", key=name)


def shown(home: Path, path: Path) -> str:
    """`path` with the home folder written as ~ (also when one of them goes through a link, as /var does on macOS)."""
    for base in (home, home.resolve()):
        if path.is_relative_to(base):
            return str(Path("~") / path.relative_to(base))
    return str(path)


def fixed(version: Version) -> bool:
    """An MCP server a Claude Code plugin provides: it counts as present, but is never copied or changed."""
    return version.origin == Origin.PLUGIN and bool(version.key)


def group_tools(group: Group) -> set[str]:
    return {tool for version in group.versions for tool in version.tools}


def likeness(left: str, right: str) -> float:
    """The larger of the texts' match ratio and the share of the shorter one's lines found in the longer."""
    if left == right:
        return 1.0
    short, long = sorted((keys(left), keys(right)), key=len)
    found = set(long)
    # Containment counts only between documents of a like size: a short rule quoted inside a long file is a section.
    whole = len(short) >= 3 and 2 * len(short) >= len(long)
    contained = sum(line in found for line in short) / len(short) if whole else 0.0
    match = SequenceMatcher(None, left, right, autojunk=len(left) * len(right) > 16_000_000)
    return max(contained, match.ratio() if match.quick_ratio() > max(contained, 0.6) else 0.0)


def pair_similar(groups: list[Group]) -> None:
    candidates = [group for group in groups if group.kind in {"instructions", "rule"} and not group.scoped_rule]
    bodies = {version.id: normalized(version.body) for group in candidates for version in group.versions}

    def similarity(left: Group, right: Group) -> float:
        return max((likeness(bodies[a.id], bodies[b.id]) for a in left.versions for b in right.versions
                    if bodies[a.id] and bodies[b.id]), default=0.0)

    for group in candidates[:]:
        if group.kind != "rule" or group not in candidates:
            continue
        tools = group_tools(group)
        matches = [(similarity(group, other), other) for other in candidates if other is not group
                   and tools.isdisjoint(tool for version in other.versions if version.origin != Origin.COPY
                                        for tool in version.tools)
                   and (len(tools) == 1 or (other.kind == "instructions" and len(group_tools(other)) == 1))]
        ratio, target = max(matches, key=lambda pair: pair[0], default=(0.0, group))
        if ratio >= 0.6:
            target.versions.extend(group.versions)
            target.matched_by = f"content {ratio:.0%} alike"
            candidates.remove(group)
            groups.remove(group)


SKILL_MARKS = (  # (tool, label, test on SKILL.md front matter, its body and the folder): what only that tool reads
    ("claude", "allowed-tools", lambda front, body, path: re.search(r"^allowed-tools:", front, re.M)),
    ("claude", "argument-hint", lambda front, body, path: re.search(r"^argument-hint:", front, re.M)),
    ("claude", "disable-model-invocation",
     lambda front, body, path: re.search(r"^disable-model-invocation:", front, re.M)),
    ("claude", "!`command` lines", lambda front, body, path: re.search(r"^!`", body, re.M)),
    ("claude", "$ARGUMENTS", lambda front, body, path: "$ARGUMENTS" in body),
    ("codex", "agents/openai.yaml", lambda front, body, path: (path / "agents/openai.yaml").is_file()),
)


def joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def tool_names(tools: Iterable[str]) -> str:
    return joined([TOOLS[tool].name for tool in tools])


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def skill_marks(version: Version) -> list[tuple[str, str]]:
    front, body = instructions.split_front(version.body)
    return [(tool, label) for tool, label, test in SKILL_MARKS if test(front, body, version.path)]


def tool_uses(versions: list[Version]) -> list[str]:
    """For skill versions: each one that uses something only its own tool reads and the others lack, in words."""
    marks = {version.id: skill_marks(version) for version in versions}
    uses = []
    for version in versions:
        for tool in version.tools:
            labels = [label for mark, label in marks[version.id] if mark == tool and any(
                tool not in other.tools and (mark, label) not in marks[other.id] for other in versions)]
            if labels:
                uses.append(f"{tool_names(version.tools)}'s uses {joined(labels)} only {TOOLS[tool].name} understands")
    return uses


def made_for_each_tool(home: Path, name: str, folders: list[tuple[tuple[str, ...], Path]]) -> bool:
    """Whether skill folders of one name were made on purpose for their tools: the same, or each using something
    only its own tool reads. Timing alone does not count here (a dotfiles checkout writes hand-made folders too)."""
    versions = [skill_version(home, name, tools, path) for tools, path in folders]
    return len({version.digest for version in versions}) == 1 or bool(tool_uses(versions))


def tailored(group: Group, installers: Iterable[str] = ()) -> str:
    """Why a skill's versions look made on purpose for each tool (by a plugin or an installer), or "" when they don't.
    Only skills: hand-written files saved together (a dotfiles checkout, a restore) are not an installer's work."""
    versions = group.versions
    if group.kind != "skill" or len({version.digest for version in versions}) < 2:
        return ""
    times = [version.modified for version in versions]
    together = (max(times) - min(times) <= 5
                and all(version.origin not in {Origin.COPY, Origin.EDITED_COPY} for version in versions))
    uses = tool_uses(versions)
    if not together and not uses:
        return ""
    who = max((name for name in installers if group.name == name or group.name.startswith(name + "-")),
              key=len, default="")
    if together:
        when = datetime.fromtimestamp(max(times))
        lead = (f"{who} wrote a separate version" if who else "A separate version was written") + (
            f" for each tool at the same moment ({when:%b} {when.day} {when:%H:%M})")
    else:
        lead = f"{who} made a separate version for each tool" if who else "Each tool has a version made for it"
    return "; ".join([lead, *uses])


Files = dict[str, dict[str, Content]]


def additions(kind: str, longer: Version, base: Version, files: Files) -> tuple[int, int] | None:
    """(lines, files) `longer` has on top of `base`, or None when it changes or lacks something of `base`'s."""
    if kind != "skill":
        return (own_lines(longer.body, base.body), 0) if extends(longer.body, base.body) else None
    mine, theirs = files[longer.id], files[base.id]
    if not skill_extends(mine, theirs):
        return None
    lines = sum(own_lines(as_text(mine[name]), as_text(data)) for name, data in theirs.items()
                if mine[name] != data)
    return lines, len(set(mine) - set(theirs))


def shared_base(group: Group) -> tuple[Version, dict[str, tuple[int, int]]] | None:
    """The version every other one only adds to, with what each adds (lines, files); None when there is none.
    A copy agent-sync made is never the base: it may be stale, and a line new in the source is not a note of its own."""
    if group.kind == "mcp":
        return None
    files = {version.id: skill_files(version.path) for version in group.versions} if group.kind == "skill" else {}
    for base in (version for version in group.versions if version.origin != Origin.COPY):
        extra = {version.id: additions(group.kind, version, base, files) for version in group.versions}
        if None not in extra.values():
            return base, extra
    return None


def texts(kind: str, version: Version) -> dict[str, str]:
    """The text whose lines are compared, by file: "" for the one file of a setting, else each text file of a skill."""
    if kind == "mcp":
        return {}
    if kind != "skill":
        return {"": version.body}
    return {name: text for name, data in skill_files(version.path).items() if (text := as_text(data)) is not None}


def leftovers(group: Group, version: Version, winner: Version) -> dict[Path, list[tuple[str, bool]]]:
    """Per file, the lines only `version` has next to `winner`, each True when it was deleted from `winner` since
    the last sync (it goes unless the user keeps it) or False when it is that tool's own (it stays)."""
    baseline = group.synced.get(winner.id, {})
    mine, theirs = texts(group.kind, version), texts(group.kind, winner)
    out = {}
    for name in sorted(mine.keys() & theirs.keys()):
        only = dict.fromkeys(line.rstrip() for tag, line in rows(mine[name], theirs[name])
                             if tag == "=" and meaningful(line))
        if only:
            gone = removed_lines(mine[name], theirs[name], baseline.get(name, ()))
            out[version.path.resolve() / name] = [(line, line in gone) for line in only]
    return out


def removals(group: Group, winner: Version) -> dict[str, list[str]]:
    """By version id, the lines it would lose by default when `winner` is kept everywhere."""
    out = {}
    for version in group.versions:
        lines = [line for found in leftovers(group, version, winner).values() for line, gone in found if gone]
        if lines and version.id != winner.id:
            out[version.id] = lines
    return out


def baseline_file(cfg: Config) -> Path:
    return cfg.state_dir / "review/baselines.json"


def synced(group: Group, stored: dict, source: str) -> dict[str, dict[str, frozenset[str]]]:
    """The lines of the version last kept everywhere, per file, by the id of each version that started from them.
    A stored baseline (apply writes one) holds for every version. Without one, agent-sync's newest copy is the
    source tool's text as last mirrored, so it holds only for the source tool's version."""
    entry = stored.get(group.key)
    if group.kind == "mcp":
        return {}
    if isinstance(entry, dict):
        files = entry.get("files") if group.kind == "skill" else {"": entry.get("lines")}
        heirs = group.versions
    else:
        copies = [version for version in group.versions if version.origin == Origin.COPY]
        if not copies:
            return {}
        files = {name: keys(text) for name, text in texts(group.kind, max(copies, key=lambda v: v.modified)).items()}
        heirs = [version for version in group.versions if source in version.tools and version.origin != Origin.COPY]
    if not isinstance(files, dict):
        return {}
    lines = {name: frozenset(value) for name, value in files.items()
             if isinstance(value, list) and all(isinstance(line, str) for line in value)}
    return {version.id: lines for version in heirs}


def baseline(kind: str, version: Version) -> dict:
    files = {name: keys(text) for name, text in texts(kind, version).items()}
    return {"digest": version.digest, **({"files": files} if kind == "skill" else {"lines": files[""]})}


def remember(path: Path, scan: Scan, choices: dict[str, str | dict]) -> None:
    """After an apply, store for each chosen setting the version now kept everywhere and the lines each tool kept of
    its own (the user saw and kept them), so the next review can tell a deleted line, a checked note and a new one
    apart. `scan` is taken after the apply. Recorded even when nothing had to be written."""
    stored, groups = load_baselines(path), {group.key: group for group in scan.groups}
    for key, choice in choices.items():
        group = groups.get(key)
        chosen = choice if isinstance(choice, str) else choice.get("version")
        winner = next((version for version in group.versions if version.id == chosen), None) if group else None
        if winner is None or group.kind == "mcp":
            continue
        kept = {str(file): sorted({line for line, _ in lines}) for version in group.versions
                if version.id != winner.id for file, lines in leftovers(group, version, winner).items()}
        stored[key] = {**baseline(group.kind, winner), "kept": kept}
    save_baselines(path, stored)


def unchecked(group: Group, base: Version) -> dict[str, int]:
    """By version id, how many lines of its own (not deleted from `base`) the user has never seen and kept."""
    out = {}
    for version in group.versions:
        if version.id == base.id:
            continue
        count = sum(not gone and line not in group.confirmed.get(str(file), ())
                    for file, lines in leftovers(group, version, base).items() for line, gone in lines)
        if count:
            out[version.id] = count
    return out


def judge(group: Group, installers: Iterable[str] = ()) -> tuple[str, str]:
    """The group's status and a one-line note: what each tool keeps of its own, what the shared version no longer
    has, or why it was made per tool."""
    note = ""
    if len({version.digest for version in group.versions}) > 1:
        if (found := shared_base(group)) is None:
            return ("tailored", reason) if (reason := tailored(group, installers)) else ("differ", "")
        base, extra = found
        gone = removals(group, base)
        parts, behind = [], {}
        for version in group.versions:
            lines, folders = extra[version.id]
            lines -= len(gone.get(version.id, []))
            if version.id in gone:
                behind.setdefault(len(gone[version.id]), {}).update(dict.fromkeys(version.tools))
            what = [counted(count, noun) for count, noun in ((lines, "line"), (folders, "file")) if count]
            if what:
                one = len(version.tools) == 1
                parts.append(f"{tool_names(version.tools)} {'keeps' if one else 'keep'} {joined(what)} "
                             f"of {'its' if one else 'their'} own")
        has = "has" if len(base.tools) == 1 else "have"
        lost = [f"{tool_names(base.tools)} no longer {has} {counted(count, 'line')} that "
                f"{tool_names(sorted(tools, key=tuple(TOOLS).index))} still {'has' if len(tools) == 1 else 'have'}"
                for count, tools in behind.items()]
        new = unchecked(group, base)
        check = [f"{tool_names(version.tools)} {'has' if len(version.tools) == 1 else 'have'} "
                 f"{counted(new[version.id], 'line')} {tool_names(base.tools)} doesn't that you haven't checked yet"
                 for version in group.versions if version.id in new]
        note = "; ".join(lost + check + ([] if check else parts))
        if lost:
            return "differ", note
        if check:
            return "check", note   # without a record of the last sync, an old rule and a tool's own note look alike
    return ("missing" if group.missing else "same"), note


def scan_plugins(home: Path, cfg: Config) -> dict[str, dict[str, list[Plugin]]]:
    plugins: dict[str, dict[str, list[Plugin]]] = {}

    def add(tool: str, plugin: Plugin) -> None:
        name = plugin.id.split("@", 1)[0]
        plugins.setdefault(name, {}).setdefault(tool, []).append(plugin)

    for pid, root in (skills.plugins(home) if "claude" in cfg.agents else {}).items():
        add("claude", Plugin(pid, skills=len(skills.plugin_skill_dirs(root))))
    config = home / TOOLS["codex"].mcp
    if "codex" in cfg.agents and config.is_file():
        for pid, entry in sorted(tomllib.loads(read_text(config)).get("plugins", {}).items()):
            enabled = entry.get("enabled", True) if isinstance(entry, dict) else bool(entry)
            add("codex", Plugin(pid, enabled=enabled))
    cursor = home / ".cursor/plugins"
    for path in sorted((cursor / "cache").glob("*/*")) if "cursor" in cfg.agents else ():
        if path.is_dir():
            add("cursor", Plugin(f"{path.name}@{path.parent.name}"))
    for path in sorted((cursor / "local").glob("*")) if "cursor" in cfg.agents else ():
        if path.is_dir():
            add("cursor", Plugin(path.name))
    return dict(sorted(plugins.items()))


def scan(home: Path, cfg: Config, kinds: Iterable[str] | None = None) -> Scan:
    """Read the user settings (all kinds, or the ones named) and count the files kept in step by package updates."""
    locked = skills.locked(home)
    groups = []
    for kind in kinds or KINDS:
        entry = KINDS[kind]
        by_name: dict[tuple[str, str], list[Version]] = {}
        for name, version in entry.scan(home, cfg, locked):
            by_name.setdefault((name, version.id if version.scoped_rule else ""), []).append(version)
        groups.extend(Group(kind, name, versions, ()) for (name, _), versions in sorted(by_name.items()))
    pair_similar(groups)
    stored = load_baselines(baseline_file(cfg))
    for group in groups:
        group.synced = synced(group, stored, cfg.source)
        kept = (stored.get(group.key) or {}).get("kept") if isinstance(stored.get(group.key), dict) else None
        group.confirmed = {name: frozenset(lines) for name, lines in (kept or {}).items()
                           if isinstance(lines, list) and all(isinstance(line, str) for line in lines)}
    managed = Counter(meta.get("source") or "Unknown package" for meta in locked.values())
    for group in [group for group in groups if all(fixed(version) for version in group.versions)]:
        groups.remove(group)
        managed.update(version.note.removeprefix("from plugin ") + " plugin MCP servers" for version in group.versions)
    installers = {pid.split("@", 1)[0] for pid in skills.plugins(home)}
    installers |= {str(meta.get("source") or "").rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
                   for meta in locked.values()}
    installers.discard("")
    for group in groups:
        have = group_tools(group)
        location = KINDS[group.kind].location
        if not group.scoped_rule:
            group.missing = tuple(tool for tool in cfg.agents if tool not in have and location(home, group.name, tool))
        if group.kind == "rule" and len(have) == 1 and not group.missing:
            group.status = "local"
            group.note = ("This rule stays in the tool that defines when it applies." if group.scoped_rule
                          else "A rule with this name already exists in the other tool.")
        else:
            group.status, group.note = judge(group, installers)
    managed.update(f"{pid} plugin skills" for _, path in skill_folders(home, cfg.agents) if (pid := plugin_of(path)))
    for tool in cfg.agents:
        pattern = TOOLS[tool].agent_glob
        count = sum(path.is_file() for path in home.glob(pattern)) if pattern else 0
        if count:
            managed[f"{TOOLS[tool].name} agents"] = count
    return Scan(groups, scan_plugins(home, cfg), dict(sorted(managed.items())))


def preferred(versions: list[Version], source: str) -> Version:
    versions = [version for version in versions if not fixed(version)] or versions
    versions = [version for version in versions if not version.missing] or versions
    rank = max(RANK[version.origin] for version in versions)
    candidates = [version for version in versions if RANK[version.origin] == rank]
    if candidates[0].origin in DATED:
        latest = max(version.modified for version in candidates)
        candidates = [version for version in candidates if latest - version.modified <= 60]
    return max(candidates, key=lambda version: (source in version.tools, version.lines))


def recommend(group: Group, source: str) -> tuple[Version, list[str]]:
    """Pick by origin, then meaningful edit date, source and length. When the others only add lines of their own
    to one version, that shared version: keeping it changes nothing and never spreads one tool's notes."""
    if len({version.digest for version in group.versions}) > 1 and (found := shared_base(group)):
        if gone := {line for lines in removals(group, found[0]).values() for line in lines}:
            return found[0], ["the shared version: the others only add lines to it",
                              f"it no longer has {counted(len(gone), 'line')} the last sync had; keeping it "
                              "removes them from the others, and their own lines stay"]
        return found[0], ["the shared version: the others only add lines of their own, which stay where they are"]
    winner = preferred(group.versions, source)
    others = [version for version in group.versions if version.id != winner.id]
    if not others:
        return winner, ["the only version found"]
    runner_up = preferred(others, source)
    if group.kind == "mcp" and (best := newest([v for v in group.versions if not v.missing and not fixed(v)])):
        older = next(v for v in group.versions if v.digest != best.digest and not v.missing)
        return best, [f"the newest version ({first_version(best, older)})"]
    if runner_up.missing and not winner.missing:
        return winner, ["the other copies point at files that no longer exist"]
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
    top = max(RANK[version.origin] for version in group.versions)
    for version in (version for version in group.versions if RANK[version.origin] == top):
        synced = group.synced.get(version.id, {}).get("", frozenset())
        others = [other for other in group.versions if other.digest != version.digest]
        if synced and others and all(reworded(version.body, other.body, synced) for other in others):
            return version, ["it rewords lines the others still have as they were at the last sync"]
    gap = winner.modified - runner_up.modified
    if winner.origin in DATED and gap > 60:
        unit, seconds = next((unit, seconds) for unit, seconds in (("day", 86400), ("hour", 3600), ("minute", 60))
                             if gap >= seconds)
        count = int(gap // seconds)
        dates = [datetime.fromtimestamp(version.modified) for version in (winner, runner_up)]
        when = " vs ".join(f"{date:%b} {date.day}" for date in dates)
        return winner, [f"edited {count} {unit}{'s' if count != 1 else ''} later ({when})"]
    if source in winner.tools and source not in runner_up.tools:
        return winner, [f"from your source of truth ({TOOLS[source].name})"]
    if winner.lines > runner_up.lines:
        return winner, [f"longer: {winner.lines} lines vs {runner_up.lines}"]
    return winner, ["tied on origin, source and length"]


def cautions(version: Version, winner: Version) -> list[str]:
    if version.missing and not winner.missing:
        more = f" and {counted(len(version.missing) - 1, 'other path')}" if len(version.missing) > 1 else ""
        return [f"stale: {version.missing[0]}{more} no longer exists"]
    if version.origin == Origin.COPY and version.modified > winner.modified:
        return ["newer only because agent-sync rewrote it"]
    if fixed(version):
        return ["comes with the plugin; agent-sync never copies it, as its path changes with every plugin update"]
    if version.origin == Origin.PACKAGE:
        return ["date is the last package update, not an edit"]
    if version.origin == Origin.SETTINGS:
        return ["date is when the settings file last changed"]
    return []


KINDS = {
    "instructions": Kind(scan_instructions, lambda home, name, tool: instructions.files(home, (tool,)).get(tool)),
    "rule": Kind(scan_rules, rule_location),
    "roles": Kind(scan_roles, lambda home, name, tool: None),
    "skill": Kind(scan_skills, lambda home, name, tool: home / TOOLS[tool].skills_dir / name),
    "mcp": Kind(scan_mcp, lambda home, name, tool: paths(home, (tool,), "mcp").get(tool)),
}


def label(version: Version, group: Group) -> str:
    names = tool_names(version.tools)
    clash = any(other.id != version.id and other.tools == version.tools for other in group.versions)
    return f"{names} ({version.path.name})" if clash else names


def difference(group: Group, winner: Version) -> str:
    """In plain words, what each version has that `winner` lacks and the other way round, counted in lines."""
    them, many = label(winner, group), len(winner.tools) > 1
    theirs = skill_files(winner.path) if group.kind == "skill" else {}
    sentences = []
    for version in group.versions:
        if version.digest == winner.digest:
            continue
        mine, plural = label(version, group), len(version.tools) > 1
        has, lacks = ("have", "don't") if plural else ("has", "doesn't")
        their_has, their_lacks = ("have", "don't") if many else ("has", "doesn't")
        tags = Counter(tag for tag, line in rows(version.body, winner.body) if meaningful(line))
        clauses = []
        if tags["-"] + tags["="]:
            clauses.append(f"{mine} {has} {counted(tags['-'] + tags['='], 'line')} {them} {their_lacks}")
        if tags["+"]:
            clauses.append(f"{them} {their_has} {counted(tags['+'], 'line')} {mine} {lacks}")
        if group.kind == "skill":
            files = skill_files(version.path)
            for owner, verb, absent, names in ((mine, has, their_lacks, sorted(set(files) - set(theirs))),
                                               (them, their_has, lacks, sorted(set(theirs) - set(files)))):
                if names:
                    shown = joined(names[:3]) + (" and more" if len(names) > 3 else "")
                    other = them if owner == mine else mine
                    clauses.append(f"{owner} {verb} {counted(len(names), 'file')} {other} {absent} ({shown})")
            changed = [name for name in set(files) & set(theirs) if name != "SKILL.md" and files[name] != theirs[name]]
            if changed:
                clauses.append(f"{counted(len(changed), 'other file')} {'differs' if len(changed) == 1 else 'differ'}")
        if not clauses:
            clauses.append(f"{mine} and {them} differ only in spacing or comments")
        sentences.append("; ".join(clauses) + ".")
    return " ".join(sentences)
