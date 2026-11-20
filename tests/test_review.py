import json
import os
import tomllib

from agent_sync import instructions, review, roles, skills
from agent_sync.config import Config


def write(home, path, body):
    target = home / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


def review_home(home, **settings):
    return review.scan(home, Config(home=home, **settings))


def test_instructions_keep_own_text_and_label_the_cursor_copy(tmp_path):
    paths = instructions.files(tmp_path)
    write(tmp_path, paths["claude"], "Make replies concise.\n")
    write(tmp_path, paths["codex"], "Use examples from the project.\n\n<!-- agent-sync: begin -->\n"
          "Make replies concise.\n<!-- agent-sync: end -->\n")
    write(tmp_path, paths["cursor"], "---\nalwaysApply: true\n---\nMake replies concise.\n")
    group, = review_home(tmp_path).groups
    claude, codex, cursor = group.versions

    assert group.name == "Global instructions" and group.status == "differ"
    assert not group.missing
    assert claude.body == cursor.body == "Make replies concise.\n"
    assert codex.body.strip() == "Use examples from the project."
    assert [version.origin for version in group.versions] == [review.Origin.YOURS, review.Origin.YOURS,
                                                              review.Origin.COPY]


def test_scan_finds_skill_edits_and_merges_links(tmp_path):
    skill = write(tmp_path, ".agents/skills/mine/SKILL.md", "Use this skill.\n")
    notes = write(tmp_path, ".agents/skills/mine/details.txt", "Supporting instructions.")
    marker = write(tmp_path, ".agents/skills/mine/.agent-sync", "copied by agent-sync from ~/.claude/skills/mine\n")
    os.utime(marker, (100, 100))
    os.utime(skill, (100, 100))
    os.utime(notes, (105, 105))
    folder = tmp_path / ".claude/skills"
    folder.mkdir(parents=True)
    (folder / "mine").symlink_to(skill.parent, target_is_directory=True)
    group, = review_home(tmp_path).groups
    version, = group.versions

    assert group.status == "same"
    assert version.path == skill.parent.resolve()
    assert version.tools == ("claude", "codex", "cursor")
    assert version.origin == review.Origin.EDITED_COPY
    assert version.modified == 105


def test_package_skills_and_agents_are_counted_separately(tmp_path):
    write(tmp_path, ".agents/skills/packaged/SKILL.md", "From a package.\n")
    write(tmp_path, ".claude/skills/packaged/SKILL.md", "A separate copy.\n")
    write(tmp_path, ".agents/.skill-lock.json", json.dumps({"skills": {"packaged": {"source": "example/package"}}}))
    write(tmp_path, ".claude/agents/example.md", "Agent settings")
    write(tmp_path, ".codex/agents/example.toml", "")
    write(tmp_path, ".cursor/plugins/local/example-agents/agents/example.md", "")
    scanned = review_home(tmp_path)

    assert scanned.groups == []
    assert scanned.managed == {"example/package": 1, "Claude Code agents": 1, "Codex agents": 1, "Cursor agents": 1}


def test_scan_aligns_plugins_by_bare_name(tmp_path):
    skill = write(tmp_path, ".claude/plugins/cache/market/clip/1/skills/clip-doc/SKILL.md", "Clip documents.\n")
    write(tmp_path, ".claude/plugins/installed_plugins.json", json.dumps({"plugins": {
        "clip@market": [{"scope": "user", "installPath": str(skill.parents[2])}],
    }}))
    write(tmp_path, ".codex/config.toml", '[plugins."clip@registry"]\nenabled = false\n')
    (tmp_path / ".cursor/plugins/local/clip").mkdir(parents=True)
    plugins = review_home(tmp_path, plugin_skills=["clip"]).plugins

    assert list(plugins) == ["clip"]
    assert plugins["clip"]["claude"] == [review.Plugin("clip@market", skills=1, shared=True)]
    assert plugins["clip"]["codex"] == [review.Plugin("clip@registry", enabled=False)]
    assert plugins["clip"]["cursor"] == [review.Plugin("clip")]


