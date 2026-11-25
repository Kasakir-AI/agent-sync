import json

import pytest

from agent_sync import instructions, pending, writes
from agent_sync.registry import TOOLS
from tests.test_pending import approve_current

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


def test_a_source_deletion_waits_and_preserves_tool_local_notes(tmp_path):
    files = setup(tmp_path, "Be brief.\nAsk before pushing.\n", "Codex-only note.\n")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert json.loads(baselines.read_text())[instructions.KEY]["lines"] == ["Be brief.", "Ask before pushing."]
    files["claude"].write_text("Be brief.\n")
    logs = []
    for now in (100, 10_000_000):
        with pending.during(baselines.parent, [], lambda *line: logs.append(line), False, now=now):
            instructions.mirror(tmp_path, "claude", baselines=baselines)
        assert "Ask before pushing." in files["codex"].read_text()
    approve_current(baselines.parent)
    with pending.during(baselines.parent, [], lambda *line: logs.append(line), False, now=10_000_001):
        instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert len(logs) == 1 and logs[0][0] == "WARN" and 'instruction "Ask before pushing."' in logs[0][1]
    assert files["codex"].read_text() == "Codex-only note.\nBe brief.\n"


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


def test_source_rewords_spread_and_gemini_rewords_are_held(tmp_path):
    old, new = "Ask before pushing to a shared branch.", "Ask before pushing to any shared branch."
    files = setup(tmp_path, f"Be brief.\n{old}\nRun the tests.\n", "")
    files.update(instructions.files(tmp_path, TOOLS))
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)

    gemini = files["gemini"]
    gemini.write_text(gemini.read_text().replace(old, new))
    files["claude"].write_text(f"Be very brief.\n{old}\nRun the tests.\n")
    warning = (f'WARN  {gemini} rewords 1 shared line(s) (first: "{old}" -> "{new}"); '
               'the other tools keep the old wording. Run agent-sync review to choose.')
    for i in range(2):
        messages = instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
        assert warning in messages
        if i:
            assert messages == [warning]
        else:                                          # a source reword spreads, and says so once
            assert messages[0] == ('info  .claude/CLAUDE.md rewords "Be brief." -> "Be very brief."; '
                                   'the other tools get the new wording')
        for path in set(files.values()):
            wording = new if path == gemini else old
            assert f"Be very brief.\n{wording}\nRun the tests." in path.read_text()
    assert json.loads(baselines.read_text())[instructions.KEY]["held"][".gemini/GEMINI.md"] == {
        "missing": [], "reworded": {old: new}}
    gemini.write_text(gemini.read_text().replace(new, old))
    assert instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS) == []
    assert "held" not in json.loads(baselines.read_text())[instructions.KEY]


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


def test_non_source_deletions_are_held_even_in_empty_files(tmp_path):
    source = "Be brief.\nAsk before pushing.\n"
    files = setup(tmp_path, source, "Codex-only note.\n")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    before = {tool: path.read_text() for tool, path in files.items()}
    files["codex"].write_text("Codex-only note.\nBe brief.\n")
    files["cursor"].write_text("")
    for _ in range(2):
        codex, cursor = instructions.mirror(tmp_path, "claude", baselines=baselines)
        assert f'{files["codex"]} no longer has 1 shared line(s) (first: "Ask before pushing.")' in codex
        assert f'{files["cursor"]} no longer has 2 shared line(s) (first: "Be brief.")' in cursor
        assert "Run agent-sync review to remove them everywhere or put them back." in codex
        assert files["claude"].read_text() == source
        assert files["codex"].read_text() == "Codex-only note.\nBe brief.\n"
        assert files["cursor"].read_text() == ""
    for tool in ("codex", "cursor"):
        files[tool].write_text(before[tool])
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []
    assert "held" not in json.loads(baselines.read_text())[instructions.KEY]


