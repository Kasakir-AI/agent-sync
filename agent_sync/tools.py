"""Shell-outs: skill packages (npx skills), the package checkout, and the three agent CLIs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from . import __version__

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[─-◿]")
# Where the CLIs usually live, added after your own PATH (a schedule starts with a bare PATH).
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin", str(Path.home() / ".local/bin"),
              "/usr/bin", "/bin"]
PATH = os.pathsep.join(d for d in dict.fromkeys(os.environ.get("PATH", "").split(os.pathsep) + EXTRA_PATH) if d)


def sh(args: list[str], timeout: int = 900, cwd: Path | None = None) -> tuple[int, str]:
    env = {**os.environ, "PATH": PATH, "HOMEBREW_NO_ENV_HINTS": "1",
           "HOMEBREW_NO_AUTO_UPDATE": "1"}
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    out = ANSI.sub("", (p.stdout or "") + (p.stderr or ""))
    tail = " ".join(l.strip() for l in out.splitlines() if l.strip() and not l.startswith("npm notice"))[-200:]
    return p.returncode, tail


def raw(args: list[str], timeout: int = 120) -> str:
    """A command's standard output ("" if it fails), for listings that are parsed."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PATH": PATH})
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


def checkout(package: str, dest: Path) -> tuple[int, str]:
    """A shallow clone of a GitHub skill package (owner/repo), refreshed every run."""
    if (dest / ".git").is_dir():
        code, out = sh(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", "origin"])
        return (code, out) if code else sh(["git", "-C", str(dest), "reset", "-q", "--hard", "origin/HEAD"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    return sh(["git", "clone", "-q", "--depth", "1", f"https://github.com/{package}.git", str(dest)])


def skills(cli: str, *args: str) -> tuple[int, str]:
    return sh(["npx", "-y", "--prefer-online", cli, *args])


def skill_args(cli: str, package: str) -> list[str]:
    """Install or update every skill of `package` for Claude Code, Codex and Cursor (one shared copy)."""
    return ["npx", "-y", "--prefer-online", cli, "add", package, "-g", "-s", "*",
            "-a", "claude-code", "codex", "cursor", "-y"]


# A CLI writes its own settings the first time it starts (Claude Code ~/.claude.json, Cursor Agent
# ~/.cursor/cli-config.json), so a dry run or `status` never starts one that has not run in this account yet.
FIRST_RUN_FILES = {"claude": ".claude.json", "cursor-agent": ".cursor/cli-config.json", "codex": ".codex"}


def started(cmd: str, home: Path) -> bool:
    return (home / FIRST_RUN_FILES[cmd]).exists() if cmd in FIRST_RUN_FILES else True


def version(cmd: str, home: Path | None = None) -> str:
    """The CLI's version; with `home`, without starting a CLI that never ran there."""
    if not shutil.which(cmd, path=PATH):
        return "not installed"
    if home is not None and not started(cmd, home):
        return "installed, never started in this account"
    return sh([cmd, "--version"], timeout=60)[1] or "?"


def homebrew_prefix(cmd: str, cask: str) -> Path | None:
    """The Homebrew prefix when `cmd` is that Homebrew cask's copy (…/Caskroom/<cask>/…), else None."""
    found = shutil.which(cmd, path=PATH)
    real = Path(os.path.realpath(found)) if found else None
    if real and f"/Caskroom/{cask}/" in str(real):
        return Path(str(real).split("/Caskroom/")[0])
    return None


def brew_writable(prefix: Path) -> bool:
    """Homebrew can only update from the account that owns it."""
    return all(os.access(prefix / d, os.W_OK) for d in ("Caskroom", "bin") if (prefix / d).exists())


def act(log, dry: bool, label: str, args: list[str], timeout: int = 900) -> None:
    """Run a command that changes something, or with dry=True only say what would run."""
    if dry:
        log("dry", f"would run: {' '.join(args)}  ({label})")
        return
    code, out = sh(args, timeout)
    log("ok" if code == 0 else "FAIL", f"{label}{': ' + out if code else ''}")


DEFAULT_CLIS = (("claude", "claude-code", ["claude", "update"]),
                ("cursor", "cursor-cli", ["cursor-agent", "update"]),
                ("codex", "codex", ["codex", "update"]))
COMMAND = {"claude": "claude", "cursor": "cursor-agent", "codex": "codex"}


def update_clis(log, cfg, dry: bool = False) -> None:
    """Each CLI: its Homebrew cask when installed that way, otherwise its own updater (`claude update`,
    `cursor-agent update`, `codex update`). `cli_commands` in the config replaces the command for a tool."""
    brew = shutil.which("brew", path=PATH)
    brew_updated = False
    for tool, cask, own in DEFAULT_CLIS:
        if not cfg.wants_cli(tool):
            continue
        cmd = COMMAND[tool]
        if not shutil.which(cmd, path=PATH):
            log("skip", f"{cmd} not installed (`agent-sync setup` installs it)")
            continue
        prefix = homebrew_prefix(cmd, cask) if cask else None
        if dry and not started(cmd, cfg.home):
            log("dry", f"{cmd} has never been started in this account; a real run would update it")
            continue
        before = version(cmd)
        if tool in cfg.cli_commands:
            act(log, dry, f"{cmd} (your command)", list(cfg.cli_commands[tool]))
        elif prefix and not brew_writable(prefix):
            log("skip", f"{cmd} comes from Homebrew in {prefix}, which another account owns; "
                        "update it from that account")
            continue
        elif prefix:
            if brew and not brew_updated and not dry:
                code, out = sh(["brew", "update", "-q"])
                log("ok" if code == 0 else "FAIL", f"brew update{': ' + out if code else ''}")
                brew_updated = True
            outdated = sh(["brew", "outdated", "--cask", "--greedy", cask], timeout=300)[1]
            if cask in outdated:
                act(log, dry, f"{cmd} upgrade", ["brew", "upgrade", "--cask", cask])
            else:
                log("ok", f"{cmd} is current (Homebrew)")
        else:
            act(log, dry, f"{cmd} update", own)
        if not dry:
            log("info", f"{cmd}: {before} -> {version(cmd)}")


def how_installed(prefix: str | None = None) -> tuple[str, str]:
    """How this copy of agent-sync was installed: ("homebrew", brew prefix), ("pipx", ""), ("git", repo URL),
    or ("other", why it can't update itself)."""
    real = os.path.realpath(prefix or sys.prefix)
    if "/Cellar/agent-sync/" in real + "/":
        return "homebrew", real.split("/Cellar/")[0]
    if "/pipx/venvs/agent-sync" in real:
        return "pipx", ""
    try:
        direct = json.loads(metadata.distribution("agent-sync").read_text("direct_url.json") or "{}")
    except (metadata.PackageNotFoundError, ValueError):
        direct = {}
    if direct.get("vcs_info", {}).get("vcs") == "git" and direct.get("url"):
        return "git", direct["url"]
    return "other", "it runs from a source checkout or a plain pip install"


def update_self(log, dry: bool = False, home: Path | None = None) -> bool:
    """Update agent-sync the way it was installed. Returns True when a new version was installed."""
    kind, where = how_installed()
    before = __version__
    if kind == "homebrew":
        if not brew_writable(Path(where)):
            log("skip", f"agent-sync comes from Homebrew in {where}, which another account owns; update it there")
            return False
        if not dry:
            code, out = sh(["brew", "update", "-q"])
            log("ok" if code == 0 else "FAIL", f"brew update{': ' + out if code else ''}")
        if "agent-sync" not in sh(["brew", "outdated", "--formula", "agent-sync"], timeout=300)[1]:
            log("ok", f"agent-sync {before} is current (Homebrew)")
            return False
        args = ["brew", "upgrade", "--formula", "agent-sync"]
    elif kind == "pipx":
        args = ["pipx", "upgrade", "agent-sync"]
    elif kind == "git":
        args = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", f"git+{where}"]
    else:
        log("skip", f"agent-sync can't update itself: {where}")
        return False
    act(log, dry, "agent-sync upgrade", args)
    if dry:
        return False
    after = sh([shutil.which("agent-sync", path=PATH) or "agent-sync", "--version"], timeout=60)[1]
    if after and after.split()[-1] != before:
        log("info", f"agent-sync: {before} -> {after.split()[-1]}")
        return True
    return False


def update_plugins(log, dry: bool = False, home: Path | None = None) -> None:
    """Claude Code: refresh every marketplace, then update each user-installed plugin.
    Codex: refresh the Git plugin marketplaces you added; its installed plugins follow them.
    Cursor: refresh the plugin marketplaces you added (Cursor manages its built-in ones itself).
    A dry run skips a CLI that never started in this account (it has no plugins yet)."""
    home = home or Path.home()

    def usable(cmd: str) -> bool:
        return bool(shutil.which(cmd, path=PATH)) and (started(cmd, home) or not dry)

    if usable("claude"):
        act(log, dry, "Claude Code plugin marketplaces", ["claude", "plugin", "marketplace", "update"])
        try:
            plugins = json.loads(raw(["claude", "plugin", "list", "--json"]) or "[]")
        except ValueError:
            plugins = []
        for plugin in plugins:
            if plugin.get("scope") in ("user", "project") and "@" in plugin.get("id", ""):
                act(log, dry, f"Claude Code plugin {plugin['id']}", ["claude", "plugin", "update", plugin["id"]])
    if usable("codex"):
        act(log, dry, "Codex plugin marketplaces", ["codex", "plugin", "marketplace", "upgrade"])
    if usable("cursor-agent"):
        for line in raw(["cursor-agent", "plugin", "marketplace", "list"]).splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "user" and parts[2].startswith(("https://", "git@")):
                act(log, dry, f"Cursor plugin marketplace {parts[0]}",
                    ["cursor-agent", "plugin", "marketplace", "update", parts[0]])


def extra_steps(log, steps: list[dict], dry: bool = False) -> None:
    """Your own commands from the config, run in order at the end."""
    for step in steps:
        act(log, dry, step.get("name") or step["command"][0], list(step["command"]),
            timeout=int(step.get("timeout", 900)))