def test_similar_cursor_rule_joins_global_instructions(tmp_path):
    common = ("Keep changes small and make each commit clear. Verify commands against a temporary home. "
              "Check the result before reporting it. ")
    write(tmp_path, ".claude/CLAUDE.md", common + "Explain delays when a check takes longer than expected.")
    cursor = write(tmp_path, ".cursor/rules/personal-preferences.mdc",
                   "---\nalwaysApply: true\n---\n" + common + "Use examples when explaining new work.")
    write(tmp_path, ".claude/rules/editor.md", "Tabs, never spaces.")
    scanned = review_home(tmp_path)
    group = next(group for group in scanned.groups if group.kind == "instructions")

    assert group.matched_by.startswith("content ") and group.matched_by.endswith("% alike")
    assert [version.path for version in group.versions if "cursor" in version.tools] == [cursor]
    assert group.missing == ("codex",)
    other, = [group for group in scanned.groups if group.kind == "rule"]
    assert other.missing == ("cursor",)


def test_roles_compare_the_choices_without_the_notes(tmp_path):
    paths = roles.files(tmp_path)
    for tool, path in paths.items():
        write(tmp_path, path, f"# Setup for {tool}\nreviewers: model-a\n\n# Notes for {tool}\n")
    group, = review_home(tmp_path).groups

    assert group.status == "same" and not group.missing
    assert {version.body for version in group.versions} == {"reviewers: model-a"}
    assert [version.origin for version in group.versions] == [review.Origin.YOURS, review.Origin.COPY,
                                                              review.Origin.COPY]


def test_plugin_skill_marker_describes_origin(tmp_path):
    write(tmp_path, ".agents/skills/clip/SKILL.md", "Clip documents.\n")
    write(tmp_path, ".agents/skills/clip/.agent-sync", skills.PLUGIN_MARK + "clip@market\n")
    group, = review_home(tmp_path).groups
    assert group.versions[0].origin == review.Origin.PLUGIN
    assert "clip@market" in group.versions[0].note


def test_recommend_an_edit_over_a_newer_copy(tmp_path):
    paths = instructions.files(tmp_path)
    claude = write(tmp_path, paths["claude"], "The version I keep.\n")
    cursor = write(tmp_path, paths["cursor"], "The old version.\n")
    os.utime(claude, (100, 100))
    os.utime(cursor, (500, 500))
    group, = review_home(tmp_path).groups

    winner, reasons = review.recommend(group, "cursor")
    copy = next(version for version in group.versions if version.path == cursor)
    assert winner.path == claude
    assert reasons == ["your own edit; the other is a copy agent-sync made"]
    assert review.cautions(copy, winner) == ["newer only because agent-sync rewrote it"]


def test_recommend_uses_dates_for_edits_then_prefers_source_then_length(tmp_path):
    paths = instructions.files(tmp_path)
    claude = write(tmp_path, paths["claude"], "Short version.\n")
    codex = write(tmp_path, paths["codex"], "Longer version.\nWith details.\n")
    os.utime(claude, (100, 100))
    os.utime(codex, (150, 150))
    group, = review_home(tmp_path).groups
    assert review.recommend(group, "claude")[0].path == claude
    assert review.recommend(group, "cursor")[0].path == codex
    os.utime(codex, (86_500, 86_500))
    group, = review_home(tmp_path).groups

    winner, reasons = review.recommend(group, "claude")
    assert winner.path == codex
    assert reasons[0].startswith("edited 1 day later")


def test_apply_global_choice_backs_up_every_replacement(tmp_path):
    paths = instructions.files(tmp_path)
    claude = write(tmp_path, paths["claude"], "New global instructions.\n")
    codex = write(tmp_path, paths["codex"], "Use global instructions in this workspace.\n")
    cursor = write(tmp_path, paths["cursor"], "---\nalwaysApply: true\n---\nOld global instructions.\n")
    custom = write(tmp_path, ".cursor/rules/preferences.mdc",
                   "---\ndescription: My rule\n---\nNew global instructions. Keep this addendum.\n")
    snapshot = {path: path.read_bytes() for path in (claude, codex, cursor, custom)}
    scan = review_home(tmp_path)
    group, = scan.groups
    winner = next(version for version in group.versions if version.path == claude)
    backup = tmp_path / "backup"

    steps = review.plan(tmp_path, scan, {group.key: winner.id}, "claude")
    assert {step.path for step in steps} == {codex, cursor, custom}
    assert {path: path.read_bytes() for path in snapshot} == snapshot
    assert not backup.exists()
    assert len(review.apply(tmp_path, steps, backup)) == 3
    assert review.apply(tmp_path, steps, backup) == []
    assert all((backup / path.relative_to(tmp_path)).read_bytes() == snapshot[path]
               for path in (codex, cursor, custom))
    assert custom.read_text() == "---\ndescription: My rule\n---\nNew global instructions.\n"
    assert review_home(tmp_path).groups[0].status == "same"


