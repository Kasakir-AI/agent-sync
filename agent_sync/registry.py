"""The commands and user settings paths for supported agents."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    command: str
    skills_agents: tuple[str, ...]
    skills_dir: str
    mcp: str | None
    mcp_format: str
    instructions: str | None
    roles: str | None
    agents_dir: str | None
    agent_format: str
    first_run: str | None
    detect: tuple[str, ...]
    brew: tuple[str, str] | None
    installer: tuple[str, ...]
    updater: tuple[str, ...]

    @property
    def agent_glob(self) -> str | None:
        if self.agents_dir is None:
            return None
        suffix = "toml" if self.agent_format == "codex-toml" else "md"
        return f"{self.agents_dir.format(pkg='*')}/*.{suffix}"


TOOLS: dict[str, Tool] = {
    "claude": Tool(
        id="claude", name="Claude Code", command="claude", skills_agents=("claude-code",),
        skills_dir=".claude/skills", mcp=".claude.json", mcp_format="claude-json",
        instructions=".claude/CLAUDE.md", roles=".claude/rules/pstack-models.md",
        agents_dir=".claude/agents", agent_format="claude-md", first_run=".claude.json",
        detect=(".claude",), brew=("cask", "claude-code"),
        installer=("/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"),
        updater=("claude", "update"),
    ),
    "codex": Tool(
        id="codex", name="Codex", command="codex", skills_agents=("codex",),
        skills_dir=".agents/skills", mcp=".codex/config.toml", mcp_format="codex-toml",
        instructions=".codex/AGENTS.md", roles=".agents/pstack-models.md",
        agents_dir=".codex/agents", agent_format="codex-toml", first_run=".codex",
        detect=(".codex",), brew=("cask", "codex"),
        installer=("/bin/sh", "-c", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
        updater=("codex", "update"),
    ),
    "cursor": Tool(
        id="cursor", name="Cursor", command="cursor-agent", skills_agents=("cursor",),
        skills_dir=".agents/skills", mcp=".cursor/mcp.json", mcp_format="cursor-json",
        instructions=".cursor/rules/agent-sync-instructions.mdc", roles=".cursor/rules/pstack-models.mdc",
        agents_dir=".cursor/plugins/local/{pkg}-agents/agents", agent_format="cursor-plugin",
        first_run=".cursor/cli-config.json", detect=(".cursor",), brew=("cask", "cursor-cli"),
        installer=("/bin/bash", "-c", "curl -fsS https://cursor.com/install | bash"),
        updater=("cursor-agent", "update"),
    ),
}
DEFAULT_AGENTS = ("claude", "codex", "cursor")


def paths(home: Path, agents: Iterable[str], field: str) -> dict[str, Path]:
    return {tool.id: home / path for tool in TOOLS.values()
            if tool.id in agents and (path := getattr(tool, field)) is not None}


def grouped_paths(mapping: dict[str, Path]) -> dict[Path, tuple[str, ...]]:
    groups: dict[Path, tuple[str, ...]] = {}
    for tool, path in mapping.items():
        groups[path] = (*groups.get(path, ()), tool)
    return groups
