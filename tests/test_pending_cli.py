import json
import os
import shlex
import subprocess
import time
from hashlib import sha256

import pytest

from agent_sync import cli, notify, pending
from agent_sync.config import Config, load
from agent_sync.locking import MAX_AGE, run_lock
from agent_sync.log import ACK_AGE, Log
from tests.test_notify import available, inbox


@pytest.fixture
def upkeep(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path, skill_packages=[], mirror_skills=False, mirror_roles=False, mirror_mcp=False,
                 update_plugins=False, update_clis=False, update_mcp_programs=False, update_self=False)
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.membership, "available", lambda cfg: {})
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test"))
    return cfg


def test_pending_command_lists_approves_keeps_and_acknowledges(upkeep, capsys):
    cfg = upkeep
    state = cfg.state_dir / pending.FILENAME
    log = Log(cfg.log_file)
    warning = 'something changed\nCheck the next line, too.'
    log("WARN", warning)
    with pending.during(cfg.state_dir, [], log, False, now=time.time()):
        for key in ("skill:approve", "plugin:codex:keep"):
            assert not pending.due(key, f"example {key}")

    assert cli.main(["pending"]) == 0
    output = capsys.readouterr().out
    assert "skill:approve | example skill:approve | since " in output
    assert "plugin:codex:keep | example plugin:codex:keep | since " in output
    assert output.count("approved no") == 2
    assert warning in output

    before = state.read_bytes()
    assert cli.main(["pending", "--approve", "skill:approve", "missing", "--ack"]) == 1
    assert "Unknown pending removal key(s): missing" in capsys.readouterr().err
    assert state.read_bytes() == before
    assert not (cfg.state_dir / "acknowledged-warnings.json").exists()
    assert cli.main(["pending", "--keep", "unknown"]) == 1

    assert cli.main(["pending", "--approve", "skill:approve", "--keep", "plugin:codex:keep", "--ack"]) == 0
    assert load(home=cfg.home).keep_installed == ["plugin:codex:keep"]
    assert cli.main(["pending"]) == 0
    output = capsys.readouterr().out
    assert "skill:approve" in output and "approved yes" in output
    assert "plugin:codex:keep" not in output and warning not in output
    assert json.loads(state.read_text())["skill:approve"]["approved"] is True
    stored = json.loads((cfg.state_dir / "acknowledged-warnings.json").read_text())
    assert sha256(warning.encode()).hexdigest() in stored
    assert all(time.time() - 60 < stamp <= time.time() for stamp in stored.values())


@pytest.fixture
def removable_items(upkeep, monkeypatch):
    paths = {f"skill:{name}": upkeep.home / name for name in ("first", "second")}
    for path in paths.values():
        path.touch()

    def remove(cfg, log, work, dry):
        for key, path in paths.items():
            if path.exists() and pending.due(key, f"example {path.name}") and not dry:
                path.unlink()
                pending.removed(key)

    monkeypatch.setattr(cli, "sync_packages", remove)
    return paths


@pytest.mark.parametrize("channel", ["terminal", "osascript", "zenity"])
def test_run_applies_choice_next_run_and_only_repeats_unacknowledged_warnings(
        channel, upkeep, removable_items, monkeypatch, capsys):
    original = "Settings differ"
    warning = [original]
    monkeypatch.setattr(cli, "log_review", lambda cfg, log: log("WARN", warning[0]))
    monkeypatch.setattr(notify, "in_terminal", lambda: channel == "terminal")
    monkeypatch.setattr(notify, "find_command", lambda name: name if channel == name else None)
    monkeypatch.setenv("DISPLAY", ":0")
    if channel != "terminal":
        monkeypatch.setattr(notify.sys, "platform", "darwin" if channel == "osascript" else "linux")

    prompts = []
    dialogs = []
    lock = upkeep.state_dir / "lock"

    def assert_locked():
        stale = time.time() - MAX_AGE - 1
        os.utime(lock, (stale, stale))
        with run_lock(lock) as held:
            assert not held
            assert lock.is_dir()

    def answer(prompt):
        assert_locked()
        prompts.append(prompt)
        return "y" if "example first" in prompt else ""

    def dialog(args, **_):
        assert_locked()
        dialogs.append(args)
        if channel == "osascript":
            reply = "1\n" if "choose from list" in args[2] and any("example first" in a for a in args) else ""
            if "display alert" in args[2]:
                reply = "button returned:OK, gave up:false"
        else:
            reply = '"skill:first"\n' if "--list" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=reply)

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(notify.subprocess, "run", dialog)

    assert cli.main(["run", "--force"]) == 0
    assert all(path.exists() for path in removable_items.values())
    assert {key: item["approved"] for key, item in pending.load(upkeep.state_dir).items()} == {
        "skill:first": True, "skill:second": False,
    }
    first_output = capsys.readouterr().out + str(dialogs)
    assert original in first_output
    assert not lock.exists()
    dialogs.clear()
    prompts.clear()

    assert cli.main(["run", "--force"]) == 0
    assert not removable_items["skill:first"].exists() and removable_items["skill:second"].exists()
    assert list(pending.load(upkeep.state_dir)) == ["skill:second"]
    assert original not in capsys.readouterr().out + str(dialogs)
    assert not any("Press Enter" in prompt for prompt in prompts)
    dialogs.clear()

    warning[0] = "Different settings differ"
    assert cli.main(["run", "--force"]) == 0
    assert warning[0] in capsys.readouterr().out + str(dialogs)
    assert removable_items["skill:second"].exists()


