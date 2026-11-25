"""Mirror user-level MCP servers from the source tool to the others.

- Claude Code: ~/.claude.json, top-level "mcpServers" (user scope)
- Cursor:      ~/.cursor/mcp.json, "mcpServers"
- Codex:       ~/.codex/config.toml, [mcp_servers.<name>] tables
Servers are added when missing. A server that already exists in a target is left alone (it may carry local
approvals or tool settings), and nothing is ever deleted.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from .registry import DEFAULT_AGENTS, TOOLS, grouped_paths, paths
from .tomledit import basic_string, key_text


def json_servers(text: str) -> dict[str, dict]:
    return json.loads(text).get("mcpServers") or {}


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
    if server.get("url"):
        return {"url": server["url"]}
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
    data = json.loads(text) if text else {}
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
    wanted = {k: p for k, v in read(home, source).items() if (p := portable(v))}
    locations = paths(home, enabled, "mcp")
    changes = []
    for path, readers in grouped_paths(locations).items():
        if path == locations.get(source):
            continue
        tool = readers[0]
        have = read(home, tool)
        missing = {k: v for k, v in wanted.items() if k not in have}
        if not missing:
            continue
        if not dry:
            current = path.read_text() if path.exists() else ""
            updated = ADDERS[TOOLS[tool].mcp_format](current, missing)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(updated)
        verb = "would add" if dry else "added"
        changes += [f"{verb} MCP server {name} to {tool}" for name in missing]
    return changes
