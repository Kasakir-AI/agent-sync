import json
import os
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

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


def test_plugin_skill_copies_are_counted_not_compared(tmp_path):
    write(tmp_path, ".agents/skills/clip/SKILL.md", "Clip documents.\n")
    write(tmp_path, ".agents/skills/clip/.agent-sync", skills.PLUGIN_MARK + "clip@market: /plugins/clip\n")
    scanned = review_home(tmp_path)
    assert scanned.groups == []
    assert scanned.managed == {"clip@market plugin skills": 1}


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


def apply_choice(home, key_part, pick, remove=None):
    scan = review_home(home)
    group = next(group for group in scan.groups if key_part in group.key)
    winner = next(version for version in group.versions if pick(version))
    choice = winner.id if remove is None else {"version": winner.id, "remove": remove}
    steps = review.plan(home, scan, {group.key: choice}, "claude")
    return review.apply(home, steps, home / "backup", review.baseline_file(Config(home=home)))


def test_merge_keeps_target_lines_and_is_idempotent():
    target = "# Notes\nKeep replies short.\nCodex only: run the linter.\nUse tabs.\n"
    winner = "# Notes\nKeep replies brief.\nUse tabs.\nAsk before pushing.\n"
    merged = review.merge(target, winner)
    assert merged == ("# Notes\nKeep replies brief.\nCodex only: run the linter.\nUse tabs.\n"
                      "Ask before pushing.\n")
    assert review.merge(merged, winner) == merged
    assert review.extends(merged, winner) and not review.extends(target, winner)
    assert review.merge(merged + "More notes.\n", winner) == merged + "More notes.\n"


def test_picking_the_shorter_instructions_leaves_a_longer_copy_alone(tmp_path):
    paths = instructions.files(tmp_path)
    shared = "".join(f"Rule {n}: keep step {n} small.\n" for n in range(1, 21))
    write(tmp_path, paths["claude"], shared)
    notes = "".join(f"Codex note {n}.\n" for n in range(1, 68))
    codex = write(tmp_path, paths["codex"], shared + notes)
    before = codex.read_bytes()
    group, = review_home(tmp_path).groups

    assert group.status == "missing" and group.note == "Codex keeps 67 lines of its own"
    assert review.recommend(group, "codex")[0].tools == ("claude",)     # the shared version, not the longer one
    assert apply_choice(tmp_path, "instructions", lambda version: version.tools == ("claude",)) == [
        "created ~/.cursor/rules/agent-sync-instructions.mdc"]
    assert codex.read_bytes() == before


def test_one_edited_line_changes_only_that_line_in_the_other_tool(tmp_path):
    paths = instructions.files(tmp_path)
    write(tmp_path, paths["claude"], "Keep replies short.\nAsk before pushing to main.\nRun the tests.\n")
    codex = write(tmp_path, paths["codex"], "Codex sandbox notes.\nKeep replies short.\n"
                  "Ask before pushing.\nRun the tests.\nUse the workspace profile.\n")
    os.utime(codex, (100, 100))
    pick = lambda version: version.tools == ("claude",)  # noqa: E731

    assert apply_choice(tmp_path, "instructions", pick) == ["updated ~/.codex/AGENTS.md",
                                                            "created ~/.cursor/rules/agent-sync-instructions.mdc"]
    assert codex.read_text() == ("Codex sandbox notes.\nKeep replies short.\nAsk before pushing to main.\n"
                                 "Run the tests.\nUse the workspace profile.\n")
    assert (tmp_path / "backup/.codex/AGENTS.md").read_text().count("Ask before pushing.\n") == 1
    assert apply_choice(tmp_path, "instructions", pick) == []
    group, = review_home(tmp_path).groups
    assert group.status == "same" and group.note == "Codex keeps 2 lines of its own"


