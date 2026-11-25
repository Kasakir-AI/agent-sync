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
    agent-sync pending                  list pending choices and unacknowledged warnings
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
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from . import (
    __version__,
    agents,
    backups,
    checks,
    cleanup,
    duplicates,
    instructions,
    inventory,
    mcp,
    membership,
    memory,
    notify,
    pending,
    plugins,
    review,
    roles,
    schedule,
    skills,
    statuslines,
    tools,
    writes,
)
from .config import Config, load, save_setting
from .locking import run_lock
from .log import ACK_AGE, ACK_FILE, Log, WarningAcks
from .registry import TOOLS
from .textmerge import save_baselines

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
        return upkeep(cfg, log=Log(cfg.log_file, echo=echo or dry,
                                   write=not dry and not cfg.log_file.is_symlink()),
                      work=work, force=force, dry=dry, stack=stack)


def upkeep(cfg: Config, log: Log, work: Path, force: bool, dry: bool, stack: ExitStack) -> int:
    now = datetime.now()
    stamp = cfg.state_dir / "last-run"
    root = cfg.state_dir / "backups"
    removals, saved = None, None
    batch = ExitStack()
    try:
        try:
            age = now.timestamp() - stamp.stat().st_mtime
        except FileNotFoundError:
            age = None
        if not (force or dry) and age is not None and age < DUE_HOURS * 3600:
            if log.echo:
                print(f"The last run was {hours(age / 3600)} ago; the next is due in "
                      f"{hours(DUE_HOURS - age / 3600)}. Run `agent-sync run --force` to run now.")
        else:
            if not dry:
                saved = batch.enter_context(backups.during(cfg.home, root, now=now))
            guarded(log, "memory-sharing choice", lambda: ask_to_share_memory(cfg, log, dry, stack))
            found = membership.untracked(cfg, membership.available(cfg))
            if found:
                names = ", ".join(TOOLS[agent].name for agent in found)
                log("info", f"Installed outside sync: {names}. "
                    f"Run `agent-sync agents add {' '.join(found)}` to add them.")
            if not online():
                log("skip", "no network; will try again at the next scheduled check")
            else:
                removals = stack.enter_context(pending.during(cfg.state_dir, cfg.keep_installed, log, dry, scoped=True))
                log("start", f"agent-sync {__version__}, source of truth: {cfg.source}"
                    + ("  (dry run: nothing is changed)" if dry else ""))
                log("info", f"skills tool: {tools.skills(cfg.skills_cli, '--version')[1]}")
                steps(cfg, log, work, dry, tools.UpdateState(), removals, stack)
    finally:
        try:
            guarded(log, "backup finalization", batch.close)
        finally:
            clean_up(cfg, log, saved.folder if saved else None, dry, now)

    acknowledgements = WarningAcks(cfg.state_dir)
    warnings = acknowledgements.unseen(log.warnings)
    questions = removals.questions() if removals is not None else {}
    if dry:
        for warning in warnings:
            print(f"WARN  {warning}")
        for what in questions.values():
            print(f"Would ask: {pending.question(what)} [y/N]")
        if warnings:
            print("Would ask: Press Enter once you have read the warnings")
        log("done", f"dry run finished, nothing was changed. A real run: {log.summary()}")
        return 0
    if log.echo and not notify.in_terminal():
        for warning in warnings:
            print(f"WARN  {warning}")
    approved, acknowledged = notify.deliver(cfg, log, warnings, questions)
    if acknowledged:
        acknowledgements.save(warnings)
    if removals is not None:
        for key in approved:
            removals.seen[key]["approved"] = True
        stamp.touch()
        summary = f", backups: {saved.folder}" if saved and saved.folder else ""
        log("done", f"agent-sync finished: {log.summary()}{summary}")
    return 1 if log.problems else 0


