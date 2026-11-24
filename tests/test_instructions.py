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