def test_dry_run_prints_without_prompting_posting_or_writing(upkeep, removable_items, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("dry run tried to notify")

    monkeypatch.setattr(notify, "find_command", unexpected)
    monkeypatch.setattr(notify, "deliver", unexpected)
    monkeypatch.setattr("builtins.input", unexpected)
    monkeypatch.setattr(cli, "log_review", lambda cfg, log: log("WARN", "Settings differ"))
    assert cli.main(["run", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Would ask: Remove example first? [y/N]" in output
    assert "Would ask: Remove example second? [y/N]" in output
    assert "Settings differ" in output and "Would ask: Press Enter" in output
    assert not upkeep.state_dir.exists()
    assert not upkeep.log_file.exists()
    assert all(path.exists() for path in removable_items.values())


def test_inbox_button_approves_one_item_for_the_next_run(upkeep, removable_items, monkeypatch):
    available(monkeypatch, "plannotator")
    processes = inbox(monkeypatch, {"result": {}}, {"result": {}})
    monkeypatch.setattr(notify, "in_terminal", lambda: False)

    assert cli.main(["run", "--force"]) == 0
    first = processes[0].messages[-1]["params"]["arguments"]["body"]
    commands = [shlex.split(line) for line in first.splitlines() if line.startswith("agent-sync pending")]
    approve_first = next(command for command in commands if command[-2:] == ["--approve", "skill:first"])
    assert cli.main(approve_first[1:]) == 0
    assert all(path.exists() for path in removable_items.values())
    assert not (upkeep.state_dir / "acknowledged-warnings.json").exists()

    assert cli.main(["run", "--force"]) == 0
    assert not removable_items["skill:first"].exists() and removable_items["skill:second"].exists()
    assert list(pending.load(upkeep.state_dir)) == ["skill:second"]


def test_expired_warning_acknowledgement_is_removed(upkeep, monkeypatch, capsys):
    state = upkeep.state_dir / "acknowledged-warnings.json"
    state.parent.mkdir(parents=True)
    warning = "Still relevant"
    digest = sha256(warning.encode()).hexdigest()
    state.write_text(json.dumps({digest: time.time() - ACK_AGE - 1}))
    monkeypatch.setattr(cli, "log_review", lambda cfg, log: log("WARN", warning))
    assert cli.main(["run", "--force", "--echo"]) == 0
    assert warning in capsys.readouterr().out
    assert json.loads(state.read_text()) == {}


def test_a_failing_step_is_logged_the_run_goes_on_and_pending_items_stay(upkeep, monkeypatch, capsys):
    cfg = upkeep
    cfg.mirror_roles = True
    cfg.mirror_memory = True
    with pending.during(cfg.state_dir, [], lambda *_: None, False, now=time.time()):
        assert not pending.due("memory:old.md", "memory copy old.md")

    def broken(*args, **kwargs):
        raise json.JSONDecodeError("bad", "{", 0)

    monkeypatch.setattr(cli.roles, "mirror", broken)
    monkeypatch.setattr(cli.memory, "mirror", broken)
    monkeypatch.setattr(cli.notify, "deliver", lambda *args: (set(), False))
    assert cli.main(["run", "--force", "--echo"]) == 1
    output = capsys.readouterr().out
    assert "FAIL  model roles: bad" in output
    assert (cfg.home / ".claude/settings.json").exists()             # the status line step still ran
    assert (cfg.state_dir / "last-run").exists()
    assert "memory:old.md" in pending.load(cfg.state_dir)            # its owner may not have run: kept

    monkeypatch.setattr(cli.roles, "mirror", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli.memory, "mirror", lambda *args, **kwargs: [])
    assert cli.main(["run", "--force", "--echo"]) == 0
    assert "memory:old.md" not in pending.load(cfg.state_dir)        # a clean run asked nothing about it


def test_disabled_step_retains_approval_and_completed_owner_expires_it(upkeep, monkeypatch):
    cfg = upkeep
    key = "memory:old.md"
    old = cfg.home / "old.md"
    old.write_text("old")
    monkeypatch.setattr(notify, "deliver", lambda *args: (set(), False))

    def mirror(*args, **kwargs):
        if old.exists() and pending.due(key, "memory copy old.md"):
            old.unlink()
            pending.removed(key)
        return []

    monkeypatch.setattr(cli.memory, "mirror", mirror)
    cfg.mirror_memory = True
    assert cli.main(["run", "--force"]) == 0
    assert cli.main(["pending", "--approve", key]) == 0
    old.unlink()

    cfg.mirror_memory = False
    assert cli.main(["run", "--force"]) == 0
    assert pending.load(cfg.state_dir)[key]["approved"]

    cfg.mirror_memory = True
    monkeypatch.setattr(cli.tools, "extra_steps", lambda log, *args: log("FAIL", "unrelated failure"))
    assert cli.main(["run", "--force"]) == 1
    assert key not in pending.load(cfg.state_dir)
    old.write_text("new copy")
    assert cli.main(["run", "--force"]) == 1
    assert old.read_text() == "new copy"
    assert not pending.load(cfg.state_dir)[key]["approved"]


@pytest.mark.parametrize("delete_file", [False, True])
def test_malformed_mcp_file_preserves_only_its_own_stale_approval(upkeep, monkeypatch, delete_file):
    from tests.test_inventory import codex_plugin, write

    cfg = upkeep
    cfg.mirror_mcp = True
    cfg.status_line = False
    monkeypatch.setattr(notify, "deliver", lambda *args: (set(), False))
    url = "https://example.com/mcp"
    server = {"url": url}
    root = codex_plugin(cfg.home, "pack@remote", "1.0.0", remote=True)
    write(root, ".mcp.json", {"pack": server})
    write(cfg.home, ".cursor/plugins/local/pack/mcp.json", {"pack": server})
    codex_entry = f'[mcp_servers.pack]\nurl = "{url}"\n'
    codex_path = write(cfg.home, ".codex/config.toml", codex_entry)
    cursor_path = write(cfg.home, ".cursor/mcp.json", {"mcpServers": {"pack": server}})
    codex_key, cursor_key = "mcp:codex:pack", "mcp:cursor:pack"

    assert cli.main(["run", "--force"]) == 0
    assert cli.main(["pending", "--approve", codex_key, cursor_key]) == 0
    if delete_file:
        codex_path.unlink()
    else:
        codex_path.write_text("")
    cursor_path.write_text("{")

    assert cli.main(["run", "--force"]) == 1
    approvals = pending.load(cfg.state_dir)
    assert set(approvals) == {cursor_key}
    assert approvals[cursor_key]["approved"]

    codex_path.write_text(codex_entry)
    assert cli.main(["run", "--force"]) == 1
    assert codex_path.read_text() == codex_entry
    assert not pending.load(cfg.state_dir)[codex_key]["approved"]


def test_launcher_and_npm_cache_failures_leave_later_steps_running(upkeep, monkeypatch):
    upkeep.launcher = True
    upkeep.mirror_mcp = True
    monkeypatch.setattr(Config, "macos", property(lambda _: True))

    def unavailable(*args):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(cli.schedule, "refresh_launcher", unavailable)
    monkeypatch.setattr(cli.mcp, "NpmLookup", unavailable)
    monkeypatch.setattr(notify, "deliver", lambda *args: (set(), False))
    assert cli.main(["run", "--force"]) == 1
    log = upkeep.log_file.read_text()
    assert "FAIL  Desktop launcher: read-only filesystem" in log
    assert "FAIL  MCP versions: read-only filesystem" in log
    assert (upkeep.home / ".claude/settings.json").is_file()


def test_pending_lists_only_warnings_the_latest_run_still_gives(upkeep, capsys):
    log = Log(upkeep.log_file)
    log("WARN", "instructions are on hold until you approve or keep the deletion of 1 line(s)")
    log("WARN", "Codex loads plugin visualize twice")
    log("done", "agent-sync finished: 1 change")
    log("WARN", "Codex loads plugin visualize twice")
    log("done", "agent-sync finished: no changes")
    log("WARN", "a warning after the last run")

    assert cli.main(["pending"]) == 0
    output = capsys.readouterr().out
    assert "on hold" not in output
    assert "Codex loads plugin visualize twice" in output and "a warning after the last run" in output


def test_a_failed_check_keeps_the_warnings_it_could_not_repeat(upkeep, capsys):
    log = Log(upkeep.log_file)
    log("WARN", "instructions are on hold until you approve or keep the deletion of 1 line(s)")
    log("done", "agent-sync finished: 1 change")
    log("FAIL", "instructions: could not read a file")
    log("done", "agent-sync finished: no changes, 1 problem")

    assert cli.main(["pending"]) == 0
    assert "on hold" in capsys.readouterr().out


def test_a_later_failed_run_does_not_bring_back_a_cleared_warning(upkeep, capsys):
    log = Log(upkeep.log_file)
    log("WARN", "instructions are on hold until you approve or keep the deletion of 1 line(s)")
    log("done", "agent-sync finished: 1 change")
    log("done", "agent-sync finished: no changes")
    log("FAIL", "plugins: could not update")
    log("done", "agent-sync finished: no changes, 1 problem")

    assert cli.main(["pending"]) == 0
    assert "on hold" not in capsys.readouterr().out
