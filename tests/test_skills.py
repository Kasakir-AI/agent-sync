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


def test_plugin_skills_only_for_the_plugins_you_choose(tmp_path):
    shared = tmp_path / ".agents/skills"
    make_plugin(tmp_path, "writing@market", ["edit-prose", "outline"])
    make_plugin(tmp_path, "lsp@market", ["lsp-only"])
    make_plugin(tmp_path, "off@market", ["off-skill"], enabled=False)
    make_plugin(tmp_path, "project@market", ["project-skill"], scope="project")

    assert skills.mirror(tmp_path, "claude") == []                       # opt-in: nothing by default
    out = skills.mirror(tmp_path, "claude", [], plugin_skills=["writing", "off", "project@market", "missing"])
    assert out == [
        "WARN  plugin_skills: off is not a Claude Code plugin installed for your user (or it is switched off)",
        "WARN  plugin_skills: project@market is not a Claude Code plugin installed for your user "
        "(or it is switched off)",
        "WARN  plugin_skills: missing is not a Claude Code plugin installed for your user (or it is switched off)",
        f"copied skill edit-prose (from plugin writing@market) to {shared}",
        f"copied skill outline (from plugin writing@market) to {shared}"]
    assert "writing's outline" in (shared / "outline/SKILL.md").read_text()
    assert not (tmp_path / ".claude/skills").exists()                     # Claude Code has them already
    assert skills.mirror(tmp_path, "claude", plugin_skills=["writing@market"]) == []

    assert skills.mirror(tmp_path, "claude", plugin_skills=[]) == [
        f"removed skill edit-prose from {shared} (its plugin is no longer chosen or installed)",
        f"removed skill outline from {shared} (its plugin is no longer chosen or installed)"]
    assert not (shared / "outline").exists()


def test_plugin_skills_from_manifest_paths_and_name_clashes(tmp_path):
    shared = tmp_path / ".agents/skills"
    root = make_plugin(tmp_path, "a@m", ["shared-name"], manifest_skills=["./extra", "./one-skill"])
    make_skill(root / "extra", "extra-skill")
    make_skill(root, "one-skill")
    make_plugin(tmp_path, "b@m", ["shared-name", "mine"])
    make_skill(tmp_path / ".claude/skills", "mine", "my own")
    make_skill(shared, "hand-made")
    make_plugin(tmp_path, "c@m", ["hand-made"])

    out = skills.mirror(tmp_path, "claude", plugin_skills=["a", "b", "c"])
    assert out == [
        "WARN  skill shared-name is in plugins a@m and b@m; only the one from a@m is copied",
        "WARN  skill mine from plugin b@m has the name of one of your own skills; only your own is copied",
        f"copied skill mine to {shared}",
        f"copied skill shared-name (from plugin a@m) to {shared}",
        f"copied skill extra-skill (from plugin a@m) to {shared}",
        f"copied skill one-skill (from plugin a@m) to {shared}",
        f"WARN  {shared / 'hand-made'} exists and is not a copy agent-sync made; left alone"]
    assert "my own" in (shared / "mine/SKILL.md").read_text()
    assert "a's shared-name" in (shared / "shared-name/SKILL.md").read_text()


def test_plugin_skills_with_codex_as_source(tmp_path):
    shared = tmp_path / ".agents/skills"
    make_plugin(tmp_path, "writing@market", ["outline"])
    make_skill(shared, "codex-own")
    make_plugin(tmp_path, "other@market", ["from-old-source"])
    old = shared / "from-old-source"                   # a copy made while Claude Code was the source
    make_skill(shared, "from-old-source")
    (old / skills.MARKER).write_text("copied by agent-sync from somewhere\n")

    out = skills.mirror(tmp_path, "codex", plugin_skills=["writing", "other"])
    assert out == [f"copied skill outline (from plugin writing@market) to {shared}",
                   f"WARN  {old} is a copy from an earlier source tool; left alone",
                   f"copied skill codex-own to {tmp_path / '.claude/skills'}"]
    assert "from-old-source" in (old / "SKILL.md").read_text()
    assert not (tmp_path / ".claude/skills/outline").exists()             # not copied back into Claude Code
    assert [line for line in skills.mirror(tmp_path, "codex", plugin_skills=["writing", "other"])
            if not line.startswith("WARN")] == []
    assert skills.mirror(tmp_path, "codex") == [
        f"removed skill outline from {shared} (its plugin is no longer chosen or installed)"]
    assert old.exists()


def test_a_copy_that_changes_origin_is_recopied(tmp_path):
    shared = tmp_path / ".agents/skills"
    make_plugin(tmp_path, "writing@market", ["outline"])
    skills.mirror(tmp_path, "claude", plugin_skills=["writing"])
    root = tmp_path / ".claude/plugins/cache/market/writing/1.0.0"
    make_skill(tmp_path / ".claude/skills", "outline", "writing's outline")    # same text, now your own skill
    assert skills.same_tree(root / "skills/outline", tmp_path / ".claude/skills/outline")
    assert skills.mirror(tmp_path, "claude", plugin_skills=["writing"]) == [
        "WARN  skill outline from plugin writing@market has the name of one of your own skills; "
        "only your own is copied",
        f"copied skill outline to {shared}"]
    assert not skills.is_plugin_copy(shared / "outline")


def test_a_link_with_a_plugin_skill_name_is_left_alone(tmp_path):
    shared = tmp_path / ".agents/skills"
    make_plugin(tmp_path, "writing@market", ["outline", "gone"])
    shared.mkdir(parents=True)
    (shared / "outline").symlink_to(tmp_path / "elsewhere")              # dangling
    (shared / "gone").symlink_to(tmp_path / ".claude/plugins")
    assert skills.mirror(tmp_path, "claude", plugin_skills=["writing"]) == [
        f"WARN  {shared / 'gone'} is a link agent-sync did not make; left alone",
        f"WARN  {shared / 'outline'} is a link agent-sync did not make; left alone"]
    assert (shared / "outline").is_symlink() and (shared / "gone").is_symlink()


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