def clean_up(cfg: Config, log: Log, current: Path | None, dry: bool, now: datetime) -> None:
    root = cfg.state_dir
    cutoff = now.timestamp() - ACK_AGE
    guarded(log, "backup retention",
            lambda: backups.prune(root / "backups", cfg.backup_days, cfg.backup_max_mb, current, dry, log, now=now))
    guarded(log, "package cache retention", lambda: cleanup.prune_packages(root / "packages", cfg.skill_packages,
                                                                          dry, log))
    for name in (ACK_FILE, notify.INBOX_FILE):
        guarded(log, name, lambda name=name: cleanup.prune_records(
            root / name, cutoff, dry, log, legacy=name == notify.INBOX_FILE))

    def prune_log() -> None:
        if count := log.prune(cfg.log_days, now, dry=dry):
            log("info", f"{'Would delete' if dry else 'Deleted'} {count} expired log lines")
    guarded(log, "log retention", prune_log)


GUARDED = (OSError, ValueError, TypeError, KeyError)   # JSON and Unicode decode errors are ValueErrors


def ask_to_share_memory(cfg: Config, log: Log, dry: bool, stack: ExitStack) -> None:
    if (question := memory.question(cfg)) is None:
        return
    if dry:
        log("info", f"Would ask: {question} [y/N]")
        return
    answer = notify.memory_choice(cfg, log, question)
    if answer is None:
        log("info", "Memory sharing is waiting for your choice. No memory was shared this run.")
        stack.callback(notify.memory_inbox, cfg, log, question, memory.CHOICE)
        return
    save_setting(cfg.home, "mirror_memory", answer)
    cfg.mirror_memory = answer


def guarded(log: Log, label: str, action: Callable[[], object], *owners: str) -> None:
    """Run one step; a step that fails logs FAIL and the run goes on with the next."""
    run = pending.current.get() if owners else None
    before = log.problems, log.kinds["skip"]
    if run is not None:
        run.step_incomplete = False
    try:
        action()
    except writes.Raced as raced:
        log("WARN", str(raced))
    except GUARDED as error:
        log("FAIL", f"{label}: {error}")
    else:
        if (run is not None and run.completed is not None and not run.step_incomplete
                and before == (log.problems, log.kinds["skip"])):
            run.completed.update(owners)


