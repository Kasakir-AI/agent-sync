import pytest

from agent_sync import instructions, review, writes
from agent_sync.config import Config


@pytest.mark.parametrize("already_copied", [False, True])
def test_review_removes_superseded_mirrored_instructions(tmp_path, already_copied):
    paths = instructions.files(tmp_path)
    source, target = paths["claude"], paths["codex"]
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text("Run the tests.\n")
    target.write_text("Use the Codex workspace profile.\n")
    instructions.mirror(tmp_path, "claude")
    before = target.read_text()
    assert "Run the tests." in before

    chosen = "Run tests using pytest.\n"
    source.write_text(chosen)
    if already_copied:
        before = chosen + ("\n<!-- agent-sync: begin (mirrored from CLAUDE.md; edit the source, not this block) -->"
                           "\nRun the tests.\n<!-- agent-sync: end -->\n")
        target.write_text(before)

    scan = review.scan(tmp_path, Config(home=tmp_path))
    group, = scan.groups
    winner = next(version for version in group.versions if version.path == source)
    steps = writes.plan(tmp_path, scan, {group.key: winner.id}, "claude")
    backup = tmp_path / "backup"
    writes.apply(tmp_path, steps, backup)

    expected = chosen if already_copied else "Use the Codex workspace profile.\n" + chosen
    assert target.read_text() == expected
    assert (backup / target.relative_to(tmp_path)).read_text() == before
    assert writes.apply(tmp_path, steps, backup) == []