def test_codex_block_is_dropped_once_its_lines_are_in_the_file(tmp_path):
    paths = instructions.files(tmp_path)
    write(tmp_path, paths["claude"], "Keep replies short.\nRun the tests.\n")
    codex = write(tmp_path, paths["codex"], "Codex sandbox notes.\n\n<!-- agent-sync: begin (mirrored) -->\n"
                  "Keep replies short.\nRun the tests.\n<!-- agent-sync: end -->\n")
    apply_choice(tmp_path, "instructions", lambda version: version.tools == ("claude",))
    assert codex.read_text() == "Codex sandbox notes.\nKeep replies short.\nRun the tests.\n"


def test_skill_merge_keeps_target_only_files_and_lines(tmp_path):
    write(tmp_path, ".claude/skills/notes/SKILL.md", "---\nname: notes\n---\nWrite notes daily.\nFile them.\n")
    (tmp_path / ".claude/skills/notes/logo.bin").write_bytes(b"\0new")
    write(tmp_path, ".claude/skills/notes/extra.md", "From Claude Code.\n")
    target = tmp_path / ".agents/skills/notes"
    write(tmp_path, ".agents/skills/notes/SKILL.md", "---\nname: notes\n---\nWrite notes weekly.\n"
          "Codex: use the notes folder.\n")
    (target / "logo.bin").write_bytes(b"\0old")
    write(tmp_path, ".agents/skills/notes/mine.md", "Only in Codex.\n")
    pick = lambda version: version.tools == ("claude",)  # noqa: E731

    assert apply_choice(tmp_path, "skill", pick) == ["updated ~/.agents/skills/notes"]
    assert (target / "SKILL.md").read_text() == ("---\nname: notes\n---\nWrite notes daily.\n"
                                                 "Codex: use the notes folder.\nFile them.\n")
    assert (target / "logo.bin").read_bytes() == b"\0new"
    assert (target / "extra.md").read_text() == "From Claude Code.\n"
    assert (target / "mine.md").read_text() == "Only in Codex.\n"
    assert (tmp_path / "backup/.agents/skills/notes/logo.bin").read_bytes() == b"\0old"
    assert apply_choice(tmp_path, "skill", pick) == []
    group, = review_home(tmp_path).groups
    assert group.status == "same" and group.note == "Codex and Cursor keep 1 line and 1 file of their own"


def tool_made_skills(home, *, claude_time=200, codex_time=200):
    claude = write(home, ".claude/skills/clipper-inbox/SKILL.md",
                   "---\nname: clipper-inbox\nallowed-tools: Bash\n---\nRead the inbox.\n!`clipper list`\n")
    codex = write(home, ".agents/skills/clipper-inbox/SKILL.md", "---\nname: clipper-inbox\n---\n"
                  "Read the inbox with `clipper list`.\n")
    yaml = write(home, ".agents/skills/clipper-inbox/agents/openai.yaml", "interface: {}\n")
    for path, when in ((claude, claude_time), (codex, codex_time), (yaml, codex_time)):
        os.utime(path, (when, when))
    plugin = write(home, "plugins/clipper/.claude-plugin/plugin.json", "{}")
    write(home, ".claude/plugins/installed_plugins.json", json.dumps({"plugins": {
        "clipper@market": [{"scope": "user", "installPath": str(plugin.parents[1])}]}}))


def test_skills_a_plugin_writes_for_each_tool_are_tailored(tmp_path):
    tool_made_skills(tmp_path)
    group, = review_home(tmp_path).groups
    when = datetime.fromtimestamp(200)

    assert group.status == "tailored"
    assert group.note == (f"clipper wrote a separate version for each tool at the same moment "
                          f"({when:%b} {when.day} {when:%H:%M}); Claude Code's uses allowed-tools and "
                          "!`command` lines only Claude Code understands; Codex and Cursor's uses "
                          "agents/openai.yaml only Codex understands")


def test_tool_markers_alone_mark_a_skill_tailored(tmp_path):
    tool_made_skills(tmp_path, claude_time=100, codex_time=90_000)
    write(tmp_path, ".agents/.skill-lock.json", "{}")
    group, = review_home(tmp_path).groups
    assert group.status == "tailored" and group.note.startswith("clipper made a separate version for each tool; ")


