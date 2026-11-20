"""agent-sync: keep Claude Code, Codex and Cursor in step.

    agent-sync run [--force] [--echo]   do the upkeep (only when the last run is 20+ hours old, unless --force)
    agent-sync run --dry-run            show everything a run would change, and change nothing
    agent-sync install [--launcher]     schedule it: hourly + at login, so a missed day catches up
                                        (macOS launchd or a Linux systemd user timer); --launcher adds a
                                        double-click "Agent Upkeep" to the Desktop (macOS)
    agent-sync launcher                 put the double-click "Agent Upkeep" on the Desktop (macOS); every run
                                        then keeps it there and up to date
    agent-sync uninstall                remove the schedule and the launcher
    agent-sync setup [--dry-run] [--yes] install what is missing: Homebrew, git, Node, Claude Code, Codex, Cursor
    agent-sync config                   change the settings by answering questions (also: agent-sync settings)
    agent-sync status                   last run, log location, installed versions
    agent-sync review [--no-open]        compare settings and choose which versions to keep in a local page
    agent-sync review --list             print the comparison and recommendations in the terminal
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import urllib.request
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from . import __version__, agents, checks, instructions, mcp, review, roles, schedule, skills, tools
from .config import Config, load, save_setting
from .locking import run_lock
from .log import Log

DUE_HOURS = 20


def online() -> bool:
    try:
        urllib.request.urlopen("https://github.com", timeout=15).close()
        return True
    except OSError:
        return False


def run(force: bool, echo: bool, dry: bool = False) -> int:
    cfg = load()
    stamp = cfg.state_dir / "last-run"
    if not (force or dry) and stamp.exists() and time.time() - stamp.stat().st_mtime < DUE_HOURS * 3600:
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
        if not online():
            log("skip", "no network; will try again at the next scheduled check")
            return 0
        log("start", f"agent-sync {__version__}, source of truth: {cfg.source}"
            + ("  (dry run: nothing is changed)" if dry else ""))
        log("info", f"skills tool: {tools.skills(cfg.skills_cli, '--version')[1]}")
        if cfg.launcher and cfg.macos:
            line = schedule.refresh_launcher(cfg.home, dry, tools.PATH)
            if line:
                log("WARN" if line.startswith("WARN") else "sync", line.removeprefix("WARN  "))
        for package in cfg.skill_packages:
            dest = work / "packages" / package.replace("/", "__")
            code, out = tools.checkout(package, dest)
            log("ok" if code == 0 else "FAIL", f"fetch {package}{': ' + out if code else ''}")
            tools.act(log, dry, f"skills from {package} for claude-code, codex, cursor",
                      tools.skill_args(cfg.skills_cli, package))
            if code == 0 and cfg.remove_dropped_skills:
                for name in skills.dropped(cfg.home, package, dest):
                    tools.act(log, dry, f"remove {name} (no longer in {package})",
                              ["npx", "-y", "--prefer-online", cfg.skills_cli, "remove", "-g", "-s", name, "-y"])
            if (dest / "agents").is_dir():
                version = "0"
                manifest = dest / ".cursor-plugin/plugin.json"
                if manifest.exists():
                    version = json.loads(manifest.read_text()).get("version", "0")
                try:
                    for line in agents.mirror(dest, package, cfg.home, cfg.agent_models, version, dry,
                                              cfg.agent_notes):
                        log("sync", line)
                    log("ok", f"agents from {package} mirrored")
                except (ValueError, OSError) as e:
                    log("FAIL", f"agents from {package}: {e}")
        if cfg.mirror_skills:
            try:
                for line in skills.mirror(cfg.home, cfg.source, cfg.skip_skills, dry, cfg.plugin_skills):
                    log("WARN" if line.startswith("WARN") else "sync", line.removeprefix("WARN  "))
                log("ok", "your own skills" + (" and plugin skills" if cfg.plugin_skills else "") + " mirrored")
            except OSError as e:
                log("FAIL", f"your own skills: {e}")
        if cfg.mirror_roles:
            for line in roles.mirror(cfg.home, cfg.source, dry):
                kind = line.split()[0] if line.startswith(("WARN", "skip")) else "sync"
                log(kind, line.removeprefix(kind).strip() if kind != "sync" else line)
            log("ok", "model roles mirrored")
        if cfg.mirror_mcp:
            try:
                for line in mcp.mirror(cfg.home, cfg.source, dry):
                    log("sync", line)
                log("ok", "MCP servers mirrored")
            except (ValueError, OSError) as e:
                log("FAIL", f"MCP servers: {e}")
        if cfg.mirror_instructions:
            try:
                for line in instructions.mirror(cfg.home, cfg.source, dry):
                    log("skip" if line.startswith("skip") else "sync", line.removeprefix("skip  "))
                log("ok", "global instructions mirrored")
            except OSError as e:
                log("FAIL", f"global instructions: {e}")
        log_review(cfg, log)
        if cfg.update_plugins:
            tools.update_plugins(log, dry, cfg.home)
        if cfg.update_clis:
            tools.update_clis(log, cfg, dry)
        tools.extra_steps(log, cfg.extra_steps, dry)
        if cfg.update_self and tools.update_self(log, dry, cfg.home) and cfg.launcher and cfg.macos:
            # the new version writes its own launcher now, not at the next run
            exe = shutil.which("agent-sync", path=tools.PATH)
            if exe:
                tools.act(log, dry, "Desktop launcher from the new version", [exe, "launcher"])
        for line in checks.unwanted_models(cfg.home, cfg.warn_models):
            log("WARN", line)
        if dry:
            log("done", "dry run finished: nothing was changed")
            return 0
        stamp.touch()
        log("done", f"agent-sync finished, {log.problems} problem(s)")
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
    for group in groups:
        winner, reasons = review.recommend(group, cfg.source)
        print(f"{group.name} ({group.kind}, {group.status})")
        if group.note:
            print(f"  {group.note}")
        if len({version.digest for version in group.versions}) > 1:
            print(f"  {review.difference(group, winner)}")
        for version in group.versions:
            names = ", ".join(review.TOOL_LABELS[tool] for tool in version.tools)
            date = datetime.fromtimestamp(version.modified).strftime("%Y-%m-%d %H:%M:%S")
            location = review.shown(cfg.home, version.path)
            recommended = " [Recommended]" if version == winner and group.status != "local" else ""
            length = "1 line" if version.lines == 1 else f"{version.lines} lines"
            print(f"  {names} | {review.ORIGIN_LABELS[version.origin]} | {date} | {length} | {location}{recommended}")
        if group.status != "local":
            for reason in reasons:
                print(f"  - {reason}")
        print()
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


def status() -> int:
    cfg = load()
    stamp = cfg.state_dir / "last-run"
    print(f"agent-sync {__version__}  source of truth: {cfg.source}  packages: {', '.join(cfg.skill_packages)}")
    print(f"last run: {time.ctime(stamp.stat().st_mtime) if stamp.exists() else 'never'}")
    print(f"log: {cfg.log_file}")
    print(f"schedule: {'installed' if schedule.installed(cfg.home) else 'not installed'}")
    for cmd in ("claude", "codex", "cursor-agent"):
        print(f"{cmd}: {tools.version(cmd, cfg.home)}")
    chosen = set(cfg.plugin_skills)
    for pid, root in skills.plugins(cfg.home).items():
        names = sorted(skills.plugin_skill_dirs(root))
        if names:
            offered = "offered to Codex and Cursor" if chosen & {pid, pid.split("@")[0]} else "Claude Code only"
            print(f"plugin {pid}: {len(names)} skill(s), {offered}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-sync", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"agent-sync {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="do the upkeep")
    r.add_argument("--force", action="store_true", help="run even if the last run is recent")
    r.add_argument("--echo", action="store_true", help="also print each log line")
    r.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    i = sub.add_parser("install", help="schedule the daily upkeep")
    i.add_argument("--launcher", action="store_true", help="also put a double-click launcher on the Desktop (macOS)")
    sub.add_parser("launcher", help="put the double-click launcher on the Desktop and keep it up to date (macOS)")
    sub.add_parser("uninstall", help="remove the schedule and the launcher")
    s = sub.add_parser("setup", help="install what is missing (Homebrew, git, Node and the three CLIs)")
    s.add_argument("--dry-run", action="store_true", help="only list what is missing and how it would be installed")
    s.add_argument("--yes", action="store_true", help="don't ask before installing")
    sub.add_parser("config", aliases=["settings"], help="change the settings by answering questions")
    sub.add_parser("status", help="show the last run and versions")
    review_parser = sub.add_parser("review", help="compare settings and choose which versions to keep")
    review_parser.add_argument("--list", action="store_true", help="print the comparison and recommendations")
    review_parser.add_argument("--no-open", action="store_true", help="print the page URL without opening the browser")
    a = ap.parse_args(argv)
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
    return {"uninstall": uninstall, "launcher": put_launcher, "status": status}[a.cmd]()
