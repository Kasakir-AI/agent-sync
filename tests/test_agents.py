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