@pytest.mark.parametrize("section", ["local", "private", "memory"])
@pytest.mark.parametrize("moved_tool", ["claude", "codex"])
def test_lines_moved_into_hidden_sections_are_neither_deleted_nor_restored(tmp_path, section, moved_tool):
    files = setup(tmp_path, "Keep replies brief.\nRun pytest.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    moved = files[moved_tool]
    local = (f"<!-- agent-sync: {section} -->\nRun pytest.\nCodex-only note.\n"
             f"<!-- agent-sync: end {section} -->\n")
    moved.write_text("Keep replies brief.\n" + local)
    for _ in range(2):
        with pending.during(baselines.parent, [], lambda *_: None, False):
            assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []
        assert pending.load(baselines.parent) == {}
        assert moved.read_text() == "Keep replies brief.\n" + local
        assert all(path.read_text().count("Run pytest.") == 1 for path in files.values())


def test_source_reword_reaches_unedited_copies_when_codex_also_rewords(tmp_path):
    files = setup(tmp_path, "Be brief.\nAsk before pushing to a shared branch.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    for tool, wording in [("claude", "any shared branch"), ("codex", "the main branch")]:
        path = files[tool]
        path.write_text(path.read_text().replace("a shared branch", wording))
    codex = files["codex"].read_text()
    info, warning, update = instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert info.startswith('info  .claude/CLAUDE.md rewords "Ask before pushing to a shared branch."')
    assert warning.startswith(f'WARN  {files["codex"]} rewords 1 shared line(s)')
    assert update == f'updated {files["cursor"]}'
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == [warning]
    assert files["codex"].read_text() == codex
    assert files["cursor"].read_text().endswith("Ask before pushing to any shared branch.\n")
    files["codex"].write_text(codex.replace("the main branch", "any shared branch"))
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []


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
    assert files["codex"].read_text() == "Keep replies brief.\n"        # what Gemini CLI had stays its own
    assert gemini.read_text() == "Run pytest.\nKeep replies brief.\n"
    stored = json.loads(baselines.read_text())[instructions.KEY]
    assert stored["local"][".gemini/GEMINI.md"] == ["Run pytest."]
    assert instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS) == []


def test_first_seed_keeps_private_sections_and_key_blocks_local(tmp_path):
    secret = "sk-proj-" + "A1" * 10
    key_body = "personal-key-material"
    local = ("<!-- agent-sync: private -->\nPersonal notes.\n<!-- agent-sync: end private -->\n"
             f"{secret}\n-----BEGIN PRIVATE KEY-----\n{key_body}\n-----END PRIVATE KEY-----\n")
    source = local + "Ask before changing credentials.\n"
    files = setup(tmp_path, source, "")
    messages = instructions.mirror(tmp_path, "claude")
    warning, = [line for line in messages if line.startswith("WARN")]
    assert warning == (f"WARN  {files['claude']} has 4 line(s) that look like secrets "
                       "(OpenAI/Anthropic API key, private key); they stay in that file only.")
    assert secret not in warning and key_body not in warning
    assert files["claude"].read_text() == source
    assert files["codex"].read_text() == "Ask before changing credentials.\n"
    assert files["cursor"].read_text().split("---\n")[-1] == files["codex"].read_text()


@pytest.mark.parametrize("changed_tool", ["claude", "codex"])
def test_secret_additions_and_rewords_stay_in_the_edited_file(tmp_path, changed_tool):
    old = "Use the service account when running the tests."
    files = setup(tmp_path, old + "\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    secret = "ghp_" + "A1" * 18
    replacement = f"Use the service account when running the tests with token {secret}."
    changed = files[changed_tool]
    changed.write_text(f"{replacement}\npassword=R4ndomFixtureValue\nRun Ruff.\n")
    for _ in range(2):
        messages = instructions.mirror(tmp_path, "claude", baselines=baselines)
        warning = (f"WARN  {changed} has 2 line(s) that look like secrets (GitHub token, password assignment); "
                   "they stay in that file only.")
        assert messages.count(warning) == 1
        assert "R4ndomFixtureValue" not in "\n".join(messages)
        assert secret not in "\n".join(messages) and secret not in baselines.read_text()
        assert changed.read_text() == f"{replacement}\npassword=R4ndomFixtureValue\nRun Ruff.\n"
        for path in set(files.values()) - {changed}:
            text = path.read_text()
            assert secret not in text and "R4ndomFixtureValue" not in text
            assert "Run Ruff." in text


@pytest.mark.parametrize("source_text", [None, "", "\n<!-- a comment -->\n"])
def test_a_missing_or_empty_source_changes_nothing_and_asks_nothing(tmp_path, source_text):
    files = setup(tmp_path, "Be brief.\nAsk first.\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    if source_text is None:
        files["claude"].unlink()
    else:
        files["claude"].write_text(source_text)
    before = {path: path.read_text() for path in (files["codex"], files["cursor"], baselines)}
    logs = []
    with pending.during(tmp_path / "state", [], lambda *line: logs.append(line), False):
        warning, = instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert warning.startswith(f"WARN  {files['claude']} is missing or has no shared lines")
    assert logs == [] and {path: path.read_text() for path in before} == before


def test_many_source_deletions_at_once_are_held_behind_one_question(tmp_path):
    source = "".join(f"Rule {n}.\n" for n in range(1, 7))
    files = setup(tmp_path, source, "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["claude"].write_text("Rule 1.\nRule 2.\n")
    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False):
        instructions.mirror(tmp_path, "claude", baselines=baselines)
    item, = pending.load(state).values()
    assert "4 shared instruction lines" in item["what"]
    assert files["codex"].read_text() == source
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["codex"].read_text() == "Rule 1.\nRule 2.\n"


def test_an_edited_local_line_stays_local(tmp_path):
    files = setup(tmp_path, "Be brief.\n", "Use the Codex workspace profile here.\n")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["codex"].write_text("Use the Codex workspace profile everywhere.\nBe brief.\n")
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []
    assert "workspace profile" not in files["claude"].read_text() + files["cursor"].read_text()
    assert json.loads(baselines.read_text())[instructions.KEY]["local"][".codex/AGENTS.md"] == [
        "Use the Codex workspace profile everywhere."]


def test_a_line_review_kept_as_one_tools_own_does_not_spread(tmp_path):
    files = setup(tmp_path, "Be brief.\n", "Be brief.\n")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["codex"].write_text("Be brief.\nCodex review note.\n")
    stored = json.loads(baselines.read_text())
    stored[instructions.KEY]["kept"] = {str(files["codex"].resolve()): ["Codex review note."]}
    baselines.write_text(json.dumps(stored))
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []
    assert "Codex review note." not in files["claude"].read_text()


def test_a_line_only_in_a_local_section_is_still_added_to_the_shared_part(tmp_path):
    local = "<!-- agent-sync: local -->\nFormat with Ruff.\n<!-- agent-sync: end local -->\n"
    files = setup(tmp_path, "Be brief.\n", "Be brief.\n" + local)
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["claude"].write_text("Be brief.\nFormat with Ruff.\n")
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["codex"].read_text() == "Be brief.\nFormat with Ruff.\n" + local


def test_seeding_and_additions_keep_code_blocks_and_repeated_lines_whole(tmp_path):
    source = "# Setup\n\n---\n\n```sh\nnpm ci\n\n```\n\n---\n\nDone.\n"
    files = setup(tmp_path, source, "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["codex"].read_text() == source            # the whole document, repeats included

    added = "\n## Test\n\n```sh\nnpm test\n```\n\n---\n\n- [ ] check\n"
    files["claude"].write_text(source + added)
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    codex = files["codex"].read_text()
    assert codex.count("```") == 4 and codex.count("---") == 3
    assert codex.endswith("Done.\n## Test\n```sh\nnpm test\n```\n---\n- [ ] check\n")


@pytest.mark.parametrize("extra", ["", "Prepare release notes.\nInspect plugin metadata.\nWatch CI.\nRecord notes.\n"])
def test_batch_wait_only_holds_changes_touching_the_deleted_line(tmp_path, extra):
    shared = "Keep replies brief.\nCheck backup retention.\nCount loaded skills.\nVerify repository URLs.\n"
    shared += extra
    removed = "Enable auto-formatting.\n"
    source = shared + removed
    files = setup(tmp_path, source, "")
    state = tmp_path / "state"
    baselines = state / "baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    independent = "Run the tests.\n"
    updated = shared.replace("Keep replies brief.", "Keep replies very brief.") + independent
    files["claude"].write_text(updated)
    files["codex"].write_text(source + removed)
    with pending.during(state, [], lambda *_: None, False):
        instructions.mirror(tmp_path, "claude", baselines=baselines)
    item, = pending.load(state).values()
    assert "1 shared instruction line" in item["what"]
    assert files["claude"].read_text() == updated
    assert files["codex"].read_text() == updated + removed * 2
    assert files["cursor"].read_text().endswith(updated + removed)

    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["codex"].read_text() == updated


def test_batch_approval_matches_exact_deletion_set(tmp_path):
    from hashlib import sha256

    source = "".join(f"Rule {n}.\n" for n in range(1, 7))
    files = setup(tmp_path, source, "")
    state = tmp_path / "state"
    baselines = state / "baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["claude"].write_text("Rule 1.\nRule 6.\n")
    with pending.during(state, [], lambda *_: None, False):
        instructions.mirror(tmp_path, "claude", baselines=baselines)
    digest = sha256(b"Rule 2.\nRule 3.\nRule 4.\nRule 5.").hexdigest()[:12]
    assert list(pending.load(state)) == [f"instructions:{digest}"]
    approve_current(state)
    files["claude"].write_text("Rule 1.\nRule 4.\n")
    with pending.during(state, [], lambda *_: None, False):
        instructions.mirror(tmp_path, "claude", baselines=baselines)
    item, = pending.load(state).values()
    assert not item["approved"]
    assert files["codex"].read_text() == source


def test_an_edited_shared_line_is_held_even_if_the_old_line_is_private(tmp_path):
    old, new = "Run tests before pushing.", "Run all tests before pushing."
    files = setup(tmp_path, old + "\n", "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    codex = files["codex"]
    changed = f"{new}\n<!-- agent-sync: private -->\n{old}\n<!-- agent-sync: end private -->\n"
    codex.write_text(changed)
    warning, = instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert "rewords 1 shared line" in warning
    assert codex.read_text() == changed
    assert files["claude"].read_text() == old + "\n"


@pytest.mark.parametrize("donor", ["claude", "codex"])
@pytest.mark.parametrize("changed", ["# Setup\n---\n---\n# Build\n---\n# Test\n",
                                     "---\n# Setup\n---\n# Build\n---\n# Test\n"])
def test_a_standalone_repeat_is_mirrored_in_place(tmp_path, donor, changed):
    source = "# Setup\n---\n# Build\n---\n# Test\n"
    files = setup(tmp_path, source, "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files[donor].write_text(changed)
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["claude"].read_text() == files["codex"].read_text() == changed
    assert instructions.mirror(tmp_path, "claude", baselines=baselines) == []


def test_first_seed_preserves_literal_comments_inside_code_fences(tmp_path):
    source = "# Setup\n\n<!-- file note -->\n```html\n<!-- literal example -->\n\n<p>Hello</p>\n```\n"
    files = setup(tmp_path, source, "")
    instructions.mirror(tmp_path, "claude")
    assert files["codex"].read_text() == source.replace("<!-- file note -->\n", "")


@pytest.mark.parametrize(("changed_tool", "staging_tool"), [("claude", "codex"), ("codex", "codex"),
                                                          ("claude", "cursor")])
def test_instruction_retry_replans_after_file_changes_during_staging(tmp_path, monkeypatch, changed_tool, staging_tool):
    old = "Run tests before pushing.\n"
    first = "Run all tests before pushing.\n"
    concurrent = "Run tests and lint before pushing.\n"
    files = setup(tmp_path, old, "")
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines)
    files["claude"].write_text(first)
    stage = writes.stage
    raced = False

    def concurrent_edit(target, data, mode):
        nonlocal raced
        temp = stage(target, data, mode)
        if target == files[staging_tool] and not raced:
            raced = True
            files[changed_tool].write_text(concurrent)
        return temp

    monkeypatch.setattr(writes, "stage", concurrent_edit)
    notes = instructions.mirror(tmp_path, "claude", baselines=baselines)
    assert files["codex"].read_text() == concurrent
    if changed_tool == "codex":
        assert any("rewords 1 shared line" in note for note in notes)
        assert files["cursor"].read_text().endswith(first)
    else:
        assert files["cursor"].read_text().endswith(concurrent)
