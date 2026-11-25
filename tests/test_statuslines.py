import json
import shlex
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path

import pytest

from agent_sync import backups, cli, pending, statuslines, tools, writes
from agent_sync.config import Config, parse

PATHS = {
    "claude": ".claude/settings.json",
    "cursor": ".cursor/cli-config.json",
    "antigravity": ".gemini/antigravity-cli/settings.json",
    "codex": ".codex/config.toml",
}
CODEX = ["model-with-reasoning", "five-hour-limit", "weekly-limit", "context-remaining",
         "used-tokens", "estimated-thread-cost"]
CHECKOUT = Path(__file__).resolve().parents[1]


def write(home, tool, text):
    path = home / PATHS[tool]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def upkeep(home, *enabled, dry=False, keep=(), now=100):
    cfg = Config(home=home, agents=list(enabled), keep_installed=list(keep))
    root = cfg.state_dir / "backups"
    logs = []
    with (nullcontext() if dry else backups.during(home, root, cfg.backup_days)), pending.during(
        cfg.state_dir, cfg.keep_installed, lambda kind, line: logs.append((kind, line)), dry, now=now,
    ) as run:
        lines = statuslines.apply(home, dry, cfg, run)
    return lines, logs


def approve(monkeypatch, home, key):
    """What `agent-sync pending --approve KEY` (or a click in the dialog or the Inbox) records."""
    monkeypatch.setattr(cli, "load", lambda: Config(home=home))
    assert cli.main(["pending", "--approve", key]) == 0


def install_executable(directory):
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / "agent-sync-statusline"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


@pytest.mark.parametrize("tool", ["claude", "cursor", "antigravity"])
def test_adds_status_line_and_backs_up_settings(tmp_path, monkeypatch, tool):
    exe = install_executable(tmp_path / "bin path")
    monkeypatch.setattr(tools, "PATH", str(exe.parent))
    original = '{ "words": " café ", "permissions": {"allow": ["read"]}, "after": true }\n'
    path = write(tmp_path, tool, original)
    lines, logs = upkeep(tmp_path, tool)

    wanted = {"type": "command", "command": shlex.join([str(exe), tool]), "padding": 0}
    if tool == "cursor":
        wanted["updateIntervalMs"] = 1000
    changed = path.read_text(encoding="utf-8")
    data = json.loads(changed)
    assert data.pop("statusLine") == wanted
    assert list(data) == ["words", "permissions", "after"] and data == json.loads(original)
    assert "café" in changed
    assert len(lines) == 1 and not logs
    assert upkeep(tmp_path, tool) == ([], [])
    saved, = tmp_path.glob(f".local/state/agent-sync/backups/run-*/{PATHS[tool]}")
    assert saved.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("old", [
    "'/old path/agent-sync-statusline' claude",
    "/old/bin/python3 -m agent_sync.statusline",
])
def test_updates_managed_path_to_stable_homebrew_link(tmp_path, monkeypatch, old):
    cellar = install_executable(tmp_path / "Cellar/agent-sync/0.9.0/bin")
    stable = tmp_path / "bin/agent-sync-statusline"
    stable.parent.mkdir()
    stable.symlink_to(cellar)
    monkeypatch.setattr(tools, "PATH", str(cellar.parent))
    original = json.dumps({"statusLine": {"type": "command", "command": old}})
    path = write(tmp_path, "claude", original)
    assert upkeep(tmp_path, "claude")[1] == []
    configured = json.loads(path.read_text())["statusLine"]["command"]
    assert shlex.split(configured) == [str(stable), "claude"]
    saved, = tmp_path.glob(f".local/state/agent-sync/backups/run-*/{PATHS['claude']}")
    assert saved.read_text() == original


@pytest.mark.parametrize("tool,original", [
    ("claude", '{"statusLine":{"type":"command","command":"custom --own"}}'),
    ("codex", '[tui]\nstatus_line = ["model"] # keep this comment\nnotifications = true\n'),
    ("codex", 'tui."status_line" = ["model"] # keep this comment\ntui.notifications = true\n'),
])
def test_existing_status_line_requires_approval_and_keep_wins(tmp_path, monkeypatch, tool, original):
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "empty-bin"))
    path = write(tmp_path, tool, original)
    key = f"statusline:{tool}"
    lines, logs = upkeep(tmp_path, tool)
    assert not lines and len(logs) == 1 and logs[0][0] == "WARN"
    assert "approval required" in logs[0][1] and "status line" in logs[0][1]
    state_path = Config(home=tmp_path).state_dir / "pending-removals.json"
    assert json.loads(state_path.read_text())[key]["approved"] is False
    assert upkeep(tmp_path, tool, now=10_000)[0] == []           # waiting is not approving
    assert path.read_text() == original

    assert upkeep(tmp_path, tool, keep=[key]) == ([], [])        # keep: never asked, never changed
    assert path.read_text() == original
    assert list(tmp_path.glob(".local/state/agent-sync/backups/run-*")) == []

    upkeep(tmp_path, tool)
    approve(monkeypatch, tmp_path, key)
    lines, logs = upkeep(tmp_path, tool, dry=True)
    assert lines == [f"dry  would update {PATHS[tool]} ({'Codex' if tool == 'codex' else 'Claude Code'} status line)"]
    assert [kind for kind, _ in logs] == ["dry"] and path.read_text() == original

    assert len(upkeep(tmp_path, tool)[0]) == 1
    if tool == "codex":
        assert tomllib.loads(path.read_text())["tui"] == {"status_line": CODEX, "notifications": True}
        assert " # keep this comment" in path.read_text()
    else:
        assert shlex.split(json.loads(path.read_text())["statusLine"]["command"]) == [
            "/usr/bin/env", f"PYTHONPATH={CHECKOUT}", sys.executable, "-m", "agent_sync.statusline", "claude",
        ]
    saved, = tmp_path.glob(f".local/state/agent-sync/backups/run-*/{PATHS[tool]}")
    assert saved.read_text() == original
    assert upkeep(tmp_path, tool) == ([], [])


