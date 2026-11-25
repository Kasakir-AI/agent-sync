import sys

import pytest

from agent_sync import setup, tools
from agent_sync.config import Config


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A Mac whose PATH holds only what a test puts in tmp_path/bin."""
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(setup, "has_git", lambda: True)
    monkeypatch.setattr(setup, "is_admin", lambda: True)
    return tmp_path


def fake_brew(tmp_path, writable=True):
    prefix = tmp_path / "brew"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "Caskroom").mkdir()
    brew = prefix / "bin/brew"
    brew.write_text(f"#!/bin/sh\n[ \"$1\" = --prefix ] && echo {prefix}\n")
    brew.chmod(0o755)
    (tmp_path / "bin/brew").symlink_to(brew)
    if not writable:
        (prefix / "Caskroom").chmod(0o555)
    return prefix


def labels(steps):
    return [(s.item, s.command[0] if s.command else None) for s in steps]


def test_empty_mac_gets_homebrew_then_everything_from_it(mac):
    steps = setup.plan(Config(home=mac))
    assert [s.item for s in steps] == ["homebrew", "node", "claude", "codex", "cursor"]
    assert steps[0].interactive and "Homebrew/install" in steps[0].command[-1]
    assert steps[2].command[-2:] == ["--cask", "claude-code"] and steps[4].command[-1] == "cursor-cli"


def test_homebrew_of_another_account_means_official_installers(mac):
    prefix = fake_brew(mac, writable=False)
    try:
        steps = setup.plan(Config(home=mac))
    finally:
        (prefix / "Caskroom").chmod(0o755)
    assert steps[0].item == "homebrew" and steps[0].command is None and "another account" in steps[0].advice
    assert steps[1].item == "node" and steps[1].command is None
    assert "claude.ai/install.sh" in steps[2].command[-1] and "chatgpt.com/codex" in steps[3].command[-1]
    assert "cursor.com/install" in steps[4].command[-1]


def test_own_brew_installs_casks_for_enabled_agents(mac):
    prefix = fake_brew(mac)
    steps = setup.plan(Config(home=mac, source="codex", agents=["codex"], update_clis=False))
    assert labels(steps) == [("node", str(prefix / "bin/brew")), ("codex", str(prefix / "bin/brew"))]


def test_your_own_install_command_runs(mac, monkeypatch):
    done = mac / "installed"
    cfg = Config(home=mac, source="codex", agents=["codex"], update_clis=False,
                 install_commands={"homebrew": ["/usr/bin/true"], "node": ["no-such-installer"],
                                   "codex": ["/usr/bin/touch", str(done)]})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert setup.run(cfg, dry=True) == 0 and not done.exists()      # a dry run only lists
    assert setup.run(cfg) == 1 and not done.exists()                # no terminal and no --yes: asks for one
    assert setup.run(cfg, yes=True) == 1                            # the missing installer is a FAIL, not a crash
    assert done.exists()


def test_nothing_missing(mac, capsys):
    for cmd in ("npx", "claude", "codex", "cursor-agent"):
        (mac / "bin" / cmd).write_text("#!/bin/sh\n")
        (mac / "bin" / cmd).chmod(0o755)
    fake_brew(mac)
    assert setup.plan(Config(home=mac)) == [] and setup.run(Config(home=mac)) == 0
    assert "Everything agent-sync needs is installed." in capsys.readouterr().out
