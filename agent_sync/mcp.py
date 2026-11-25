"""Maintain user-level MCP servers in the enabled tools.

- Claude Code: ~/.claude.json, top-level "mcpServers" (user scope)
- Cursor:      ~/.cursor/mcp.json, "mcpServers"
- Codex:       ~/.codex/config.toml, [mcp_servers.<name>] tables
Servers are added when missing. A server that already exists in a target is left alone (it may carry local
approvals or tool settings).
"""
from __future__ import annotations

import functools
import json
import os
import re
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import inventory, pending, tools
from .privacy import secret_values
from .registry import DEFAULT_AGENTS, TOOLS, grouped_paths, paths
from .tomledit import PART, basic_string, key_text


class InvalidSettings(ValueError):
    pass


def server_table(data: dict, key: str) -> dict:
    servers = data.get(key, {})
    if not isinstance(servers, dict):
        raise InvalidSettings(f"{key} must be an object")
    return servers


def json_settings(text: str) -> dict:
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise InvalidSettings("settings must be a JSON object")
    return data


def json_servers(text: str) -> dict[str, dict]:
    return server_table(json_settings(text), "mcpServers")


def codex_entries(text: str) -> dict:
    return server_table(tomllib.loads(text), "mcp_servers")


def codex_servers(text: str) -> dict[str, dict]:
    return {name: {key: value for key, value in entry.items() if key in ("command", "args", "env", "url")}
            for name, entry in codex_entries(text).items() if isinstance(entry, dict)}


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


def settings_problem(path: Path, error: Exception) -> str:
    level = "WARN" if isinstance(error, InvalidSettings) else "FAIL"
    return f"{level}  MCP servers in {path}: {error}"


def portable(server: dict) -> dict | None:
    """The part every tool understands: a stdio command (command/args/env) or a remote url."""
    if not isinstance(server, dict):
        return None
    if command := server.get("command"):
        args, env = server.get("args"), server.get("env")
        args = [] if args is None else args
        if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            return None
        if env is not None and not isinstance(env, dict):
            return None
        out = {"command": command, "args": args.copy()}
        if env:
            out["env"] = env.copy()
        return out
    for key in ("url", "httpUrl", "serverUrl"):
        if isinstance(url := server.get(key), str) and url:
            return {"url": url}
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
    data = json_settings(text)
    data["mcpServers"] = {**server_table(data, "mcpServers"), **missing}
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


def without_secrets(server: dict) -> tuple[dict, list[str]]:
    """The server without the env values that look like credentials, and the names of those variables."""
    env = server.get("env") or {}
    secret = [var for var, value in env.items()
              if secret_values(str(value)) or secret_values(f"{var}={value}")]
    if not secret:
        return server, []
    kept = {var: value for var, value in env.items() if var not in secret}
    return {**{key: value for key, value in server.items() if key != "env"}, **({"env": kept} if kept else {})}, secret


def mirror(home: Path, source: str, dry: bool = False, *, enabled=DEFAULT_AGENTS) -> list[str]:
    from . import writes  # writes builds on this module

    enabled = tuple(enabled)
    try:
        wanted = {k: p for k, v in read(home, source).items() if (p := portable(v))}
    except (OSError, ValueError) as error:
        return [settings_problem(home / TOOLS[source].mcp, error)]
    locations = paths(home, enabled, "mcp")
    loaded = inventory.plugins(home, enabled)
    changes = []
    for path, readers in grouped_paths(locations).items():
        if path == locations.get(source):
            continue
        tool = readers[0]
        servers = [entry for plugin in reader_plugins(loaded, readers) for entry in plugin.servers]
        brought: dict[str, set[inventory.Endpoint | None]] = {}
        for name, server in servers:
            brought.setdefault(name, set()).add(inventory.endpoint(server))
        endpoints = {endpoint for choices in brought.values() for endpoint in choices if endpoint is not None}
        found: list[str] = []

        def add(text: str, tool=tool, readers=readers, brought=brought, endpoints=endpoints, found=found) -> str:
            have = READERS[TOOLS[tool].mcp_format](text)
            found.clear()
            missing = {}
            for name, server in wanted.items():
                if name in have:
                    continue
                endpoint = inventory.endpoint(server)
                if any(other != endpoint for other in brought.get(name, ())):
                    found.append(f"WARN  MCP server {name} not added to {tool_names(readers)}: a plugin there brings "
                                 "a different server of that name. Rename one of them to load both.")
                    continue
                if endpoint in endpoints:
                    continue
                server, unset = without_secrets(server)
                found += [f"WARN  MCP server {name} in {tool_names(readers)} needs {var} set: agent-sync does not "
                          "copy values that look like credentials" for var in unset]
                missing[name] = server
            found += [f"{'would add' if dry else 'added'} MCP server {name} to {tool}" for name in missing]
            return ADDERS[TOOLS[tool].mcp_format](text, missing) if missing else text

        try:
            if dry:
                add(path.read_text() if path.exists() else "")
            else:
                writes.update_text(path, add, new_mode=0o600)
        except writes.Raced as raced:
            changes.append(f"WARN  {raced}")
            continue
        except (OSError, ValueError) as error:
            changes.append(settings_problem(path, error))
            continue
        changes += found
    return changes