def steps(cfg: Config, log: Log, work: Path, dry: bool, updates: tools.UpdateState, removals: pending.Run,
          stack: ExitStack) -> None:
    """Every upkeep step, each in its own guard. Plugins update first, so the checks after them see one state."""
    home, enabled = cfg.home, cfg.agents
    if cfg.launcher and cfg.macos:
        guarded(log, "Desktop launcher",
                lambda: log.lines(filter(None, [schedule.refresh_launcher(home, dry, tools.PATH)])))
    guarded(log, "skill packages", lambda: sync_packages(cfg, log, work, dry), "skill", "agent")
    guarded(log, "skills update", lambda: tools.update_skills(log, dry, cfg.skills_cli, home=home, enabled=enabled))
    if cfg.plugin_skills:
        log("WARN", "plugin_skills is no longer used: Cursor loads Claude Code plugins' skills itself, so copies "
            "made it load them twice. To give Codex a plugin's skills, install the plugin in Codex "
            "(`codex plugin list`), then remove plugin_skills from your settings.")
    if cfg.update_plugins:
        guarded(log, "plugin updates", lambda: tools.update_plugins(log, dry, home, enabled=enabled))
        if ("codex" in enabled and shutil.which("codex", path=tools.PATH)
                and (not dry or tools.started("codex", home))):
            guarded(log, "Codex's own plugins",
                    lambda: plugins.own_versions(home, dry, log, enabled=enabled, state_dir=cfg.state_dir), "plugin")
    stack.enter_context(inventory.snapshot())

    if cfg.mirror_skills:
        def mirror_skills() -> None:
            log.lines(skills.mirror(home, cfg.source, cfg.skip_skills, dry, enabled=enabled))
            log("ok", "your own skills mirrored")
        guarded(log, "your own skills", mirror_skills, "skill-copy")

    def duplicate_skills() -> None:
        found = duplicates.report(home, cfg.allow_duplicate_skills, enabled=enabled)
        log.lines(found, "WARN")
        if not found:
            log("ok", "no tool loads a skill twice")
    guarded(log, "duplicate skills check", duplicate_skills)
    guarded(log, "duplicate plugins check", lambda: log.lines(plugins.twice(home, enabled=enabled), "WARN"))
    if cfg.mirror_roles:
        def mirror_roles() -> None:
            log.lines(roles.mirror(home, cfg.source, dry, enabled=enabled))
            log("ok", "model roles mirrored")
        guarded(log, "model roles", mirror_roles)
    if cfg.mirror_mcp:
        def mcp_versions() -> None:
            log.lines(mcp.pins(home, dry, enabled=enabled, keep=cfg.keep_mcp_versions,
                               latest=updates.npm.latest, released=updates.npm.released,
                               major=cfg.mcp_major_updates))

        failed = log.problems
        guarded(log, "MCP repair", lambda: log.lines(writes.repair_mcp(home, cfg, dry)))
        guarded(log, "MCP duplicates", lambda: log.lines(mcp.dedupe(home, dry, enabled=enabled, source=cfg.source)))
        guarded(log, "MCP versions", mcp_versions)
        guarded(log, "MCP servers", lambda: log.lines(mcp.mirror(home, cfg.source, dry, enabled=enabled)))
        guarded(log, "MCP servers", lambda: log.lines(mcp.same_server_twice(home, enabled=enabled), "WARN"))
        if log.problems == failed:
            log("ok", "MCP servers mirrored")
    if cfg.mirror_instructions:
        def mirror_instructions() -> None:
            changes = instructions.mirror(home, cfg.source, dry, review.baseline_file(cfg), enabled=enabled)
            log.lines(changes)
            if not any(line.startswith("WARN  ") for line in changes):
                log("ok", "global instructions mirrored")
        guarded(log, "global instructions", mirror_instructions, "instructions", "instruction")
    if cfg.mirror_memory:
        guarded(log, "shared memory", lambda: log.lines(memory.mirror(
            home, dry, enabled=enabled, private_memory=cfg.private_memory, share_memory=cfg.share_memory)), "memory")
    if cfg.status_line:
        guarded(log, "status line", lambda: log.lines(statuslines.apply(home, dry, cfg, removals)),
                *(f"statusline:{tool}" for tool in enabled if tool != "gemini"))
    log_review(cfg, log)
    if cfg.update_mcp_programs:
        guarded(log, "MCP programs", lambda: tools.update_mcp_programs(log, cfg, dry, state=updates))
    guarded(log, "global tools", lambda: tools.update_global_tools(log, cfg, dry, state=updates))
    if cfg.update_clis:
        guarded(log, "CLI updates", lambda: tools.update_clis(log, cfg, dry, state=updates))
        guarded(log, "git and Node",
                lambda: tools.update_prerequisites(log, dry, state=updates, majors=cfg.global_major_updates))
    guarded(log, "extra steps", lambda: tools.extra_steps(log, cfg.extra_steps, dry))
    if cfg.update_self:
        def update_self() -> None:
            if tools.update_self(log, dry, home, state=updates) and cfg.launcher and cfg.macos:
                # the new version writes its own launcher now, not at the next run
                exe = shutil.which("agent-sync", path=tools.PATH)
                if exe:
                    backups.save(schedule.launcher_path(home), follow=True)
                    tools.act(log, dry, "Desktop launcher from the new version", [exe, "launcher"])
        guarded(log, "agent-sync update", update_self)
    guarded(log, "model check", lambda: log.lines(checks.unwanted_models(home, cfg.warn_models, enabled=enabled),
                                                  "WARN"))


