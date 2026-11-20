from agent_sync import instructions


def test_instructions_block_keeps_the_targets_own_text(tmp_path):
    files = instructions.files(tmp_path)
    files["claude"].parent.mkdir(parents=True)
    files["claude"].write_text("# Mine\n\nBe brief.\n")
    files["codex"].parent.mkdir(parents=True)
    files["codex"].write_text("Codex-only note.\n")

    assert instructions.mirror(tmp_path, "claude", dry=True) == [f"would update {files['codex']}",
                                                                 f"would update {files['cursor']}"]
    assert files["codex"].read_text() == "Codex-only note.\n" and not files["cursor"].exists()
    instructions.mirror(tmp_path, "claude")
    codex = files["codex"].read_text()
    assert codex.startswith("Codex-only note.\n\n<!-- agent-sync: begin") and "Be brief." in codex
    assert "alwaysApply: true" in files["cursor"].read_text()
    assert instructions.mirror(tmp_path, "claude") == []                 # idempotent

    files["claude"].write_text("# Mine\n\nBe very brief.\n")
    instructions.mirror(tmp_path, "claude")
    codex = files["codex"].read_text()
    assert "Be very brief." in codex and "Be brief." not in codex and codex.count("agent-sync: begin") == 1


def test_cursor_cannot_be_the_instructions_source(tmp_path):
    assert instructions.mirror(tmp_path, "cursor")[0].startswith("skip")


def test_a_target_that_already_has_every_source_line_gets_no_block(tmp_path):
    files = instructions.files(tmp_path)
    files["claude"].parent.mkdir(parents=True)
    files["claude"].write_text("# Mine\n\nBe brief.\n")
    files["codex"].parent.mkdir(parents=True)
    files["codex"].write_text("Codex-only note.\n# Mine\n\nBe brief.\n")
    files["cursor"].parent.mkdir(parents=True)

    assert instructions.mirror(tmp_path, "claude") == [f"updated {files['cursor']}"]
    assert files["codex"].read_text() == "Codex-only note.\n# Mine\n\nBe brief.\n"

    stale = "Codex-only note.\n# Mine\n\nBe brief.\n\n" + instructions.BEGIN.format(src="CLAUDE.md") + "\nOld.\n" \
        + instructions.END + "\n"
    files["codex"].write_text(stale)
    instructions.mirror(tmp_path, "claude")
    assert files["codex"].read_text() == "Codex-only note.\n# Mine\n\nBe brief.\n"