def reader_plugins(loaded: dict[str, list[inventory.Plugin]], readers: tuple[str, ...]):
    return (plugin for tool in readers for plugin in loaded.get(tool, []))


def tool_names(readers: tuple[str, ...]) -> str:
    return ", ".join(TOOLS[tool].name for tool in readers)


def groups(home: Path, enabled) -> dict[Path, tuple[str, ...]]:
    return grouped_paths(paths(home, enabled, "mcp"))


def settings_files(home: Path, enabled, failed: list[str] | None = None):
    """The original text and raw server entries in each settings file. A file that can't be read is skipped (and
    named in `failed`, when given)."""
    for path, readers in groups(home, enabled).items():
        try:
            text, codex = path.read_bytes().decode(), TOOLS[readers[0]].mcp_format == "codex-toml"
            entries = codex_entries(text) if codex else json_servers(text)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            if failed is not None:
                failed.append(settings_problem(path, error))
            continue
        yield path, readers, codex, text, {k: v for k, v in entries.items() if isinstance(v, dict)}


COPY_KEYS = {"command", "args", "env", "url", "httpUrl", "serverUrl", "type", "enabled"}


def dedupe(home: Path, dry: bool, *, enabled=DEFAULT_AGENTS, source: str | None = None) -> list[str]:
    """Remove a settings entry a loaded plugin brings exactly, after approval. The source tool's settings are never
    touched: the mirror reads them, so other tools (and ones added later) depend on them."""
    from . import writes

    enabled = tuple(enabled)
    loaded = inventory.plugins(home, enabled)
    lines: list[str] = []
    for path, readers in groups(home, enabled).items():
        if source in readers:
            continue
        codex = TOOLS[readers[0]].mcp_format == "codex-toml"
        owner = f"mcp:{readers[0]}"
        providers = {}
        for plugin in reader_plugins(loaded, readers):
            for server_name, server in plugin.servers:
                if identity := inventory.endpoint(server):
                    providers.setdefault(identity, []).append((plugin.id, server))
                providers.setdefault(("name", server_name), []).append((plugin.id, server))
        found: list[str] = []
        removed: list[str] = []

        def remove(text: str, path=path, readers=readers, codex=codex, owner=owner, providers=providers,
                   found=found, removed=removed) -> str:
            found.clear()
            removed.clear()
            entries = codex_entries(text) if codex else json_servers(text)
            updated = text
            for name, server in entries.items():
                key = f"{owner}:{name}"
                if not isinstance(server, dict) or pending.keeps(key):
                    continue
                matches = [*providers.get(inventory.endpoint(server), ()), *providers.get(("name", name), ())]
                if not matches:
                    continue
                own = portable(server)
                exact = next((pid for pid, other in matches if own is not None and own == portable(other)), None)
                if (not exact or set(server) - COPY_KEYS or server.get("enabled", True) is not True
                        or (server.get("env") and "env" not in (own or {}))):
                    found.append(f'WARN  {tool_names(readers)} load MCP server {name} twice '
                                 f'(settings and plugin {matches[0][0]}); remove one, or add "{key}" '
                                 'to keep_installed to stop this warning')
                    continue
                what = f"MCP server {name} from {tool_names(readers)} settings: plugin {exact} brings the same server"
                if not pending.due(key, what) or dry:
                    continue
                try:
                    updated = (writes.codex_mcp_text if codex else writes.json_mcp_text)(updated, name, None)
                except ValueError:
                    found.append(f"WARN  could not remove MCP server {name} from {path}: its entry is written in a "
                                 "form agent-sync does not edit (remove it by hand)")
                    continue
                found.append(f"removed {what}")
                removed.append(key)
            return updated

        try:
            if dry:
                remove((writes.read_bytes(path) or b"").decode())
            else:
                writes.update_text(path, remove)
        except writes.Raced as raced:
            lines.append(f"WARN  {raced}")
            continue
        except (OSError, ValueError) as error:
            lines.append(settings_problem(path, error))
            continue
        pending.complete(owner)
        lines += found
        for key in removed:
            pending.removed(key)
    return lines


