import json
import os
import time
from datetime import datetime

import pytest

from agent_sync import backups, cleanup, cli, notify
from agent_sync.config import Config, parse
from agent_sync.log import ACK_AGE, ACK_FILE
from tests.test_inventory import write


@pytest.fixture
def state(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path, skill_packages=["owner/active"], update_clis=False, notify=False)
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: False)
    monkeypatch.setattr(cli.membership, "available", lambda _: {})
    return cfg


@pytest.mark.parametrize("mode", ["offline", "recent", "dry", "failed"])
def test_run_cleans_only_owned_state_even_when_upkeep_is_skipped(state, mode, monkeypatch, capsys):
    root = state.state_dir
    expired = write(root, "backups/review-20000101-000000/file", "expired backup")
    unmanaged = write(root, "backups/manual-copy/file", "manual backup")
    dropped = write(root, "packages/owner__dropped/file", "obsolete checkout")
    write(dropped.parent, ".git/config", '[remote "origin"]\n\turl = https://github.com/owner/dropped.git\n')
    active = write(root, "packages/owner__active/file", "current checkout")
    write(active.parent, ".git/config", '[remote "origin"]\n\turl = https://github.com/owner/active.git\n')
    personal = write(root, "packages/personal/file", "manual checkout")
    manual = write(root, "packages/owner__manual/file", "manual folder")
    different = write(root, "packages/owner__different/file", "checkout with a different origin")
    write(different.parent, ".git/config", '[remote "origin"]\n\turl = https://example.invalid/project.git\n')
    outside = write(state.home, "outside/file", "outside")
    (root / "packages/other__link").symlink_to(outside.parent, target_is_directory=True)
    (expired.parent / "link").symlink_to(outside.parent, target_is_directory=True)
    old, new = time.time() - ACK_AGE - 10, time.time()
    ack = write(root, ACK_FILE, {"old": old, "new": new})
    inbox = write(root, notify.INBOX_FILE, {"old": old, "new": new, "legacy": True})
    log = write(state.log_file.parent, state.log_file.name, "2000-01-01 00:00:00 WARN  old line\n\tcontinued\n")

    def fail(*args):
        cli.guarded(args[1], "fake step", unreadable)

    def unreadable():
        raise OSError("the step failed")

    if mode == "failed":
        monkeypatch.setattr(cli, "online", lambda: True)
        monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test"))
        monkeypatch.setattr(cli, "steps", fail)
    if mode == "recent":
        (root / "last-run").touch()
        monkeypatch.setattr(cli, "online", lambda: pytest.fail("recent run started upkeep"))

    before = {path: path.read_bytes() for path in (expired, dropped, ack, inbox, log)}
    args = ["run", "--echo", "--dry-run" if mode == "dry" else "--force"]
    if mode == "recent":
        args.pop()
    assert cli.main(args) == (1 if mode == "failed" else 0)

    output = capsys.readouterr().out
    assert "skipped symlink" in output and "other__link" in output
    assert "old line" not in output
    assert all(path.exists() for path in (unmanaged, active, personal, manual, different, outside))
    assert (root / "packages/other__link").is_symlink()
    if mode == "dry":
        assert {path: path.read_bytes() for path in before} == before
        assert "Would delete backup" in output and "Would delete package checkout" in output
        assert "Would delete 1 expired records" in output and "Would delete 2 expired log lines" in output
    else:
        assert not expired.parent.exists() and not dropped.parent.exists()
        assert json.loads(ack.read_text()) == {"new": new}
        records = json.loads(inbox.read_text())
        assert records["new"] == new and type(records["legacy"]) is float and "old" not in records
        assert "old line" not in log.read_text() and "continued" not in log.read_text()