def test_apply_model_choices_keeps_each_tools_notes(tmp_path):
    paths = roles.files(tmp_path)
    for tool, path in paths.items():
        write(tmp_path, path, f"# Setup for {tool}\nreviewers: model-{tool}\n\n# Notes for {tool}\n")
    scan = review_home(tmp_path)
    group, = scan.groups
    winner = next(version for version in group.versions if version.tools == ("cursor",))

    steps = review.plan(tmp_path, scan, {group.key: winner.id}, "claude")
    assert len(review.apply(tmp_path, steps, tmp_path / "backup")) == 2
    for tool, path in paths.items():
        assert path.read_text() == f"# Setup for {tool}\nreviewers: model-cursor\n\n# Notes for {tool}\n"
    assert review_home(tmp_path).groups[0].status == "same"


def test_mcp_overwrites_transport_and_keeps_settings_in_each_tool(tmp_path):
    claude = write(tmp_path, ".claude.json", json.dumps({"mcpServers": {"docs.api": {
        "url": "https://example.com/docs", "type": "http",
    }}}))
    codex = write(tmp_path, ".codex/config.toml",
                  '[mcp_servers."docs.api"]\ncommand = "old"\nargs = ["-v"]\nstartup_timeout_sec = 22 # keep\n'
                  'tools = ["lookup"]\n[mcp_servers."docs.api".env]\nROOT = "/tmp"\n'
                  '[plugins."clip@registry"]\nenabled = false\n')
    cursor = write(tmp_path, ".cursor/mcp.json", json.dumps({"mcpServers": {"docs.api": {
        "type": "stdio", "command": "old", "env": {"ROOT": "/tmp"}, "custom": "keep",
    }}}))
    old = codex.read_bytes()
    scan = review_home(tmp_path)
    group, = scan.groups
    winner = next(version for version in group.versions if version.path == claude)

    steps = review.plan(tmp_path, scan, {group.key: winner.id}, "claude")
    backup = tmp_path / "backup"
    assert len(review.apply(tmp_path, steps, backup)) == 2
    assert review.apply(tmp_path, steps, backup) == []
    settings = tomllib.loads(codex.read_text())
    assert settings["mcp_servers"]["docs.api"] == {
        "url": "https://example.com/docs", "startup_timeout_sec": 22, "tools": ["lookup"],
    }
    assert "22 # keep" in codex.read_text() and settings["plugins"]["clip@registry"]["enabled"] is False
    assert json.loads(cursor.read_text())["mcpServers"]["docs.api"] == {
        "url": "https://example.com/docs", "custom": "keep", "type": "http",
    }
    assert (backup / ".codex/config.toml").read_bytes() == old
    assert review_home(tmp_path).groups[0].status == "same"


def test_copy_skill_to_one_shared_target_with_marker(tmp_path):
    source = write(tmp_path, ".claude/skills/example/SKILL.md", "Use the new skill.\n")
    write(tmp_path, ".claude/skills/example/details.txt", "Supporting instructions.")
    scan = review_home(tmp_path)
    group, = scan.groups
    winner, = group.versions

    steps = review.plan(tmp_path, scan, {group.key: winner.id}, "claude")
    step, = steps
    assert step.tools == ("codex", "cursor") and step.action == "create"
    assert len(review.apply(tmp_path, steps, tmp_path / "backup")) == 1
    assert review.apply(tmp_path, steps, tmp_path / "backup") == []
    assert (step.path / "details.txt").read_text() == "Supporting instructions."
    assert (step.path / skills.MARKER).read_text() == f"copied by agent-sync from {source.parent}\n"
    assert review_home(tmp_path).groups[0].status == "same"


