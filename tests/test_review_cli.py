import os
import time
from datetime import datetime

import pytest

from agent_sync import cli
from agent_sync.config import config_path, load
from agent_sync.locking import run_lock


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


@pytest.fixture
def upkeep_home(home, monkeypatch):
    path = config_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("skill_packages = []\nupdate_plugins = false\nupdate_clis = false\nupdate_self = false\n")
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test version"))
    return home


def put(home, name, text):
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_review_list_recommends_the_original_and_writes_nothing(home, capsys):
    original = put(home, ".claude/CLAUDE.md", "Keep the checklist.\nAsk before publishing.\n")
    copy = put(home, ".cursor/rules/agent-sync-instructions.mdc", "Use a new checklist.\n")
    old, new = 2_000_000_000, 2_000_086_400
    os.utime(original, (old, old))
    os.utime(copy, (new, new))
    assert cli.main(["review", "--list"]) == 0
    output = capsys.readouterr().out
    assert "Global instructions (instructions, differ)" in output
    old_date = datetime.fromtimestamp(old).strftime("%Y-%m-%d %H:%M:%S")
    assert f"Claude Code | your edit | {old_date} | 2 lines | ~/.claude/CLAUDE.md [Recommended]" in output
    new_date = datetime.fromtimestamp(new).strftime("%Y-%m-%d %H:%M:%S")
    assert f"Cursor | copied by agent-sync | {new_date} | 1 line" in output
    assert "  - " in output and "copy" in output.split("[Recommended]")[-1]
    assert sorted(p for p in home.rglob("*") if p.is_file()) == sorted([original, copy])


def test_run_reports_differences_after_mirroring(upkeep_home, capsys):
    put(upkeep_home, ".claude/CLAUDE.md", "Use the project checklist.\nOnly publish when asked.\n")
    put(upkeep_home, ".cursor/rules/agent-sync-instructions.mdc", "Draft a different workflow.\n")
    put(upkeep_home, ".cursor/rules/archives.mdc", "Preserve old archives outside the working folder.\n")
    put(upkeep_home, ".claude.json", '{"mcpServers": {"reference": {"command": "lookup"}}}')
    assert cli.main(["run", "--force", "--echo"]) == 0
    summary = '1 setting differs between the tools and 1 is only in one; run "agent-sync review" to choose'
    output = capsys.readouterr().out
    assert output.count(summary) == 1
    assert summary in load(home=upkeep_home).log_file.read_text()


def test_scan_failure_warns_without_failing_upkeep(upkeep_home, capsys):
    put(upkeep_home, ".claude/CLAUDE.md", "bad bytes follow").write_bytes(b"\xff")
    assert cli.main(["run", "--force", "--echo"]) == 0
    output = capsys.readouterr().out
    assert "WARN  could not compare settings for review" in output
    assert "agent-sync finished, 0 problem(s)" in output


def test_run_recovers_an_expired_lock(upkeep_home, capsys):
    cfg = load(home=upkeep_home)
    lock = cfg.state_dir / "lock"
    lock.mkdir(parents=True)
    assert cli.main(["run", "--force", "--echo"]) == 0
    assert "another agent-sync run is in progress" in capsys.readouterr().out
    assert lock.is_dir() and not (cfg.state_dir / "last-run").exists()

    stale = time.time() - 3 * 60 * 60
    os.utime(lock, (stale, stale))
    assert cli.main(["run", "--force", "--echo"]) == 0
    assert "agent-sync finished, 0 problem(s)" in capsys.readouterr().out
    assert not lock.exists() and (cfg.state_dir / "last-run").exists()


def test_run_shares_the_review_lock_but_dry_run_uses_its_own(upkeep_home, capsys):
    cfg = load(home=upkeep_home)
    lock = cfg.state_dir / "lock"
    with run_lock(lock) as acquired:
        assert acquired
        stale = time.time() - 3 * 60 * 60
        os.utime(lock, (stale, stale))
        assert cli.main(["run", "--force", "--echo"]) == 0
        assert "another agent-sync run is in progress" in capsys.readouterr().out
        assert cli.main(["run", "--dry-run"]) == 0
        assert "dry run finished: nothing was changed" in capsys.readouterr().out
        assert lock.is_dir() and not (cfg.state_dir / "last-run").exists() and not cfg.log_file.exists()
    assert not lock.exists()


def test_tool_made_skills_are_listed_but_not_counted(upkeep_home, capsys):
    claude = put(upkeep_home, ".claude/skills/clipper/SKILL.md", "---\nallowed-tools: Bash\n---\nRead the inbox.\n")
    codex = put(upkeep_home, ".agents/skills/clipper/SKILL.md", "Read the inbox with the command.\n")
    for path, when in ((claude, 100), (codex, 90_000)):
        os.utime(path, (when, when))
    put(upkeep_home, ".claude/CLAUDE.md", "Use the checklist.\n")
    put(upkeep_home, ".codex/AGENTS.md", "Use the checklist.\nCodex note.\n")
    assert cli.main(["review", "--list"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("0 differ, 1 only in one tool, 1 the same (1 made for each tool)\n")
    tailored = "clipper (skill, tailored)\n  Each tool has a version made for it; Claude Code's uses allowed-tools"
    assert tailored in output
    assert "Global instructions (instructions, missing)\n  Codex keeps 1 line of its own\n" in output
    assert cli.main(["run", "--force", "--echo"]) == 0
    assert '0 settings differ between the tools and 1 is only in one' in capsys.readouterr().out
