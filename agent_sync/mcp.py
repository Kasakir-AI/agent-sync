"""Maintain user-level MCP servers in the enabled tools.

- Claude Code: ~/.claude.json, top-level "mcpServers" (user scope)
- Cursor:      ~/.cursor/mcp.json, "mcpServers"
- Codex:       ~/.codex/config.toml, [mcp_servers.<name>] tables
Servers are added when missing. A server that already exists in a target is left alone (it may carry local
approvals or tool settings).
"""
from __future__ import annotations

import json
import re
import tempfile
import tomllib
from pathlib import Path

from . import backups, inventory, pending, tools
from .registry import DEFAULT_AGENTS, TOOLS, grouped_paths, paths
from .tomledit import PART, basic_string, key_text


def json_servers(text: str) -> dict[str, dict]:
    return (json.loads(text).get("mcpServers") or {}) if text.strip() else {}   # an app may leave the file empty


def codex_servers(text: str) -> dict[str, dict]:
    servers = tomllib.loads(text).get("mcp_servers") or {}
    return {name: {key: value for key, value in entry.items() if key in ("command", "args", "env", "url")}
            for name, entry in servers.items() if isinstance(entry, dict)}


def read_gemini(text: str) -> dict[str, dict]:
    # Gemini's `url` is SSE. Only `httpUrl` is portable to every target.
    return {name: {**{key: value for key, value in server.items() if key not in ("url", "httpUrl")},
                   **({"url": server["httpUrl"]} if server.get("httpUrl") else {})}
            for name, server in json_servers(text).items() if isinstance(server, dict)}


def read_antigravity(text: str) -> dict[str, dict]:
    return {name: {**server, "url": server.get("url") or server.get("serverUrl")}
            for name, server in json_servers(text).items() if isinstance(server, dict)}


READERS = {
    "claude-json": json_servers,
    "codex-toml": codex_servers,
    "cursor-json": json_servers,
    "gemini-json": read_gemini,
    "antigravity-json": read_antigravity,
}


def read(home: Path, tool: str) -> dict[str, dict]:
    agent = TOOLS[tool]
    if agent.mcp is None:
        return {}
    path = home / agent.mcp
    return READERS[agent.mcp_format](path.read_text()) if path.exists() else {}


def portable(server: dict) -> dict | None:
    """The part every tool understands: a stdio command (command/args/env) or a remote url."""
    if not isinstance(server, dict):
        return None
    if server.get("command"):
        out = {"command": server["command"], "args": list(server.get("args") or [])}
        if server.get("env"):
            out["env"] = dict(server["env"])
        return out
    for key in ("url", "httpUrl", "serverUrl"):
        if server.get(key):
            return {"url": server[key]}
    return None


