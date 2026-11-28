import json
import tomllib

from agent_sync import agents, pending
from agent_sync.config import AgentModels
from tests.test_pending import approve_current

HELPER = """---
name: helper
description: Does one small job for its parent.
is_background: true
model: some-cursor-model-xhigh
---

# Helper

Read the task, do it, report back.
"""
REVIEWER = """---
name: reviewer
description: Reviews a diff and reports only.
---

Report findings only.
"""


def package(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "agents").mkdir(parents=True)
    (pkg / "agents/helper.md").write_text(HELPER)
    (pkg / "agents/reviewer.md").write_text(REVIEWER)
    return pkg


def test_each_tool_gets_its_own_model(tmp_path):
    home = tmp_path / "home"
    changes = agents.mirror(package(tmp_path), "someone/pack", home, AgentModels(), "1.2.3")
    assert changes
    claude = (home / ".claude/agents/helper.md").read_text()
    assert "model: opus" in claude and "xhigh" not in claude and "is_background: true" in claude
    cursor = home / ".cursor/plugins/local/pack-agents"
    assert "model: claude-opus-5-5-high-fast" in (cursor / "agents/helper.md").read_text()
    manifest = json.loads((cursor / ".cursor-plugin/plugin.json").read_text())
    assert manifest["agents"] == "./agents/" and manifest["version"] == "1.2.3"
    codex = tomllib.loads((home / ".codex/agents/helper.toml").read_text())
    assert codex["model"] == "gpt-6.2-sol" and codex["model_reasoning_effort"] == "high"
    assert codex["developer_instructions"].startswith("# Helper")


def test_an_agent_without_a_model_stays_without_one(tmp_path):
    home = tmp_path / "home"
    agents.mirror(package(tmp_path), "someone/pack", home, AgentModels())
    assert "model:" not in (home / ".claude/agents/reviewer.md").read_text()


def test_second_run_changes_nothing_and_dropped_agents_leave_cursor(tmp_path):
    home, pkg = tmp_path / "home", package(tmp_path)
    agents.mirror(pkg, "someone/pack", home, AgentModels())
    assert agents.mirror(pkg, "someone/pack", home, AgentModels()) == []
    (pkg / "agents/reviewer.md").unlink()
    state = tmp_path / "state"
    old = home / ".cursor/plugins/local/pack-agents/agents/reviewer.md"
    with pending.during(state, [], lambda *_: None, False):
        assert agents.mirror(pkg, "someone/pack", home, AgentModels()) == []
    assert old.exists()
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        changes = agents.mirror(pkg, "someone/pack", home, AgentModels())
    assert changes == [f"removed Cursor agent {old} (no longer in someone/pack)"]
    assert not old.exists()


def test_agent_notes_are_added_to_every_tools_copy(tmp_path):
    home = tmp_path / "home"
    agents.mirror(package(tmp_path), "someone/pack", home, AgentModels(), notes={"helper": "Local note."})
    assert (home / ".claude/agents/helper.md").read_text().endswith("report back.\n\nLocal note.\n")
    assert "Local note." in (home / ".cursor/plugins/local/pack-agents/agents/helper.md").read_text()
    assert "Local note." in tomllib.loads((home / ".codex/agents/helper.toml").read_text())["developer_instructions"]
    assert "Local note." not in (home / ".claude/agents/reviewer.md").read_text()


def test_emoji_in_an_agent_makes_valid_codex_toml():
    agent = agents.Agent.parse("---\nname: shipper\ndescription: Ships it 🚀\n---\nSay 😀 when done.\n")
    data = tomllib.loads(agent.codex_toml("gpt-x", "high"))
    assert data["description"] == "Ships it 🚀" and data["developer_instructions"] == "Say 😀 when done."


def test_mirror_replaces_an_unmarked_agent_only_after_approval(tmp_path):
    """A same-name agent without agent-sync's mark may be the user's own: it is replaced only once approved."""
    home, pkg = tmp_path / "home", package(tmp_path)
    existing = home / ".claude/agents/helper.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("My helper.\n")
    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False) as run:
        agents.mirror(pkg, "someone/pack", home, AgentModels())
        assert existing.read_text() == "My helper.\n"
        assert any(str(existing) in what for what in run.questions().values())
    approve_current(state)
    existing.write_text("My helper, rewritten after the approval.\n")      # the approval was for the old file
    with pending.during(state, [], lambda *_: None, False) as run:
        agents.mirror(pkg, "someone/pack", home, AgentModels())
        assert existing.read_text() == "My helper, rewritten after the approval.\n"
        assert any(str(existing) in what for what in run.questions().values())
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        agents.mirror(pkg, "someone/pack", home, AgentModels())
    assert "My helper" not in existing.read_text()


def test_mirror_adopts_an_unmarked_agent_it_wrote_before(tmp_path):
    """Agents written before agent-sync marked them stay in step: an unmarked copy equal to the package's is adopted."""
    home, pkg = tmp_path / "home", package(tmp_path)
    agents.mirror(pkg, "someone/pack", home, AgentModels())
    path = home / ".claude/agents/helper.md"
    record = agents.ownership(path.read_bytes(), "someone/pack")
    path.write_bytes(record[1])                         # what 0.9.7 wrote: the same agent without the mark
    changes = agents.mirror(pkg, "someone/pack", home, AgentModels())
    assert changes == [f"updated {path}"] and agents.ownership(path.read_bytes(), "someone/pack") is not None


def test_mirror_replaces_an_edited_agent_only_after_approval(tmp_path):
    home, pkg = tmp_path / "home", package(tmp_path)
    path = home / ".claude/agents/helper.md"
    agents.mirror(pkg, "someone/pack", home, AgentModels())
    edited = path.read_text() + "My edit.\n"
    path.write_text(edited)
    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False) as run:
        agents.mirror(pkg, "someone/pack", home, AgentModels())
        assert path.read_text() == edited
        assert any(str(path) in what for what in run.questions().values())
    approve_current(state)
    later_edit = edited + "Another edit.\n"
    path.write_text(later_edit)
    with pending.during(state, [], lambda *_: None, False) as run:
        agents.mirror(pkg, "someone/pack", home, AgentModels())
        assert path.read_text() == later_edit
        assert any(str(path) in what for what in run.questions().values())
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        agents.mirror(pkg, "someone/pack", home, AgentModels())
    assert "My edit." not in path.read_text()


def test_mirror_keeps_an_unmarked_agent_in_the_local_plugin(tmp_path):
    home, pkg = tmp_path / "home", package(tmp_path)
    agents.mirror(pkg, "someone/pack", home, AgentModels())
    path = home / ".cursor/plugins/local/pack-agents/agents/local.md"
    path.write_text("My plugin agent.\n")
    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False) as run:
        changes = agents.mirror(pkg, "someone/pack", home, AgentModels())
        assert not run.questions()
    assert path.read_text() == "My plugin agent.\n"
    assert any(line.startswith("WARN  ") and str(path) in line for line in changes)
