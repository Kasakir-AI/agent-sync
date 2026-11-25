"""Settings, read from ~/.config/agent-sync/config.toml (every key is optional)."""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import registry


@dataclass
class AgentModels:
    """The model each tool's copy of a mirrored agent runs on. Codex can only run its own models natively."""
    claude: str = "opus"
    cursor: str = "claude-opus-5-5-high-fast"
    codex: str = "gpt-6.2-sol"
    codex_effort: str = "high"
    gemini: str = ""


@dataclass
class Config:
    source: str = "claude"                                   # the tool whose setup the others mirror
    agents: list[str] = field(default_factory=lambda: list(registry.DEFAULT_AGENTS))
    skill_packages: list[str] = field(default_factory=lambda: ["backnotprop/pstack"])
    skills_cli: str = "skills@latest"                        # https://www.npmjs.com/package/skills
    remove_dropped_skills: bool = True                       # uninstall package skills the package no longer ships
    agent_models: AgentModels = field(default_factory=AgentModels)
    agent_notes: dict[str, str] = field(default_factory=dict)  # agent name -> text added to the end of its prompt
    mirror_roles: bool = True                                # pstack model roles (pstack-models files)
    mirror_mcp: bool = True                                  # user-level MCP servers
    mirror_skills: bool = True                               # your own skills (not from a package) in the source tool
    skip_skills: list[str] = field(default_factory=list)     # skill names never mirrored
    plugin_skills: list[str] = field(default_factory=list)   # no longer used; a run warns while it is set
    allow_duplicate_skills: list[str] = field(default_factory=list)  # skill names a tool may load more than once
    keep_installed: list[str] = field(default_factory=list)
    keep_mcp_versions: list[str] = field(default_factory=list)  # MCP servers whose pinned package version stays
    mirror_instructions: bool = False                        # global instructions across enabled tools
    mirror_memory: bool = True                               # Claude Code's home-project memory
    private_memory: list[str] = field(default_factory=list)   # memory notes kept in Claude Code
    share_memory: list[str] = field(default_factory=list)     # sensitive notes allowed to be shared
    status_line: bool = True                                 # one status line in every CLI that supports it
    warn_models: list[str] = field(default_factory=list)     # warn when a model name containing one of these appears
    update_clis: bool | list[str] = True                     # true, false, or a list like ["claude", "codex"]
    update_global_tools: bool | list[str] = False
    update_plugins: bool = True                              # plugins and extensions for enabled agents
    update_mcp_programs: bool = True                          # executables used by enabled agents' MCP servers
    cli_commands: dict[str, list[str]] = field(default_factory=dict)   # your own update command per CLI
    install_commands: dict[str, list[str]] = field(default_factory=dict)  # your own install command for setup
    extra_steps: list[dict] = field(default_factory=list)    # [{name = "...", command = ["..."]}] run at the end
    launcher: bool = False                                   # keep "Agent Upkeep" on the Desktop (macOS)
    update_self: bool = True                                 # update agent-sync itself (Homebrew, pipx, git)
    notify: bool = True
    log_days: int = 7
    backup_days: int = 30
    home: Path = field(default_factory=Path.home)

    def wants_cli(self, tool: str) -> bool:
        return tool in self.agents and (
            self.update_clis if isinstance(self.update_clis, bool) else tool in self.update_clis)

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


KEYS = ("source", "agents", "skill_packages", "skills_cli", "remove_dropped_skills", "agent_notes", "mirror_roles",
        "mirror_mcp", "mirror_skills", "skip_skills", "plugin_skills", "allow_duplicate_skills",
        "mirror_instructions", "mirror_memory", "private_memory", "share_memory", "warn_models", "keep_mcp_versions",
        "status_line", "keep_installed",
        "update_clis", "update_global_tools", "update_plugins", "update_mcp_programs", "cli_commands",
        "install_commands", "extra_steps", "launcher", "update_self", "notify", "log_days", "backup_days")


def config_path(home: Path) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") if home == Path.home() else None
    return (Path(xdg) if xdg else home / ".config") / "agent-sync/config.toml"


def load(path: Path | None = None, home: Path | None = None) -> Config:
    home = home or Path.home()
    path = path or config_path(home)
    return parse(path.read_text() if path.exists() else "", path, home)


def save_setting(home: Path, key: str, value) -> None:
    """Write one top-level setting into the config file, keeping everything else in it."""
    from .tomledit import set_value
    path = config_path(home)
    text = path.read_text() if path.exists() else ""
    new = set_value(text, None, key, value)
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)


def parse(text: str, path: Path, home: Path) -> Config:
    """The settings in `text` (the contents of `path`). Raises ValueError (tomllib's errors are ValueErrors too)."""
    cfg = Config(home=home)
    data = tomllib.loads(text)
    unknown = set(data) - set(KEYS) - {"agent_models"}
    if unknown:
        raise ValueError(f"unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
    for key in KEYS:
        if key in data:
            setattr(cfg, key, data[key])
    if not isinstance(cfg.agents, list) or any(
            not isinstance(agent, str) or agent not in registry.TOOLS for agent in cfg.agents):
        raise ValueError(f"agents must be a list of agent ids: {', '.join(registry.TOOLS)}")
    cfg.agents = [agent for agent in registry.TOOLS if agent in cfg.agents]
    if not isinstance(cfg.source, str) or cfg.source not in registry.TOOLS:
        raise ValueError(f"source must be one of {', '.join(registry.TOOLS)}, not {cfg.source!r}")
    if cfg.source not in cfg.agents:
        raise ValueError(f"source {cfg.source!r} must be enabled in agents")
    if not isinstance(cfg.notify, bool):
        raise ValueError("notify must be true or false")
    if type(cfg.backup_days) is not int or cfg.backup_days < 0:
        raise ValueError("backup_days must be a nonnegative whole number")
    if not isinstance(cfg.status_line, bool):
        raise ValueError("status_line must be true or false")
    if not isinstance(cfg.keep_installed, list) or any(not isinstance(key, str) for key in cfg.keep_installed):
        raise ValueError("keep_installed must be a list of removal keys or names")
    for key in ("private_memory", "share_memory"):
        names = getattr(cfg, key)
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError(f"{key} must be a list of memory note names")
    for step in cfg.extra_steps:
        if not isinstance(step.get("command"), list) or not step["command"]:
            raise ValueError(f"extra step {step.get('name', '?')!r} needs command = [\"program\", \"arg\", ...]")
    for key, value in (data.get("agent_models") or {}).items():
        if not hasattr(cfg.agent_models, key):
            raise ValueError(f"unknown agent_models key {key!r}; use claude, cursor, codex, codex_effort or gemini")
        setattr(cfg.agent_models, key, value)
    return cfg
