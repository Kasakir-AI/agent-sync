import json

import pytest

from agent_sync import pending, skills
from tests.test_pending import approve_current


def make_skill(folder, name, text="Do the thing."):
    (folder / name).mkdir(parents=True)
    (folder / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {text}\n---\n{text}\n")


def mirror_removals(home, source):
    state = home / "state"
    with pending.during(state, [], lambda *_: None, False):
        skills.mirror(home, source)
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        return skills.mirror(home, source)


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
    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False):
        assert skills.mirror(tmp_path, "claude") == [f"copied skill skipped to {shared}"]
    assert (shared / "mine").exists()
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        assert skills.mirror(tmp_path, "claude") == [
            f"removed skill mine from {shared} (no longer in the source tool)"]
    assert not (shared / "mine").exists()


def test_a_folder_agent_sync_did_not_make_is_left_alone(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "same-name", "Claude's version")
    make_skill(shared, "same-name", "a hand-made version")
    out = skills.mirror(tmp_path, "claude")
    assert out == [f"WARN  {shared / 'same-name'} exists and is not a copy agent-sync made; left alone"]
    assert "hand-made" in (shared / "same-name/SKILL.md").read_text()


@pytest.mark.parametrize("is_file", [False, True], ids=["empty-folder", "file"])
def test_an_unmanaged_non_skill_at_the_destination_does_not_stop_mirroring(tmp_path, is_file):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "blocked")
    make_skill(claude, "later")
    shared.mkdir(parents=True)
    blocked = shared / "blocked"
    if is_file:
        blocked.write_text("Unrelated content")
    else:
        blocked.mkdir()

    assert skills.mirror(tmp_path, "claude") == [
        f"WARN  {blocked} exists and is not a copy agent-sync made; left alone",
        f"copied skill later to {shared}",
    ]
    if is_file:
        assert blocked.read_text() == "Unrelated content"
    else:
        assert list(blocked.iterdir()) == []
    assert (shared / "later/SKILL.md").read_text() == (claude / "later/SKILL.md").read_text()


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


def make_plugin(home, pid, skills_in, enabled=True, scope="user", manifest_skills=None):
    """Install a fake Claude Code plugin `pid` (plugin@marketplace) with the given skills."""
    name, market = pid.split("@")
    root = home / ".claude/plugins/cache" / market / name / "1.0.0"
    for s in skills_in:
        make_skill(root / "skills", s, f"{name}'s {s}")
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name} | ({"skills": manifest_skills} if manifest_skills else {})
    (root / ".claude-plugin/plugin.json").write_text(json.dumps(manifest))
    index = home / ".claude/plugins/installed_plugins.json"
    data = json.loads(index.read_text()) if index.exists() else {"version": 2, "plugins": {}}
    data["plugins"][pid] = [{"scope": scope, "installPath": str(root), "version": "1.0.0"}]
    index.write_text(json.dumps(data))
    settings = home / ".claude/settings.json"
    s = json.loads(settings.read_text()) if settings.exists() else {}
    s.setdefault("enabledPlugins", {})[pid] = enabled
    settings.write_text(json.dumps(s))
    return root


def test_plugin_skills_are_never_copied_and_old_copies_go(tmp_path):
    shared = tmp_path / ".agents/skills"
    root = make_plugin(tmp_path, "writing@market", ["outline", "edit-prose"])
    make_skill(shared, "outline", "writing's outline")                 # a copy an earlier version made
    (shared / "outline" / skills.MARKER).write_text(f"{skills.PLUGIN_MARK}writing@market: {root}/skills/outline\n")

    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, True) as run:
        assert skills.mirror(tmp_path, "claude", dry=True) == []
        assert list(run.questions()) == ["skill-copy:.agents/skills:outline"]
    assert (shared / "outline").exists() and not state.exists()
    assert mirror_removals(tmp_path, "claude") == [
        f"removed skill outline from {shared} (agent-sync no longer copies plugin skills)"]
    assert not (shared / "outline").exists() and not (shared / "edit-prose").exists()
    assert skills.mirror(tmp_path, "claude") == []


def test_old_plugin_copies_go_when_codex_is_the_source(tmp_path):
    shared = tmp_path / ".agents/skills"
    root = make_plugin(tmp_path, "writing@market", ["outline"])
    make_skill(shared, "outline", "writing's outline")
    (shared / "outline" / skills.MARKER).write_text(f"{skills.PLUGIN_MARK}writing@market: {root}/skills/outline\n")
    make_skill(shared, "codex-own")
    assert mirror_removals(tmp_path, "codex") == [
        f"removed skill outline from {shared} (agent-sync no longer copies plugin skills)"]
    assert (shared / "codex-own").exists()


