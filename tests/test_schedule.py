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
    assert f"agent-sync {schedule.__version__}" in text and " config\n" in text   # s opens the settings


def test_launcher_is_added_kept_current_and_never_replaces_a_stranger(tmp_path, monkeypatch):
    p = schedule.launcher_path(tmp_path)
    assert schedule.refresh_launcher(tmp_path, dry=True) == f"would add the Desktop launcher {p}"
    assert not p.exists()
    assert schedule.refresh_launcher(tmp_path) == f"added the Desktop launcher {p}"
    assert p.stat().st_mode & 0o111 and schedule.refresh_launcher(tmp_path) is None
    monkeypatch.setattr(schedule, "__version__", "99.0")              # a new version writes it differently
    assert schedule.refresh_launcher(tmp_path) == f"updated the Desktop launcher {p}"
    assert "agent-sync 99.0" in p.read_text()
    p.write_text("#!/bin/bash\necho mine\n")
    assert schedule.refresh_launcher(tmp_path) == (f"WARN  {p} has the Desktop launcher's name, but agent-sync "
                                                   "did not make it; left alone")
    assert p.read_text() == "#!/bin/bash\necho mine\n"


def test_save_setting_keeps_the_file(tmp_path):
    from agent_sync.config import config_path, save_setting
    path = config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("# mine\nsource = \"claude\"\n")
    save_setting(tmp_path, "launcher", True)
    assert path.read_text() == "# mine\nsource = \"claude\"\nlauncher = true\n"
    assert load(path, home=tmp_path).launcher is True


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
