"""`agent-sync setup`: install what agent-sync and the three tools need, when it is missing.

In order: Homebrew (macOS), git, Node (for npx), Claude Code, Codex, Cursor Agent. Each comes from Homebrew when
this account can use it (the same casks agent-sync upgrades later), otherwise from the tool's official per-user
installer, which needs no administrator rights. `install_commands` in the config replaces the command for an item.
Installing Homebrew asks for an administrator's password, so it only happens in an interactive terminal.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import tools

HOMEBREW = ["/bin/bash", "-c", 'curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh | bash']
OFFICIAL = {"claude": ["/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
            "codex": ["/bin/sh", "-c", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"],
            "cursor": ["/bin/bash", "-c", "curl -fsS https://cursor.com/install | bash"]}
CASK = {"claude": "claude-code", "codex": "codex", "cursor": "cursor-cli"}
NAME = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor Agent"}


@dataclass
class Step:
    item: str                    # homebrew, git, node, claude, codex, cursor
    label: str
    command: list[str] | None    # None: nothing to run, see `advice`
    advice: str = ""
    interactive: bool = False    # needs a terminal (password prompts)


def brew_prefix() -> Path | None:
    brew = shutil.which("brew", path=tools.PATH)
    if not brew:
        return None
    out = tools.raw([brew, "--prefix"]).strip()
    return Path(out) if out else Path(os.path.realpath(brew)).parents[1]


def is_admin() -> bool:
    return sys.platform != "darwin" or "admin" in subprocess.run(
        ["id", "-Gn"], capture_output=True, text=True, check=False).stdout.split()


def has_git() -> bool:
    if sys.platform == "darwin":
        # /usr/bin/git is a stub until the Command Line Tools are installed, and running it opens a dialog.
        other = [p for p in tools.PATH.split(os.pathsep) if p != "/usr/bin" and (Path(p) / "git").exists()]
        return bool(other) or subprocess.run(["xcode-select", "-p"], capture_output=True, check=False).returncode == 0
    return bool(shutil.which("git", path=tools.PATH))


def plan(cfg) -> list[Step]:
    """What is missing, and how each missing piece would be installed."""
    steps: list[Step] = []
    own = cfg.install_commands
    prefix = brew_prefix()
    brew_ok = prefix is not None and tools.brew_writable(prefix)
    brew = str(prefix / "bin/brew") if prefix else "brew"
    if sys.platform == "darwin" and prefix is None:
        if "homebrew" in own:
            steps.append(Step("homebrew", "Homebrew (your command)", list(own["homebrew"])))
        elif is_admin():
            steps.append(Step("homebrew", "Homebrew", HOMEBREW, interactive=True))
            brew_ok, brew = True, "/opt/homebrew/bin/brew" if os.uname().machine == "arm64" else "/usr/local/bin/brew"
        else:
            steps.append(Step("homebrew", "Homebrew", None, "Homebrew needs an administrator account; the tools below "
                              "use their own per-user installers instead"))
    elif prefix is not None and not brew_ok:
        steps.append(Step("homebrew", "Homebrew", None, f"Homebrew in {prefix} belongs to another account; the "
                          "tools below use their own per-user installers instead"))

    def add(item: str, label: str, formula_or_cask: list[str] | None, fallback: list[str] | None, advice: str = ""):
        if item in own:
            steps.append(Step(item, f"{label} (your command)", list(own[item])))
        elif brew_ok and formula_or_cask:
            steps.append(Step(item, label, [brew, "install", *formula_or_cask]))
        else:
            steps.append(Step(item, label, fallback, advice))

    if not has_git() and not any(s.item == "homebrew" and s.command for s in steps):
        # (the Homebrew installer brings the Command Line Tools, which include git)
        add("git", "git", ["git"], ["xcode-select", "--install"] if sys.platform == "darwin" else None,
            "install git with your system's package manager")
    if not shutil.which("npx", path=tools.PATH):
        add("node", "Node (for npx)", ["node"], None,
            "install Node from https://nodejs.org or your system's package manager")
    for tool in ("claude", "codex", "cursor"):
        if cfg.wants_cli(tool) and not shutil.which(tools.COMMAND[tool], path=tools.PATH):
            add(tool, NAME[tool], ["--cask", CASK[tool]] if sys.platform == "darwin" else None, OFFICIAL[tool])
    return steps


def run(cfg, dry: bool = False, yes: bool = False) -> int:
    steps = plan(cfg)
    if not steps:
        print("Everything agent-sync needs is installed.")
        return 0
    print("Missing:" if not dry else "Missing (dry run: nothing is installed):")
    for s in steps:
        print(f"  {s.label}: " + (" ".join(s.command) if s.command else s.advice))
    if dry:
        return 0
    runnable = [s for s in steps if s.command]
    if not runnable:
        return 1
    interactive = sys.stdin.isatty()
    if not yes:
        if not interactive:
            print("Run `agent-sync setup` in a terminal to install these, or add --yes.")
            return 1
        if input("Install them now? [y/N] ").strip().lower() != "y":
            print("Nothing was installed.")
            return 0
    problems = 0
    for s in runnable:
        if s.interactive and not interactive:
            print(f"skip  {s.label}: needs a terminal for the administrator password")
            problems += 1
            continue
        print(f"\n== {s.label}")
        env = {**os.environ, "PATH": tools.PATH}
        try:
            code = subprocess.run(s.command, env=env, check=False).returncode
        except OSError as e:
            code, why = 127, str(e)
        else:
            why = f"exit {code}"
        if code:
            print(f"FAIL  {s.label} ({why})")
            problems += 1
        if s.item == "homebrew" and code == 0:
            tools.PATH = os.pathsep.join([*(f"{p}/bin" for p in ("/opt/homebrew", "/usr/local")), tools.PATH])
    still = [s.label for s in plan(cfg) if s.command]
    print()
    if still:
        print("Still missing: " + ", ".join(still))
    else:
        print("Everything agent-sync needs is installed.")
    if any(s.item == "homebrew" for s in runnable):
        print("Note: Homebrew's installer printed how to add it to your shell's PATH; do that once.")
    local_bin = str(Path.home() / ".local/bin")
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep) and (Path(local_bin)).is_dir():
        print(f"Note: add {local_bin} to your shell's PATH to use the tools installed there by name.")
    return 1 if problems or still else 0
