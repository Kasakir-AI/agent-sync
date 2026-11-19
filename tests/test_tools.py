import os

from agent_sync import tools
from agent_sync.config import Config


def fake_cask(tmp_path, cmd, cask):
    """<prefix>/Caskroom/<cask>/1.0/<cmd>, linked from <prefix>/bin like Homebrew does."""
    prefix = tmp_path / "brew"
    real = prefix / "Caskroom" / cask / "1.0" / cmd
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\necho \"$0 $*\" >> \"$HOME/calls.log\"\necho 1.0\n")
    real.chmod(0o755)
    (prefix / "bin").mkdir()
    (prefix / "bin" / cmd).symlink_to(real)
    return prefix


def run_update(tmp_path, monkeypatch, dry):
    lines = []
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "brew/bin"))
    monkeypatch.setenv("HOME", str(tmp_path))
    tools.update_clis(lambda kind, msg: lines.append((kind, msg)), Config(home=tmp_path, update_clis=["cursor"]), dry)
    return lines


def test_homebrew_copy_is_recognised(tmp_path, monkeypatch):
    prefix = fake_cask(tmp_path, "cursor-agent", "cursor-cli")
    monkeypatch.setattr(tools, "PATH", str(prefix / "bin"))
    assert tools.homebrew_prefix("cursor-agent", "cursor-cli") == prefix
    assert tools.homebrew_prefix("cursor-agent", "claude-code") is None


def test_homebrew_of_another_account_is_left_alone(tmp_path, monkeypatch):
    prefix = fake_cask(tmp_path, "cursor-agent", "cursor-cli")
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/cli-config.json").write_text("{}")
    os.chmod(prefix / "Caskroom", 0o555)
    try:
        lines = run_update(tmp_path, monkeypatch, dry=False)
    finally:
        os.chmod(prefix / "Caskroom", 0o755)
    assert lines[-1][0] == "skip" and "another account owns" in lines[-1][1]
    assert "update" not in (tmp_path / "calls.log").read_text()     # its own updater never runs


def test_dry_run_does_not_start_a_cli_that_never_ran(tmp_path, monkeypatch):
    fake_cask(tmp_path, "cursor-agent", "cursor-cli")
    lines = run_update(tmp_path, monkeypatch, dry=True)
    assert lines == [("dry", "cursor-agent has never been started in this account; a real run would update it")]
    assert not (tmp_path / "calls.log").exists()
    assert tools.version("cursor-agent", tmp_path) == "installed, never started in this account"