def test_replace_real_skill_folder_without_touching_link_or_marker(tmp_path):
    shared = write(tmp_path, ".agents/skills/example/SKILL.md", "Old instructions.\n")
    marker = write(tmp_path, ".agents/skills/example/.agent-sync", "Original marker\n")
    cursor = write(tmp_path, ".cursor/skills/example/SKILL.md", "New instructions.\n")
    write(tmp_path, ".cursor/skills/example/.agent-sync", "Other marker\n")
    link = tmp_path / ".claude/skills/example"
    link.parent.mkdir(parents=True)
    link.symlink_to(shared.parent, target_is_directory=True)
    scan = review_home(tmp_path)
    group, = scan.groups
    winner = next(version for version in group.versions if version.path == cursor.parent.resolve())

    steps = review.plan(tmp_path, scan, {group.key: winner.id}, "claude")
    assert len(review.apply(tmp_path, steps, tmp_path / "backup")) == 1
    assert link.is_symlink() and link.readlink() == shared.parent
    assert shared.read_text() == "New instructions.\n" and marker.read_text() == "Original marker\n"
    assert (tmp_path / "backup/.agents/skills/example/SKILL.md").read_text() == "Old instructions.\n"
    assert review_home(tmp_path).groups[0].status == "same"


def test_daily_mirror_keeps_equal_instructions_as_is(tmp_path):
    paths = instructions.files(tmp_path)
    write(tmp_path, paths["claude"], "Shared instructions.\n")
    codex = write(tmp_path, paths["codex"], "Shared instructions.\n")
    cursor = write(tmp_path, paths["cursor"], "---\ndescription: Keep this\nalwaysApply: true\n---\n"
                   "Shared instructions.\n")
    snapshot = codex.read_bytes(), cursor.read_bytes()
    assert instructions.mirror(tmp_path, "claude") == []
    assert (codex.read_bytes(), cursor.read_bytes()) == snapshot


def test_apply_mcp_choices_sharing_one_settings_file(tmp_path):
    write(tmp_path, ".claude.json", json.dumps({"mcpServers": {
        "documents": {"command": "docs", "env": {"ROOT": "/work"}},
        "files.api": {"url": "https://example.com/files"},
    }}))
    codex = write(tmp_path, ".codex/config.toml",
                  'mcp_servers = { documents = { url = "https://example.com/old", startup_timeout_sec = 42 } }\n'
                  '[plugins."clip@registry"]\nenabled = true\n')
    scan = review_home(tmp_path)
    choices = {group.key: group.versions[0].id for group in scan.groups}
    old = codex.read_bytes()
    backup = tmp_path / "backup"

    steps = review.plan(tmp_path, scan, choices, "claude")
    assert len(review.apply(tmp_path, steps, backup)) == 2
    servers = tomllib.loads(codex.read_text())["mcp_servers"]
    assert servers["documents"] == {"command": "docs", "args": [], "env": {"ROOT": "/work"}, "startup_timeout_sec": 42}
    assert servers["files.api"] == {"url": "https://example.com/files"}
    assert (backup / ".codex/config.toml").read_bytes() == old
    assert review.apply(tmp_path, steps, backup) == []


def test_mcp_removes_empty_env_table_and_keeps_its_comment(tmp_path):
    write(tmp_path, ".claude.json", json.dumps({"mcpServers": {"docs": {"url": "https://example.com/docs"}}}))
    codex = write(tmp_path, ".codex/config.toml",
                  '[mcp_servers.docs]\ncommand = "old"\n[mcp_servers.docs.env]\n# Keep this comment\nROOT = "/tmp"\n')
    scan = review_home(tmp_path)
    group, = scan.groups
    steps = review.plan(tmp_path, scan, {group.key: group.versions[0].id}, "claude")

    review.apply(tmp_path, steps, tmp_path / "backup")
    assert tomllib.loads(codex.read_text())["mcp_servers"]["docs"] == {"url": "https://example.com/docs"}
    assert "# Keep this comment" in codex.read_text()
