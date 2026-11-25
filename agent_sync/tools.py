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

from . import __version__, backups, mcp
from .registry import DEFAULT_AGENTS, TOOLS, Tool, inside

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


def raw(args: list[str], timeout: int = 120, *, ok_codes: tuple[int, ...] = (0,), failure: str = "") -> str:
    """Return stdout for accepted exit codes and `failure` otherwise."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PATH": PATH, "HOMEBREW_NO_AUTO_UPDATE": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return failure
    return p.stdout if p.returncode in ok_codes else failure


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


def safe_id(value: object) -> bool:
    """A plugin, extension or marketplace id that can go to a CLI as one argument and into a path as one name:
    nothing a CLI reads as a flag, no folder separators or parent references, no spaces."""
    return (isinstance(value, str) and bool(value) and not value.startswith("-") and "/" not in value
            and ".." not in value and not any(char.isspace() for char in value))


def act(log, dry: bool, label: str, args: list[str], timeout: int = 900, *, run=None) -> bool:
    """Run a command that changes something, or with dry=True only say what would run."""
    if dry:
        log("dry", f"would run: {' '.join(args)}  ({label})")
        return False
    code, out = sh(args, timeout) if run is None else run(args)
    log("ran" if code == 0 else "FAIL", f"{label}{': ' + out if code else ''}")
    return code == 0


def update_skills(log, dry: bool, cli: str, *, home: Path, enabled=DEFAULT_AGENTS) -> None:
    """Check for global skill updates and back up the installs the updater can change."""
    from . import skills as installed_skills

    if not (locked := installed_skills.locked(home)):
        return
    command = ["npx", "-y", "--prefer-online", cli]
    label = "skills from every source (skills update)"
    names = []
    if not dry:
        failure = "skills check did not complete"
        report = ANSI.sub("", raw([*command, "check", "-g"], timeout=900, ok_codes=(0, 1), failure=failure))
        reported = set(re.findall(r"^↑ (.+) has an update$", report, re.MULTILINE))
        legacy = re.findall(r"^  ↑ (.+)$", report, re.MULTILINE)
        names = sorted(name for name in reported if name in locked and safe_id(name))
        failed = report == failure or any(message in report for message in (
            "Failed to check skills from", "Failed to update", "Could not check ",
        ))
        if failed or reported != set(names):
            log("FAIL", "skills check did not confirm all global updates")
        if legacy:
            log("info", "this skills CLI updates all skills at once; backing up all locked skills")
            names = []
        elif not names:
            if failed or reported or any(line in report.splitlines() for line in (
                "✓ All global skills are up to date", "✓ All skills are up to date",
                "No global skills to check.", "No GitHub skills to check.",
                "No global skills tracked in lock file.", "No skills tracked in lock file.",
            )):
                return
            log("info", "skills update list could not be read; backing up all locked skills before updating")
        installed_skills.backup_installs(home, enabled, names or locked)
    act(log, dry, label, [*command, "update", "-g", "-y", *names])


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
            prefix = Path(os.path.realpath(shutil.which("brew", path=PATH))).parents[1]
            if not brew_writable(prefix):
                log("skip", f"Homebrew in {prefix} belongs to another account; update its tools from that account")
                continue
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


# Interpreters and runners an MCP server starts its code with: updating one (`brew upgrade python@3.13`) is not the
# server's update, so agent-sync never does it for a server.
INTERPRETERS = re.compile(r"python\d*(?:\.\d+)?|ruby|java|node|bash|zsh|sh|perl|php|go|deno|bun|bunx|docker|uv|uvx"
                          r"|npx|npm|pnpm|pnpx")


def mcp_commands(cfg, log):
    """Yield the name, executable and resolved executable of each switched-on stdio server's own program."""
    clis = {tool.command for tool in TOOLS.values()}
    notices: list[str] = []
    settings = list(mcp.settings_files(cfg.home, cfg.agents, notices))
    for notice in notices:
        level, _, message = notice.partition("  ")
        log(level, message)
    for _, _, _, _, servers in settings:
        for name, server in servers.items():
            if server.get("enabled", True) is False or server.get("disabled") is True or server.get("url"):
                continue
            cmd = server.get("command")
            if not isinstance(cmd, str) or not cmd or Path(cmd).name in clis or INTERPRETERS.fullmatch(Path(cmd).name):
                continue
            found = shutil.which(cmd, path=PATH)
            if not found:
                continue
            executable, real = Path(found), Path(os.path.realpath(found))
            if real.name in clis or INTERPRETERS.fullmatch(real.name) or any(
                    part.lower().endswith(".app") for part in (*executable.parts[:-1], *real.parts[:-1])):
                continue
            yield name, executable, real