def test_a_copy_whose_name_another_skill_has_is_kept_with_a_warning(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "outline", "my outline")
    make_skill(claude, "mine")
    make_skill(tmp_path / ".codex/plugins/cache/market/writing/1.0.0/skills", "outline", "the plugin's outline")
    (tmp_path / ".codex/config.toml").write_text('[plugins."writing@market"]\nenabled = true\n')
    make_skill(shared, "outline", "my outline")                         # copied before Codex had the plugin
    (shared / "outline" / skills.MARKER).write_text(f"{skills.COPY_MARK}{claude / 'outline'}\n")

    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False):
        assert skills.mirror(tmp_path, "claude") == [
            f"copied skill mine to {shared}",
            f"WARN  skill outline: Codex also loads a different skill of that name, so {shared} gets no new copy "
            "and keeps any it has. Rename one to load both"]
    assert not pending.load(state)                       # nothing is offered for removal
    assert (shared / "outline").exists() and (shared / "mine").exists()


def test_a_link_with_a_skill_name_is_left_alone(tmp_path):
    shared = tmp_path / ".agents/skills"
    make_skill(tmp_path / ".claude/skills", "outline")
    shared.mkdir(parents=True)
    (shared / "outline").symlink_to(tmp_path / "elsewhere")              # dangling
    assert skills.mirror(tmp_path, "claude") == [
        f"WARN  {shared / 'outline'} is a link agent-sync did not make; left alone"]
    assert (shared / "outline").is_symlink()


def test_a_version_an_installer_made_for_each_tool_is_left_alone_quietly(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    (claude / "inbox").mkdir(parents=True)
    (claude / "inbox/SKILL.md").write_text("---\nname: inbox\nallowed-tools: Bash\n---\n!`inbox --open`\n")
    (shared / "inbox/agents").mkdir(parents=True)
    (shared / "inbox/SKILL.md").write_text("---\nname: inbox\n---\nRun `inbox --open`.\n")
    (shared / "inbox/agents/openai.yaml").write_text("name: inbox\n")
    make_skill(claude, "same")
    make_skill(shared, "same")

    assert skills.mirror(tmp_path, "claude") == []
    assert "Run `inbox --open`." in (shared / "inbox/SKILL.md").read_text()


def test_a_package_listed_earlier_keeps_the_names_it_ships(tmp_path):
    first, second = tmp_path / "pstack", tmp_path / "other"
    for name in ("tdd", "teach", "unslop"):
        make_skill(first / "skills", name)
    for name in ("tdd", "grilling"):
        make_skill(second / "skills/engineering", name)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents/.skill-lock.json").write_text(json.dumps({"skills": {
        "tdd": {"source": "a/pstack"}, "other-tdd": {"source": "b/other", "skillName": "tdd"},
        "grilling": {"source": "b/other"}}}))
    taken: set[str] = set()
    assert skills.package_choice(tmp_path, "a/pstack", first, taken) == (None, [])      # all of them
    assert skills.package_choice(tmp_path, "b/other", second, taken) == (["grilling"], ["other-tdd"])
    assert taken == {"tdd", "teach", "unslop", "grilling"}


def test_a_copy_the_user_edited_is_replaced_only_after_approval(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "notes", "first")
    assert skills.mirror(tmp_path, "claude") == [f"copied skill notes to {shared}"]
    (shared / "notes/SKILL.md").write_text("my own edit\n")
    (claude / "notes/SKILL.md").write_text("---\nname: notes\ndescription: second\n---\nsecond\n")
    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False):
        assert skills.mirror(tmp_path, "claude") == []
    assert (shared / "notes/SKILL.md").read_text() == "my own edit\n"
    assert "your edits to the copy of skill notes" in pending.load(state)["skill-copy:.agents/skills:notes"]["what"]
    assert mirror_removals(tmp_path, "claude") == [f"copied skill notes to {shared}"]
    assert "second" in (shared / "notes/SKILL.md").read_text()


def test_a_skill_in_a_folder_whose_name_extends_the_copys_still_counts(tmp_path):
    from agent_sync import duplicates

    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "foo", "same")
    make_skill(shared, "foo-bar", "same")
    (shared / "foo-bar/SKILL.md").write_text((claude / "foo/SKILL.md").read_text())
    seen = duplicates.views(tmp_path, enabled=("claude", "codex", "cursor"))
    assert duplicates.elsewhere(tmp_path, seen, shared, "foo", claude / "foo", readers=("codex",)) == [
        "Codex: ~/.agents/skills"]


def test_shared_copy_survives_until_codex_has_an_active_identical_alternative(tmp_path):
    from tests.test_inventory import write

    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    state = tmp_path / "state"
    enabled = ("claude", "codex", "gemini")
    make_skill(claude, "notes")
    skills.mirror(tmp_path, "claude", enabled=enabled)
    copied = shared / "notes/SKILL.md"
    write(tmp_path, ".codex/config.toml", f'[[skills.config]]\npath = "{copied}"\nenabled = false\n')
    plugin = tmp_path / ".gemini/extensions/pack"
    write(plugin, "gemini-extension.json", {"name": "pack"})
    make_skill(plugin / "skills", "notes")

    with pending.during(state, [], lambda *_: None, False) as run:
        skills.mirror(tmp_path, "claude", enabled=enabled)
        assert not run.questions()
    assert copied.is_file()

    native = tmp_path / ".codex/skills"
    make_skill(native, "notes", "Different content.")
    with pending.during(state, [], lambda *_: None, False) as run:
        skills.mirror(tmp_path, "claude", enabled=enabled)
        assert not run.questions()
    assert copied.is_file()

    alternative = native / "notes/SKILL.md"
    alternative.write_bytes(copied.read_bytes())
    with pending.during(state, [], lambda *_: None, False) as run:
        skills.mirror(tmp_path, "claude", enabled=enabled)
        assert list(run.questions()) == ["skill-copy:.agents/skills:notes"]
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        skills.mirror(tmp_path, "claude", enabled=enabled)
    assert not copied.exists()
    assert alternative.is_file()


