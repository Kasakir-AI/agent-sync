import json
import shutil

from agent_sync import duplicates, skills
from tests.test_skills import make_plugin, make_skill


def codex_plugin(home, pid, names, version="1.0.0"):
    name, market = pid.split("@")
    root = home / ".codex/plugins/cache" / market / name / version / "skills"
    for n in names:
        make_skill(root / "group", n, f"{name}'s {n}")   # Codex finds skills in nested folders
    return root / "group"


def codex_config(home, text):
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex/config.toml").write_text(text)


def package(home, source, names):
    shared = home / ".agents/skills"
    for n in names:
        make_skill(shared, n, f"{source}'s {n}")
    lock = home / ".agents/.skill-lock.json"
    data = json.loads(lock.read_text()) if lock.exists() else {"skills": {}}
    data["skills"] |= {n: {"source": source} for n in names}
    lock.write_text(json.dumps(data))


def test_a_pack_installed_as_a_package_and_a_codex_plugin_is_reported(tmp_path):
    package(tmp_path, "someone/skills", ["grilling", "tdd"])
    codex_plugin(tmp_path, "pack@someone", ["grilling", "tdd"])
    codex_config(tmp_path, '[plugins."pack@someone"]\nenabled = true\n')
    assert duplicates.report(tmp_path) == [
        "Codex loads 2 skills more than once, from Codex plugin pack@someone; "
        "~/.agents/skills (package someone/skills): grilling, tdd"]


def test_switched_off_and_allowed_skills_are_not_reported(tmp_path):
    package(tmp_path, "someone/skills", ["grilling", "tdd"])
    codex_plugin(tmp_path, "pack@someone", ["grilling", "tdd"])
    codex_config(tmp_path, '[plugins."pack@someone"]\nenabled = false\n')
    assert duplicates.report(tmp_path) == []

    codex_config(tmp_path, '[plugins."pack@someone"]\nenabled = true\n\n[[skills.config]]\n'
                           f'path = "{tmp_path}/.codex/plugins/cache/someone/pack/1.0.0/skills/group/tdd/SKILL.md"\n'
                           'enabled = false\n')
    assert duplicates.report(tmp_path) == [
        "Codex loads grilling more than once, from Codex plugin pack@someone; "
        "~/.agents/skills (package someone/skills)"]
    assert duplicates.report(tmp_path, allow=["Grilling"]) == []


def test_a_plugin_skill_is_matched_by_its_name_after_the_colon(tmp_path):
    make_skill(tmp_path / ".codex/skills/.system", "skill-creator")
    make_skill(tmp_path / ".agents/skills", "maker")
    (tmp_path / ".agents/skills/maker/SKILL.md").write_text("---\nname: anthropic:skill-creator\n---\n")
    assert duplicates.report(tmp_path) == [
        "Codex loads skill-creator more than once, from Codex built-in; ~/.agents/skills"]


def test_cursor_reads_one_folder_per_path_and_claude_code_plugins(tmp_path):
    package(tmp_path, "backnotprop/pstack", ["unslop"])
    claude = tmp_path / ".claude/skills"
    claude.mkdir(parents=True)
    (claude / "unslop").symlink_to(tmp_path / ".agents/skills/unslop")   # the skills CLI links Claude Code's copy
    make_skill(claude, "notes", "Claude Code's notes")
    make_skill(tmp_path / ".agents/skills", "notes", "the shared notes")  # same path: Cursor reads Claude's only
    assert duplicates.report(tmp_path) == []

    cursor_pack = tmp_path / ".cursor/plugins/cache/cursor-public/pstack/abc123/skills"
    make_skill(cursor_pack, "unslop")
    make_plugin(tmp_path, "pack@market", ["notes"])
    assert duplicates.report(tmp_path) == [
        "Claude Code loads notes more than once, from Claude Code plugin pack@market; ~/.claude/skills",
        "Cursor loads notes more than once, from Claude Code plugin pack@market; ~/.claude/skills",
        "Cursor loads unslop more than once, from Cursor plugin pstack@cursor-public; "
        "~/.agents/skills (package backnotprop/pstack)"]


def test_a_copy_every_reader_already_loads_is_removed(tmp_path):
    shared = tmp_path / ".agents/skills"
    make_plugin(tmp_path, "writing@market", ["outline"])
    assert skills.mirror(tmp_path, "claude", plugin_skills=["writing"]) == [
        f"copied skill outline (from plugin writing@market) to {shared}"]   # Codex has no other copy

    native = codex_plugin(tmp_path, "writing@market", [])
    shutil.copytree(tmp_path / ".claude/plugins/cache/market/writing/1.0.0/skills/outline", native / "outline")
    codex_config(tmp_path, '[plugins."writing@market"]\nenabled = true\n')
    assert duplicates.report(tmp_path) != []
    assert skills.mirror(tmp_path, "claude", plugin_skills=["writing"]) == [
        f"removed skill outline (from plugin writing@market) from {shared} (already loaded from "
        "Codex: Codex plugin writing@market; Cursor: Claude Code plugin writing@market)"]
    assert not (shared / "outline").exists()
    assert skills.mirror(tmp_path, "claude", plugin_skills=["writing"]) == []
    assert duplicates.report(tmp_path) == []


def test_a_new_plugin_version_with_the_same_files_is_not_copied_again(tmp_path):
    shared = tmp_path / ".agents/skills"
    root = make_plugin(tmp_path, "writing@market", ["outline"])
    skills.mirror(tmp_path, "claude", plugin_skills=["writing"])
    newer = root.with_name("1.0.1")
    shutil.copytree(root, newer)
    index = tmp_path / ".claude/plugins/installed_plugins.json"
    index.write_text(index.read_text().replace(str(root), str(newer)))
    assert skills.mirror(tmp_path, "claude", plugin_skills=["writing"]) == []
    assert str(newer) in skills.marker_text(shared / "outline")


def test_a_copy_codex_switched_off_and_cursor_loads_elsewhere_is_removed(tmp_path):
    shared = tmp_path / ".agents/skills"
    make_plugin(tmp_path, "maker@market", ["skill-creator"])
    skills.mirror(tmp_path, "claude", plugin_skills=["maker"])
    codex_config(tmp_path, f'[[skills.config]]\npath = "{shared}/skill-creator/SKILL.md"\nenabled = false\n')
    assert skills.mirror(tmp_path, "claude", plugin_skills=["maker"]) == [
        f"removed skill skill-creator (from plugin maker@market) from {shared} (already loaded from "
        "Codex: switched off there; Cursor: Claude Code plugin maker@market)"]


def test_a_codex_plugin_from_its_online_catalog_does_not_count(tmp_path):
    package(tmp_path, "obra/superpowers", ["brainstorming"])
    codex_plugin(tmp_path, "superpowers@openai-curated-remote", ["brainstorming"])
    (tmp_path / ".codex/plugins/cache/openai-curated-remote/superpowers/.codex-remote-plugin-install.json").write_text(
        '{"schema_version": 1}')
    assert duplicates.report(tmp_path) == []          # Codex loads none of their skills