def package_under(real: Path, root: Path | None, manager: str) -> tuple[str, str, None] | None:
    if root is None or not real.is_relative_to(root):
        return None
    parts = real.relative_to(root).parts
    size = 2 if manager == "npm" and parts and parts[0].startswith("@") else 1
    return (manager, "/".join(parts[:size]), None) if len(parts) > size else None


def standalone_folder(home: Path, executable: Path, real: Path) -> Path | None:
    opt = Path("/usr/local/opt")
    if executable.is_relative_to(opt):
        parts = executable.relative_to(opt).parts
        if len(parts) > 1 and real.is_relative_to(root := (opt / parts[0]).resolve()):
            return root
    local = (home / ".local").resolve()
    if real.is_relative_to(local):
        folder = real.parent
        if folder.name in {"bin", "sbin", "libexec"}:
            folder = folder.parent
        if folder != local and folder.is_relative_to(local) and folder != local / "share":
            return folder
    if real.is_relative_to(opt) and len(parts := real.relative_to(opt).parts) > 1:
        return opt / parts[0]
    cellar = opt.parent / "Cellar"
    if real.is_relative_to(cellar) and len(parts := real.relative_to(cellar).parts) > 2:
        root = (opt / parts[0]).resolve()
        if real.is_relative_to(root):
            return root
    return None


def update_mcp_programs(log, cfg, dry: bool, *, state: UpdateState | None = None) -> None:
    """Update the programs enabled agents' stdio servers run: a Homebrew, npm, pipx or uv package when it is
    outdated, or a program listed in `program_updates` with its own command. A dry run runs nothing."""
    state = state or UpdateState()
    pipx = (Path(os.environ.get("PIPX_HOME") or cfg.home / ".local/pipx") / "venvs").resolve()
    uv = (cfg.home / ".local/share/uv/tools").resolve()

    @cache
    def npm_root() -> Path | None:
        if dry or not shutil.which("npm", path=PATH):
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
    for name, cmd, real in mcp_commands(cfg, log):
        if real in seen_paths:
            continue
        seen_paths.add(real)
        own = cfg.program_updates.get(cmd.name) or cfg.program_updates.get(real.name)
        if own:
            if not dry:
                if folder := standalone_folder(cfg.home, cmd, real):
                    backups.save(folder)
                backups.save(real)
            act(log, dry, f"{cmd.name} update (MCP server {name})", list(own))
            continue
        managed = next((match for resolve in package_resolvers if (match := resolve(real))), None)
        if managed is None:
            continue
        kind, package, prefix = managed
        key = (kind, package)
        if key in seen_packages:
            continue
        seen_packages.add(key)
        if prefix and not brew_writable(prefix):
            log("skip", f"{cmd.name} comes from Homebrew in {prefix}, which another account owns; "
                        "update it from that account")
            continue
        if dry:
            log("dry", f"would update {package} ({'Homebrew' if prefix else kind}) if it is outdated, "
                       f"for MCP server {name}")
            continue
        if prefix:
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


BUILD = "-agy."   # Antigravity's rebuilds of one release: 1.2.3-agy.1, 1.2.3-agy.2


def catalog_newer(installed: object, available: object) -> bool:
    """The catalog's version is newer than the installed one: a higher X.Y.Z; for equal numbers, the release of an
    installed prerelease or another Antigravity build of it. Never a move from a release to a prerelease."""
    old, new = version_triplet(installed), version_triplet(available)
    if not old or not new or installed == available:
        return False
    old_suffix, new_suffix = re.sub(r"^\d+\.\d+\.\d+", "", installed), re.sub(r"^\d+\.\d+\.\d+", "", available)

    def prerelease(suffix: str) -> bool:
        return bool(suffix) and not suffix.startswith(BUILD)

    if prerelease(new_suffix) and not prerelease(old_suffix):
        return False
    if new != old:
        return new > old
    return (prerelease(old_suffix) and not new_suffix) or (
        old_suffix.startswith(BUILD) and new_suffix.startswith(BUILD))


