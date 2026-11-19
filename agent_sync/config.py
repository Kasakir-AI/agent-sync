"""Settings, read from ~/.config/agent-sync/config.toml (every key is optional)."""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TOOLS = ("claude", "codex", "cursor")


@dataclass
class AgentModels:
    """The model each tool's copy of a mirrored agent runs on. Codex can only run its own models natively."""
    claude: str = "opus"
    cursor: str = "claude-opus-5-5-high-fast"
    codex: str = "gpt-6.2-sol"
    codex_effort: str = "high"


@dataclass
class Config:
    source: str = "claude"                                   # the tool whose setup the others mirror
    skill_packages: list[str] = field(default_factory=lambda: ["backnotprop/pstack"])
    skills_cli: str = "skills@latest"                        # https://www.npmjs.com/package/skills
    remove_dropped_skills: bool = True                       # uninstall package skills the package no longer ships
    agent_models: AgentModels = field(default_factory=AgentModels)
    agent_notes: dict[str, str] = field(default_factory=dict)  # agent name -> text added to the end of its prompt
    mirror_roles: bool = True                                # pstack model roles (pstack-models files)
    mirror_mcp: bool = True                                  # user-level MCP servers
    mirror_skills: bool = True                               # your own skills (not from a package) in the source tool
    skip_skills: list[str] = field(default_factory=list)     # skill names never mirrored
    mirror_instructions: bool = False                        # CLAUDE.md <-> AGENTS.md <-> a Cursor rule
    warn_models: list[str] = field(default_factory=list)     # warn when a model name containing one of these appears
    update_clis: bool | list[str] = True                     # true, false, or a list like ["claude", "codex"]
    update_plugins: bool = True                              # plugin marketplaces and plugins of all three tools
    cli_commands: dict[str, list[str]] = field(default_factory=dict)   # your own update command per CLI
    install_commands: dict[str, list[str]] = field(default_factory=dict)  # your own install command for setup
    extra_steps: list[dict] = field(default_factory=list)    # [{name = "...", command = ["..."]}] run at the end
    log_days: int = 7
    home: Path = field(default_factory=Path.home)

    def wants_cli(self, tool: str) -> bool:
        return self.update_clis if isinstance(self.update_clis, bool) else tool in self.update_clis

    @property
    def macos(self) -> bool:
        return sys.platform == "darwin"

    @property
    def log_file(self) -> Path:
        if self.macos:
            return self.home / "Library/Logs/agent-sync.log"
        return self.state_dir / "agent-sync.log"

    @property
    def state_dir(self) -> Path:
        xdg = os.environ.get("XDG_STATE_HOME") if self.home == Path.home() else None
        return Path(xdg) / "agent-sync" if xdg else self.home / ".local/state/agent-sync"


KEYS = ("source", "skill_packages", "skills_cli", "remove_dropped_skills", "agent_notes", "mirror_roles",
        "mirror_mcp", "mirror_skills", "skip_skills", "mirror_instructions", "warn_models", "update_clis",
        "update_plugins", "cli_commands", "install_commands", "extra_steps", "log_days")


def config_path(home: Path) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") if home == Path.home() else None
    return (Path(xdg) if xdg else home / ".config") / "agent-sync/config.toml"


def load(path: Path | None = None, home: Path | None = None) -> Config:
    home = home or Path.home()
    path = path or config_path(home)
    cfg = Config(home=home)
    if not path.exists():
        return cfg
    data = tomllib.loads(path.read_text())
    unknown = set(data) - set(KEYS) - {"agent_models"}
    if unknown:
        raise ValueError(f"unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
    for key in KEYS:
        if key in data:
            setattr(cfg, key, data[key])
    if cfg.source not in TOOLS:
        raise ValueError(f"source must be one of {', '.join(TOOLS)}, not {cfg.source!r}")
    for step in cfg.extra_steps:
        if not isinstance(step.get("command"), list) or not step["command"]:
            raise ValueError(f"extra step {step.get('name', '?')!r} needs command = [\"program\", \"arg\", ...]")
    for key, value in (data.get("agent_models") or {}).items():
        if not hasattr(cfg.agent_models, key):
            raise ValueError(f"unknown agent_models key {key!r}; use claude, cursor, codex or codex_effort")
        setattr(cfg.agent_models, key, value)
    return cfg
