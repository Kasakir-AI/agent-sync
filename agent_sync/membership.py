"""Choose which installed or supported agents agent-sync keeps in step."""
from __future__ import annotations

import sys
from collections.abc import Callable, Iterable

from . import tools
from .config import Config, save_setting
from .registry import TOOLS


def available(cfg: Config) -> dict[str, bool]:
    return {agent.id: tools.installed(agent, cfg.home) for agent in TOOLS.values()}


def untracked(cfg: Config, installed: dict[str, bool]) -> list[str]:
    return [agent for agent, present in installed.items() if present and agent not in cfg.agents]


def changed(cfg: Config, action: str, ids: Iterable[str]) -> list[str]:
    requested = set(ids)
    unknown = requested - TOOLS.keys()
    if unknown:
        raise ValueError(f"Unknown agent id: {', '.join(sorted(unknown))}. Use {', '.join(TOOLS)}.")
    enabled = set(cfg.agents)
    if action == "add":
        enabled.update(requested)
    else:
        if cfg.source in requested:
            raise ValueError(f"{TOOLS[cfg.source].name} is your source. Change source before removing it.")
        enabled.difference_update(requested)
    return [agent for agent in TOOLS if agent in enabled]


def describe(cfg: Config, enabled: list[str], installed: dict[str, bool], say: Callable[[str], None]) -> None:
    for agent in enabled:
        if agent not in cfg.agents:
            say(f"Keeping {TOOLS[agent].name} in step.")
            if not installed[agent]:
                say(f"Run `agent-sync setup` to install {TOOLS[agent].name}.")
    for agent in cfg.agents:
        if agent not in enabled:
            say(f"agent-sync stops updating {TOOLS[agent].name} and leaves its files in place.")


def save(cfg: Config, enabled: list[str], installed: dict[str, bool]) -> None:
    if enabled != cfg.agents:
        save_setting(cfg.home, "agents", enabled)
        describe(cfg, enabled, installed, print)
        cfg.agents = enabled


def offer(cfg: Config, installed: dict[str, bool]) -> None:
    if not sys.stdin.isatty():
        return
    accepted = []
    for agent in untracked(cfg, installed):
        while True:
            try:
                answer = input(f"{TOOLS[agent].name} is installed. Keep it in step? [Y/n] ").strip().lower()
            except EOFError:
                save(cfg, changed(cfg, "add", accepted), installed)
                return
            if answer in ("", "y", "yes"):
                accepted.append(agent)
                break
            if answer in ("n", "no"):
                break
    save(cfg, changed(cfg, "add", accepted), installed)


def run(cfg: Config, action: str | None, ids: list[str]) -> int:
    installed = available(cfg)
    if action:
        try:
            enabled = changed(cfg, action, ids)
        except ValueError as err:
            print(err, file=sys.stderr)
            return 2
        if enabled == cfg.agents:
            print("The enabled agents are already set.")
        save(cfg, enabled, installed)
    else:
        for agent, present in installed.items():
            state = "in step" if agent in cfg.agents else "outside sync"
            source = ", source" if agent == cfg.source else ""
            print(f"{TOOLS[agent].name} ({agent})  {'installed' if present else 'not installed'}, {state}{source}")
        offer(cfg, installed)
    return 0
