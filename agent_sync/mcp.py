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

from .tomledit import basic_string, key_text


def read(home: Path, tool: str) -> dict[str, dict]:
    if tool == "claude":
        p = home / ".claude.json"
        return (json.loads(p.read_text()).get("mcpServers") or {}) if p.exists() else {}
    if tool == "cursor":
        p = home / ".cursor/mcp.json"
        return (json.loads(p.read_text()).get("mcpServers") or {}) if p.exists() else {}
    p = home / ".codex/config.toml"
    if not p.exists():
        return {}
    servers = tomllib.loads(p.read_text()).get("mcp_servers") or {}
    return {name: {key: value for key, value in entry.items() if key in ("command", "args", "env", "url")}
            for name, entry in servers.items() if isinstance(entry, dict)}


def portable(server: dict) -> dict | None:
    """The part every tool understands: a stdio command (command/args/env) or a remote url."""
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


def mirror(home: Path, source: str, dry: bool = False) -> list[str]:
    wanted = {k: p for k, v in read(home, source).items() if (p := portable(v))}
    changes = []
    for tool in ("claude", "codex", "cursor"):
        if tool == source:
            continue
        have = read(home, tool)
        missing = {k: v for k, v in wanted.items() if k not in have}
        if not missing:
            continue
        if dry:
            changes += [f"would add MCP server {n} to {tool}" for n in missing]
            continue
        if tool == "codex":
            p = home / ".codex/config.toml"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                for name, s in missing.items():
                    f.write(codex_block(name, s))
            tomllib.loads(p.read_text())          # fail loudly rather than leave a broken config
        else:
            p = home / (".claude.json" if tool == "claude" else ".cursor/mcp.json")
            data = json.loads(p.read_text()) if p.exists() else {}
            data.setdefault("mcpServers", {}).update(missing)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2) + "\n")
        changes += [f"added MCP server {n} to {tool}" for n in missing]
    return changes
