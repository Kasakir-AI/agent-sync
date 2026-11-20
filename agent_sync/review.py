"""Compare user settings across the three tools."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
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
    status: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"


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
class Link:
    target: str


Content = bytes | Link
Reader = Callable[[Path], Content | None]


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


@dataclass(frozen=True)
class Kind:
    scan: Callable[[Path, Config, dict], Iterator[tuple[str, Version]]]
    location: Callable[[Path, str, str], Path | None]
    write: Callable[[Step, Reader], dict[Path, tuple[Content, str]]]


RULES = {"claude": (".claude/rules", ".md"), "cursor": (".cursor/rules", ".mdc")}
MCP_FILES = {"claude": ".claude.json", "codex": ".codex/config.toml", "cursor": ".cursor/mcp.json"}
SKILL_ROOTS = {".claude/skills": ("claude",), ".agents/skills": ("codex", "cursor"),
               ".codex/skills": ("codex",), ".cursor/skills": ("cursor",)}
SKILL_IGNORES = {skills.MARKER, ".DS_Store"}
COMMENT = re.compile(r"^\s*<!--.*-->\s*$")


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def as_text(data: Content | None) -> str | None:
    if isinstance(data, bytes) and b"\0" not in data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def meaningful(line: str) -> bool:
    return bool(line.strip()) and not COMMENT.match(line)


def keys(text: str) -> list[str]:
    """The lines that carry meaning: no blank lines, no lines that are only an HTML comment."""
    return [line.rstrip() for line in text.splitlines() if meaningful(line)]


def normalized(body: str) -> str:
    text = "\n".join(line.rstrip() for line in body.splitlines() if not COMMENT.match(line))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extends(longer: str, base: str) -> bool:
    """True when `longer` has every line of `base`, in the same order, plus perhaps lines of its own."""
    lines = iter(keys(longer))
    return all(line in lines for line in keys(base))


def partner(line: str, candidates: list[str]) -> int | None:
    """The first candidate that reads like an edit of `line`, or None when `line` is unlike them all."""
    text = line.strip()
    return next((index for index, other in enumerate(candidates)
                 if SequenceMatcher(None, text, other.strip()).ratio() >= 0.6), None)


def rows(target: str, winner: str) -> list[tuple[str, str]]:
    """What keeping `winner` does to `target`, line by line: " " stays, "+" comes from the winner,
    "-" is an edited line the winner's replaces, "=" is the target's own line and stays.
    The winner's lines that are only an HTML comment are metadata of its own file and are not added."""
    old, new = target.splitlines(), winner.splitlines()
    if extends(target, winner):
        wanted, found, out = keys(winner), 0, []
        for line in old:
            matched = meaningful(line) and found < len(wanted) and wanted[found] == line.rstrip()
            found += matched
            out.append((" " if matched or not meaningful(line) else "=", line))
        return out
    out = []
    match = SequenceMatcher(None, [line.rstrip() for line in old], [line.rstrip() for line in new], autojunk=False)
    for tag, i1, i2, j1, j2 in match.get_opcodes():
        if tag == "equal":
            out += [(" ", line) for line in old[i1:i2]]
            continue
        added, start = [line for line in new[j1:j2] if not COMMENT.match(line)], 0
        for line in old[i1:i2]:
            index = partner(line, added[start:])
            if index is None:
                out.append(("=", line))
            else:
                out += [("-", line)] + [("+", other) for other in added[start:start + index + 1]]
                start += index + 1
        out += [("+", line) for line in added[start:]]
    return out


def merge(target: str, winner: str) -> str:
    """Write the winner's lines into `target` and keep the lines only `target` has."""
    if extends(target, winner):
        return target
    lines = [line for tag, line in rows(target, winner) if tag != "-"]
    return "\n".join(lines) + "\n" if lines else ""


def own_lines(target: str, winner: str) -> int:
    return sum(tag == "=" and meaningful(line) for tag, line in rows(target, winner))


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


def skill_folders(home: Path) -> dict[tuple[str, Path], set[str]]:
    """(name, real folder) -> the tools that read it, for every skill folder in the tools' skill folders."""
    folders: dict[tuple[str, Path], set[str]] = {}
    for root, tools in SKILL_ROOTS.items():
        for path in sorted((home / root).iterdir()) if (home / root).is_dir() else ():
            if not path.name.startswith(".") and (path / "SKILL.md").is_file():
                folders.setdefault((path.name, path.resolve()), set()).update(tools)
    return folders