def update_antigravity_plugins(log, dry: bool, home: Path) -> None:
    """Install newer versions from Antigravity's cached marketplaces."""
    config = read_json_object(home / ".gemini/config/config.json")
    installed = config.get("plugins")
    if not isinstance(installed, dict):
        return

    cache_root = home / ".gemini/antigravity-cli/cache/marketplaces"

    @cache
    def catalog(marketplace: str) -> dict:
        path = cache_root / marketplace / "marketplace.json"
        if not inside(cache_root, path):
            return {}
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
        if not safe_id(marketplace) or not safe_id(name):
            continue
        latest = catalog(marketplace).get(name)
        if latest == version and version_triplet(version):
            current += 1
        elif catalog_newer(version, latest):
            backup_plugins(home, "antigravity", dry, (home / ".gemini/config/plugins" / name,))
            act(log, dry, f"Antigravity plugin {name}", ["agy", "plugin", "install", f"{name}@{marketplace}"])
    if current:
        log("ok", f"Antigravity plugins are current ({current})")


PLUGIN_STATE = {   # the files each tool's plugin commands rewrite
    "claude": (".claude/plugins/installed_plugins.json", ".claude/plugins/known_marketplaces.json",
               ".claude/settings.json", ".claude.json"),
    "codex": (".codex/config.toml",),
    "cursor": (".cursor/cli-config.json",),
    "gemini": (".gemini/settings.json", ".gemini/extensions/extension-enablement.json"),
    "antigravity": (".gemini/config/config.json",),
}


def backup_plugins(home: Path, tool: str, dry: bool, folders: tuple[Path, ...] = ()) -> None:
    """Save the state files the tool's plugin commands rewrite, and the folders a command replaces or removes."""
    if not dry:
        for relative in PLUGIN_STATE[tool]:
            backups.save(home / relative, follow=True)
        for folder in folders:
            backups.save(folder)


def gemini_update_folders(home: Path) -> tuple[Path, ...]:
    root = home / ".gemini/extensions"
    return tuple(
        manifest.parent for manifest in sorted(root.glob("*/gemini-extension.json"))
        if isinstance(read_json_object(manifest).get("name"), str)
        and read_json_object(manifest.parent / ".gemini-extension-install.json").get("type")
        in ("git", "github-release", "local")
    )


def update_plugins(log, dry: bool = False, home: Path | None = None, *, enabled=DEFAULT_AGENTS) -> None:
    """Claude Code: refresh every marketplace, then update each user-installed plugin.
    Codex: refresh the Git plugin marketplaces you added; its installed plugins follow them.
    Cursor: refresh the plugin marketplaces you added (Cursor manages its built-in ones itself).
    A dry run skips a CLI that never started in this account (it has no plugins yet)."""
    from . import skills as installed_skills

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
        installs = {pid: root for pid, (root, _, _) in installed_skills.plugin_installs(home).items()}
        for plugin in plugins if isinstance(plugins, list) else ():
            pid = plugin.get("id") if isinstance(plugin, dict) else None
            # a project-scope plugin can only be updated inside its project
            if safe_id(pid) and "@" in pid and plugin.get("scope") == "user":
                backup_plugins(home, "claude", dry, tuple(filter(None, [installs.get(pid)])))
                act(log, dry, f"Claude Code plugin {pid}", ["claude", "plugin", "update", pid])
    if usable("codex"):
        backup_plugins(home, "codex", dry)
        act(log, dry, "Codex plugin marketplaces", ["codex", "plugin", "marketplace", "upgrade"])
    if usable("cursor"):
        for line in raw(["cursor-agent", "plugin", "marketplace", "list"]).splitlines():
            parts = line.split()
            if (len(parts) >= 3 and parts[1] == "user" and parts[2].startswith(("https://", "git@"))
                    and safe_id(parts[0])):
                backup_plugins(home, "cursor", dry)
                act(log, dry, f"Cursor plugin marketplace {parts[0]}",
                    ["cursor-agent", "plugin", "marketplace", "update", parts[0]])
    if usable("gemini"):
        backup_plugins(home, "gemini", dry, () if dry else gemini_update_folders(home))
        act(log, dry, f'{TOOLS["gemini"].name} extensions', [TOOLS["gemini"].command, "extensions", "update", "--all"])
    if "antigravity" in enabled and shutil.which(TOOLS["antigravity"].command, path=PATH):
        update_antigravity_plugins(log, dry, home)


def extra_steps(log, steps: list[dict], dry: bool = False) -> None:
    """Your own commands from the config, run in order at the end."""
    for step in steps:
        act(log, dry, step.get("name") or step["command"][0], list(step["command"]),
            timeout=int(step.get("timeout", 900)))