def same_server_twice(home: Path, *, enabled=DEFAULT_AGENTS) -> list[str]:
    lines = []
    for _, readers, _, _, entries in settings_files(home, enabled, lines):
        names: dict[inventory.Endpoint, list[str]] = {}
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


def npm_released(package: str) -> dict[str, str]:
    """When each version of `package` was published (ISO 8601 times)."""
    try:
        times = json.loads(tools.raw(["npm", "view", package, "time", "--json"]) or "{}")
    except ValueError:
        return {}
    return {key: value for key, value in times.items() if isinstance(value, str)} if isinstance(times, dict) else {}


RUNNERS = {"npx": (), "bunx": (), "pnpm": ("dlx",), "npm": ("exec", "x")}
PACKAGE_FLAGS = ("-p", "--package")
NPM_SPEC = re.compile(r"(?P<package>(?:@[\w.-]+/)?[\w][\w.-]*)@(?P<version>\d+\.\d+\.\d+"
                      r"(?:-[\w.-]+)?)")


def npm_pins(server: dict):
    """(argument index, package, version) of each pinned package the runner runs: the operands of -p/--package and
    the first argument that is not a flag. Arguments after that belong to the server."""
    command, args = server.get("command"), server.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return
    runner = Path(command).name
    if runner not in RUNNERS:
        return
    index, subcommand = 0, RUNNERS[runner]
    if subcommand:
        while index < len(args) and isinstance(args[index], str) and args[index].startswith("-"):
            index += 1
        if index >= len(args) or args[index] not in subcommand:
            return
        index += 1
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str) or arg == "--":
            return
        operand = None
        if arg in PACKAGE_FLAGS and index + 1 < len(args):
            index += 1
            operand = args[index]
        elif arg.startswith("--package="):
            operand = arg.partition("=")[2]
        elif not arg.startswith("-"):
            operand = arg
        if isinstance(operand, str) and (spec := NPM_SPEC.fullmatch(operand)):
            yield index, spec["package"], spec["version"]
        if not arg.startswith("-"):
            return
        index += 1


PLAIN_VERSION = re.compile(r"\d+\.\d+\.\d+")
JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
TOML_STRING = re.compile(PART)


def pin_update(current: str, latest: str) -> bool:
    if not PLAIN_VERSION.fullmatch(latest or ""):
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


SETTLE_DAYS = 2   # a release this new may still be pulled; wait before every tool runs it


def settled(released: str | None, now: float) -> bool:
    try:
        stamp = datetime.fromisoformat(released.replace("Z", "+00:00")).timestamp() if released else None
    except ValueError:
        stamp = None
    return stamp is not None and now - stamp >= SETTLE_DAYS * 86400


def pin_server(server: dict) -> dict | None:
    endpoint = inventory.endpoint({key: server[key] for key in ("command", "args") if key in server})
    if not isinstance(endpoint, tuple):
        return None
    normalized = {**server, "args": endpoint[1]}
    if normalized.get("env") in (None, {}):
        normalized.pop("env", None)
    if normalized.get("type") == "stdio":
        normalized.pop("type")
    if normalized.get("enabled") is True:
        normalized.pop("enabled")
    return normalized