def test_cleanup_runs_after_backups_finish_even_when_a_run_raises(state, monkeypatch):
    cfg = state
    cfg.backup_max_mb = 0
    root = cfg.state_dir / "backups"
    old_folders = [write(root, f"review-{stamp}/file", "old").parent
                   for stamp in ("20000101-000000", datetime.now().strftime("%Y%m%d-000000"))]
    target = write(cfg.home, "target", "old bytes")
    unchanged = write(cfg.home, "unchanged", "same")

    def fail(*_):
        backups.save(target)
        backups.save(unchanged)
        target.write_text("new bytes")
        raise RuntimeError("stopped early")

    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test"))
    monkeypatch.setattr(cli, "steps", fail)
    with pytest.raises(RuntimeError, match="stopped early"):
        cli.main(["run", "--force"])
    assert all(not folder.exists() for folder in old_folders)
    current, = root.iterdir()
    assert (current / "target").read_text() == "old bytes"
    assert not (current / "unchanged").exists()


def test_symlinked_backups_are_skipped_and_warning_needs_ack(state, monkeypatch, capsys):
    payload = write(state.home, "outside/run-20000101-000000/file", "untouched")
    root = state.state_dir / "backups"
    root.parent.mkdir(parents=True)
    root.symlink_to(payload.parent.parent, target_is_directory=True)
    prompts = []
    monkeypatch.setattr(notify, "in_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert cli.main(["run", "--force"]) == 0
    assert cli.main(["run", "--force"]) == 0
    assert prompts == ["Press Enter once you have read the warnings. "]
    assert capsys.readouterr().out.count(f"Warning: skipped symlink {root}") == 1
    assert payload.read_text() == "untouched"
    with backups.during(state.home, root), pytest.raises(OSError, match="symlink"):
        backups.save(payload)


def test_status_reports_backup_size_folder_count_and_limits(state, monkeypatch, capsys):
    root = state.state_dir / "backups"
    write(root, "run-20000101-000000/file", "x" * backups.MIB)
    write(root, "manual/file", "x" * (backups.MIB // 2))
    write(root, "file", "x" * (backups.MIB // 2))
    (root / "link").symlink_to(root / "manual")
    monkeypatch.setattr(cli.tools, "version", lambda *_: "test")
    assert cli.main(["status"]) == 0
    assert "backups: 2.00 MiB in 2 folders (limits: 500 MiB and 30 days)" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["true", "-1", "1.5"])
def test_config_rejects_invalid_backup_limit(tmp_path, value):
    with pytest.raises(ValueError, match="backup_max_mb"):
        parse(f"backup_max_mb = {value}", tmp_path / "config.toml", tmp_path)


def test_undated_inbox_records_expire_using_the_files_original_age(state):
    inbox = write(state.state_dir, notify.INBOX_FILE, {"legacy": True})
    old = time.time() - ACK_AGE - 10
    os.utime(inbox, (old, old))
    assert cli.main(["run", "--force"]) == 0
    assert json.loads(inbox.read_text()) == {}


def test_the_scheduler_output_is_trimmed_to_its_newest_whole_lines(tmp_path):
    out = tmp_path / "launchd.out"
    lines = [f"line {n}\n".encode() for n in range(200_000)]
    out.write_bytes(b"".join(lines))
    messages = []
    running_job = out.open("ab")    # launchd's handle for the job that is running now

    cleanup.trim_output(out, True, lambda *args: messages.append(args))
    assert out.read_bytes() == b"".join(lines)

    cleanup.trim_output(out, False, lambda *args: messages.append(args))
    running_job.write(b"after the trim\n")
    running_job.close()
    kept = out.read_bytes()
    assert len(kept) <= cleanup.OUTPUT_KEEP + len(b"after the trim\n")
    assert kept.startswith(b"line ") and kept.endswith(lines[-1] + b"after the trim\n")
    assert [kind for kind, _ in messages] == ["info", "info"]

    cleanup.trim_output(out, False, lambda *args: messages.append(args))
    assert out.read_bytes() == kept and len(messages) == 2
