"""Offer removal choices and warning acknowledgements at the end of upkeep."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from queue import Empty, Queue
from threading import Thread

from . import __version__, pending, textmerge, tools
from .config import Config
from .log import Log

DIALOG_TIMEOUT = 12 * 3600
INBOX_TIMEOUT = 30
INBOX_FILE = "inbox-notifications.json"
REMOVAL_PROMPT = ("agent-sync asks before removing these or upgrading them to a new major version. Select what may "
                  "go ahead; anything not selected stays as it is and is asked about again next run.")
MACOS_CHOOSE = """
on run argv
    with timeout of 43200 seconds
        set selected to choose from list (rest of argv) with title "agent-sync" with prompt (item 1 of argv) ¬
            default items {} OK button name "Approve selected" cancel button name "Keep all for now" ¬
            with multiple selections allowed and empty selection allowed
    end timeout
    if selected is false then return ""
    set AppleScript's text item delimiters to tab
    set resultIDs to ""
    repeat with choice in selected
        set resultIDs to resultIDs & (first text item of (contents of choice)) & linefeed
    end repeat
    return resultIDs
end run
"""
MACOS_ALERT = """
on run argv
    with timeout of 43200 seconds
        set answer to display alert (item 1 of argv) message (item 2 of argv) buttons {"OK"} ¬
            default button "OK" giving up after 43200
    end timeout
    return answer
end run
"""
SHARE = "Share"
DECLINE = "Don't share"
MACOS_MEMORY = """
on run argv
    with timeout of 43200 seconds
        set answer to display dialog (item 1 of argv) with title "agent-sync" ¬
            buttons {"Don't share", "Share"} default button "Don't share" giving up after 43200
    end timeout
    if gave up of answer then return ""
    return button returned of answer