def sync_packages(cfg: Config, log: Log, work: Path, dry: bool) -> None:
    root = work / "packages"
    if root.is_symlink() or not cfg.skill_packages:
        pending.incomplete()
        return
    taken: set[str] = set()
    packages = []
    for package in cfg.skill_packages:
        checkout = root / cleanup.checkout_name(package)
        if checkout.is_symlink():
            pending.incomplete()
            continue
        code, out = tools.checkout(package, checkout)
        log("ok" if code == 0 else "FAIL", f"fetch {package}{': ' + out if code else ''}")
        if code:
            continue
        names, gone = skills.package_choice(cfg.home, package, checkout, taken)
        packages.append((package, checkout, names))
        removals = {name: f"{package} ships it, but an earlier package has that name" for name in gone}
        if cfg.remove_dropped_skills:
            for name in skills.dropped(cfg.home, package, checkout):
                removals.setdefault(name, f"no longer in {package}")
        for name, reason in removals.items():
            what = f"skill {name} ({reason})"
            key = f"skill:{name}"
            if pending.due(key, what) and not dry:
                skills.backup_installs(cfg.home, cfg.agents, [name])
                if tools.act(log, dry, f"remove {what}", tools.remove_args(cfg.skills_cli, name, enabled=cfg.agents)):
                    pending.removed(key)

    installed = skills.locked(cfg.home)
    for package, checkout, names in packages:
        shipped = skills.shipped(checkout) if names is None else names
        blocked = {skills.installed_name(name, meta) for name, meta in installed.items()
                   if meta.get("source") in cfg.skill_packages and meta.get("source") != package}
        if blocked.intersection(shipped):
            names = [name for name in shipped if name not in blocked]
        if names != []:
            if not dry:
                skills.backup_installs(cfg.home, cfg.agents, shipped if names is None else names)
            tools.act(log, dry, f"skills from {package} for {', '.join(tools.skill_agents(cfg.agents))}",
                      tools.skill_args(cfg.skills_cli, package, names, enabled=cfg.agents))
        if (checkout / "agents").is_dir():
            manifest = checkout / ".cursor-plugin/plugin.json"
            version = json.loads(manifest.read_text()).get("version", "0") if manifest.exists() else "0"
            try:
                log.lines(agents.mirror(checkout, package, cfg.home, cfg.agent_models, version, dry,
                                        cfg.agent_notes, enabled=cfg.agents))
                log("ok", f"agents from {package} mirrored")
            except (ValueError, OSError) as e:
                log("FAIL", f"agents from {package}: {e}")


def log_review(cfg: Config, log: Log) -> None:
    try:
        scan = review.scan(cfg.home, cfg)
        counts = Counter(group.status for group in scan.groups)
    except Exception as e:
        log("WARN", f"could not compare settings for review: {e}")
        return
    log.lines(scan.problems)
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
    scan = review.scan(cfg.home, cfg)
    for problem in scan.problems:
        print(problem, file=sys.stderr)
    groups = scan.groups
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
    with inventory.snapshot():
        return print_inventory(load())


