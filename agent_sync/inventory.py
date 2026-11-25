"""Plugins and the skills and MCP servers they load."""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from . import duplicates, mcp, skills
from .registry import DEFAULT_AGENTS, inside

MANIFESTS = {
    "codex": ".codex-plugin/plugin.json",
    "claude": ".claude-plugin/plugin.json",
    "cursor": ".cursor-plugin/plugin.json",
    "antigravity": "plugin.json",
    "gemini": "gemini-extension.json",
}
MCP_FILES = (".mcp.json", "mcp.json", "mcp_config.json")
Endpoint = str | tuple[str, tuple[str, ...]]


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
    parts: Counter[str] = field(default_factory=Counter)


def hook_parts(root: Path, manifest: dict, made_for: str | None) -> Iterator[str]:
    extensions = manifest.get("extensions")
    openai = extensions.get("com.openai") if isinstance(extensions, dict) else None
    declared = (openai if made_for == "codex" and isinstance(openai, dict) else manifest).get("hooks")
    sources = list(declared) if isinstance(declared, list) else [declared] if declared is not None else []
    if declared is None or made_for == "claude":
        default = root / "hooks/hooks.json"
        if default.is_file():
            sources.append("hooks/hooks.json")
    for source in sources:
        if isinstance(source, str):
            path = root / source
            if not inside(root, path):
                continue
            events = settings(path).get("hooks")
        else:
            events = source
        if not isinstance(events, dict):
            continue
        for event, groups in events.items():
            for group in groups if isinstance(groups, list) else ():
                actions = group.get("hooks") if isinstance(group, dict) else None
                for action in actions if isinstance(actions, list) else ():
                    if isinstance(action, dict):
                        definition = json.dumps([group.get("matcher", ""), action], sort_keys=True)
                        yield f"hook:{event}:{definition}"


def plugin_parts(root: Path, manifest: dict, made_for: str | None) -> Counter[str]:
    found = {f"command:{path.stem}" for path in root.glob("commands/*.md")}
    found |= {f"agent:{path.stem}" for path in root.glob("agents/*") if path.suffix in (".md", ".toml")}
    parts = Counter(found)
    parts.update(hook_parts(root, manifest, made_for))
    return parts


def family(name: str) -> str:
    return re.sub(r"(?:-plugin|-skills)$", "", name.strip().lower())


def endpoint(server: dict) -> Endpoint | None:
    for key in ("url", "httpUrl", "serverUrl"):
        if isinstance(url := server.get(key), str) and url:
            return url.rstrip("/")
    command, args = server.get("command"), server.get("args")
    args = [] if args is None else args
    if not isinstance(command, str) or not command or not isinstance(args, list):
        return None
    if not all(isinstance(arg, str) for arg in args):
        return None
    normalized = args.copy()
    for index, _, version in mcp.npm_pins(server):
        normalized[index] = normalized[index].removesuffix(f"@{version}")
    return command, tuple(normalized)


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
    if isinstance(declared, str):
        sources[root / declared] = None
    if isinstance(declared, dict):
        sources[manifest_path] = declared
    for path, data in sources.items():
        if not inside(root, path):
            continue
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
    standard = duplicates.walk(root / "skills", within=root) if tool != "claude" else ()
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
                  tuple(file for file, _ in servers.values()), plugin_parts(root, manifest, made_for))


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


def gemini_enabled(rules: object, home: Path) -> bool:
    """Gemini CLI's extension-enablement.json: overrides are path globs, a "!" one switches the extension off there,
    and the last that matches wins. agent-sync works at user level, so it asks about the home folder."""
    location = str(home).replace("\\", "/").rstrip("/") + "/"
    for rule in reversed(rules) if isinstance(rules, list) else ():
        if isinstance(rule, str):
            pattern = rule.removeprefix("!")
            pattern = re.sub(r"(/?)\\\*", r"(?:\1.*)?", re.escape(pattern))
            if re.fullmatch(pattern, location):
                return not rule.startswith("!")
    return True


def gemini_plugins(home: Path) -> Iterator[tuple[str, Path, str, bool]]:
    root = home / ".gemini/extensions"
    enablement = settings(root / "extension-enablement.json")
    for path in sorted(root.glob("*/gemini-extension.json")):
        entry = enablement.get(path.parent.name)
        rules = entry.get("overrides") if isinstance(entry, dict) else None
        yield f"{path.parent.name}@extensions", path.parent, "", gemini_enabled(rules, home)


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
        if (home, tool) not in cache:
            cache[home, tool] = read_plugins(home, (tool,), True)[tool]
        found[tool] = [plugin for plugin in cache[home, tool] if include_disabled or plugin.loads]
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