def test_plain_skill_edits_still_differ(tmp_path):
    write(tmp_path, ".claude/skills/notes/SKILL.md", "Write notes daily.\n")
    write(tmp_path, ".agents/skills/notes/SKILL.md", "Write notes weekly.\n")
    os.utime(tmp_path / ".claude/skills/notes/SKILL.md", (100, 100))
    group, = review_home(tmp_path).groups
    assert group.status == "differ" and review.tailored(group) == ""


def test_mcp_server_from_a_plugin_counts_as_present_and_is_never_written(tmp_path):
    root = tmp_path / "plugins/clipper"
    write(root, ".mcp.json", json.dumps({"clipper-inbox": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/inbox"}}))
    write(root, ".claude-plugin/plugin.json", json.dumps({"mcpServers": "extra.json"}))
    write(root, "extra.json", json.dumps({"mcpServers": {"clipper-notes": {"url": "https://example.com/n"}}}))
    write(tmp_path, ".claude/plugins/installed_plugins.json", json.dumps({"plugins": {
        "clipper@market": [{"scope": "user", "installPath": str(root)}]}}))
    write(tmp_path, ".codex/config.toml", '[mcp_servers.clipper-inbox]\ncommand = "inbox"\n')
    scan = review_home(tmp_path)
    inbox, = scan.groups                  # clipper-notes is only the plugin's: counted, never offered

    assert inbox.missing == ("cursor",) and scan.managed["clipper@market plugin MCP servers"] == 1
    plugin = next(version for version in inbox.versions if version.origin == review.Origin.PLUGIN)
    assert plugin.tools == ("claude",) and plugin.note == "from plugin clipper@market"
    assert json.loads(plugin.body)["command"] == f"{root}/bin/inbox"
    codex = next(version for version in inbox.versions if version.tools == ("codex",))
    steps = review.plan(tmp_path, scan, {inbox.key: codex.id}, "claude")
    assert {step.path for step in steps} == {tmp_path / ".cursor/mcp.json"}
    assert review.recommend(inbox, "claude")[0] == codex     # the plugin's cache path never wins
    assert review.cautions(plugin, codex)[0].startswith("comes with the plugin")
    try:
        review.plan(tmp_path, scan, {inbox.key: plugin.id}, "claude")
    except ValueError as e:
        assert "comes with a plugin" in str(e)
    else:
        raise AssertionError("a plugin's server was planned as the version to copy")


def test_hand_copy_with_extras_and_comment_lines_joins_global_instructions(tmp_path):
    preferences = "".join(f"- Preference {n}: keep it simple.\n" for n in range(1, 9))
    write(tmp_path, ".claude/CLAUDE.md", "<!-- imported-from: notes -->\n# Preferences\n" + preferences)
    rule = write(tmp_path, ".cursor/rules/personal-preferences.mdc", "---\nalwaysApply: true\n---\n# Preferences\n"
                 + preferences + "".join(f"- Cursor extra {n}: prefer tabs and short reviews.\n" for n in range(9)))
    group, = review_home(tmp_path).groups

    assert group.kind == "instructions" and group.matched_by == "content 100% alike"
    assert rule in [version.path for version in group.versions]
    assert group.status == "missing" and group.note == "Cursor keeps 9 lines of its own"


def test_difference_names_what_each_side_has():
    def version(tool, body):
        return review.make_version("rule", "x", (tool,), Path("x"), body, review.Origin.YOURS, modified=0)
    claude = version("claude", "Keep replies short.\nRun the tests.\n")
    codex = version("codex", "Codex note.\nKeep replies short.\nCodex other note.\n")
    group = review.Group("rule", "x", [claude, codex], ())
    assert review.difference(group, claude) == ("Codex has 2 lines Claude Code doesn't; "
                                                "Claude Code has 1 line Codex doesn't.")


def test_comment_only_lines_are_not_copied_or_compared(tmp_path):
    paths = instructions.files(tmp_path)
    write(tmp_path, paths["claude"], "<!-- imported-from: notes -->\nKeep replies short.\nRun the tests.\n")
    codex = write(tmp_path, paths["codex"], "Keep replies short.\n")
    os.utime(codex, (100, 100))
    apply_choice(tmp_path, "instructions", lambda version: version.tools == ("claude",))
    assert codex.read_text() == "Keep replies short.\nRun the tests.\n"
    assert review_home(tmp_path).groups[0].status == "same"


def test_a_short_rule_quoted_inside_the_instructions_stays_its_own_rule(tmp_path):
    inbox = "# Inbox\n\n- Send long work to the Inbox.\n- Ask questions there.\n- One thread per stream.\n"
    write(tmp_path, ".claude/CLAUDE.md", "# Preferences\n" + "".join(f"- Rule {n}.\n" for n in range(12)))
    write(tmp_path, ".codex/AGENTS.md", "# Preferences\n" + "".join(f"- Rule {n}.\n" for n in range(12)) + inbox)
    write(tmp_path, ".claude/rules/inbox.md", inbox)
    groups = {group.key: group for group in review_home(tmp_path).groups}

    assert set(groups) == {"instructions:Global instructions", "rule:inbox"}
    assert groups["instructions:Global instructions"].status == "missing"      # Codex only adds the section
    assert groups["rule:inbox"].matched_by == ""


def test_hand_written_instructions_saved_together_still_differ(tmp_path):
    claude = write(tmp_path, ".claude/CLAUDE.md", "# Preferences\n\nBe brief.\n")
    codex = write(tmp_path, ".codex/AGENTS.md", "Codex-only note.\n")
    for path in (claude, codex):
        os.utime(path, (1_000, 1_000))          # a dotfiles checkout writes both in the same second
    group, = review_home(tmp_path).groups

    assert group.status == "differ" and group.note == ""


def test_merge_drops_only_the_named_target_lines():
    target = "Codex note.\nKeep replies short.\nAsk before pushing.\nRun the tests.\n"
    winner = "Keep replies short.\nRun the tests.\n"
    assert review.merge(target, winner, frozenset()) == target
    merged = review.merge(target, winner, frozenset({"Ask before pushing."}))
    assert merged == "Codex note.\nKeep replies short.\nRun the tests.\n"
    assert review.merge(merged, winner, frozenset({"Ask before pushing."})) == merged


def test_removed_lines_are_the_synced_lines_the_winner_dropped():
    target = "Codex note.\nKeep replies short.\nAsk before pushing.\n"
    assert review.removed_lines(target, "Keep replies short.\n", ["Keep replies short.", "Ask before pushing."]) == {
        "Ask before pushing."}
    assert review.removed_lines(target, "Keep replies short.\n", []) == set()


def deleted_rule_home(home):
    """Claude Code's instructions lost a rule that Codex's own text and agent-sync's Cursor copy still have."""
    paths = instructions.files(home)
    write(home, paths["claude"], "Keep replies short.\nRun the tests.\n")
    write(home, paths["codex"], "Codex sandbox notes.\nKeep replies short.\nAsk before pushing.\nRun the tests.\n")
    write(home, paths["cursor"], "---\nalwaysApply: true\n---\nKeep replies short.\nAsk before pushing.\n"
          "Run the tests.\n")
    return paths


def test_a_line_deleted_from_the_kept_version_goes_from_the_others(tmp_path):
    paths = deleted_rule_home(tmp_path)
    group, = review_home(tmp_path).groups
    assert group.status == "differ"
    assert group.note == ("Claude Code no longer has 1 line that Codex and Cursor still have; "
                          "Codex keeps 1 line of its own")
    winner, reasons = review.recommend(group, "claude")
    assert winner.tools == ("claude",) and "no longer has 1 line" in reasons[1]

    pick = lambda version: version.tools == ("claude",)  # noqa: E731
    assert apply_choice(tmp_path, "instructions", pick) == ["updated ~/.codex/AGENTS.md",
                                                            "updated ~/.cursor/rules/agent-sync-instructions.mdc"]
    assert paths["codex"].read_text() == "Codex sandbox notes.\nKeep replies short.\nRun the tests.\n"
    assert paths["cursor"].read_text() == "---\nalwaysApply: true\n---\nKeep replies short.\nRun the tests.\n"
    assert apply_choice(tmp_path, "instructions", pick) == []
    group, = review_home(tmp_path).groups
    assert group.status == "same" and group.note == "Codex keeps 1 line of its own"


def test_the_user_decides_which_lines_only_a_target_has_go(tmp_path):
    paths = deleted_rule_home(tmp_path)
    remove = {"~/.codex/AGENTS.md": ["Codex sandbox notes."]}     # keep the deleted rule, drop the note
    apply_choice(tmp_path, "instructions", lambda version: version.tools == ("claude",), remove)
    assert paths["codex"].read_text() == "Keep replies short.\nAsk before pushing.\nRun the tests.\n"
    assert "Ask before pushing." not in paths["cursor"].read_text()     # not named: the default holds


def test_only_lines_a_target_has_can_be_removed(tmp_path):
    deleted_rule_home(tmp_path)
    scan = review_home(tmp_path)
    group, = scan.groups
    winner = next(version for version in group.versions if version.tools == ("claude",))
    for remove in ({"~/.codex/AGENTS.md": ["Run the tests."]}, {"~/.claude/CLAUDE.md": ["Run the tests."]}):
        with pytest.raises(review.ChoiceError):
            review.plan(tmp_path, scan, {group.key: {"version": winner.id, "remove": remove}}, "claude")


def test_agent_syncs_copy_only_vouches_for_the_source_tool(tmp_path):
    paths = instructions.files(tmp_path)
    claude = write(tmp_path, paths["claude"], "Keep replies short.\nAsk before pushing.\n")
    write(tmp_path, paths["codex"], "Keep replies short.\n")
    write(tmp_path, paths["cursor"], "Keep replies short.\nAsk before pushing.\n")
    apply_choice(tmp_path, "instructions", lambda version: version.tools == ("codex",))
    assert claude.read_text() == "Keep replies short.\nAsk before pushing.\n"   # Codex never had the line


def test_apply_remembers_the_kept_version_for_the_next_review(tmp_path):
    claude = write(tmp_path, ".claude/rules/style.md", "Use tabs.\nWrap at 100.\n")
    cursor = write(tmp_path, ".cursor/rules/style.mdc", "---\nalwaysApply: true\n---\nUse tabs.\nFormat on save.\n")
    pick = lambda version: version.tools == ("claude",)  # noqa: E731
    apply_choice(tmp_path, "rule", pick)
    store = review.baseline_file(Config(home=tmp_path))
    assert json.loads(store.read_text())["rule:style"]["lines"] == ["Use tabs.", "Wrap at 100."]

    claude.write_text("Use tabs.\n")
    group, = review_home(tmp_path).groups
    assert group.status == "differ"
    assert group.note == "Claude Code no longer has 1 line that Cursor still has; Cursor keeps 1 line of its own"
    assert apply_choice(tmp_path, "rule", pick) == ["updated ~/.cursor/rules/style.mdc"]
    assert cursor.read_text() == "---\nalwaysApply: true\n---\nUse tabs.\nFormat on save.\n"
    assert apply_choice(tmp_path, "rule", pick) == []

    claude.write_text("")
    store.unlink()                      # without a record of the last sync, a line only Cursor has is its own
    group, = review_home(tmp_path).groups
    assert group.status == "same"


def test_a_skill_copy_loses_deleted_lines_and_keeps_its_own_files(tmp_path):
    write(tmp_path, ".claude/skills/notes/SKILL.md", "Write notes daily.\n")
    copy = write(tmp_path, ".agents/skills/notes/SKILL.md", "Write notes daily.\nFile them weekly.\n")
    extra = write(tmp_path, ".agents/skills/notes/codex-tips.md", "Codex only.\n")
    marker = write(tmp_path, ".agents/skills/notes/.agent-sync", "copied by agent-sync from ~/.claude/skills/notes\n")
    for path in (copy, extra):
        os.utime(path, (100, 100))
    os.utime(marker, (200, 200))
    group, = review_home(tmp_path).groups
    assert group.status == "differ"
    apply_choice(tmp_path, "skill", lambda version: version.tools == ("claude",))
    assert copy.read_text() == "Write notes daily.\n" and extra.read_text() == "Codex only.\n"