def print_inventory(cfg: Config) -> int:
    home, enabled = cfg.home, cfg.agents
    npm = mcp.NpmLookup()
    latest = npm.latest
    seen = duplicates.views(home, enabled=enabled)
    problems: list[str] = []
    settings = {tool: entries for _, readers, _, _, entries in mcp.settings_files(home, enabled, problems)
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
    problems += (duplicates.report(home, cfg.allow_duplicate_skills, enabled=enabled)
                + plugins.twice(home, enabled=enabled)
                + mcp.same_server_twice(home, enabled=enabled)
                + mcp.pins(home, True, enabled=enabled, keep=cfg.keep_mcp_versions, latest=latest,
                           released=npm.released, major=cfg.mcp_major_updates))
    with pending.during(cfg.state_dir, cfg.keep_installed, lambda _, text: problems.append(text), True) as removals:
        problems.extend(mcp.dedupe(home, True, enabled=enabled, source=cfg.source))
        problems.extend(f"approval required to {pending.change(what)}" for what in removals.questions().values())
    for line in dict.fromkeys(problems or ["None"]):
        print(f"  {line}")
    return 0


def pending_command(approve: list[str], keep: list[str], ack: bool) -> int:
    cfg = load()
    with ExitStack() as stack:
        editing = bool(approve or keep or ack)
        if editing:
            if not stack.enter_context(run_lock(cfg.state_dir / "lock")):
                print("another agent-sync run is in progress. Try again when it finishes.", file=sys.stderr)
                return 1
            cfg = load()
        memory_prompt = memory.question(cfg)
        share, decline = memory.CHOICE in approve, memory.CHOICE in keep
        if share and decline:
            print("Choose either Share or Don't share for memory sharing.", file=sys.stderr)
            return 1
        if share or decline:
            if memory_prompt is None:
                print("There is no pending memory-sharing choice.", file=sys.stderr)
                return 1
            approve = [key for key in approve if key != memory.CHOICE]
            keep = [key for key in keep if key != memory.CHOICE]
        entries = pending.load(cfg.state_dir)
        unknown = sorted((set(approve) | set(keep)) - entries.keys())
        if unknown:
            print(f"Unknown pending removal key(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
        if share or decline or keep:
            stack.enter_context(backups.during(cfg.home, cfg.state_dir / "backups"))
        if share or decline:
            save_setting(cfg.home, "mirror_memory", share)
        if approve or keep:
            for key in approve:
                entries[key]["approved"] = True
            if keep:
                save_setting(cfg.home, "keep_installed", list(dict.fromkeys([*cfg.keep_installed, *keep])))
                for key in set(keep):
                    del entries[key]
            save_baselines(cfg.state_dir / pending.FILENAME, entries)
        if ack or not editing:
            acknowledgements = WarningAcks(cfg.state_dir)
            log = Log(cfg.log_file, write=False)
            warnings = acknowledgements.unseen(log.recent_warnings(cfg.log_days))
            if ack:
                acknowledgements.save(warnings)
            else:
                print("Pending removals")
                for key, item in sorted(entries.items()):
                    since = datetime.fromtimestamp(item["first"]).strftime("%Y-%m-%d %H:%M:%S")
                    approved = "yes" if item["approved"] else "no"
                    print(f"  {key} | {item['what']} | since {since} | approved {approved}")
                if not entries:
                    print("  None")
                if memory_prompt:
                    print(f"Pending memory sharing\n  {memory_prompt}\n"
                          "  agent-sync pending --approve memory-sharing\n"
                          "  agent-sync pending --keep memory-sharing")
                print("Unacknowledged recent warnings")
                for warning in warnings or ["None"]:
                    print(f"  {warning}")
    return 0


def status() -> int:
    cfg = load()
    stamp = cfg.state_dir / "last-run"
    print(f"agent-sync {__version__}  source of truth: {cfg.source}  packages: {', '.join(cfg.skill_packages)}")
    print(f"last run: {time.ctime(stamp.stat().st_mtime) if stamp.exists() else 'never'}")
    print(f"log: {cfg.log_file}")
    entries = backups.folder_entries(cfg.state_dir / "backups", lambda warning: print(f"WARN  {warning}"))
    size = sum(size for _, _, size in entries) / backups.MIB
    count = sum(folder for _, folder, _ in entries)
    print(f"backups: {size:.2f} MiB in {count} {'folder' if count == 1 else 'folders'}"
          f" (limits: {cfg.backup_max_mb} MiB and {cfg.backup_days} days)")
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
    p = sub.add_parser("pending", help="list or act on pending removals, memory sharing and warnings")
    p.add_argument("--approve", metavar="KEY", nargs="+", default=[], action="extend")
    p.add_argument("--keep", metavar="KEY", nargs="+", default=[], action="extend")
    p.add_argument("--ack", action="store_true", help="acknowledge all recent warnings")
    sub.add_parser("status", help="show the last run and versions")
    review_parser = sub.add_parser("review", help="compare settings and choose which versions to keep")
    review_parser.add_argument("--list", action="store_true", help="print the comparison and recommendations")
    review_parser.add_argument("--no-open", action="store_true", help="print the page URL without opening the browser")
    a = ap.parse_args(argv)
    if a.cmd == "agents":
        return membership.run(load(), a.agent_action, getattr(a, "ids", []))
    if a.cmd == "run":
        return run(a.force, a.echo, a.dry_run)
    if a.cmd == "pending":
        return pending_command(a.approve, a.keep, a.ack)
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
