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


def test_applying_a_review_choice_keeps_each_files_local_sections(tmp_path):
    paths = instructions.files(tmp_path)
    source, target = paths["claude"], paths["codex"]
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    local = "<!-- agent-sync: local -->\nClaude-only instruction.\n<!-- agent-sync: end local -->\n"
    memory = "<!-- agent-sync: memory -->\nMemory index.\n<!-- agent-sync: end memory -->\n"
    source.write_text("Run the tests.\n" + local)
    target.write_text("Use the Codex workspace profile.\n" + memory)
    cfg = Config(home=tmp_path)
    baselines = review.baseline_file(cfg)
    instructions.mirror(tmp_path, "claude", baselines=baselines)

    source.write_text("Run tests using pytest.\n" + local)
    scan = review.scan(tmp_path, cfg)
    group, = scan.groups
    winner = next(version for version in group.versions if version.path == source)
    choices = {group.key: winner.id}
    steps = writes.plan(tmp_path, scan, choices, "claude")
    writes.apply(tmp_path, steps, tmp_path / "backup")
    review.remember(baselines, review.scan(tmp_path, cfg), choices)
    assert target.read_text() == "Use the Codex workspace profile.\n" + memory + "Run tests using pytest.\n"
    assert source.read_text() == "Run tests using pytest.\n" + local
    assert "Claude-only instruction." not in paths["cursor"].read_text()
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []


def test_review_applies_a_held_non_source_reword(tmp_path):
    paths = instructions.files(tmp_path)
    cfg = Config(home=tmp_path)
    baselines = review.baseline_file(cfg)
    source, codex = paths["claude"], paths["codex"]
    source.parent.mkdir()
    old, new = "Run tests before pushing.\n", "Run all tests before pushing.\n"
    source.write_text(old)
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    codex.write_text(new)
    warning, = instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert "rewords 1 shared line" in warning
    scan = review.scan(tmp_path, cfg)
    group, = scan.groups
    winner = next(version for version in group.versions if version.path == codex)
    choices = {group.key: winner.id}
    writes.apply(tmp_path, writes.plan(tmp_path, scan, choices, "claude"), tmp_path / "review-backup")
    review.remember(baselines, review.scan(tmp_path, cfg), choices)
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []
    assert source.read_text() == codex.read_text() == new
    assert paths["cursor"].read_text().endswith(new)


def test_line_count_ignores_comment_lines_the_comparison_skips(tmp_path):
    paths = instructions.files(tmp_path)
    source, target = paths["claude"], paths["codex"]
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text("<!-- imported-from: codex:user:instructions -->\nRun the tests.\nUse pytest.\n")
    target.write_text("Run the tests.\n<!-- tool-specific note -->\nUse pytest.\n")

    group, = review.scan(tmp_path, Config(home=tmp_path)).groups
    written = [version for version in group.versions if version.path in {source, target}]
    assert len({version.digest for version in written}) == 1
    assert [version.lines for version in written] == [2, 2]


def test_review_ignores_third_party_instruction_blocks(tmp_path):
    paths = instructions.files(tmp_path)
    source, target = paths["claude"], paths["codex"]
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text("A\nB\n")
    target.write_text("A\nB\n<!-- plannotator-inbox:start v9 -->\n"
                      "Inbox rule.\n<!-- plannotator-inbox:end -->\n")

    group, = review.scan(tmp_path, Config(home=tmp_path)).groups
    versions = {version.path: version for version in group.versions}
    assert versions[source].digest == versions[target].digest


def test_review_keeps_content_after_an_unmatched_third_party_start(tmp_path):
    source = instructions.files(tmp_path)["claude"]
    source.parent.mkdir(parents=True)
    source.write_text("A\n<!-- x:start -->\nB\n<!-- other.tool_2:start -->\n"
                      "Installer rule.\n<!-- other.tool_2:end -->\nC\n")

    group, = review.scan(tmp_path, Config(home=tmp_path)).groups
    version, = group.versions
    assert version.body == "A\n<!-- x:start -->\nB\nC\n"
