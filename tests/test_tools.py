import json
import os

from agent_sync import pending, tools
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


def test_how_agent_sync_was_installed(tmp_path):
    cellar = tmp_path / "opt/homebrew/Cellar/agent-sync/0.3.0/libexec"
    cellar.mkdir(parents=True)
    assert tools.how_installed(str(cellar)) == ("homebrew", os.path.realpath(tmp_path / "opt/homebrew"))
    pipx = tmp_path / ".local/pipx/venvs/agent-sync"
    pipx.mkdir(parents=True)
    assert tools.how_installed(str(pipx))[0] == "pipx"


def run_update_self(monkeypatch, kind, where, outdated=""):
    calls, lines = [], []
    monkeypatch.setattr(tools, "how_installed", lambda: (kind, where))
    monkeypatch.setattr(tools, "brew_writable", lambda prefix: True)

    def sh(args, timeout=900, cwd=None):
        calls.append(args)
        if args[:2] == ["brew", "outdated"]:
            return 0, outdated
        if args[-1] == "--version":
            return 0, "agent-sync 9.9.9"
        return 0, ""
    monkeypatch.setattr(tools, "sh", sh)
    upgraded = tools.update_self(lambda kind, text: lines.append(f"{kind} {text}"))
    return upgraded, calls, lines


def test_update_self_homebrew(monkeypatch):
    upgraded, calls, lines = run_update_self(monkeypatch, "homebrew", "/opt/homebrew")
    assert not upgraded and ["brew", "upgrade", "--formula", "agent-sync"] not in calls
    assert lines[-1] == f"ok agent-sync {tools.__version__} is current (Homebrew)"
    upgraded, calls, lines = run_update_self(monkeypatch, "homebrew", "/opt/homebrew", outdated="agent-sync")
    assert upgraded and ["brew", "upgrade", "--formula", "agent-sync"] in calls
    assert lines[-1] == f"info agent-sync: {tools.__version__} -> 9.9.9"


def test_update_self_other_installs(monkeypatch):
    _, calls, _ = run_update_self(monkeypatch, "pipx", "")
    assert ["pipx", "upgrade", "agent-sync"] in calls
    tags = "a\trefs/tags/v0.9.0\nb\trefs/tags/v0.10.0\nc\trefs/tags/v0.10.0-rc1\n"
    monkeypatch.setattr(tools, "raw", lambda args, timeout=120: tags)
    _, calls, _ = run_update_self(monkeypatch, "git", "https://github.com/o/agent-sync.git")
    assert calls[0][-1] == "git+https://github.com/o/agent-sync.git@v0.10.0"      # the newest release, not main
    _, calls, _ = run_update_self(monkeypatch, "pipx", "https://github.com/o/agent-sync.git")
    assert calls[0] == ["pipx", "install", "--force", "git+https://github.com/o/agent-sync.git@v0.10.0"]
    monkeypatch.setattr(tools, "raw", lambda args, timeout=120: "")
    upgraded, calls, lines = run_update_self(monkeypatch, "git", "https://github.com/o/agent-sync.git")
    assert not upgraded and calls == [] and "it only updates to a release" in lines[0]
    upgraded, calls, lines = run_update_self(monkeypatch, "other", "it runs from a source checkout")
    assert not upgraded and calls == []
    assert lines == ["skip agent-sync can't update itself: it runs from a source checkout"]


def prerequisites(monkeypatch, state_dir, formulae, keep=()):
    """Run the git and Node step against a fake `brew outdated --json=v2`; returns (brew upgrades, log, questions)."""
    calls, lines = [], []
    monkeypatch.setattr(tools.shutil, "which",
                        lambda cmd, path=None: "/opt/homebrew/bin/brew" if cmd == "brew" else None)
    monkeypatch.setattr(tools, "brew_writable", lambda prefix: True)
    monkeypatch.setattr(tools, "sh", lambda args, timeout=900, cwd=None: calls.append(args) or (0, ""))
    report = json.dumps({"formulae": [{"name": name, "installed_versions": [old], "current_version": new,
                                       "pinned": False} for name, old, new in formulae], "casks": []})
    monkeypatch.setattr(tools, "raw", lambda args, timeout=120: report if args[:2] == ["brew", "outdated"] else "")
    log = lambda kind, text: lines.append(f"{kind} {text}")     # noqa: E731
    with pending.during(state_dir, list(keep), log, False) as run:
        tools.update_prerequisites(log, dry=False)
        questions = run.questions()
    return [args for args in calls if args[:2] == ["brew", "upgrade"]], lines, questions


def test_git_and_node_upgrade_within_their_major_version(monkeypatch, tmp_path):
    upgraded, lines, questions = prerequisites(
        monkeypatch, tmp_path, [("node", "25.1.0", "25.2.0"), ("wget", "1.24", "1.25"), ("git", "2.50.0", "2.51.0")])
    assert upgraded == [["brew", "upgrade", "--formula", "git"], ["brew", "upgrade", "--formula", "node"]]
    assert lines == ["ran git 2.50.0 -> 2.51.0 (Homebrew)", "ran node 25.1.0 -> 25.2.0 (Homebrew)"]
    assert questions == {}


def test_a_new_node_major_version_waits_for_approval(monkeypatch, tmp_path):
    upgraded, _, questions = prerequisites(monkeypatch, tmp_path / "a", [("node", "25.4.0", "26.0.0")])
    assert upgraded == []
    assert questions == {"upgrade:homebrew:node@26": "upgrade node 25.4.0 -> 26.0.0 "
                                                     "(a new major version, which can include breaking changes)"}
    kept = prerequisites(monkeypatch, tmp_path / "b", [("node", "25.4.0", "26.0.0")], keep=["upgrade:homebrew:node"])
    assert kept[0] == [] and kept[2] == {}
