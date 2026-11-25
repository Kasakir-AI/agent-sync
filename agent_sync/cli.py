"""agent-sync: keep your agents in step.

    agent-sync run [--force] [--echo]   do the upkeep (only when the last run is 20+ hours old, unless --force;
                                        prints each step when started from a terminal)
    agent-sync run --dry-run            show everything a run would change, and change nothing
    agent-sync install [--launcher]     schedule it: hourly + at login, so a missed day catches up
                                        (macOS launchd or a Linux systemd user timer); --launcher adds a
                                        double-click "Agent Upkeep" to the Desktop (macOS)
    agent-sync launcher                 put the double-click "Agent Upkeep" on the Desktop (macOS); every run
                                        then keeps it there and up to date
    agent-sync uninstall                remove the schedule and the launcher
    agent-sync setup [--dry-run] [--yes] discover agents and install missing prerequisites and enabled CLIs
    agent-sync config                   change the settings by answering questions (also: agent-sync settings)
    agent-sync agents                   list supported agents and offer to enable installed agents
    agent-sync agents add <id>...       choose agents to keep in step
    agent-sync agents remove <id>...    stop keeping agents in step and leave their files in place
    agent-sync inventory                show loaded plugins, MCP servers, skills, and upkeep findings
    agent-sync status                   last run, log location, enabled CLI versions
    agent-sync review [--no-open]        compare settings and choose which versions to keep in a local page
    agent-sync review --list             print the comparison and recommendations in the terminal
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from . import (
    __version__,
    agents,
    checks,
    duplicates,
    instructions,
    inventory,
    mcp,
    membership,
    memory,
    plugins,
    review,
    roles,
    schedule,
    skills,
    tools,
    writes,
)
from .config import Config, load, save_setting
from .locking import run_lock
from .log import Log
from .registry import TOOLS

DUE_HOURS = 20


def online() -> bool:
    try:
        urllib.request.urlopen("https://github.com", timeout=15).close()
        return True
    except OSError:
        return False


def hours(n: float) -> str:
    return "under an hour" if n < 1 else f"{n:.0f} hour{'' if round(n) == 1 else 's'}"


def run(force: bool, echo: bool, dry: bool = False) -> int:
    cfg = load()
    echo = echo or sys.stdout.isatty()
    stamp = cfg.state_dir / "last-run"
    age = time.time() - stamp.stat().st_mtime if stamp.exists() else None
    if not (force or dry) and age is not None and age < DUE_HOURS * 3600:
        if echo:
            print(f"The last run was {hours(age / 3600)} ago; the next is due in "
                  f"{hours(DUE_HOURS - age / 3600)}. Run `agent-sync run --force` to run now.")
        return 0
    with ExitStack() as stack:
        work = cfg.state_dir
        if dry:
            # Keep both the package checkouts and npm downloads out of the real state directories.
            work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="agent-sync-dry.")))
            cache = os.environ.get("npm_config_cache")
            if cache is None:
                stack.callback(os.environ.pop, "npm_config_cache", None)
            else:
                stack.callback(os.environ.__setitem__, "npm_config_cache", cache)
            os.environ["npm_config_cache"] = str(work / "npm-cache")
        if not stack.enter_context(run_lock(work / "lock")):
            if echo:
                print("another agent-sync run is in progress")
            return 0
        log = Log(cfg.log_file, echo=echo or dry, write=not dry)
        if not dry:
            log.prune(cfg.log_days)
        found = membership.untracked(cfg, membership.available(cfg))
        if found:
            names = ", ".join(TOOLS[agent].name for agent in found)
            log("info", f"Installed outside sync: {names}. Run `agent-sync agents add {' '.join(found)}` to add them.")
        if not online():
            log("skip", "no network; will try again at the next scheduled check")
            return 0
        log("start", f"agent-sync {__version__}, source of truth: {cfg.source}"
            + ("  (dry run: nothing is changed)" if dry else ""))
        log("info", f"skills tool: {tools.skills(cfg.skills_cli, '--version')[1]}")
        if cfg.launcher and cfg.macos:
            log.lines(filter(None, [schedule.refresh_launcher(cfg.home, dry, tools.PATH)]))
        taken: set[str] = set()          # skill names a package listed earlier ships: that package's version wins
        for package in cfg.skill_packages:
            dest = work / "packages" / package.replace("/", "__")
            code, out = tools.checkout(package, dest)
            log("ok" if code == 0 else "FAIL", f"fetch {package}{': ' + out if code else ''}")
            names, gone = skills.package_choice(cfg.home, package, dest, taken) if code == 0 else (None, [])
            for name in gone:
                tools.act(log, dry, f"remove {name} ({package} ships it, but an earlier package has that name)",
                          tools.remove_args(cfg.skills_cli, name, enabled=cfg.agents))
            if names != []:
                tools.act(log, dry, f"skills from {package} for {', '.join(tools.skill_agents(cfg.agents))}",
                          tools.skill_args(cfg.skills_cli, package, names, enabled=cfg.agents))
            if code == 0 and cfg.remove_dropped_skills:
                for name in skills.dropped(cfg.home, package, dest):
                    tools.act(log, dry, f"remove {name} (no longer in {package})",
                              tools.remove_args(cfg.skills_cli, name, enabled=cfg.agents))
            if (dest / "agents").is_dir():
                version = "0"
                manifest = dest / ".cursor-plugin/plugin.json"
                if manifest.exists():
                    version = json.loads(manifest.read_text()).get("version", "0")
                try:
                    log.lines(agents.mirror(dest, package, cfg.home, cfg.agent_models, version, dry,
                                            cfg.agent_notes, enabled=cfg.agents))
                    log("ok", f"agents from {package} mirrored")
                except (ValueError, OSError) as e:
                    log("FAIL", f"agents from {package}: {e}")
        if cfg.plugin_skills:
            log("WARN", "plugin_skills is no longer used: Cursor loads Claude Code plugins' skills itself, so copies "
                "made it load them twice. To give Codex a plugin's skills, install the plugin in Codex "
                "(`codex plugin list`), then remove plugin_skills from your settings.")
        if cfg.mirror_skills:
            try:
                log.lines(skills.mirror(cfg.home, cfg.source, cfg.skip_skills, dry, enabled=cfg.agents))
                log("ok", "your own skills mirrored")
            except OSError as e:
                log("FAIL", f"your own skills: {e}")
        try:
            found = duplicates.report(cfg.home, cfg.allow_duplicate_skills, enabled=cfg.agents)
            log.lines(found, "WARN")
            if not found:
                log("ok", "no tool loads a skill twice")
        except OSError as e:
            log("FAIL", f"duplicate skills check: {e}")
        log.lines(plugins.twice(cfg.home, enabled=cfg.agents), "WARN")
        if cfg.mirror_roles:
            log.lines(roles.mirror(cfg.home, cfg.source, dry, enabled=cfg.agents))
            log("ok", "model roles mirrored")
        if cfg.mirror_mcp:
            try:
                log.lines(writes.repair_mcp(cfg.home, cfg, dry))
                backups = cfg.state_dir / "backups"
                log.lines(mcp.dedupe(cfg.home, dry, backups, enabled=cfg.agents))
                log.lines(mcp.pins(cfg.home, dry, enabled=cfg.agents, keep=cfg.keep_mcp_versions))
                log.lines(mcp.mirror(cfg.home, cfg.source, dry, enabled=cfg.agents))
                log.lines(mcp.same_server_twice(cfg.home, enabled=cfg.agents), "WARN")
                log("ok", "MCP servers mirrored")
            except (ValueError, OSError) as e:
                log("FAIL", f"MCP servers: {e}")
        if cfg.mirror_instructions:
            try:
                log.lines(instructions.mirror(cfg.home, cfg.source, dry, review.baseline_file(cfg),
                                              enabled=cfg.agents))
                log("ok", "global instructions mirrored")
            except OSError as e:
                log("FAIL", f"global instructions: {e}")
        if cfg.mirror_memory:
            try:
                log.lines(memory.mirror(cfg.home, dry, enabled=cfg.agents))
            except OSError as e:
                log("FAIL", f"shared memory: {e}")
        log_review(cfg, log)
        if cfg.update_plugins:
            tools.update_plugins(log, dry, cfg.home, enabled=cfg.agents)
            if ("codex" in cfg.agents and shutil.which("codex", path=tools.PATH)
                    and (not dry or tools.started("codex", cfg.home))):
                plugins.own_versions(cfg.home, dry, log, enabled=cfg.agents, state_dir=cfg.state_dir)
        if cfg.update_clis:
            tools.update_clis(log, cfg, dry)
            tools.update_prerequisites(log, dry)
        tools.extra_steps(log, cfg.extra_steps, dry)
        if cfg.update_self and tools.update_self(log, dry, cfg.home) and cfg.launcher and cfg.macos:
            # the new version writes its own launcher now, not at the next run
            exe = shutil.which("agent-sync", path=tools.PATH)
            if exe:
                tools.act(log, dry, "Desktop launcher from the new version", [exe, "launcher"])
        log.lines(checks.unwanted_models(cfg.home, cfg.warn_models, enabled=cfg.agents), "WARN")
        if dry:
            log("done", f"dry run finished, nothing was changed. A real run: {log.summary()}")
            return 0
        stamp.touch()
        log("done", f"agent-sync finished: {log.summary()}")
        return 1 if log.problems else 0


def log_review(cfg: Config, log: Log) -> None:
    try:
        counts = Counter(group.status for group in review.scan(cfg.home, cfg).groups)
    except Exception as e:
        log("WARN", f"could not compare settings for review: {e}")
        return
    differ, check, missing = counts["differ"], counts["check"], counts["missing"]
    if differ or check or missing:
        different = "1 setting differs" if differ == 1 else f"{differ} settings differ"
        unseen = f", {check} to check once" if check else ""
        absent = "1 is" if missing == 1 else f"{missing} are"
        log("info", f'{different} between the tools{unseen} and {absent} only in one; '
            'run "agent-sync review" to choose')


def review_settings(list_only: bool, no_open: bool) -> int:
    cfg = load()
    if not list_only:
        from . import reviewpage
        return reviewpage.serve(cfg.home, cfg, open_browser=not no_open)
    groups = review.scan(cfg.home, cfg).groups
    counts = Counter(group.status for group in groups)
    print(f"{counts['differ']} differ, {counts['check']} to check once, "
          f"{counts['missing'] + counts['local']} only in one tool, "
          f"{counts['same'] + counts['tailored']} the same ({counts['tailored']} made for each tool)\n")
    order = ("differ", "check", "missing", "local", "tailored")
    for group in sorted((g for g in groups if g.status != "same"), key=lambda g: order.index(g.status)):
        winner, reasons = review.recommend(group, cfg.source)
        print(f"{group.name} ({group.kind}, {group.status})")
        if group.note:
            print(f"  {group.note}")
        if len({version.digest for version in group.versions}) > 1:
            print(f"  {review.difference(group, winner)}")
        for version in group.versions:
            names = ", ".join(TOOLS[tool].name for tool in version.tools)
            date = datetime.fromtimestamp(version.modified).strftime("%Y-%m-%d %H:%M:%S")
            location = review.shown(cfg.home, version.path)
            recommended = " [Recommended]" if version == winner and group.status != "local" else ""
            length = "1 line" if version.lines == 1 else f"{version.lines} lines"
            print(f"  {names} | {review.ORIGIN_LABELS[version.origin]} | {date} | {length} | {location}{recommended}")
        if group.status != "local":
            for reason in reasons:
                print(f"  - {reason}")
        print()
    same: dict[str, list[str]] = {}
    for group in groups:
        if group.status == "same":
            same.setdefault(group.kind, []).append(group.name)
    if same:
        print("The same in every tool that has them:")
        for kind, names in same.items():
            print(f"  {kind}: {', '.join(names)}")
    return 0


def install(launcher: bool) -> int:
    cfg = load()
    for line in schedule.install(cfg.home, cfg.state_dir, launcher):
        print(line)
    if launcher and cfg.macos:
        save_setting(cfg.home, "launcher", True)
    print(f"A run only does the work once every {DUE_HOURS} hours. Log: {cfg.log_file}")
    return 0


def uninstall() -> int:
    cfg = load()
    for line in schedule.uninstall(cfg.home):
        print(line)
    if cfg.launcher:
        save_setting(cfg.home, "launcher", False)
    return 0


def put_launcher() -> int:
    cfg = load()
    if not cfg.macos:
        print("The Desktop launcher is macOS-only; run `agent-sync run --force --echo` instead.")
        return 1
    line = schedule.refresh_launcher(cfg.home, path=tools.PATH)
    if line and line.startswith("WARN"):
        print(line.removeprefix("WARN  "))
        return 1
    save_setting(cfg.home, "launcher", True)
    print(f"Launcher: {schedule.launcher_path(cfg.home)} (double-click to run the upkeep; every run keeps it "
          "up to date)" + ("" if line else ", already current"))
    return 0


def show_inventory() -> int:
    cfg = load()
    home, enabled = cfg.home, cfg.agents
    latest = mcp.pin_lookup()
    seen = duplicates.views(home, enabled=enabled)
    settings = {tool: entries for _, readers, _, _, entries in mcp.settings_files(home, enabled)
                for tool in readers}
    for tool, found in inventory.plugins(home, enabled, include_disabled=True).items():
        print(TOOLS[tool].name)
        print(f"  Skills loaded: {len(seen[tool])}")
        for plugin in found:
            fmt = ""
            if plugin.made_for == "claude" and tool != "claude":
                fmt = " (Claude Code format)"
            elif plugin.made_for:
                fmt = f" (made for {TOOLS[plugin.made_for].name})"
            off = "" if plugin.loads else " (not loaded)"
            print(f"  Plugin {plugins.label(plugin)}{fmt}{off}")

        servers = [(name, server, "settings") for name, server in settings.get(tool, {}).items()]
        servers += [(name, server, f"plugin {plugin.id}") for plugin in found if plugin.loads
                    for name, server in plugin.servers]
        for name, server, origin in servers:
            details = []
            for _, package, version in mcp.npm_pins(server):
                available = latest(package)
                newer = f", {available} is out" if mcp.pin_update(version, available) else ""
                details.append(f"{package} {version}{newer}")
            pins = f" ({'; '.join(details)})" if details else ""
            print(f"  MCP server {name} from {origin}{pins}")
        print()

    print("Problems found")
    problems = (duplicates.report(home, cfg.allow_duplicate_skills, enabled=enabled)
                + plugins.twice(home, enabled=enabled)
                + mcp.dedupe(home, True, cfg.state_dir / "backups", enabled=enabled)
                + mcp.same_server_twice(home, enabled=enabled)
                + mcp.pins(home, True, enabled=enabled, keep=cfg.keep_mcp_versions, latest=latest))
    for line in problems or ["None"]:
        print(f"  {line}")
    return 0


def status() -> int:
    cfg = load()
    stamp = cfg.state_dir / "last-run"
    print(f"agent-sync {__version__}  source of truth: {cfg.source}  packages: {', '.join(cfg.skill_packages)}")
    print(f"last run: {time.ctime(stamp.stat().st_mtime) if stamp.exists() else 'never'}")
    print(f"log: {cfg.log_file}")
    print(f"schedule: {'installed' if schedule.installed(cfg.home) else 'not installed'}")
    for agent in cfg.agents:
        cmd = TOOLS[agent].command
        print(f"{cmd}: {tools.version(cmd, cfg.home)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-sync", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"agent-sync {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="do the upkeep")
    r.add_argument("--force", action="store_true", help="run even if the last run is recent")
    r.add_argument("--echo", action="store_true", help="also print each log line (the default in a terminal)")
    r.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    i = sub.add_parser("install", help="schedule the daily upkeep")
    i.add_argument("--launcher", action="store_true", help="also put a double-click launcher on the Desktop (macOS)")
    sub.add_parser("launcher", help="put the double-click launcher on the Desktop and keep it up to date (macOS)")
    sub.add_parser("uninstall", help="remove the schedule and the launcher")
    s = sub.add_parser("setup", help="discover agents and install missing prerequisites and enabled CLIs")
    s.add_argument("--dry-run", action="store_true", help="only list what is missing and how it would be installed")
    s.add_argument("--yes", action="store_true", help="don't ask before installing")
    sub.add_parser("config", aliases=["settings"], help="change the settings by answering questions")
    agent_parser = sub.add_parser("agents", help="list agents and choose which to keep in step")
    membership_commands = agent_parser.add_subparsers(dest="agent_action")
    for action in ("add", "remove"):
        command = membership_commands.add_parser(action, help=f"{action} agents")
        command.add_argument("ids", metavar="id", nargs="+")
    sub.add_parser("inventory", help="show loaded plugins, MCP servers, skills, and upkeep findings")
    sub.add_parser("status", help="show the last run and versions")
    review_parser = sub.add_parser("review", help="compare settings and choose which versions to keep")
    review_parser.add_argument("--list", action="store_true", help="print the comparison and recommendations")
    review_parser.add_argument("--no-open", action="store_true", help="print the page URL without opening the browser")
    a = ap.parse_args(argv)
    if a.cmd == "agents":
        return membership.run(load(), a.agent_action, getattr(a, "ids", []))
    if a.cmd == "run":
        return run(a.force, a.echo, a.dry_run)
    if a.cmd == "review":
        return review_settings(a.list, a.no_open)
    if a.cmd == "setup":
        from . import setup
        return setup.run(load(), a.dry_run, a.yes)
    if a.cmd in ("config", "settings"):
        from . import configure
        return configure.run(Path.home(), dry_run=lambda: run(force=False, echo=False, dry=True))
    if a.cmd == "install":
        return install(a.launcher)
    return {"uninstall": uninstall, "launcher": put_launcher, "status": status,
            "inventory": show_inventory}[a.cmd]()