def plugin_of(path: Path) -> str:
    """The plugin a skill copy came from, or "" when agent-sync did not copy it from a plugin."""
    if not skills.is_plugin_copy(path):
        return ""
    return read_text(path / skills.MARKER).removeprefix(skills.PLUGIN_MARK).split(":", 1)[0].strip() or "unknown"


def scan_skills(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    for (name, path), tools in skill_folders(home).items():
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


def scan_mcp(home: Path, cfg: Config, locked: dict) -> Iterator[tuple[str, Version]]:
    for tool, filename in MCP_FILES.items():
        for name, server in sorted(mcp.read(home, tool).items()):
            if (value := portable(server)) is not None:
                body = json.dumps(value, indent=2, sort_keys=True)
                yield name, make_version("mcp", name, (tool,), home / filename, body, Origin.SETTINGS, key=name)
    for pid, root in skills.plugins(home).items():
        for path, name, server in plugin_servers(root):
            if (value := portable(server)) is not None:
                body = json.dumps(value, indent=2, sort_keys=True)
                body = body.replace("${CLAUDE_PLUGIN_ROOT}", json.dumps(str(root))[1:-1])
                yield name, make_version("mcp", name, ("claude",), path, body, Origin.PLUGIN,
                                         f"from plugin {pid}", key=name)


def group_tools(group: Group) -> set[str]:
    return {tool for version in group.versions for tool in version.tools}


def likeness(left: str, right: str) -> float:
    """The larger of the texts' match ratio and the share of the shorter one's lines found in the longer."""
    if left == right:
        return 1.0
    short, long = sorted((keys(left), keys(right)), key=len)
    found = set(long)
    contained = sum(line in found for line in short) / len(short) if len(short) >= 3 else 0.0
    match = SequenceMatcher(None, left, right, autojunk=len(left) * len(right) > 16_000_000)
    return max(contained, match.ratio() if match.quick_ratio() > max(contained, 0.6) else 0.0)


def pair_similar(groups: list[Group]) -> None:
    candidates = [group for group in groups if group.kind in {"instructions", "rule"}]
    bodies = {version.id: normalized(version.body) for group in candidates for version in group.versions}

    def similarity(left: Group, right: Group) -> float:
        return max((likeness(bodies[a.id], bodies[b.id]) for a in left.versions for b in right.versions
                    if bodies[a.id] and bodies[b.id]), default=0.0)

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
    return joined([TOOL_LABELS[tool] for tool in tools])


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def skill_marks(version: Version) -> list[tuple[str, str]]:
    front, body = instructions.split_front(version.body)
    return [(tool, label) for tool, label, test in SKILL_MARKS if test(front, body, version.path)]


def tailored(group: Group, installers: Iterable[str] = ()) -> str:
    """Why the versions look made on purpose for each tool (by a plugin or an installer), or "" when they don't."""
    versions = group.versions
    if group.kind not in {"skill", "rule", "instructions"} or len({version.digest for version in versions}) < 2:
        return ""
    times = [version.modified for version in versions]
    together = (max(times) - min(times) <= 5
                and all(version.origin not in {Origin.COPY, Origin.EDITED_COPY} for version in versions))
    uses = []
    if group.kind == "skill":
        marks = {version.id: skill_marks(version) for version in versions}
        for version in versions:
            for tool in version.tools:
                labels = [label for mark, label in marks[version.id] if mark == tool and any(
                    tool not in other.tools and (mark, label) not in marks[other.id] for other in versions)]
                if labels:
                    uses.append(f"{tool_names(version.tools)}'s uses {joined(labels)} "
                                f"only {TOOL_LABELS[tool]} understands")
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


def judge(group: Group, installers: Iterable[str] = ()) -> tuple[str, str]:
    """The group's status and a one-line note: what each tool keeps of its own, or why it was made per tool."""
    note = ""
    if len({version.digest for version in group.versions}) > 1:
        files = {version.id: skill_files(version.path) for version in group.versions} if group.kind == "skill" else {}
        for base in group.versions if group.kind != "mcp" else ():
            extra = {version.id: additions(group.kind, version, base, files) for version in group.versions}
            if None not in extra.values():
                break
        else:
            return ("tailored", reason) if (reason := tailored(group, installers)) else ("differ", "")
        parts = []
        for version in group.versions:
            lines, folders = extra[version.id]
            what = [counted(count, noun) for count, noun in ((lines, "line"), (folders, "file")) if count]
            if what:
                one = len(version.tools) == 1
                parts.append(f"{tool_names(version.tools)} {'keeps' if one else 'keep'} {joined(what)} "
                             f"of {'its' if one else 'their'} own")
        note = "; ".join(parts)
    return ("missing" if group.missing else "same"), note


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
    installers = {pid.split("@", 1)[0] for pid in skills.plugins(home)}
    installers |= {str(meta.get("source") or "").rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
                   for meta in locked.values()}
    installers.discard("")
    for group in groups:
        have = group_tools(group)
        location = KINDS[group.kind].location
        group.missing = tuple(tool for tool in TOOLS if tool not in have and location(home, group.name, tool))
        group.status, group.note = judge(group, installers)
    managed = Counter(meta.get("source") or "Unknown package" for meta in locked.values())
    managed.update(f"{pid} plugin skills" for _, path in skill_folders(home) if (pid := plugin_of(path)))
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
            if version.digest != winner.digest and version.origin != Origin.PLUGIN:
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


def write_text(rewrite: Callable[[Step, str], str], merged: bool = True) -> Callable[[Step, Reader], dict]:
    def write(step: Step, read: Reader) -> dict[Path, tuple[Content, str]]:
        before = read(step.path)
        text = as_text(before) if before is not None else ""
        if text is None:
            raise ValueError(f"not a text file: {step.path}")
        return {step.path: (rewrite(step, text).encode(), step.winner.body if merged else "")}
    return write


def instruction_text(step: Step, text: str) -> str:
    body = step.winner.body
    if "cursor" in step.tools:
        front, rest = instructions.split_front(text)
        if not front:
            return instructions.rendered("cursor", "", merge(rest, body).rstrip("\n"), step.winner.path)
        return front + merge(rest, body)
    block = instructions.BLOCK.search(text)
    own = instructions.BLOCK.sub("\n", text) if block else text
    merged = merge(own, body)
    if merged == own:
        return text
    if block and not set(keys(block[0])) <= set(keys(merged)):
        return merged.rstrip("\n") + "\n\n" + block[0].strip("\n") + "\n"
    return merged


def rule_text(step: Step, text: str) -> str:
    front, rest = instructions.split_front(text)
    if not front and "cursor" in step.tools and step.action == "create":
        name = step.key.split(":", 1)[1]
        front = f"---\ndescription: {name}\nalwaysApply: true\n---\n"
    return front + merge(rest, step.winner.body)


def role_text(step: Step, text: str) -> str:
    header, old, rest = roles.split(text)
    lines = merge("\n".join(old), step.winner.body).splitlines()
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
    if step.marker and not step.path.exists():
        out[step.path / skills.MARKER] = (step.marker.encode(), "")
    for name, data in winner.items():
        path = step.path / name
        if Path(os.path.realpath(path)) != path or (path.exists() and not path.is_file()):
            continue
        before, theirs = read(path), as_text(data)
        mine = as_text(before)
        if before is None:
            out[path] = (data, theirs or "")
        elif mine is not None and theirs is not None:
            out[path] = (merge(mine, theirs).encode(), theirs)
        elif not isinstance(data, Link):
            out[path] = (data, "")
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


KINDS = {
    "instructions": Kind(scan_instructions, lambda home, name, tool: instructions.files(home)[tool],
                         write_text(instruction_text)),
    "rule": Kind(scan_rules, rule_location, write_text(rule_text)),
    "roles": Kind(scan_roles, lambda home, name, tool: None, write_text(role_text)),
    "skill": Kind(scan_skills, lambda home, name, tool: skills.folders(home)[tool] / name, write_skill),
    "mcp": Kind(scan_mcp, lambda home, name, tool: home / MCP_FILES[tool], write_text(mcp_text, merged=False)),
}


def edits(steps: list[Step]) -> list[Edit]:
    """Every file the steps change, with its content before and after; steps on one settings file chain."""
    pending: dict[Path, tuple[Step, Content, str]] = {}

    def read(path: Path) -> Content | None:
        return pending[path][1] if path in pending else current(path)

    for step in steps:
        kind = step.key.split(":", 1)[0]
        for path, (after, winner) in KINDS[kind].write(step, read).items():
            if path in pending and pending[path][0].key != step.key and kind != "mcp":
                raise ValueError(f"more than one selection would write {path}")
            pending[path] = (step, after, winner)
    changes = []
    for path, (step, after, winner) in pending.items():
        before = current(path)
        if after != before:
            changes.append(Edit(step.key, step.tools, step.path, step.action, path, before, after, winner))
    return changes


def apply(home: Path, steps: list[Step], backup_dir: Path) -> list[str]:
    """Write only what changes: stage each file beside its target, back up the originals, then rename into place."""
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
    return [f"{'created' if action == 'create' else 'updated'} {target}" for target, action in targets.items()]


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


def set_plugin_skills(home: Path, ids: list[str]) -> None:
    save_setting(home, "plugin_skills", ids)
