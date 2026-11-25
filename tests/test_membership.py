import sys
import tomllib
from dataclasses import replace

import pytest

from agent_sync import cli, setup, tools
from agent_sync.config import config_path
from agent_sync.registry import TOOLS


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "bin"))
    detect = tuple(path for path in TOOLS["antigravity"].detect if not path.startswith("/"))
    monkeypatch.setitem(TOOLS, "antigravity", replace(TOOLS["antigravity"], detect=detect))
    return tmp_path


def seed(home, relative, content=""):
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_list_detects_by_path_and_command_without_a_terminal(home, capsys):
    seed(home, ".gemini/antigravity/config")
    seed(home, "bin/codex", "#!/bin/sh\n").chmod(0o755)
    assert cli.main(["agents"]) == 0
    out = capsys.readouterr().out
    assert "Claude Code (claude)  not installed, in step, source" in out
    assert "Codex (codex)  installed, in step" in out
    assert "Gemini CLI (gemini)  not installed, outside sync" in out
    assert "Antigravity (antigravity)  installed, outside sync" in out
    assert not config_path(home).exists()


def test_terminal_offer_accepts_and_declines(home, monkeypatch, capsys):
    seed(home, ".gemini/settings.json", "{}")
    seed(home, ".gemini/antigravity/config")
    cfg = seed(home, ".config/agent-sync/config.toml", "# Keep this comment\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["n", ""])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt.rstrip()) or next(answers))
    assert cli.main(["agents"]) == 0
    assert prompts == ["Gemini CLI is installed. Keep it in step? [Y/n]",
                       "Antigravity is installed. Keep it in step? [Y/n]"]
    assert "Keeping Antigravity in step." in capsys.readouterr().out
    assert tomllib.loads(cfg.read_text())["agents"] == ["claude", "codex", "cursor", "antigravity"]
    assert "# Keep this comment" in cfg.read_text()


def test_add_and_remove_are_validated_and_leave_settings_in_place(home, capsys):
    keep = seed(home, ".gemini/config/config.json", "{}")
    cfg = seed(home, ".config/agent-sync/config.toml", 'source = "codex" # keep\n')
    assert cli.main(["agents", "add", "antigravity", "gemini"]) == 0
    assert tomllib.loads(cfg.read_text())["agents"] == list(TOOLS)
    assert "agent-sync setup" in capsys.readouterr().out
    before = cfg.read_text()
    assert cli.main(["agents", "remove", "antigravity", "codex"]) == 2
    assert "Change source before removing it" in capsys.readouterr().err
    assert cli.main(["agents", "add", "fake"]) == 2
    assert "claude, codex, cursor, gemini, antigravity" in capsys.readouterr().err
    assert cfg.read_text() == before
    assert cli.main(["agents", "remove", "antigravity", "claude"]) == 0
    assert "leaves its files in place" in capsys.readouterr().out
    assert keep.read_text() == "{}"
    assert "# keep" in cfg.read_text()


def test_setup_offers_before_planning_but_dry_run_only_plans(home, monkeypatch):
    seed(home, ".gemini/settings.json", "{}")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    plans = []

    def plan(cfg):
        plans.append(cfg.agents)
        return []

    prompts = []
    monkeypatch.setattr(setup, "plan", plan)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt.rstrip()) or "")
    assert cli.main(["setup", "--dry-run"]) == 0
    assert cli.main(["setup", "--yes"]) == 0
    assert not prompts and not config_path(home).exists()
    assert cli.main(["setup"]) == 0
    assert prompts == ["Gemini CLI is installed. Keep it in step? [Y/n]"]
    assert plans == [["claude", "codex", "cursor"], ["claude", "codex", "cursor"],
                     ["claude", "codex", "cursor", "gemini"]]


def test_run_reports_installed_agents_once_as_information(home, monkeypatch, capsys):
    seed(home, ".gemini/settings.json", "{}")
    seed(home, ".gemini/antigravity/config")
    monkeypatch.setattr(cli, "online", lambda: False)
    assert cli.main(["run", "--echo", "--force"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if "agents add" in line]
    assert len(lines) == 1
    assert "info" in lines[0] and "Gemini CLI, Antigravity" in lines[0]
    assert "agent-sync agents add gemini antigravity" in lines[0]
