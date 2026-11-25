"""Plugins and the skills and MCP servers they load."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from . import duplicates, mcp, skills
from .registry import DEFAULT_AGENTS, inside

NPM_SPEC = re.compile(r"(?P<package>(?:@[\w.-]+/)?[\w][\w.-]*)@(?P<version>\d+\.\d+\.\d+"
                      r"(?:-[\w.-]+)?)")
MANIFESTS = {
    "codex": ".codex-plugin/plugin.json",
    "claude": ".claude-plugin/plugin.json",
    "cursor": ".cursor-plugin/plugin.json",
    "antigravity": "plugin.json",
    "gemini": "gemini-extension.json",
}
MCP_FILES = (".mcp.json", "mcp.json", "mcp_config.json")


@dataclass(frozen=True)
class Plugin:
    tool: str
    id: str
    name: str
    marketplace: str
    version: str
    root: Path
    family: str
    skill_names: frozenset[str]
    loads: bool
    made_for: str | None
    servers: tuple[tuple[str, dict], ...]
    skills: int
    skill_dirs: tuple[Path, ...] = ()
    server_files: tuple[Path, ...] = ()


def family(name: str) -> str:
    return re.sub(r"(?:-plugin|-skills)$", "", name.strip().lower())


def endpoint(server: dict) -> str:
    for key in ("url", "httpUrl", "serverUrl"):
        if isinstance(url := server.get(key), str) and url:
            return url.rstrip("/")
    command, args = server.get("command"), server.get("args") or []
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return ""
    return " ".join([command, *(spec["package"] if (spec := NPM_SPEC.fullmatch(arg)) else arg for arg in args)])


def object_at(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"expected an object in {path}")
    return data


def settings(path: Path) -> dict:
    try:
        return object_at(path)
    except (OSError, ValueError):
        return {}


def plugin_servers(root: Path, manifest: dict, manifest_path: Path) -> Iterator[tuple[Path, str, dict]]:
    """Each MCP server a plugin brings. A server file that is missing, malformed or outside the plugin is skipped."""
    declared = manifest.get("mcpServers")
    sources = {root / file: None for file in MCP_FILES if (root / file).is_file()}
    if isinstance(declared, str) and inside(root, root / declared):
        sources[root / declared] = None
    if isinstance(declared, dict):
        sources[manifest_path] = declared
    for path, data in sources.items():
        if data is None:
            try:
                data = object_at(path)
            except (OSError, ValueError):
                continue
            data = data.get("mcpServers", data)
        if isinstance(data, dict):
            for name, server in sorted(data.items()):
                if isinstance(server, dict):
                    yield path, name, server


def describe(tool: str, pid: str, root: Path, version: str, loads: bool) -> Plugin:
    name, _, market = pid.partition("@")
    manifests = {owner: root / path for owner, path in MANIFESTS.items() if (root / path).is_file()}
    if tool == "codex" and (root / "plugin.json").is_file() and "codex" not in manifests:
        manifests["codex"] = root / "plugin.json"
    made_for = tool if tool in manifests else next(iter(manifests), None)
    path = manifests.get(made_for, root / MANIFESTS[tool])
    manifest = object_at(path) if made_for else {}
    interface = manifest.get("interface")
    display = interface.get("displayName") if isinstance(interface, dict) else None
    if tool == "codex" and not display and (root / ".app.json").is_file():
        apps = settings(root / ".app.json").get("apps")
        if isinstance(apps, dict):
            display = next(iter(apps), None)
    display = display or manifest.get("name") or name
    standard = duplicates.walk(root / "skills") if tool != "claude" else []
    extra = skills.plugin_skill_dirs(root).values() if made_for == "claude" or tool == "claude" else ()
    dirs = tuple({d.resolve(): d for d in (*extra, *standard)}.values())
    servers = {}
    for file, server_name, server in plugin_servers(root, manifest, path):
        value = mcp.portable(server)
        if value:
            value = json.loads(json.dumps(value).replace("${CLAUDE_PLUGIN_ROOT}", json.dumps(str(root))[1:-1]))
            servers[server_name] = (file, value)
    version = manifest.get("version") or version
    return Plugin(tool, pid, name, market, str(version), root, family(str(display)),
                  frozenset(duplicates.skill_name(d) for d in dirs), loads, made_for,
                  tuple((n, v) for n, (_, v) in servers.items()), len(dirs), dirs,
                  tuple(file for file, _ in servers.values()))


def claude_plugins(home: Path) -> Iterator[tuple[str, Path, str, bool]]:
    for pid, (root, version, loads) in skills.plugin_installs(home).items():
        version = version or (root.name if re.fullmatch(r"\d+(?:\.\d+)+", root.name) else "")
        yield pid, root, version, loads


def cached(root: Path) -> Iterator[tuple[str, Path, Path]]:
    for folder in sorted(root.glob("*/*")):
        if (version := duplicates.newest(folder)) is not None:
            yield f"{folder.name}@{folder.parent.name}", folder, version


def codex_plugins(home: Path) -> Iterator[tuple[str, Path, str, bool]]:
    config = duplicates.codex_config(home).get("plugins") or {}
    if not isinstance(config, dict):
        return
    for pid, folder, version in cached(home / ".codex/plugins/cache"):
        remote = folder / ".codex-remote-plugin-install.json"
        if pid not in config and not remote.is_file():
            continue
        if remote.is_file():
            try:
                object_at(remote)
            except (OSError, ValueError):
                continue
        entry = config.get(pid, True)
        loads = entry.get("enabled", True) if isinstance(entry, dict) else bool(entry)
        yield pid, version, version.name, bool(loads)


def cursor_plugins(home: Path) -> Iterator[tuple[str, Path, str, bool]]:
    root = home / ".cursor/plugins"
    for pid, _, version in cached(root / "cache"):
        yield pid, version, version.name, True
    for folder in sorted((root / "local").glob("*")):
        if folder.is_dir():
            yield f"{folder.name}@local", folder, "", True


def antigravity_plugins(home: Path) -> Iterator[tuple[str, Path, str, bool]]:
    root = home / ".gemini/config"
    entries = settings(root / "config.json").get("plugins")
    for name, entry in sorted(entries.items()) if isinstance(entries, dict) else ():
        if isinstance(entry, dict) and (folder := root / "plugins" / name).is_dir():
            installed = entry.get("installedFrom")
            if isinstance(installed, dict):
                yield (f"{name}@{installed.get('marketplace') or ''}", folder, installed.get("version") or "",
                       entry.get("enabled") is not False)


def gemini_plugins(home: Path) -> Iterator[tuple[str, Path, str, bool]]:
    for path in sorted((home / ".gemini/extensions").glob("*/gemini-extension.json")):
        yield f"{path.parent.name}@extensions", path.parent, "", True


READERS = {
    "claude": claude_plugins, "codex": codex_plugins, "cursor": cursor_plugins,
    "gemini": gemini_plugins, "antigravity": antigravity_plugins,
}


SNAPSHOT: ContextVar[dict | None] = ContextVar("inventory", default=None)


@contextmanager
def snapshot():
    """Read each tool's plugins once while this is open: upkeep opens it after the plugin updates, so every later
    step sees the same plugins without walking them again."""
    token = SNAPSHOT.set({})
    try:
        yield
    finally:
        SNAPSHOT.reset(token)


def plugins(home: Path, enabled=DEFAULT_AGENTS, *, include_disabled: bool = False) -> dict[str, list[Plugin]]:
    cache = SNAPSHOT.get()
    if cache is None:
        return read_plugins(home, enabled, include_disabled)
    found = {}
    for tool in (tool for tool in READERS if tool in enabled):
        key = (home, tool, include_disabled)
        if key not in cache:
            cache[key] = read_plugins(home, (tool,), include_disabled)[tool]
        found[tool] = list(cache[key])
    return found


def read_plugins(home: Path, enabled, include_disabled: bool) -> dict[str, list[Plugin]]:
    result = {tool: [] for tool in READERS if tool in enabled}
    for tool, found in result.items():
        for pid, root, version, loads in READERS[tool](home):
            if loads or include_disabled:
                try:
                    found.append(describe(tool, pid, root, version, loads))
                except (OSError, ValueError, TypeError):
                    continue
    return result
