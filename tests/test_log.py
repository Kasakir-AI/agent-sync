from datetime import datetime, timedelta

from agent_sync.log import STAMP, Log


def test_lines_older_than_the_limit_are_dropped_in_place(tmp_path):
    p = tmp_path / "a.log"
    old = (datetime.now() - timedelta(days=8)).strftime(STAMP)
    p.write_text(f"{old} info  old line\n")
    inode = p.stat().st_ino
    log = Log(p)
    log("ok", "new line")
    assert log.prune(7) == 1
    text = p.read_text()
    assert "old line" not in text and "new line" in text and p.stat().st_ino == inode


def test_failures_are_counted(tmp_path):
    log = Log(tmp_path / "b.log")
    log("FAIL", "x"); log("ok", "y")
    assert log.problems == 1


def test_mirror_lines_keep_their_own_kind_and_the_summary_counts_them(tmp_path):
    log = Log(tmp_path / "c.log")
    log.lines(["updated ~/.codex/AGENTS.md", "WARN  ~/x is not a copy agent-sync made; left alone",
               "skip  no ~/.claude/rules/pstack-models.md"])
    text = (tmp_path / "c.log").read_text()
    assert " sync  updated ~/.codex/AGENTS.md" in text
    assert " WARN  ~/x is not a copy" in text and " skip  no ~/.claude" in text
    assert log.summary() == "1 change, no commands run, 1 warning, no problems"
    log("ran", "codex update"); log("dry", "would run: claude update")
    assert log.summary() == "1 change, 2 commands run, 1 warning, no problems"


def test_log_is_private_for_new_and_existing_files(tmp_path):
    from stat import S_IMODE

    for name in ("new.log", "existing.log"):
        path = tmp_path / name
        if name == "existing.log":
            path.write_text("previous entry\n")
            path.chmod(0o644)
        log = Log(path)
        log("ok", "new entry")
        assert S_IMODE(path.stat().st_mode) == 0o600
        assert "new entry" in path.read_text()