def test_approval_is_bound_to_the_replacement_that_was_shown(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "empty-bin"))
    path = write(tmp_path, "claude", '{"statusLine":{"type":"command","command":"original"}}')
    upkeep(tmp_path, "claude")
    approve(monkeypatch, tmp_path, "statusline:claude")
    changed = '{"statusLine":{"type":"command","command":"unreviewed"}}'
    path.write_text(changed)
    lines, logs = upkeep(tmp_path, "claude")
    assert not lines and logs[0][0] == "WARN" and "unreviewed" in logs[0][1]
    assert path.read_text() == changed


@pytest.mark.parametrize("text", [
    "# heading\n[tui]\nnotifications = true\n",
    "tui = { notifications = true }\n",
    "tui.notifications = true\n",
])
def test_adds_missing_codex_setting_and_skips_gemini(tmp_path, text):
    path = write(tmp_path, "codex", text)
    lines, logs = upkeep(tmp_path, "gemini", "codex")
    assert "info  Gemini CLI has no custom status line; skipped" in lines
    assert len(lines) == 2 and not logs
    assert tomllib.loads(path.read_text())["tui"] == {"notifications": True, "status_line": CODEX}
    assert not (tmp_path / ".gemini").exists()
    assert upkeep(tmp_path, "codex") == ([], [])


def test_dry_run_leaves_settings_and_state_absent(tmp_path):
    lines, logs = upkeep(tmp_path, "claude", "codex", "cursor", "antigravity", "gemini", dry=True)
    assert sum(line.startswith("dry  would create ") for line in lines) == 4
    assert "info  Gemini CLI has no custom status line; skipped" in lines
    assert not logs and list(tmp_path.iterdir()) == []


def test_leaves_settings_replaced_during_upkeep_alone(tmp_path, monkeypatch):
    path = write(tmp_path, "claude", "{}")
    custom = '{"statusLine":{"type":"command","command":"custom"}}'
    save = writes.backups.save

    def change_on_save(target):
        save(target)
        if target == path:
            path.write_text(custom)

    monkeypatch.setattr(writes.backups, "save", change_on_save)
    lines, logs = upkeep(tmp_path, "claude")
    assert len(lines) == 1 and "target changed while preparing the edit" in lines[0]
    assert not logs and path.read_text() == custom


def test_status_line_defaults_on_and_can_be_disabled(tmp_path):
    path = Path("config.toml")
    assert parse("", path, tmp_path).status_line is True
    cfg = parse("status_line = false", path, tmp_path)
    with pending.during(cfg.state_dir, [], lambda *_: None, False) as run:
        assert statuslines.apply(tmp_path, False, cfg, run) == []
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="status_line must be true or false"):
        parse('status_line = "false"', path, tmp_path)


def test_cli_approval_replaces_the_status_line_at_the_next_run(tmp_path, monkeypatch, capsys):
    cfg = Config(home=tmp_path, agents=["claude"], skill_packages=[], mirror_skills=False, mirror_roles=False,
                 mirror_mcp=False, update_plugins=False, update_mcp_programs=False, update_global_tools=False,
                 update_clis=False, update_self=False)
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(tools, "sh", lambda *_args, **_kwargs: (0, ""))
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "empty-bin"))
    path = write(tmp_path, "claude", '{"statusLine":{"type":"command","command":"custom"}}')
    assert cli.main(["run", "--force", "--echo"]) == 0
    assert json.loads(path.read_text())["statusLine"]["command"] == "custom"
    assert cli.main(["pending", "--approve", "statusline:claude"]) == 0
    assert cli.main(["run", "--force", "--echo"]) == 0
    command = json.loads(path.read_text())["statusLine"]["command"]
    result = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, check=True, text=True, capture_output=True,
                            input='{"model":{"display_name":"Opus"}}')
    assert "Opus" in result.stdout
