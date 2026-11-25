"""Shell-outs: skill packages (npx skills), the package checkout, and agent CLIs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import cache
from importlib import metadata
from pathlib import Path
from stat import S_ISREG, S_ISUID

from . import __version__, backups, mcp, registry
from .registry import DEFAULT_AGENTS, TOOLS, Tool

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


def raw(args: list[str], timeout: int = 120, *, ok_codes: tuple[int, ...] = (0,)) -> str:
    """A command's standard output when its exit code is accepted."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PATH": PATH, "HOMEBREW_NO_AUTO_UPDATE": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode in ok_codes else ""


def checkout(package: str, dest: Path) -> tuple[int, str]:
    """A shallow clone of a GitHub skill package (owner/repo), refreshed every run."""
    if (dest / ".git").is_dir():
        code, out = sh(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", "origin"])
        return (code, out) if code else sh(["git", "-C", str(dest), "reset", "-q", "--hard", "origin/HEAD"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    return sh(["git", "clone", "-q", "--depth", "1", f"https://github.com/{package}.git", str(dest)])


def skills(cli: str, *args: str) -> tuple[int, str]:
    return sh(["npx", "-y", "--prefer-online", cli, *args])


def skill_args(cli: str, package: str, names: list[str] | None = None,
               *, enabled=DEFAULT_AGENTS) -> list[str]:
    """Install or update package skills for the enabled agents."""
    return ["npx", "-y", "--prefer-online", cli, "add", package, "-g", "-s", *(names or ["*"]),
            "-a", *skill_agents(enabled), "-y"]


def skill_agents(enabled) -> list[str]:
    return list(dict.fromkeys(agent for tool in TOOLS.values() if tool.id in enabled
                              for agent in tool.skills_agents))


def remove_args(cli: str, name: str, *, enabled=DEFAULT_AGENTS) -> list[str]:
    """Remove one package skill from the enabled agents."""
    return ["npx", "-y", "--prefer-online", cli, "remove", "-g", "-s", name, "-a", *skill_agents(enabled), "-y"]


# A CLI writes its own settings the first time it starts (Claude Code ~/.claude.json, Cursor Agent
# ~/.cursor/cli-config.json), so a dry run or `status` never starts one that has not run in this account yet.
def installed(tool: Tool, home: Path) -> bool:
    return bool(shutil.which(tool.command, path=PATH)) or any((home / path).exists() for path in tool.detect)


def started(cmd: str, home: Path) -> bool:
    first_run = next((tool.first_run for tool in TOOLS.values() if tool.command == cmd), None)
    return first_run is None or (home / first_run).exists()


def version(cmd: str, home: Path | None = None) -> str:
    """The CLI's version; with `home`, without starting a CLI that never ran there."""
    if not shutil.which(cmd, path=PATH):
        return "not installed"
    if home is not None and not started(cmd, home):
        return "installed, never started in this account"
    return sh([cmd, "--version"], timeout=60)[1] or "?"


def homebrew_prefix(cmd: str, name: str, kind: str = "cask") -> Path | None:
    """The Homebrew prefix when it owns this copy of the command."""
    found = shutil.which(cmd, path=PATH)
    real = Path(os.path.realpath(found)) if found else None
    package = brew_package(real) if real else None
    return package[2] if package and package[:2] == (kind, name) else None


def brew_package(real: Path) -> tuple[str, str, Path] | None:
    """The Homebrew kind, package and prefix of a resolved executable."""
    for room, kind in (("Cellar", "formula"), ("Caskroom", "cask")):
        prefix, sep, tail = str(real).partition(f"/{room}/")
        if sep and "/" in tail:
            return kind, tail.split("/", 1)[0], Path(prefix or "/")
    return None


def brew_writable(prefix: Path) -> bool:
    """Homebrew can only update from the account that owns it."""
    return all(os.access(prefix / d, os.W_OK) for d in ("Caskroom", "Cellar", "bin") if (prefix / d).exists())


def act(log, dry: bool, label: str, args: list[str], timeout: int = 900, *, run=None) -> bool:
    """Run a command that changes something, or with dry=True only say what would run."""
    if dry:
        log("dry", f"would run: {' '.join(args)}  ({label})")
        return False
    code, out = sh(args, timeout) if run is None else run(args)
    log("ran" if code == 0 else "FAIL", f"{label}{': ' + out if code else ''}")
    return code == 0


def update_skills(log, dry: bool, cli: str, *, home: Path, enabled=DEFAULT_AGENTS) -> None:
    """Update globally installed skills when the lock lists any."""
    from . import skills as installed_skills

    if not (locked := installed_skills.locked(home)):
        return
    if not dry:
        installed_skills.backup_installs(home, enabled, locked)
    act(log, dry, "skills from every source (skills update)",
        ["npx", "-y", "--prefer-online", cli, "update", "-g", "-y"])


@dataclass
class UpdateState:
    brew_updated: bool = False
    npm_outdated: set[str] | None = None
    npm_upgraded: set[str] = field(default_factory=set)


GLOBAL_MANAGERS = ("npm", "pipx", "uv", "homebrew")


def global_managers(setting: bool | list[str]) -> tuple[str, ...]:
    if isinstance(setting, bool):
        return GLOBAL_MANAGERS[:-1] if setting else ()
    return tuple(manager for manager in GLOBAL_MANAGERS if manager in setting)


def update_global_tools(log, cfg, dry: bool, *, state: UpdateState) -> None:
    for manager in global_managers(cfg.update_global_tools):
        cmd = "brew" if manager == "homebrew" else manager
        if not shutil.which(cmd, path=PATH):
            continue
        label = f"global {manager} tools"
        if manager == "npm":
            if dry:
                act(log, dry, label, ["npm", "outdated", "-g", "--json"])
                continue
            for package in sorted(outdated_npm(state) - state.npm_upgraded):
                upgrade_npm(log, dry, package, state, label=f"{label} ({package})")
        elif manager == "homebrew":
            refresh_brew(log, dry, state)
            act(log, dry, label, ["brew", "upgrade"])
        else:
            args = ["pipx", "upgrade-all"] if manager == "pipx" else ["uv", "tool", "upgrade", "--all"]
            act(log, dry, label, args)


def refresh_brew(log, dry: bool, state: UpdateState, *, quiet: bool = False) -> None:
    if dry or state.brew_updated:
        return
    state.brew_updated = True

    def report(kind: str, message: str) -> None:
        if kind == "FAIL" or not quiet:
            log("ok" if kind == "ran" else kind, message)

    act(report, dry, "brew update", ["brew", "update", "-q"])


def json_object(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def outdated_npm(state: UpdateState) -> set[str]:
    if state.npm_outdated is None:
        state.npm_outdated = set(json_object(raw(["npm", "outdated", "-g", "--json"], ok_codes=(0, 1))))
    return state.npm_outdated


def upgrade_npm(log, dry: bool, package: str, state: UpdateState, *, label: str = "") -> None:
    if package not in state.npm_upgraded and act(
            log, dry, label or f"{package} upgrade (npm)", ["npm", "install", "-g", f"{package}@latest"]):
        state.npm_upgraded.add(package)


def update_clis(log, cfg, dry: bool = False, *, state: UpdateState | None = None) -> None:
    """Each CLI: its Homebrew cask when installed that way, otherwise its own updater (`claude update`,
    `cursor-agent update`, `codex update`). `cli_commands` in the config replaces the command for a tool."""
    brew = shutil.which("brew", path=PATH)
    state = state or UpdateState()
    for agent in TOOLS.values():
        tool, cmd = agent.id, agent.command
        if not cfg.wants_cli(tool):
            continue
        if not shutil.which(cmd, path=PATH):
            log("skip", f"{cmd} not installed (`agent-sync setup` installs it)")
            continue
        kind, name = agent.brew or ("", "")
        prefix = homebrew_prefix(cmd, name, kind) if agent.brew else None
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
            if brew:
                refresh_brew(log, dry, state)
            greedy = ["--greedy"] if kind == "cask" else []
            outdated = sh(["brew", "outdated", f"--{kind}", *greedy, name], timeout=300)[1]
            if name in outdated:
                act(log, dry, f"{cmd} upgrade", ["brew", "upgrade", f"--{kind}", name])
            else:
                log("ok", f"{cmd} is current (Homebrew)")
        else:
            act(log, dry, f"{cmd} update", list(agent.updater))
        if not dry:
            log("info", f"{cmd}: {before} -> {version(cmd)}")


PREREQUISITES = ("git", "node")


def update_prerequisites(log, dry: bool = False, *, state: UpdateState | None = None) -> None:
    """Update Homebrew git and Node, and npm if installed as a global package."""
    state = state or UpdateState()
    brew = shutil.which("brew", path=PATH)
    if brew:
        prefix = Path(os.path.realpath(brew)).parents[1]
        if brew_writable(prefix):
            refresh_brew(log, dry, state, quiet=True)
            outdated = set(raw(["brew", "outdated", "--formula", "--quiet"], timeout=300).split())
            for formula in PREREQUISITES:
                if formula in outdated:
                    act(log, dry, f"{formula} upgrade (Homebrew)", ["brew", "upgrade", "--formula", formula])
        else:
            log("skip", f"git and Node come from Homebrew in {prefix}, which another account owns; update them there")
    if shutil.which("npm", path=PATH) and "npm" in outdated_npm(state):
        upgrade_npm(log, dry, "npm", state)


MCP_RUNNERS = {"npx", "npm", "pnpm", "bunx", "bun", "uvx", "uv", "node", "python", "python3", "deno", "docker"}


def mcp_commands(cfg):
    """Yield the name and resolved executable of each stdio server."""
    skip = MCP_RUNNERS | {tool.command for tool in TOOLS.values()}
    for readers in registry.grouped_paths(registry.paths(cfg.home, cfg.agents, "mcp")).values():
        try:
            servers = mcp.read(cfg.home, readers[0])
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(servers, dict):
            continue
        for name, server in servers.items():
            cmd = server.get("command") if isinstance(server, dict) and not server.get("url") else None
            if not isinstance(cmd, str) or not cmd or Path(cmd).name in skip:
                continue
            found = shutil.which(cmd, path=PATH)
            if not found:
                continue
            executable, real = Path(found), Path(os.path.realpath(found))
            if real.name in skip or any(part.lower().endswith(".app")
                                        for part in (*executable.parts[:-1], *real.parts[:-1])):
                continue
            yield name, executable, real


def package_under(real: Path, root: Path | None, manager: str) -> tuple[str, str, None] | None:
    if root is None or not real.is_relative_to(root):
        return None
    parts = real.relative_to(root).parts
    size = 2 if manager == "npm" and parts and parts[0].startswith("@") else 1
    return (manager, "/".join(parts[:size]), None) if len(parts) > size else None


def can_self_update(real: Path, home: Path) -> bool:
    if not any(real.is_relative_to(root) for root in (home.resolve(), Path("/usr/local"), Path("/opt"))):
        return False
    try:
        stat = real.stat()
    except OSError:
        return False
    return S_ISREG(stat.st_mode) and not (stat.st_mode & S_ISUID) and stat.st_uid in (0, os.getuid())


def update_own_program(log, dry: bool, name: str, cmd: Path) -> None:
    help_text = raw([str(cmd), "--help"], timeout=30)
    match = re.search(rf"^\s*{re.escape(cmd.name)} ((?:self-)?update)\b", help_text, re.MULTILINE)
    if match:
        if not dry:
            backups.save(cmd)
        act(log, dry, f"{cmd.name} update (MCP server {name})", [str(cmd), match[1]])


def update_mcp_programs(log, cfg, dry: bool, *, state: UpdateState | None = None) -> None:
    """Update installed executables used by enabled agents' stdio servers."""
    state = state or UpdateState()
    pipx = (Path(os.environ.get("PIPX_HOME") or cfg.home / ".local/pipx") / "venvs").resolve()
    uv = (cfg.home / ".local/share/uv/tools").resolve()

    @cache
    def npm_root() -> Path | None:
        if not shutil.which("npm", path=PATH):
            return None
        root = raw(["npm", "root", "-g"]).strip()
        return Path(os.path.realpath(root)) if root else None

    @cache
    def brew_outdated(kind: str) -> dict[str, str]:
        greedy = ["--greedy"] if kind == "cask" else []
        names = raw(["brew", "outdated", f"--{kind}", *greedy, "--quiet"], timeout=300).split()
        return {name.rsplit("/", 1)[-1]: name for name in names}

    package_resolvers = (
        brew_package,
        lambda real: package_under(real, npm_root(), "npm"),
        lambda real: package_under(real, pipx, "pipx"),
        lambda real: package_under(real, uv, "uv"),
    )
    seen_paths: set[Path] = set()
    seen_packages: set[tuple[str, str]] = set()
    for name, cmd, real in mcp_commands(cfg):
        if real in seen_paths:
            continue
        seen_paths.add(real)
        managed = next((match for resolve in package_resolvers if (match := resolve(real))), None)
        if managed is None:
            if can_self_update(real, cfg.home):
                update_own_program(log, dry, name, real)
            continue
        kind, package, prefix = managed
        key = (kind, package)
        if key in seen_packages:
            continue
        seen_packages.add(key)
        if prefix:
            if not brew_writable(prefix):
                log("skip", f"{cmd.name} comes from Homebrew in {prefix}, which another account owns; "
                            "update it from that account")
                continue
            refresh_brew(log, dry, state, quiet=True)
            if full_name := brew_outdated(kind).get(package):
                act(log, dry, f"{package} upgrade (Homebrew, for MCP server {name})",
                    ["brew", "upgrade", f"--{kind}", full_name])
        elif kind == "npm":
            if package in outdated_npm(state):
                upgrade_npm(log, dry, package, state)
        else:
            upgrade = ["uv", "tool", "upgrade"] if kind == "uv" else ["pipx", "upgrade"]
            act(log, dry, f"{package} upgrade ({kind}, for MCP server {name})", [*upgrade, package])


def how_installed(prefix: str | None = None) -> tuple[str, str]:
    """How this copy of agent-sync was installed: ("homebrew", brew prefix), ("pipx", repo URL when pipx installed
    it from git, else ""), ("git", repo URL), or ("other", why it can't update itself)."""
    real = os.path.realpath(prefix or sys.prefix)
    if "/Cellar/agent-sync/" in real + "/":
        return "homebrew", real.split("/Cellar/")[0]
    try:
        direct = json.loads(metadata.distribution("agent-sync").read_text("direct_url.json") or "{}")
    except (metadata.PackageNotFoundError, ValueError):
        direct = {}
    url = direct.get("url", "") if direct.get("vcs_info", {}).get("vcs") == "git" else ""
    if "/pipx/venvs/agent-sync" in real:
        return "pipx", url
    if url:
        return "git", url
    return "other", "it runs from a source checkout or a plain pip install"


def latest_release(url: str) -> str | None:
    """The newest vX.Y.Z tag of a git repository: a copy installed from git updates to releases, never to main."""
    found = []
    for line in raw(["git", "ls-remote", "--tags", "--refs", url, "v*"], timeout=60).splitlines():
        tag = line.rsplit("refs/tags/", 1)[-1]
        if re.fullmatch(r"v\d+(?:\.\d+)*", tag):
            found.append((tuple(int(part) for part in tag[1:].split(".")), tag))
    return max(found)[1] if found else None


def update_self(log, dry: bool = False, home: Path | None = None, *, state: UpdateState | None = None) -> bool:
    """Update agent-sync the way it was installed. Returns True when a new version was installed."""
    kind, where = how_installed()
    before = __version__
    if kind == "homebrew":
        if not brew_writable(Path(where)):
            log("skip", f"agent-sync comes from Homebrew in {where}, which another account owns; update it there")
            return False
        refresh_brew(log, dry, state or UpdateState())
        if "agent-sync" not in sh(["brew", "outdated", "--formula", "agent-sync"], timeout=300)[1]:
            log("ok", f"agent-sync {before} is current (Homebrew)")
            return False
        args = ["brew", "upgrade", "--formula", "agent-sync"]
    elif kind == "pipx" and not where:
        args = ["pipx", "upgrade", "agent-sync"]
    elif kind in ("pipx", "git"):              # from a git repository: to its newest release, never to main
        tag = latest_release(where)
        if tag is None:
            log("skip", f"agent-sync found no release in {where}; it only updates to a release")
            return False
        if tag == f"v{before}":
            log("ok", f"agent-sync {before} is the latest release")
            return False
        args = (["pipx", "install", "--force", f"git+{where}@{tag}"] if kind == "pipx" else
                [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", f"git+{where}@{tag}"])
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


def read_json_object(path: Path) -> dict:
    try:
        return json_object(path.read_text())
    except (OSError, ValueError):
        return {}


def version_triplet(version: object) -> tuple[int, ...]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
    return tuple(map(int, match.groups())) if match else ()


def update_antigravity_plugins(log, dry: bool, home: Path) -> None:
    """Install newer versions from Antigravity's cached marketplaces."""
    config = read_json_object(home / ".gemini/config/config.json")
    installed = config.get("plugins")
    if not isinstance(installed, dict):
        return

    @cache
    def catalog(marketplace: str) -> dict:
        path = home / ".gemini/antigravity-cli/cache/marketplaces" / marketplace / "marketplace.json"
        entries = read_json_object(path).get("plugins")
        if not isinstance(entries, list):
            return {}
        return {entry["name"]: entry.get("version") for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)}

    current = 0
    for name, plugin in installed.items():
        source = plugin.get("installedFrom") if isinstance(plugin, dict) else None
        if not isinstance(source, dict):
            continue
        marketplace, version = source.get("marketplace"), source.get("version")
        if not isinstance(marketplace, str) or not marketplace:
            continue
        latest = catalog(marketplace).get(name)
        old, new = version_triplet(version), version_triplet(latest)
        if not old or not new:
            continue
        if latest == version:
            current += 1
        elif new >= old:
            backup_plugins(home, "antigravity", dry)
            act(log, dry, f"Antigravity plugin {name}", ["agy", "plugin", "install", f"{name}@{marketplace}"])
    if current:
        log("ok", f"Antigravity plugins are current ({current})")


PLUGIN_PATHS = {
    "claude": (".claude/plugins", ".claude/settings.json", ".claude.json"),
    "codex": (".codex/plugins", ".codex/config.toml"),
    "cursor": (".cursor/plugins", ".cursor/cli-config.json"),
    "gemini": (".gemini/extensions", ".gemini/settings.json"),
    "antigravity": (".gemini/config/plugins", ".gemini/config/config.json"),
}


def backup_plugins(home: Path, tool: str, dry: bool) -> None:
    if not dry:
        for relative in PLUGIN_PATHS[tool]:
            backups.save(home / relative, follow=True)


def update_plugins(log, dry: bool = False, home: Path | None = None, *, enabled=DEFAULT_AGENTS) -> None:
    """Claude Code: refresh every marketplace, then update each user-installed plugin.
    Codex: refresh the Git plugin marketplaces you added; its installed plugins follow them.
    Cursor: refresh the plugin marketplaces you added (Cursor manages its built-in ones itself).
    A dry run skips a CLI that never started in this account (it has no plugins yet)."""
    home = home or Path.home()

    def usable(tool: str) -> bool:
        cmd = TOOLS[tool].command
        return tool in enabled and bool(shutil.which(cmd, path=PATH)) and (started(cmd, home) or not dry)

    if usable("claude"):
        backup_plugins(home, "claude", dry)
        act(log, dry, "Claude Code plugin marketplaces", ["claude", "plugin", "marketplace", "update"])
        try:
            plugins = json.loads(raw(["claude", "plugin", "list", "--json"]) or "[]")
        except ValueError:
            plugins = []
        for plugin in plugins:
            if plugin.get("scope") in ("user", "project") and "@" in plugin.get("id", ""):
                act(log, dry, f"Claude Code plugin {plugin['id']}", ["claude", "plugin", "update", plugin["id"]])
    if usable("codex"):
        backup_plugins(home, "codex", dry)
        act(log, dry, "Codex plugin marketplaces", ["codex", "plugin", "marketplace", "upgrade"])
    if usable("cursor"):
        for line in raw(["cursor-agent", "plugin", "marketplace", "list"]).splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "user" and parts[2].startswith(("https://", "git@")):
                backup_plugins(home, "cursor", dry)
                act(log, dry, f"Cursor plugin marketplace {parts[0]}",
                    ["cursor-agent", "plugin", "marketplace", "update", parts[0]])
    if usable("gemini"):
        backup_plugins(home, "gemini", dry)
        act(log, dry, f'{TOOLS["gemini"].name} extensions', [TOOLS["gemini"].command, "extensions", "update", "--all"])
    if "antigravity" in enabled and shutil.which(TOOLS["antigravity"].command, path=PATH):
        update_antigravity_plugins(log, dry, home)


def extra_steps(log, steps: list[dict], dry: bool = False) -> None:
    """Your own commands from the config, run in order at the end."""
    for step in steps:
        act(log, dry, step.get("name") or step["command"][0], list(step["command"]),
            timeout=int(step.get("timeout", 900)))