end run
"""


def in_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def find_command(name: str) -> str | None:
    return shutil.which(name, path=tools.PATH)


def _terminal(_log: Log, warnings: list[str], removals: dict[str, str]) -> tuple[set[str], bool]:
    for warning in warnings:
        print(f"Warning: {warning}")
    try:
        approved = {key for key, what in removals.items()
                    if input(f"{pending.question(what)} [y/N] ").strip().lower() == "y"}
        if warnings:
            input("Press Enter once you have read the warnings. ")
    except (EOFError, KeyboardInterrupt):
        return set(), False
    return approved, bool(warnings)


def _warning_title(count: int) -> str:
    return f"agent-sync: {count} new warning{'' if count == 1 else 's'}"


def _dialog(log: Log, args: list[str]) -> str | None:
    try:
        executable = find_command(args[0]) or args[0]
        result = subprocess.run([executable, *args[1:]], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=DIALOG_TIMEOUT, env={**os.environ, "PATH": tools.PATH})
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        log("info", f"{args[0]} dialog unavailable: {exc}")
        return None
    return result.stdout if result.returncode == 0 else None


def _macos(log: Log, warnings: list[str], removals: dict[str, str]) -> tuple[set[str], bool]:
    approved = set()
    if removals:
        keys = {str(i): key for i, key in enumerate(removals, 1)}
        choices = [f"{i}\t{removals[key]}" for i, key in keys.items()]
        selected = _dialog(log, ["osascript", "-e", MACOS_CHOOSE, "--", REMOVAL_PROMPT, *choices])
        approved = {keys[i] for i in (selected or "").splitlines() if i in keys}
    if not warnings:
        return approved, False
    body = "\n".join(warnings[:15])
    if len(warnings) > 15:
        body += f"\n\nand {len(warnings) - 15} more in {log.path}"
    reply = _dialog(log, ["osascript", "-e", MACOS_ALERT, "--", _warning_title(len(warnings)), body]) or ""
    clicked = re.search(r"(?:^|,)\s*button returned:\s*OK\s*(?:,|$)", reply)
    timed_out = re.search(r"(?:^|,)\s*gave up:\s*true\s*(?:,|$)", reply)
    return approved, bool(clicked and not timed_out)


def _linux(log: Log, warnings: list[str], removals: dict[str, str]) -> tuple[set[str], bool]:
    approved = set()
    if removals:
        keys = {json.dumps(key): key for key in removals}
        rows = [value for encoded, key in keys.items() for value in ("FALSE", encoded, removals[key])]
        selected = _dialog(log, ["zenity", "--list", "--checklist", "--title=agent-sync", f"--text={REMOVAL_PROMPT}",
                                 "--ok-label=Approve selected", "--cancel-label=Keep all for now", "--column=Approve",
                                 "--column=Key", "--column=Item", "--hide-column=2", "--print-column=2",
                                 "--separator=\n", f"--timeout={DIALOG_TIMEOUT}", "--", *rows])
        approved = {keys[key] for key in (selected or "").splitlines() if key in keys}
    if not warnings:
        return approved, False
    title, body = _warning_title(len(warnings)), "\n".join(warnings)
    clicked = _dialog(log, ["zenity", "--info", f"--title={title}", f"--text={body}", "--no-markup",
                            f"--timeout={DIALOG_TIMEOUT}"])
    return approved, clicked is not None


Channel = tuple[Callable[[], bool], Callable[[Log, list[str], dict[str, str]], tuple[set[str], bool]]]
CHANNELS: tuple[Channel, ...] = (
    (lambda: in_terminal(), _terminal),
    (lambda: sys.platform == "darwin" and bool(find_command("osascript")), _macos),
    (lambda: sys.platform.startswith("linux") and bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))
     and bool(find_command("zenity")), _linux),
)


def memory_choice(cfg: Config, log: Log, question: str) -> bool | None:
    try:
        if in_terminal():
            answer = input(f"{question} [y/N] ").strip().lower()
            return {"y": True, "yes": True, "n": False, "no": False, "": False}.get(answer)
        if not cfg.notify:
            return None
        if CHANNELS[1][0]():
            result = _dialog(log, ["osascript", "-e", MACOS_MEMORY, "--", question])
        elif CHANNELS[2][0]():
            result = _dialog(log, ["zenity", "--list", "--title=agent-sync", f"--text={question}",
                                   "--column=Choice", f"--timeout={DIALOG_TIMEOUT}", "--", SHARE, DECLINE])
        else:
            return None
        return {SHARE: True, DECLINE: False}.get((result or "").strip())
    except (EOFError, KeyboardInterrupt):
        return None


def memory_inbox(cfg: Config, log: Log, question: str, key: str) -> None:
    if cfg.notify and (command := find_command("plannotator")):
        buttons = [f":::run\n{label}\n\n```\n{_inbox_command(action, key)}\n```\n:::"
                   for action, label in (("approve", SHARE), ("keep", DECLINE))]
        _post_inbox(command, cfg, log, "\n\n".join([question, *buttons]), uuid.uuid4().hex)


def _inbox_command(action: str, key: str) -> str:
    if any(markup in key for markup in ("\n", "\r", "![", "<")):
        # Keep the argument literal on one Markdown line, including any trailing newline.
        escaped = "".join(f"\\0{byte:03o}" for byte in key.encode())
        command = f"key=$(printf '%b.' {shlex.quote(escaped)}); agent-sync pending --{action}"
        return command + ' "${key%.}"'
    return f"agent-sync pending --{action} {shlex.quote(key)}"


def _inbox_line(text: str) -> str:
    line = " ".join(text.splitlines())
    return line.replace("![", "!&#91;").replace("<", "&lt;")


def _inbox_body(warnings: list[str], removals: dict[str, str]) -> str:
    parts = ["\n".join(f"- {_inbox_line(warning)}" for warning in sorted(warnings))] if warnings else []
    for key, what in sorted(removals.items()):
        label = _inbox_line(what)
        for action, title, detail in (
            ("approve", f"Approve: {pending.change(label)}",
             "agent-sync asks before removing anything or installing a new major version. "
             "Press to let the next run do it."),
            ("keep", f"Keep: {label}", "Press to keep this item and stop asking about it."),
        ):
            parts.append(f":::run\n{title}\n{detail}\n\n```\n{_inbox_command(action, key)}\n```\n:::")
    return "\n\n".join(parts)


def _send_inbox(command: str, cfg: Config, arguments: dict[str, str]) -> None:
    deadline = time.monotonic() + INBOX_TIMEOUT
    process = subprocess.Popen([command, "inbox", "mcp"], cwd=cfg.home, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={**os.environ, "PATH": tools.PATH})
    outcome: Queue[Exception | None] = Queue()

    def send(method: str, params: dict | None = None, request: int | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if request is not None:
            message["id"] = request
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()

    def receive(request: int) -> dict:
        for line in process.stdout:
            reply = json.loads(line)
            if not isinstance(reply, dict) or reply.get("id") != request:
                continue
            value = reply.get("result")
            if "error" in reply or not isinstance(value, dict) or value.get("isError"):
                raise ValueError(f"MCP request {request} failed")
            return value
        raise ValueError("MCP server closed its output")

    def exchange() -> None:
        with process:
            try:
                send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "agent-sync", "version": __version__}}, 1)
                receive(1)
                send("notifications/initialized")
                send("tools/call", {"name": "send_message", "arguments": arguments}, 2)
                receive(2)
                if process.poll() not in (None, 0):
                    raise ValueError(f"MCP server exited with status {process.returncode}")
            except (OSError, ValueError) as exc:
                outcome.put(exc)
            else:
                outcome.put(None)

    # The stdio server waits for more requests after replying. Stop it once the exchange finishes.
    Thread(target=exchange, daemon=True).start()
    try:
        error = outcome.get(timeout=max(0, deadline - time.monotonic()))
        if error is not None:
            raise error
    except Empty as exc:
        raise TimeoutError(f"timed out after {INBOX_TIMEOUT} seconds") from exc
    finally:
        if process.poll() is None:
            process.kill()


def _inbox(command: str, cfg: Config, log: Log, warnings: list[str], removals: dict[str, str]) -> None:
    body = _inbox_body(warnings, removals)
    contents = json.dumps([sorted(warnings), sorted(removals.items())]).encode()
    digest = hashlib.sha256(contents).hexdigest()
    path = cfg.state_dir / INBOX_FILE
    if path.is_symlink():
        return
    seen = textmerge.load_baselines(path)
    if digest in seen:
        return
    if _post_inbox(command, cfg, log, body, digest):
        try:
            textmerge.save_baselines(path, {**seen, digest: time.time()})
        except (OSError, ValueError) as exc:
            log("info", f"Could not save Inbox notification history: {exc}")


def _post_inbox(command: str, cfg: Config, log: Log, body: str, digest: str) -> bool:
    try:
        _send_inbox(command, cfg, {"body": body, "thread": "agent-sync upkeep", "subject": "agent-sync upkeep",
                                   "agent_name": "agent-sync", "agent_host": "agent-sync",
                                   "project_path": str(cfg.home), "idempotency_key": digest})
    except (OSError, ValueError) as exc:
        log("info", f"Plannotator Inbox notification failed: {exc}")
        return False
    return True


def deliver(cfg: Config, log: Log, warnings: list[str], removals: dict[str, str]) -> tuple[set[str], bool]:
    """Return approved removal keys and whether the supplied warnings were acknowledged."""
    if not warnings and not removals:
        return set(), False
    channels = CHANNELS if cfg.notify else CHANNELS[:1]
    interact = next((handler for available, handler in channels if available()), None)
    try:
        result = interact(log, warnings, removals) if interact else (set(), False)
    except KeyboardInterrupt:
        result = (set(), False)
    if cfg.notify and (command := find_command("plannotator")):
        _inbox(command, cfg, log, warnings, removals)
    return result
