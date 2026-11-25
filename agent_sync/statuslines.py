from __future__ import annotations

import json
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

from . import pending, tomledit, tools, writes
from .config import Config
from .registry import TOOLS

COMMAND = "agent-sync-statusline"
MODULE = "agent_sync.statusline"
JSON_FILES = {
    "claude": ".claude/settings.json",
    "cursor": ".cursor/cli-config.json",
    "antigravity": ".gemini/antigravity-cli/settings.json",
}
CODEX_ITEMS = [
    "model-with-reasoning", "five-hour-limit", "weekly-limit", "context-remaining",
    "used-tokens", "estimated-thread-cost",
]
MISSING = object()


def executable() -> list[str]:
    found = shutil.which(COMMAND, path=tools.PATH)
    if not found:
        command = [sys.executable, "-m", MODULE]
        checkout = Path(__file__).resolve().parents[1]
        if (checkout / "pyproject.toml").is_file():
            return ["/usr/bin/env", f"PYTHONPATH={checkout}", *command]
        return command
    path = Path(found).absolute()
    for parent in path.parents:
        if parent.name == "Cellar":
            found = shutil.which(COMMAND, path=str(parent.parent / "bin")) or str(path)
            break
    return [str(Path(found).absolute())]


def managed(value: object) -> bool:
    if not isinstance(value, dict) or value.get("type") != "command" or not isinstance(value.get("command"), str):
        return False
    try:
        args = shlex.split(value["command"])
    except ValueError:
        return False
    if len(args) >= 2 and args[0] == "/usr/bin/env" and args[1].startswith("PYTHONPATH="):
        args = args[2:]
    if not args:
        return False
    program = Path(args[0]).name
    if program == COMMAND:
        return len(args) == 1 or len(args) == 2 and args[1] in (*JSON_FILES, "generic")
    return (args[0] == sys.executable or program.startswith(("python", "pypy"))) and (
        args[1:3] == ["-m", MODULE] and (len(args) == 3 or len(args) == 4 and args[3] in (*JSON_FILES, "generic"))
    )


def approved(run: pending.Run, tool: str, current: object, wanted: object) -> bool:
    if current is MISSING or managed(current):
        return True
    command = current.get("command", current) if isinstance(current, dict) else current
    label = command if isinstance(command, str) and command.isprintable() else repr(command)
    what = f"your {TOOLS[tool].name} status line ({label}); agent-sync's replaces it"
    return run.due(f"statusline:{tool}", what)


def codex_update(text: str, run: pending.Run) -> str:
    tui = tomllib.loads(text).get("tui", {})
    if not isinstance(tui, dict):
        raise ValueError("expected tui to be a table")
    current = tui.get("status_line", MISSING)
    if current == CODEX_ITEMS or not approved(run, "codex", current, CODEX_ITEMS):
        return text
    root = tomledit.sections(text)[0]
    if tomledit.has_value(text, None, "tui"):
        new = tomledit.set_value(text, None, "tui", {**tui, "status_line": CODEX_ITEMS})
    elif any(parts[0] == "tui" for parts, *_ in root.dotted):
        new = tomledit.set_value(text, None, ("tui", "status_line"), CODEX_ITEMS)
    else:
        new = tomledit.set_value(text, ("tui",), "status_line", CODEX_ITEMS)
    tomllib.loads(new)
    return new


def json_update(text: str, tool: str, command: list[str], run: pending.Run) -> str:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    wanted = {"type": "command", "command": shlex.join([*command, tool]), "padding": 0}
    if tool == "cursor":
        wanted["updateIntervalMs"] = 1000
    current = data.get("statusLine", MISSING)
    if current == wanted or not approved(run, tool, current, wanted):
        return text
    data["statusLine"] = wanted
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def apply(home: Path, dry: bool, cfg: Config, run: pending.Run) -> list[str]:
    if not cfg.status_line:
        return []
    lines = []
    command = executable()
    for tool in cfg.agents:
        if tool == "gemini":
            lines.append("info  Gemini CLI has no custom status line; skipped")
            continue
        path = home / (".codex/config.toml" if tool == "codex" else JSON_FILES[tool])
        created = not path.exists()

        def change(text: str, tool=tool) -> str:
            return codex_update(text, run) if tool == "codex" else json_update(text, tool, command, run)

        try:
            if dry:
                text = path.read_text() if path.exists() else ("" if tool == "codex" else "{}")
                if change(text) != text:
                    verb = "create" if created else "update"
                    lines.append(f"dry  would {verb} {path.relative_to(home)} ({TOOLS[tool].name} status line)")
            elif writes.update_text(path, change, new_mode=0o600, empty="" if tool == "codex" else "{}"):
                lines.append(f"{'created' if created else 'updated'} {path.relative_to(home)}")
        except writes.Raced as raced:
            pending.incomplete()
            lines.append(f"WARN  {raced}")
        except (OSError, ValueError) as error:
            lines.append(f"FAIL  {TOOLS[tool].name} status line: {error}")
    return lines
