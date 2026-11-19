import plistlib

from agent_sync import checks, schedule
from agent_sync.config import load


def test_plist_and_systemd_units_are_valid(tmp_path):
    p = plistlib.loads(schedule.plist(tmp_path / "out").encode())
    assert p["Label"] == schedule.LABEL and p["ProgramArguments"][-1] == "run"
    assert p["StartInterval"] == 3600 and p["RunAtLoad"]
    units = schedule.systemd_units()
    assert "ExecStart=" in units["agent-sync.service"] and units["agent-sync.service"].rstrip().endswith("Nice=10")
    assert "OnUnitActiveSec=1h" in units["agent-sync.timer"] and "Persistent=true" in units["agent-sync.timer"]


def test_launcher_runs_with_echo_and_force():
    text = schedule.launcher()
    assert text.startswith("#!/bin/bash\n" + schedule.LAUNCHER_MARK) and "run --force --echo" in text


def test_unknown_setting_is_an_error(tmp_path):
    (tmp_path / "c.toml").write_text("mirror_skils = false\n")
    try:
        load(tmp_path / "c.toml", home=tmp_path)
    except ValueError as e:
        assert "mirror_skils" in str(e)
    else:
        raise AssertionError("a typo in a setting name should not be ignored")


def test_unwanted_models_are_reported(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('model = "some-model-preview"\n')
    assert checks.unwanted_models(tmp_path, ["preview"]) == [
        f"{tmp_path / '.codex/config.toml'} names preview (listed in warn_models)"]
    assert checks.unwanted_models(tmp_path, []) == []


def test_schedule_prefers_the_installed_command(tmp_path, monkeypatch):
    exe = tmp_path / "agent-sync"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert schedule.command() == ([str(exe)], None)
    assert "PYTHONPATH" not in schedule.plist(tmp_path / "out") and "PYTHONPATH" not in schedule.launcher()
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    cmd, pythonpath = schedule.command()
    assert cmd[1:] == ["-m", "agent_sync"] and pythonpath and "PYTHONPATH" in schedule.plist(tmp_path / "out")