def toml_value(v) -> str:
    if isinstance(v, str):
        return basic_string(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    raise TypeError(type(v))


def codex_block(name: str, s: dict) -> str:
    name = key_text(name)
    lines = [f"\n[mcp_servers.{name}]"]
    for key in ("command", "args", "url"):
        if key in s:
            lines.append(f"{key} = {toml_value(s[key])}")
    if s.get("env"):
        lines.append(f"\n[mcp_servers.{name}.env]")
        lines += [f"{basic_string(k)} = {toml_value(str(v))}" for k, v in s["env"].items()]
    return "\n".join(lines) + "\n"


def add_codex(text: str, missing: dict[str, dict]) -> str:
    updated = text + "".join(codex_block(name, server) for name, server in missing.items())
    tomllib.loads(updated)
    return updated


def add_json(text: str, missing: dict[str, dict]) -> str:
    data = json.loads(text) if text.strip() else {}
    data["mcpServers"] = {**(data.get("mcpServers") or {}), **missing}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def gemini_entry(server: dict) -> dict:
    return {"httpUrl" if key == "url" else key: value for key, value in server.items()}


def add_gemini(text: str, missing: dict[str, dict]) -> str:
    return add_json(text, {name: gemini_entry(server) for name, server in missing.items()})


ADDERS = {
    "claude-json": add_json,
    "codex-toml": add_codex,
    "cursor-json": add_json,
    "gemini-json": add_gemini,
    "antigravity-json": add_json,
}


def mirror(home: Path, source: str, dry: bool = False, *, enabled=DEFAULT_AGENTS) -> list[str]:
    enabled = tuple(enabled)
    wanted = {k: p for k, v in read(home, source).items() if (p := portable(v))}
    locations = paths(home, enabled, "mcp")
    loaded = inventory.plugins(home, enabled)
    changes = []
    for path, readers in grouped_paths(locations).items():
        if path == locations.get(source):
            continue
        tool = readers[0]
        have = read(home, tool)
        provided = [(name, inventory.endpoint(server)) for plugin in reader_plugins(loaded, readers)
                    for name, server in plugin.servers]
        missing = {k: v for k, v in wanted.items() if k not in have
                   and not any(k == name or inventory.endpoint(v) == endpoint for name, endpoint in provided)}
        if not missing:
            continue
        if not dry:
            current = path.read_text() if path.exists() else ""
            updated = ADDERS[TOOLS[tool].mcp_format](current, missing)
            path.parent.mkdir(parents=True, exist_ok=True)
            backups.save(path, follow=True)
            path.write_text(updated)
        verb = "would add" if dry else "added"
        changes += [f"{verb} MCP server {name} to {tool}" for name in missing]
    return changes


def reader_plugins(loaded: dict[str, list[inventory.Plugin]], readers: tuple[str, ...]):
    return (plugin for tool in readers for plugin in loaded.get(tool, []))


def tool_names(readers: tuple[str, ...]) -> str:
    return ", ".join(TOOLS[tool].name for tool in readers)


def settings_files(home: Path, enabled):
    """The original text and raw server entries in each settings file."""
    for path, readers in grouped_paths(paths(home, enabled, "mcp")).items():
        if path.is_file():
            text = path.read_bytes().decode()
            codex = TOOLS[readers[0]].mcp_format == "codex-toml"
            entries = tomllib.loads(text).get("mcp_servers", {}) if codex else json_servers(text)
            if isinstance(entries, dict):
                yield path, readers, codex, text, {k: v for k, v in entries.items() if isinstance(v, dict)}


COPY_KEYS = {"command", "args", "env", "url", "httpUrl", "serverUrl", "type", "enabled"}


def dedupe(home: Path, dry: bool, backup_dir: Path | None = None, *, enabled=DEFAULT_AGENTS) -> list[str]:
    from . import writes

    enabled = tuple(enabled)
    loaded = inventory.plugins(home, enabled)
    lines, edits = [], []
    removed = []
    for path, readers, codex, text, entries in settings_files(home, enabled):
        providers = {}
        for plugin in reader_plugins(loaded, readers):
            for server_name, server in plugin.servers:
                if identity := inventory.endpoint(server):
                    providers.setdefault(identity, []).append((plugin.id, server))
                providers.setdefault(("name", server_name), []).append((plugin.id, server))
        updated = text
        for name, server in entries.items():
            key = f"mcp:{readers[0]}:{name}"
            if pending.keeps(key):
                continue
            matches = [*providers.get(inventory.endpoint(server), ()), *providers.get(("name", name), ())]
            if not matches:
                continue
            own = portable(server)
            exact = next((pid for pid, other in matches if own is not None and own == portable(other)), None)
            if (not exact or set(server) - COPY_KEYS or server.get("enabled", True) is not True
                    or (server.get("env") and "env" not in (own or {}))):
                provider = matches[0][0]
                lines.append(f'WARN  {tool_names(readers)} load MCP server {name} twice '
                             f'(settings and plugin {provider}); remove one, or add "{key}" '
                             'to keep_installed to stop this warning')
                continue
            what = f"MCP server {name} from {tool_names(readers)} settings: plugin {exact} brings the same server"
            if not pending.due(key, what) or dry:
                continue
            rewrite = writes.codex_mcp_text if codex else writes.json_mcp_text
            updated = rewrite(updated, name, None)
            lines.append(f"removed {what}")
            removed.append(key)
        if updated != text:
            target = writes.checked_path(home, path)
            edits.append(writes.Edit("mcp", readers, target, "replace", target, text.encode(), updated.encode()))
    writes.apply_edits(home, edits, backup_dir)
    for key in removed:
        pending.removed(key)
    return lines


def same_server_twice(home: Path, *, enabled=DEFAULT_AGENTS) -> list[str]:
    lines = []
    for _, readers, _, _, entries in settings_files(home, enabled):
        names: dict[str, list[str]] = {}
        for name, server in entries.items():
            if identity := inventory.endpoint(server):
                names.setdefault(identity, []).append(name)
        for group in names.values():
            if len(group) > 1:
                lines.append(f"{tool_names(readers)} settings list the same MCP server more than once: "
                             + ", ".join(group))
    return lines


def npm_latest(package: str) -> str:
    return tools.raw(["npm", "view", package, "version"]).strip()


def npm_pins(server: dict):
    command, args = server.get("command"), server.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return
    runner = Path(command).name
    if not (runner in ("npx", "bunx") or (runner == "pnpm" and "dlx" in args)
            or (runner == "npm" and "exec" in args)):
        return
    for index, arg in enumerate(args):
        if isinstance(arg, str) and (spec := inventory.NPM_SPEC.fullmatch(arg)):
            yield index, spec["package"], spec["version"]


PLAIN_VERSION = re.compile(r"\d+\.\d+\.\d+")
JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
TOML_STRING = re.compile(PART)


def pin_update(current: str, latest: str) -> bool:
    if not PLAIN_VERSION.fullmatch(latest):
        return False
    base, sep, _ = current.partition("-")
    old, new = (tuple(map(int, version.split("."))) for version in (base, latest))
    return new > old or (new == old and bool(sep))


def pin_text(text: str, codex: bool, name: str, index: int, old: str, new: str) -> str:
    """Replace the string whose parsed value is the selected server argument."""
    parse = tomllib.loads if codex else json.loads
    expected = parse(text)
    expected["mcp_servers" if codex else "mcpServers"][name]["args"][index] = new
    for match in (TOML_STRING if codex else JSON_STRING).finditer(text):
        token = match.group()
        if token.startswith("'") and codex:
            value, replacement = token[1:-1], f"'{new}'"
        elif token.startswith('"'):
            try:
                value = tomllib.loads(f"arg = {token}")["arg"] if codex else json.loads(token)
            except ValueError:
                continue
            replacement = basic_string(new) if codex else json.dumps(new)
        else:
            continue
        if value == old:
            updated = text[:match.start()] + replacement + text[match.end():]
            try:
                if parse(updated) == expected:
                    return updated
            except ValueError:
                continue
    raise ValueError(f"could not update the pinned MCP argument for {name!r}")


def pins(home: Path, dry: bool, *, enabled=DEFAULT_AGENTS, keep: list[str], latest=npm_latest,
         backup_dir: Path | None = None) -> list[str]:
    from . import writes

    lines, edits, versions = [], [], {}
    for path, readers, codex, text, entries in settings_files(home, enabled):
        updated = text
        for name, server in entries.items():
            if name in keep:
                continue
            for index, package, version in npm_pins(server):
                if package not in versions:
                    versions[package] = latest(package)
                available = versions[package]
                if not pin_update(version, available):
                    continue
                verb = "would update" if dry else "updated"
                lines.append(f"{verb} MCP server {name} in {tool_names(readers)} settings: "
                             f"{package} {version} -> {available}")
                if not dry:
                    updated = pin_text(updated, codex, name, index, f"{package}@{version}", f"{package}@{available}")
        if updated != text:
            target = writes.checked_path(home, path)
            edits.append(writes.Edit("mcp", readers, target, "replace", target, text.encode(), updated.encode()))
    writes.apply_edits(home, edits, backup_dir)
    return lines


def pin_lookup():
    """A cached npm lookup whose npm logs and cache stay in a temporary directory."""
    import os
    from functools import cache

    @cache
    def latest(package: str) -> str:
        with tempfile.TemporaryDirectory(prefix="agent-sync-npm.") as folder:
            previous = os.environ.get("npm_config_cache")
            os.environ["npm_config_cache"] = folder
            try:
                return npm_latest(package)
            finally:
                if previous is None:
                    os.environ.pop("npm_config_cache", None)
                else:
                    os.environ["npm_config_cache"] = previous

    return latest
