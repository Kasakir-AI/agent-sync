import re

from agent_sync import instructions, memory
from agent_sync.registry import TOOLS

INDEX = "- [Workspace preferences](workspace.md) — habits to use across projects\n"


def setup(home):
    source = home / ".claude/projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(home)) / "memory"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text(INDEX)
    (source / "workspace.md").write_text("Prefer local worktrees.\n")
    files = instructions.files(home, TOOLS)
    files["claude"].parent.mkdir(exist_ok=True)
    files["claude"].write_text("Keep replies brief.\n")
    return source, files, home / ".agents/memory"


def test_dry_run_and_copy_index_for_each_distinct_enabled_tool(tmp_path):
    source, files, dest = setup(tmp_path)
    paths = [files[tool] for tool in ("codex", "cursor", "gemini")]
    expected = ["update memory copy (2 files)", *(f"update memory index in {path}" for path in paths)]
    assert memory.mirror(tmp_path, True, enabled=TOOLS) == ["would " + line for line in expected]
    assert not dest.exists()
    assert all(not path.exists() for path in paths)

    assert memory.mirror(tmp_path, False, enabled=TOOLS) == [
        "updated memory copy (2 files)", *(f"updated memory index in {path}" for path in paths)]
    assert (dest / ".agent-sync").is_file()
    assert (dest / "MEMORY.md").read_bytes() == (source / "MEMORY.md").read_bytes()
    assert (dest / "workspace.md").read_text() == "Prefer local worktrees.\n"
    assert files["claude"].read_text() == "Keep replies brief.\n"
    for path in paths:
        section = path.read_text()
        assert "## Memory (kept by Claude Code)\n" in section
        assert ("Read the matching file in ~/.agents/memory/ when an entry applies. "
                "These are facts and preferences about the user and their projects.\n") in section
        assert INDEX.replace("(workspace.md)", "(~/.agents/memory/workspace.md)") in section
    assert files["cursor"].read_text().startswith("---\ndescription:")
    assert memory.mirror(tmp_path, False, enabled=TOOLS) == []


def test_an_unowned_folder_is_left_alone(tmp_path):
    _, files, dest = setup(tmp_path)
    dest.mkdir(parents=True)
    (dest / "mine.md").write_text("Keep this.\n")

    for dry in (True, False):
        warning, = memory.mirror(tmp_path, dry, enabled=TOOLS)
        assert warning.startswith("WARN") and str(dest) in warning
        assert sorted(path.name for path in dest.iterdir()) == ["mine.md"]
        assert (dest / "mine.md").read_text() == "Keep this.\n"
        assert not files["codex"].exists()


def test_refresh_removes_stale_copies_and_replaces_the_index_in_place(tmp_path):
    source, files, dest = setup(tmp_path)
    memory.mirror(tmp_path, False, enabled=TOOLS)
    (source / "workspace.md").unlink()
    (source / "new.md").write_text("Another preference.\n")
    (source / "MEMORY.md").write_text("- [New](new.md) — another preference\n")
    codex = files["codex"]
    codex.write_text("Before.\n\n" + codex.read_text() + "\nAfter.\n")

    assert memory.mirror(tmp_path, False, enabled=TOOLS)[0] == "updated memory copy (2 files)"
    assert not (dest / "workspace.md").exists()
    assert (dest / "new.md").read_text() == "Another preference.\n"
    text = codex.read_text()
    assert text.count("<!-- agent-sync: memory -->") == text.count("<!-- agent-sync: end memory -->") == 1
    assert "[New](~/.agents/memory/new.md) — another preference" in text
    assert "workspace.md" not in text and text.startswith("Before.\n\n") and text.endswith("\nAfter.\n")
    assert memory.mirror(tmp_path, False, enabled=TOOLS) == []


def test_memory_is_local_when_instructions_sync_runs_after_it(tmp_path):
    _, files, _ = setup(tmp_path)
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    memory.mirror(tmp_path, False, enabled=TOOLS)
    codex = files["codex"]
    codex.write_text("Run pytest.\n" + codex.read_text())

    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    assert files["claude"].read_text() == "Keep replies brief.\nRun pytest.\n"
    assert codex.read_text().count("<!-- agent-sync: memory -->") == 1
    assert memory.mirror(tmp_path, False, enabled=TOOLS) == []


def test_without_enabled_claude_or_its_home_project_memory_nothing_is_shared(tmp_path):
    _, files, dest = setup(tmp_path)
    assert memory.mirror(tmp_path, False, enabled=("codex",)) == []
    assert not files["codex"].exists() and not dest.exists()
    other_home = tmp_path / "another-home"
    assert memory.mirror(other_home, False, enabled=TOOLS) == []
    assert not other_home.exists()


def test_cli_upkeep_shares_instructions_then_memory(tmp_path, monkeypatch, capsys):
    from agent_sync import cli
    from agent_sync.config import config_path

    _, files, dest = setup(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test version"))
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("skill_packages = []\nmirror_instructions = true\nmirror_memory = true\n"
                      "mirror_mcp = false\nmirror_roles = false\nmirror_skills = false\n"
                      "update_plugins = false\nupdate_clis = false\nupdate_self = false\n")
    assert cli.main(["run", "--force", "--echo"]) == 0
    output = capsys.readouterr().out
    assert output.index("global instructions mirrored") < output.index("updated memory copy")
    assert (dest / "workspace.md").is_file()
    codex = files["codex"].read_text()
    assert codex.startswith("Keep replies brief.\n\n<!-- agent-sync: memory -->\n")
    assert "[Workspace preferences](~/.agents/memory/workspace.md)" in codex
