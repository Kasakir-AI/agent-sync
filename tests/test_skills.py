import json

from agent_sync import skills


def make_skill(folder, name, text="Do the thing."):
    (folder / name).mkdir(parents=True)
    (folder / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {text}\n---\n{text}\n")


def test_own_skills_copied_updated_and_removed(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "mine")
    make_skill(claude, "from-package")
    make_skill(claude, "skipped")
    (tmp_path / ".agents/.skill-lock.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agents/.skill-lock.json").write_text(json.dumps({"skills": {"from-package": {"source": "a/b"}}}))

    assert skills.mirror(tmp_path, "claude", ["skipped"], dry=True) == [f"would copy skill mine to {shared}"]
    assert not shared.exists() or not (shared / "mine").exists()
    assert skills.mirror(tmp_path, "claude", ["skipped"]) == [f"copied skill mine to {shared}"]
    assert (shared / "mine/SKILL.md").read_text() == (claude / "mine/SKILL.md").read_text()
    assert not (shared / "from-package").exists() and not (shared / "skipped").exists()
    assert skills.mirror(tmp_path, "claude", ["skipped"]) == []          # nothing changed, nothing to do

    (claude / "mine/SKILL.md").write_text("changed")
    assert skills.mirror(tmp_path, "claude", ["skipped"]) == [f"copied skill mine to {shared}"]
    assert (shared / "mine/SKILL.md").read_text() == "changed"

    (claude / "mine/SKILL.md").unlink()
    (claude / "mine").rmdir()
    assert skills.mirror(tmp_path, "claude") == [
        f"copied skill skipped to {shared}",
        f"removed skill mine from {shared} (no longer in the source tool)"]
    assert not (shared / "mine").exists()


def test_a_folder_agent_sync_did_not_make_is_left_alone(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "same-name", "Claude's version")
    make_skill(shared, "same-name", "a hand-made version")
    out = skills.mirror(tmp_path, "claude")
    assert out == [f"WARN  {shared / 'same-name'} exists and is not a copy agent-sync made; left alone"]
    assert "hand-made" in (shared / "same-name/SKILL.md").read_text()


def test_links_to_the_source_count_as_shared(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "linked")
    shared.mkdir(parents=True)
    (shared / "linked").symlink_to(claude / "linked")
    assert skills.mirror(tmp_path, "claude") == []


def test_codex_as_source_copies_to_claude_only(tmp_path):
    make_skill(tmp_path / ".agents/skills", "codex-skill")
    assert skills.mirror(tmp_path, "codex") == [f"copied skill codex-skill to {tmp_path / '.claude/skills'}"]


def test_dropped_package_skills(tmp_path):
    checkout = tmp_path / "checkout"
    make_skill(checkout / "skills", "kept")
    lock = {"skills": {"kept": {"source": "o/p", "skillPath": "skills/kept/SKILL.md"},
                       "gone": {"source": "o/p", "skillPath": "skills/gone/SKILL.md"},
                       "other": {"source": "x/y", "skillPath": "skills/other/SKILL.md"}}}
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents/.skill-lock.json").write_text(json.dumps(lock))
    assert skills.dropped(tmp_path, "o/p", checkout) == ["gone"]