def pins(home: Path, dry: bool, *, enabled=DEFAULT_AGENTS, keep: list[str], latest=npm_latest,
         released=npm_released, major: bool = False, now: float | None = None) -> list[str]:
    """Move each pinned npm package of a stdio server to its newest release: not across a major version (unless
    `major`), not to a release under two days old, and not for a server a plugin of that tool brings too."""
    from . import writes

    now = time.time() if now is None else now
    lines: list[str] = []
    loaded = inventory.plugins(home, enabled)
    files = list(settings_files(home, enabled, lines))
    pinned: dict[str, set[str]] = {}
    for _, _, _, _, entries in files:                # every lookup first, before reading a file to change it
        for name, server in entries.items():
            for _, package, version in npm_pins(server):
                if name not in keep:
                    pinned.setdefault(package, set()).add(version)
    wanted: dict[str, tuple[str, set[str]]] = {}     # package -> (the release to move to, the versions that move)
    notes = []
    for package, versions in pinned.items():
        available = latest(package)
        movable = {version for version in versions if pin_update(version, available)}
        same_major = {version for version in movable if version.split(".")[0] == available.split(".")[0]}
        if movable - same_major and not major:
            notes.append(f"info  {package} {available} is a new major version; MCP servers keep "
                         f"{', '.join(sorted(movable - same_major))} (set mcp_major_updates = true to move them)")
        movable = movable if major else same_major
        if movable and not settled(released(package).get(available), now):
            notes.append(f"info  {package} {available} is under {SETTLE_DAYS} days old; MCP servers move to it later")
            continue
        if movable:
            wanted[package] = (available, movable)
    for path, readers, codex, _, _ in files:
        brought = [normalized for plugin in reader_plugins(loaded, readers)
                   for _, server in plugin.servers if (normalized := pin_server(server)) is not None]
        found: list[str] = []

        def bump(text: str, path=path, readers=readers, codex=codex, brought=brought, found=found) -> str:
            found.clear()
            entries = codex_entries(text) if codex else json_servers(text)
            updated = text
            for name, server in entries.items():
                if name in keep or not isinstance(server, dict) or pin_server(server) in brought:
                    continue
                for index, package, version in npm_pins(server):
                    available, movable = wanted.get(package, ("", ()))
                    if version not in movable:
                        continue
                    try:
                        arg = server["args"][index]
                        updated = pin_text(updated, codex, name, index, arg, arg.removesuffix(version) + available)
                    except (ValueError, KeyError, TypeError):
                        found.append(f"WARN  could not update MCP server {name} in {path}: its entry is written in a "
                                     "form agent-sync does not edit")
                        continue
                    found.append(f"{'would update' if dry else 'updated'} MCP server {name} in "
                                 f"{tool_names(readers)} settings: {package} {version} -> {available}")
            return updated

        try:
            if dry:
                bump(path.read_text())
            else:
                writes.update_text(path, bump)
        except writes.Raced as raced:
            lines.append(f"WARN  {raced}")
            continue
        except (OSError, ValueError) as error:
            lines.append(settings_problem(path, error))
            continue
        lines += found
    return lines + list(dict.fromkeys(notes))


class NpmLookup:
    """Cached npm lookups for one run, sharing one temporary npm cache (its logs and downloads stay out of yours)."""

    def __init__(self):
        self.folder = tempfile.TemporaryDirectory(prefix="agent-sync-npm.")
        self.latest = functools.cache(lambda package: self.view(npm_latest, package))
        self.released = functools.cache(lambda package: self.view(npm_released, package))

    def view(self, lookup, package: str):
        previous = os.environ.get("npm_config_cache")
        os.environ["npm_config_cache"] = self.folder.name
        try:
            return lookup(package)
        finally:
            if previous is None:
                os.environ.pop("npm_config_cache", None)
            else:
                os.environ["npm_config_cache"] = previous
