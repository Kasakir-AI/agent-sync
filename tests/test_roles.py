from agent_sync import roles

CLAUDE = """# pstack model configuration.
# budget: medium (high)
feature, refactoring: gpt-6.2-sol
judgment and prose: opus

# How to run each model in Claude Code
# Claude family: the Agent tool.
"""
CURSOR = """---
description: pstack per-role model choices
alwaysApply: true
---
# pstack model configuration.
feature, refactoring: gpt-6.1-sol
judgment and prose: opus

# How to run each model in Cursor
# Cursor's own notes stay.
"""


def test_roles_follow_the_source_and_each_tools_notes_stay(tmp_path):
    files = roles.files(tmp_path)
    for tool, text in (("claude", CLAUDE), ("cursor", CURSOR), ("codex", CURSOR.split("---\n", 2)[2])):
        files[tool].parent.mkdir(parents=True, exist_ok=True)
        files[tool].write_text(text)
    changes = roles.mirror(tmp_path, "claude")
    assert len(changes) == 2
    cursor = files["cursor"].read_text()
    assert "feature, refactoring: gpt-6.2-sol" in cursor and "gpt-6.1" not in cursor
    assert cursor.startswith("---\ndescription:") and "Cursor's own notes stay." in cursor
    assert roles.mirror(tmp_path, "claude") == []
