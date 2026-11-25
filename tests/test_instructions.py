import json

from agent_sync import instructions
from agent_sync.registry import TOOLS

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
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    codex = files["codex"].read_text()
    assert "agent-sync: begin" not in codex
    assert codex.count("Be brief.") == 1 and codex.count("Codex-only note.") == 1
    assert "alwaysApply: true" in files["cursor"].read_text()
    assert files["cursor"].read_text().endswith("# Mine\n\nBe brief.\n")
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []

    files["claude"].write_text("# Mine\n\nBe very brief.\n")
    instructions.mirror(tmp_path, "claude", baselines=baselines)
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


def test_cursor_can_seed_the_shared_instructions(tmp_path):
    files = setup(tmp_path, "", "")
    files["cursor"].parent.mkdir(parents=True)
    files["cursor"].write_text("---\nalwaysApply: true\n---\nRun the tests.\n")
    instructions.mirror(tmp_path, "cursor")
    assert files["claude"].read_text() == files["codex"].read_text() == "Run the tests.\n"


def test_a_target_that_already_has_every_source_line_is_left_alone(tmp_path):
    files = setup(tmp_path, "# Mine\n\nBe brief.\n", "Codex-only note.\n# Mine\n\nBe brief.\n")
    files["cursor"].parent.mkdir(parents=True)
    assert instructions.mirror(tmp_path, "claude") == [f"updated {files['cursor']}"]
    assert files["codex"].read_text() == "Codex-only note.\n# Mine\n\nBe brief.\n"


def test_rewords_from_the_source_and_gemini_reach_every_file(tmp_path):
    files = setup(tmp_path, "Be brief.\nAsk before pushing to a shared branch.\nRun the tests.\n", "")
    files.update(instructions.files(tmp_path, TOOLS))
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)

    gemini = files["gemini"]
    gemini.write_text(gemini.read_text().replace("to a shared branch", "to any shared branch"))
    files["claude"].write_text("Be very brief.\nAsk before pushing to a shared branch.\nRun the tests.\n")
    messages = instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    assert messages and all(line.startswith("updated ") for line in messages)
    for path in set(files.values()):
        assert "Be very brief.\nAsk before pushing to any shared branch.\nRun the tests." in path.read_text()
    assert instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS) == []


def test_first_run_keeps_similar_tool_only_wording_in_place(tmp_path):
    files = setup(tmp_path, "Ask before pushing to a shared branch.\n", "Ask before pushing to any shared branch.\n")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["codex"].read_text() == ("Ask before pushing to any shared branch.\n"
                                          "Ask before pushing to a shared branch.\n")
    assert json.loads(baselines.read_text())[instructions.KEY]["local"][".codex/AGENTS.md"] == [
        "Ask before pushing to any shared branch."]
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []
    assert "to any shared branch" not in files["claude"].read_text()


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


def test_codex_additions_follow_their_neighbour_in_every_tool(tmp_path):
    first = "Run the tests.\nCheck the formatter.\n"
    files = setup(tmp_path, first, "Only use the Codex workspace profile here.\n")
    files.update(instructions.files(tmp_path, TOOLS))
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    assert files["gemini"] == files["antigravity"]
    assert "Codex workspace profile" not in files["gemini"].read_text()

    new = "Review the diff.\nUpdate the release notes.\n"
    codex = files["codex"]
    codex.write_text(codex.read_text().replace("Run the tests.\n", "Run the tests.\n" + new))
    messages = instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    assert messages == [f"updated {files[tool]}" for tool in ("claude", "cursor", "gemini")]
    for tool in ("claude", "gemini", "cursor"):
        text = files[tool].read_text()
        assert text.endswith("Run the tests.\n" + new + "Check the formatter.\n")
        assert text.count("Review the diff.") == 1
        assert "Codex workspace profile" not in text
    assert instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS) == []


def test_codex_delete_leaves_tool_local_notes_and_fills_empty_targets(tmp_path):
    files = setup(tmp_path, "Be brief.\nAsk before pushing.\n", "Codex-only note.\n")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["codex"].write_text("Codex-only note.\nBe brief.\n")
    files["cursor"].write_text("")
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["claude"].read_text() == "Be brief.\n"
    assert files["codex"].read_text() == "Codex-only note.\nBe brief.\n"
    assert files["cursor"].read_text().endswith("\n---\nBe brief.\n")


def test_lines_moved_into_local_sections_do_not_signal_deletion(tmp_path):
    files = setup(tmp_path, "Keep replies brief.\nRun pytest.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    codex = files["codex"]
    local = ("<!-- agent-sync: local -->\nRun pytest.\nCodex-only note.\n"
             "<!-- agent-sync: end local -->\n")
    codex.write_text("Keep replies brief.\n" + local)
    files["claude"].write_text("Keep replies brief.\nRun pytest.\nFormat with Ruff.\n")
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["claude"].read_text() == "Keep replies brief.\nRun pytest.\nFormat with Ruff.\n"
    assert files["cursor"].read_text().count("Run pytest.") == 1
    assert codex.read_text() == "Keep replies brief.\nFormat with Ruff.\n" + local
    assert json.loads(baselines.read_text())[instructions.KEY]["lines"] == ["Keep replies brief.", "Format with Ruff."]


def test_conflicting_rewords_stay_in_place_until_resolved(tmp_path):
    files = setup(tmp_path, "Be brief.\nAsk before pushing to a shared branch.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    for tool, wording in [("claude", "any shared branch"), ("codex", "the main branch")]:
        path = files[tool]
        path.write_text(path.read_text().replace("a shared branch", wording))
    snapshot = {tool: path.read_text() for tool, path in files.items()}
    for _ in range(2):
        warning, = instructions.mirror(tmp_path, "claude", baselines=baselines)
        assert warning.startswith("WARN  conflicting rewords")
        assert str(files["claude"]) in warning and str(files["codex"]) in warning
        assert {tool: path.read_text() for tool, path in files.items()} == snapshot
    files["codex"].write_text(snapshot["codex"].replace("the main branch", "any shared branch"))
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["cursor"].read_text().endswith("Ask before pushing to any shared branch.\n")


def test_dry_run_leaves_files_and_baselines_unchanged(tmp_path):
    files = setup(tmp_path, "Keep replies brief.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines, dry=True)
    assert not baselines.exists()
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["codex"].write_text("Keep replies brief.\nRun pytest.\n")
    snapshot = {path: path.read_bytes() for path in [*files.values(), baselines]}
    assert instructions.mirror(tmp_path, "claude", baselines=baselines, dry=True) == [
        f"would update {files['claude']}", f"would update {files['cursor']}"]
    assert {path: path.read_bytes() for path in snapshot} == snapshot


def test_enabling_an_existing_file_does_not_delete_shared_instructions(tmp_path):
    files = setup(tmp_path, "Keep replies brief.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    gemini = instructions.files(tmp_path, ("gemini",))["gemini"]
    gemini.parent.mkdir()
    gemini.write_text("Run pytest.\n")
    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    assert files["codex"].read_text() == "Keep replies brief.\nRun pytest.\n"
    assert gemini.read_text() == "Run pytest.\nKeep replies brief.\n"