def test_plugin_skill_children_and_definition_links_cannot_escape_root(tmp_path):
    root = make_plugin(tmp_path, "pack@market", ["inside"], manifest_skills=["custom"])
    external = tmp_path / "external"
    make_skill(external, "foreign")
    (root / "skills/escaped").symlink_to(external / "foreign", target_is_directory=True)
    (root / "custom").mkdir()
    (root / "custom/escaped").symlink_to(external / "foreign", target_is_directory=True)
    (root / "skills/escaped-definition").mkdir()
    (root / "skills/escaped-definition/SKILL.md").symlink_to(external / "foreign/SKILL.md")
    (external / "return-link").mkdir()
    (external / "return-link/SKILL.md").symlink_to(root / "skills/inside/SKILL.md")
    (root / "skills/return-link").symlink_to(external / "return-link", target_is_directory=True)

    assert set(skills.plugin_skill_dirs(root)) == {"inside"}


def test_a_legacy_skill_copy_with_unknown_previous_source_needs_approval(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(claude, "notes", "new source")
    make_skill(shared, "notes", "my own edit")
    dest = shared / "notes"
    (dest / skills.MARKER).write_text(f"{skills.COPY_MARK}{claude / 'notes'}\n")

    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False):
        skills.mirror(tmp_path, "claude")
    assert "my own edit" in (dest / "SKILL.md").read_text()
    assert "your edits" in pending.load(state)["skill-copy:.agents/skills:notes"]["what"]
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        skills.mirror(tmp_path, "claude")
    assert "new source" in (dest / "SKILL.md").read_text()


def test_a_legacy_copy_identical_to_its_available_previous_source_can_refresh(tmp_path):
    claude, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    previous = tmp_path / "previous"
    make_skill(previous, "notes", "old source")
    make_skill(shared, "notes", "old source")
    make_skill(claude, "notes", "new source")
    dest = shared / "notes"
    (dest / skills.MARKER).write_text(f"{skills.COPY_MARK}{previous / 'notes'}\n")

    assert skills.mirror(tmp_path, "claude") == [f"copied skill notes to {shared}"]
    assert "new source" in (dest / "SKILL.md").read_text()


def test_mirror_keeps_the_previous_skill_if_publishing_the_copy_fails(tmp_path, monkeypatch):
    from pathlib import Path

    source, shared = tmp_path / ".claude/skills", tmp_path / ".agents/skills"
    make_skill(source, "notes", "original")
    skills.mirror(tmp_path, "claude")
    dest = shared / "notes"
    previous = (dest / "SKILL.md").read_bytes()
    (source / "notes/SKILL.md").write_text("revised\n")
    rename = Path.rename

    def rename_skill(path, target):
        if target == dest and path.name == "notes":
            raise OSError("could not publish the staged skill")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", rename_skill)
    with pytest.raises(OSError, match="could not publish"):
        skills.mirror(tmp_path, "claude")
    assert dest.is_dir() and (dest / "SKILL.md").read_bytes() == previous


def test_package_ownership_is_read_where_the_skills_cli_writes_it(tmp_path, monkeypatch):
    """With XDG_STATE_HOME set the skills CLI keeps its lock file there; reading another file would make a package's
    own skills look like someone else's and ask about them on every run."""
    from tests.test_inventory import write

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    write(tmp_path, "state/skills/.skill-lock.json", {"skills": {"notes": {"source": "org/pack"}}})
    write(tmp_path, ".agents/.skill-lock.json", {"skills": {}})
    assert skills.locked(tmp_path) == {"notes": {"source": "org/pack"}}


def test_internal_skills_are_read_from_their_metadata_only(tmp_path):
    """The skills CLI skips a skill only when `metadata.internal` is true; the words elsewhere don't count."""
    make_skill(tmp_path / "pack/skills", "hidden")
    make_skill(tmp_path / "pack/skills", "flow")
    make_skill(tmp_path / "pack/skills", "public")
    for name, extra in (("hidden", "metadata:\n  internal: true\n"), ("flow", "metadata: { internal: true }\n"),
                        ("public", "summary: \"not internal: true at all\"\n")):
        definition = tmp_path / f"pack/skills/{name}/SKILL.md"
        definition.write_text(definition.read_text().replace("description:", extra + "description:"))
    assert skills.shipped(tmp_path / "pack") == ["public"]
