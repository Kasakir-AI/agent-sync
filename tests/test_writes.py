from datetime import datetime

import pytest

from agent_sync import backups, roles, writes
from agent_sync.config import Config
from agent_sync.review import scan


def test_update_writes_through_a_link_and_backs_up_its_target(tmp_path):
    home = tmp_path / "home"
    repo = home / "dotfiles/claude.json"
    repo.parent.mkdir(parents=True)
    repo.write_text('{"a": 1}\n')
    repo.chmod(0o640)
    link = home / ".claude.json"
    link.symlink_to(repo)
    with backups.during(home, tmp_path / "backups", now=datetime(2026, 11, 1)):
        assert writes.update_text(link, lambda text: text.replace("1", "2"))
    assert link.is_symlink() and repo.read_text() == '{"a": 2}\n'
    assert repo.stat().st_mode & 0o777 == 0o640
    saved, = (tmp_path / "backups").iterdir()
    assert (saved / "dotfiles/claude.json").read_text() == '{"a": 1}\n'


def test_update_creates_new_files_with_the_mode_asked_for(tmp_path):
    path = tmp_path / "settings.json"
    assert writes.update_text(path, lambda _: "{}\n", new_mode=0o600)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not writes.update_text(path, lambda text: text)
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]   # no temp file left behind


def test_role_mirror_warns_and_leaves_a_file_that_keeps_changing(tmp_path, monkeypatch):
    src = tmp_path / ".claude/rules/pstack-models.md"
    src.parent.mkdir(parents=True)
    src.write_text("feature: opus\n")
    target = tmp_path / ".agents/pstack-models.md"
    target.parent.mkdir(parents=True)
    target.write_text("feature: old\n")
    save, count = backups.save, iter(range(10))

    def busy(path, **kwargs):
        save(path, **kwargs)
        target.write_text(f"feature: user{next(count)}\n")

    monkeypatch.setattr(backups, "save", busy)
    lines = roles.mirror(tmp_path, "claude", enabled=("claude", "codex"))
    assert lines == [f"WARN  {target} changed while agent-sync was updating it; will retry next run"]
    assert target.read_text().startswith("feature: user")


def test_review_apply_writes_nothing_when_a_file_changed_after_planning(tmp_path):
    claude, codex = tmp_path / ".claude/CLAUDE.md", tmp_path / ".codex/AGENTS.md"
    for path, text in ((claude, "Shared\nNew\n"), (codex, "Shared\n")):
        path.parent.mkdir(parents=True)
        path.write_text(text)
    cfg = Config(home=tmp_path, agents=["claude", "codex"])
    found = scan(tmp_path, cfg, ["instructions"])
    group = next(g for g in found.groups if g.kind == "instructions")
    winner = next(v for v in group.versions if "claude" in v.tools)
    changes = writes.edits(writes.plan(tmp_path, found, {group.key: winner.id}, "claude"))
    codex.write_text("Shared\nMine\n")
    with pytest.raises(writes.Raced):
        writes.apply_edits(tmp_path, changes)
    assert codex.read_text() == "Shared\nMine\n"


def test_update_replans_and_backs_up_the_new_target_when_link_moves_during_staging(tmp_path, monkeypatch):
    first, second, link = (tmp_path / name for name in ("first", "second", "settings"))
    first.write_text("first")
    second.write_text("second")
    link.symlink_to(first)
    stage = writes.stage

    def switch(target, data, mode):
        temp = stage(target, data, mode)
        if link.resolve() == first.resolve():
            link.unlink()
            link.symlink_to(second)
        return temp

    monkeypatch.setattr(writes, "stage", switch)
    with backups.during(tmp_path, tmp_path / "backups") as run:
        assert writes.update_text(link, lambda text: text + " updated")
    assert first.read_text() == "first"
    assert second.read_text() == "second updated"
    assert link.resolve() == second.resolve()
    assert (run.folder / "second").read_text() == "second"
    assert not (run.folder / "first").exists()


def test_review_writer_does_not_accept_bytes_changed_between_rewrite_and_edit(tmp_path, monkeypatch):
    claude, codex = tmp_path / ".claude/CLAUDE.md", tmp_path / ".codex/AGENTS.md"
    for path, text in ((claude, "Shared\nNew\n"), (codex, "Shared\n")):
        path.parent.mkdir(parents=True)
        path.write_text(text)
    cfg = Config(home=tmp_path, agents=["claude", "codex"])
    found = scan(tmp_path, cfg, ["instructions"])
    group = next(g for g in found.groups if g.kind == "instructions")
    winner = next(v for v in group.versions if "claude" in v.tools)
    steps = writes.plan(tmp_path, found, {group.key: winner.id}, "claude")
    writer = writes.WRITERS["instructions"]

    def concurrent_edit(step, read):
        result = writer(step, read)
        codex.write_text("Shared\nConcurrent edit\n")
        return result

    monkeypatch.setitem(writes.WRITERS, "instructions", concurrent_edit)
    with pytest.raises(writes.Raced):
        writes.apply(tmp_path, steps)
    assert codex.read_text() == "Shared\nConcurrent edit\n"
