import json

from agent_sync import instructions

OLD_BLOCK = ("<!-- agent-sync: begin (mirrored from CLAUDE.md; edit the source, not this block) -->\n{}\n"
             "<!-- agent-sync: end -->\n")


def setup(tmp_path, claude, codex):
    files = instructions.files(tmp_path)
    for tool, text in (("claude", claude), ("codex", codex)):
        files[tool].parent.mkdir(parents=True, exist_ok=True)
        files[tool].write_text(text)
    return files


def test_source_lines_are_merged_in_and_the_targets_own_notes_stay(tmp_path):
    files = setup(tmp_path, "# Mine\n\nBe brief.\n", "Codex-only note.\n")
    assert instructions.mirror(tmp_path, "claude", dry=True) == [f"would update {files['codex']}",
                                                                 f"would update {files['cursor']}"]
    assert files["codex"].read_text() == "Codex-only note.\n" and not files["cursor"].exists()
    instructions.mirror(tmp_path, "claude")
    codex = files["codex"].read_text()
    assert "agent-sync: begin" not in codex
    assert codex.count("Be brief.") == 1 and codex.count("Codex-only note.") == 1
    assert "alwaysApply: true" in files["cursor"].read_text()
    assert instructions.mirror(tmp_path, "claude") == []                 # idempotent

    files["claude"].write_text("# Mine\n\nBe very brief.\n")
    instructions.mirror(tmp_path, "claude")
    codex = files["codex"].read_text()
    assert "Be very brief." in codex and "Be brief.\n" not in codex and "Codex-only note." in codex


def test_an_old_mirrored_block_folds_into_one_copy_of_each_line(tmp_path):
    hand_kept = "# Mine\n\nBe brief.\n\nCodex-only note.\n"
    source = "# Mine\n\nBe brief.\n\n## Cloudflare\n\nUse the cf CLI.\n"
    files = setup(tmp_path, source, hand_kept + "\n" + OLD_BLOCK.format(source.strip()))
    instructions.mirror(tmp_path, "claude")
    codex = files["codex"].read_text()
    assert "agent-sync: begin" not in codex
    for line in ("# Mine", "Be brief.", "Codex-only note.", "## Cloudflare", "Use the cf CLI."):
        assert codex.count(line) == 1, line


def test_a_line_deleted_from_the_source_leaves_the_target_but_its_own_notes_stay(tmp_path):
    files = setup(tmp_path, "Be brief.\nAsk before pushing.\n", "Codex-only note.\n")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert json.loads(baselines.read_text())[instructions.KEY]["lines"] == ["Be brief.", "Ask before pushing."]
    files["claude"].write_text("Be brief.\n")
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    codex = files["codex"].read_text()
    assert "Ask before pushing." not in codex and "Codex-only note." in codex and "Be brief." in codex


def test_cursor_cannot_be_the_instructions_source(tmp_path):
    assert instructions.mirror(tmp_path, "cursor")[0].startswith("skip")


def test_a_target_that_already_has_every_source_line_is_left_alone(tmp_path):
    files = setup(tmp_path, "# Mine\n\nBe brief.\n", "Codex-only note.\n# Mine\n\nBe brief.\n")
    files["cursor"].parent.mkdir(parents=True)
    assert instructions.mirror(tmp_path, "claude") == [f"updated {files['cursor']}"]
    assert files["codex"].read_text() == "Codex-only note.\n# Mine\n\nBe brief.\n"


def test_a_rule_reworded_in_the_target_is_kept_and_reported_until_the_source_has_it(tmp_path):
    files = setup(tmp_path, "Be brief.\nAsk before pushing to a shared branch.\nRun the tests.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)

    codex = files["codex"]
    codex.write_text(codex.read_text().replace("Ask before pushing to a shared branch.",
                                               "Ask before pushing to any shared branch."))
    files["claude"].write_text("Be very brief.\nAsk before pushing to a shared branch.\nRun the tests.\n")
    out = instructions.mirror(tmp_path, "claude", baselines=baselines)
    text = codex.read_text()
    assert "Ask before pushing to any shared branch." in text and "to a shared branch." not in text
    assert "Be very brief." in text                                     # the source's own change still arrives
    assert out[0] == (f"WARN  {codex} rewords 1 line CLAUDE.md still has as last synced; kept it. Make the same "
                      f"change in {files['claude']} (or run agent-sync review) so every tool has it")

    files["claude"].write_text("Be very brief.\nAsk before pushing to any shared branch.\nRun the tests.\n")
    assert not [line for line in instructions.mirror(tmp_path, "claude", baselines=baselines)
                if line.startswith("WARN")]
    assert codex.read_text().count("shared branch") == 1


def test_without_a_record_of_the_last_sync_the_source_wording_wins(tmp_path):
    files = setup(tmp_path, "Ask before pushing to a shared branch.\n", "Ask before pushing to any shared branch.\n")
    instructions.mirror(tmp_path, "claude")
    assert files["codex"].read_text() == "Ask before pushing to a shared branch.\n"


def test_review_recommends_the_tool_that_reworded_a_synced_line(tmp_path):
    from agent_sync import review
    from agent_sync.config import Config
    cfg = Config(home=tmp_path)
    files = setup(tmp_path, "Be brief.\nAsk before pushing to a shared branch.\n", "")
    instructions.mirror(tmp_path, "claude", baselines=review.baseline_file(cfg))
    codex = files["codex"]
    codex.write_text(codex.read_text().replace("to a shared branch", "to any shared branch"))
    group = next(group for group in review.scan(tmp_path, cfg).groups if group.kind == "instructions")
    winner, reasons = review.recommend(group, "claude")
    assert winner.tools == ("codex",)
    assert reasons == ["it rewords lines the others still have as they were at the last sync"]


def test_a_rewording_is_kept_when_the_source_adds_a_line_right_after_it():
    from agent_sync import textmerge
    synced = ["Ask before pushing to a shared branch."]
    winner = "Ask before pushing to a shared branch.\nRun the tests before committing.\n"
    assert textmerge.reworded("Ask before pushing to any shared branch.\n", winner, synced) == {
        0: "Ask before pushing to any shared branch."}


def test_a_rewording_of_one_of_two_identical_lines_changes_only_that_one(tmp_path):
    from pathlib import Path
    source = "## Frontend\n- Run `npm test`.\n\n## Backend\n- Run `npm test`.\n"
    target = source.replace("- Run `npm test`.", "- Run `npm run test:unit`.", 1)
    merged = instructions.rendered("codex", target, source.strip(), Path("CLAUDE.md"),
                                   frozenset(["## Frontend", "- Run `npm test`.", "## Backend"]))
    assert merged.split("## Backend")[0].count("npm run test:unit") == 1
    assert merged.split("## Backend")[1].strip() == "- Run `npm test`."


def test_review_never_recommends_a_reworded_copy_over_your_own_edits(tmp_path):
    from agent_sync import review
    from agent_sync.config import Config
    cfg = Config(home=tmp_path)
    files = setup(tmp_path, "Be brief.\nAsk before pushing to a shared branch.\n", "")
    instructions.mirror(tmp_path, "claude", baselines=review.baseline_file(cfg))
    cursor = files["cursor"]
    cursor.write_text(cursor.read_text().replace("to a shared branch", "to any shared branch"))
    group = next(group for group in review.scan(tmp_path, cfg).groups if group.kind == "instructions")
    assert "cursor" not in review.recommend(group, "claude")[0].tools
