"""The schedule (hourly and at login; a run only works when the last one is 20+ hours old) and the launcher.

- macOS: a launchd agent, ~/Library/LaunchAgents/io.github.agent-sync.plist
- Linux: a systemd user timer, ~/.config/systemd/user/agent-sync.{service,timer}
- Launcher (macOS): ~/Desktop/Agent Upkeep.command, which runs the upkeep now with every step on screen. With
  `launcher = true` in the config, every run puts it back when it is missing and rewrites it when a new version
  writes it differently.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import __version__, backups

LABEL = "io.github.agent-sync"
OUTPUT_FILE = "launchd.out"   # where launchd sends the scheduled run's stdout and stderr
LAUNCHER_MARK = "# made by agent-sync install --launcher"


def command(path: str | None = None) -> tuple[list[str], str | None]:
    """How the schedule starts agent-sync, and the PYTHONPATH it needs (None: none).

    The `agent-sync` command on PATH (or on `path`) comes first: it is a link that stays put when Homebrew or pipx
    upgrade the package, while this Python's own path can change with every version. From a source checkout with
    no command installed, this Python runs this copy of the package."""
    exe = shutil.which("agent-sync", path=path)
    if exe:
        return [exe], None
    return [sys.executable, "-m", "agent_sync"], str(Path(__file__).resolve().parents[1])


def plist_path(home: Path) -> Path:
    return home / "Library/LaunchAgents" / f"{LABEL}.plist"


def systemd_dir(home: Path) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") if home == Path.home() else None
    return (Path(xdg) if xdg else home / ".config") / "systemd/user"


def started_this_run() -> bool:
    """True inside the launchd job (launchd names it in XPC_SERVICE_NAME) or a systemd service."""
    return os.environ.get("XPC_SERVICE_NAME") == LABEL or "INVOCATION_ID" in os.environ


def launcher_path(home: Path) -> Path:
    return home / "Desktop/Agent Upkeep.command"


def plist(log_out: Path) -> str:
    cmd, pythonpath = command()
    args = "".join(f"<string>{escape(a)}</string>" for a in [*cmd, "run"])
    env = (f"\n  <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>{escape(pythonpath)}</string></dict>"
           if pythonpath else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>{env}
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>{log_out}</string>
  <key>StandardErrorPath</key><string>{log_out}</string>
</dict></plist>
"""


def systemd_units() -> dict[str, str]:
    cmd, pythonpath = command()
    exec_start = " ".join(shlex.quote(a) for a in [*cmd, "run"])
    env = f'Environment="PYTHONPATH={pythonpath}"\n' if pythonpath else ""
    return {
        "agent-sync.service": f"""[Unit]
Description=agent-sync: keep Claude Code, Codex and Cursor in step
After=network-online.target

[Service]
Type=oneshot
{env}ExecStart={exec_start}
Nice=10
""",
        "agent-sync.timer": """[Unit]
Description=Run agent-sync hourly and soon after login (it works once every 20 hours)

[Timer]
OnStartupSec=2min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
""",
    }


def launcher(path: str | None = None) -> str:
    cmd, pythonpath = command(path)
    start = " ".join(shlex.quote(a) for a in cmd)
    if pythonpath:
        start = f"PYTHONPATH={shlex.quote(pythonpath)} {start}"
    prompt = ("Press r to review differences between the tools, s to change the settings, "
              "or any other key to close this window. ")
    return f"""#!/bin/bash
{LAUNCHER_MARK}
# Written by agent-sync {__version__}; agent-sync rewrites this file when a new version changes it.
# Double-click to run the upkeep now, with every step on screen.
printf '\\033]0;Agent Upkeep\\007'
{start} run --force --echo
status=$?
echo
[ "$status" -eq 0 ] && echo "Finished." || echo "Finished with problems (exit $status); see the lines above."
echo
read -n 1 -s -r -p "{prompt}" key
echo
case "$key" in
    r|R) {start} review ;;
    s|S) {start} config ;;
esac
"""


def refresh_launcher(home: Path, dry: bool = False, path: str | None = None) -> str | None:
    """Put the launcher on the Desktop, or rewrite it when this version writes it differently.
    Returns a change line, a "WARN ..." line when a file agent-sync did not make has its name, or None."""
    p = launcher_path(home)
    text = launcher(path)
    if p.exists() and LAUNCHER_MARK not in p.read_text(errors="replace"):
        return f"WARN  {p} has the Desktop launcher's name, but agent-sync did not make it; left alone"
    if p.exists() and p.read_text() == text:
        return None
    verb = ("would update" if p.exists() else "would add") if dry else ("updated" if p.exists() else "added")
    if not dry:
        p.parent.mkdir(parents=True, exist_ok=True)
        backups.save(p, follow=True)
        p.write_text(text)
        p.chmod(0o755)
    return f"{verb} the Desktop launcher {p}"


def install(home: Path, state_dir: Path, with_launcher: bool = False) -> list[str]:
    done = []
    if sys.platform == "darwin":
        p = plist_path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(plist(state_dir / OUTPUT_FILE))
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(p)], check=True)
        done.append(f"Scheduled: {p} (hourly and at login)")
    elif sys.platform.startswith("linux"):
        d = systemd_dir(home)
        d.mkdir(parents=True, exist_ok=True)
        for name, text in systemd_units().items():
            (d / name).write_text(text)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "agent-sync.timer"], check=True)
        done.append(f"Scheduled: {d}/agent-sync.timer (hourly and after login). "
                    "To keep it running while you are logged out: loginctl enable-linger")
    else:
        raise SystemExit(f"No scheduler support for {sys.platform}; run `agent-sync run` from your own scheduler.")
    if with_launcher:
        if sys.platform != "darwin":
            done.append("The Desktop launcher is macOS-only; run `agent-sync run --force --echo` instead.")
        else:
            line = refresh_launcher(home)
            if line and line.startswith("WARN"):
                done.append(line.removeprefix("WARN  "))
            else:
                done.append(f"Launcher: {launcher_path(home)} (double-click to run now; kept up to date by every run)")
    return done


def uninstall(home: Path) -> list[str]:
    done = []
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        if plist_path(home).exists():
            plist_path(home).unlink()
            done.append("Schedule removed.")
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "agent-sync.timer"], capture_output=True)
        for name in systemd_units():
            if (systemd_dir(home) / name).exists():
                (systemd_dir(home) / name).unlink()
                done.append(f"Removed {systemd_dir(home) / name}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    p = launcher_path(home)
    if p.exists() and LAUNCHER_MARK in p.read_text():
        p.unlink()
        done.append(f"Removed {p}")
    return done or ["Nothing was installed."]


def installed(home: Path) -> bool:
    if sys.platform == "darwin":
        return plist_path(home).exists()
    return (systemd_dir(home) / "agent-sync.timer").exists()
