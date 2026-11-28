import json
import sys
from dataclasses import replace

import pytest

from agent_sync import agents, checks, instructions, mcp, review, roles, setup, skills, tools, writes
from agent_sync.config import AgentModels, Config, parse
from agent_sync.registry import TOOLS

GOOGLE = ["gemini", "antigravity"]
ALL = list(TOOLS)


def write(home, filename, text):
    path = home / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_enabled_agents_and_package_targets(tmp_path):
    cfg = parse('source = "gemini"\nagents = ["antigravity", "gemini", "gemini"]', tmp_path / "config.toml", tmp_path)
    assert cfg.agents == GOOGLE
    assert cfg.wants_cli("gemini") and not cfg.wants_cli("claude")
    assert tools.skill_args("skills", "example/package", enabled=cfg.agents)[-4:] == [
        "-a", "gemini-cli", "antigravity", "-y"]
    assert tools.remove_args("skills", "old", enabled=cfg.agents)[-4:] == [
        "-a", "gemini-cli", "antigravity", "-y"]
    with pytest.raises(ValueError, match="must be enabled"):
        parse('agents = ["gemini"]', tmp_path / "config.toml", tmp_path)
    with pytest.raises(ValueError, match="agents must be a list"):
        parse('agents = ["unknown"]', tmp_path / "config.toml", tmp_path)


def test_google_mcp_formats_and_legacy_entries(tmp_path):
    gemini = write(tmp_path, TOOLS["gemini"].mcp, json.dumps({
        "theme": "dark", "mcpServers": {
            "remote": {"httpUrl": "https://example.com/mcp"},
            "legacy": {"url": "https://example.com/sse"},
            "local": {"command": "tool", "args": ["serve"], "env": {"KEY": "example"}},
        },
    }))
    agy = write(tmp_path, TOOLS["antigravity"].mcp, json.dumps({
        "mcpServers": {"old": {"serverUrl": "https://example.com/old"}},
    }))
    assert mcp.read(tmp_path, "antigravity")["old"]["url"] == "https://example.com/old"
    mcp.mirror(tmp_path, "gemini", enabled=GOOGLE)
    assert json.loads(agy.read_text())["mcpServers"] == {
        "old": {"serverUrl": "https://example.com/old"},
        "remote": {"url": "https://example.com/mcp"},
        "local": {"command": "tool", "args": ["serve"], "env": {"KEY": "example"}},
    }
    mcp.mirror(tmp_path, "antigravity", enabled=GOOGLE)
    assert json.loads(gemini.read_text())["mcpServers"]["old"] == {"httpUrl": "https://example.com/old"}
    assert json.loads(gemini.read_text())["theme"] == "dark"
    assert not mcp.mirror(tmp_path, "antigravity", dry=True, enabled=GOOGLE)
    assert not (tmp_path / ".claude.json").exists()


def test_shared_instructions_and_roles_are_written_once(tmp_path):
    write(tmp_path, TOOLS["claude"].instructions, "Write concise instructions.\n")
    write(tmp_path, TOOLS["claude"].roles, "bug-fix: opus\n")
    shared_roles = write(tmp_path, TOOLS["gemini"].roles, "bug-fix: old\n\nLocal notes.\n")
    md = tmp_path / TOOLS["gemini"].instructions
    assert [line for line in instructions.mirror(tmp_path, "claude", dry=True, enabled=ALL)
            if str(md) in line] == [f"would update {md}"]
    assert [line for line in instructions.mirror(tmp_path, "claude", enabled=ALL)
            if str(md) in line] == [f"updated {md}"]
    cursor_roles = tmp_path / TOOLS["cursor"].roles
    assert roles.mirror(tmp_path, "claude", dry=True, enabled=ALL) == [
        f"would update {shared_roles}", f"would update {cursor_roles}"]
    assert not cursor_roles.exists()
    assert roles.mirror(tmp_path, "claude", enabled=ALL) == [f"updated {shared_roles}", f"updated {cursor_roles}"]
    assert cursor_roles.read_text().startswith("---\ndescription: pstack model roles\nalwaysApply: true\n---\n")
    assert md.read_text() == "Write concise instructions.\n"
    assert shared_roles.read_text() == "bug-fix: opus\n\nLocal notes.\n"
    assert instructions.mirror(tmp_path, "gemini", enabled=GOOGLE) == []
    assert roles.mirror(tmp_path, "antigravity", enabled=GOOGLE) == []


def test_shared_mcp_and_agent_paths_are_visited_once(tmp_path, monkeypatch):
    monkeypatch.setitem(TOOLS, "antigravity", replace(TOOLS["antigravity"],
                        mcp=TOOLS["gemini"].mcp, mcp_format=TOOLS["gemini"].mcp_format,
                        agents_dir=TOOLS["gemini"].agents_dir, agent_format=TOOLS["gemini"].agent_format))
    write(tmp_path, TOOLS["claude"].mcp, '{"mcpServers": {"docs": {"url": "https://example.com/mcp"}}}')
    assert mcp.mirror(tmp_path, "claude", dry=True, enabled=["claude", *GOOGLE]) == [
        "would add MCP server docs to gemini"]
    assert mcp.mirror(tmp_path, "claude", enabled=["claude", *GOOGLE]) == ["added MCP server docs to gemini"]
    assert mcp.mirror(tmp_path, "gemini", enabled=GOOGLE) == []
    package = tmp_path / "package"
    write(package, "agents/helper.md", "---\nname: helper\ndescription: A helper\n---\nHelp.\n")
    path = tmp_path / ".gemini/agents/helper.md"
    assert agents.mirror(package, "example/package", tmp_path, AgentModels(), dry=True, enabled=GOOGLE) == [
        f"would update {path}"]
    assert agents.mirror(package, "example/package", tmp_path, AgentModels(), enabled=GOOGLE) == [f"updated {path}"]


def test_google_subagent_markdown_and_model_checks(tmp_path):
    package = tmp_path / "package"
    write(package, "agents/go.md", "---\nname: go\ndescription: 'Plan: do it'\nmodel: opus\n---\nStart here.\n")
    write(package, "agents/Upper.md", "---\nname: Upper\ndescription: Try it\n---\nStart here.\n")
    agents.mirror(package, "example/package", tmp_path, AgentModels(), enabled=GOOGLE)
    gemini = tmp_path / ".gemini/agents/go.md"
    agy = tmp_path / ".gemini/config/agents/go.md"
    assert gemini.read_text().startswith("---\n")
    assert gemini.read_text().endswith("name: go\ndescription: 'Plan: do it'\n---\nStart here.\n")
    assert not (gemini.parent / "Upper.md").exists()
    assert agy.read_text().startswith("---\n")
    assert agy.read_text().endswith(
        "name: go\ndescription: 'Plan: do it'\nsubagent: true\nmainAgent: false\n"
        "tools: [read_file, edit_file, write_file, run_command, manage_task, search_web]\n---\nStart here.\n")
    agents.mirror(package, "example/package", tmp_path, AgentModels(gemini="gemini-model"), enabled=GOOGLE)
    assert "model: gemini-model" in gemini.read_text() and "model:" not in agy.read_text()
    settings = write(tmp_path, ".gemini/settings.json", "{}")
    config = write(tmp_path, ".gemini/config/config.json", "{}")
    assert {settings, config, gemini, agy} <= set(checks.model_files(tmp_path, enabled=GOOGLE))


def test_google_review_groups_shared_resources_and_writes_each_mcp_format(tmp_path):
    cfg = Config(home=tmp_path, source="gemini", agents=GOOGLE)
    write(tmp_path, TOOLS["gemini"].instructions, "Instructions\n")
    write(tmp_path, TOOLS["gemini"].roles, "bug-fix: opus\n")
    write(tmp_path, ".gemini/skills/native/SKILL.md", "Native skill\n")
    gemini = write(tmp_path, TOOLS["gemini"].mcp, '{"mcpServers": {"docs": {"httpUrl": "https://example.com/new"}}}')
    agy = write(tmp_path, TOOLS["antigravity"].mcp, '{"mcpServers": {"docs": {"serverUrl": "https://example.com/old"}}}')
    groups = {group.kind: group for group in review.scan(tmp_path, cfg).groups}
    assert groups["instructions"].versions[0].tools == tuple(GOOGLE)
    assert groups["roles"].versions[0].tools == tuple(GOOGLE)
    assert groups["skill"].versions[0].tools == ("gemini",)
    group = groups["mcp"]
    scan = review.scan(tmp_path, cfg, ["mcp"])
    for index, path, key, suffix in ((0, agy, "url", "new"), (1, gemini, "httpUrl", "old")):
        steps = writes.plan(tmp_path, scan, {group.key: group.versions[index].id}, "gemini")
        assert [step.path for step in steps] == [path]
        entry = json.loads(writes.edits(steps)[0].after)["mcpServers"]["docs"]
        assert entry == {key: f"https://example.com/{suffix}"}
    assert skills.mirror(tmp_path, "gemini", enabled=GOOGLE)
    assert (tmp_path / ".gemini/config/skills/native/SKILL.md").read_text() == "Native skill\n"


def test_google_setup_and_update_routes(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    for command in ("npx", "brew"):
        write(tmp_path, f"bin/{command}", "#!/bin/sh\n").chmod(0o755)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(tools, "PATH", str(bin_dir))
    monkeypatch.setattr(setup, "brew_prefix", lambda: tmp_path)
    monkeypatch.setattr(setup, "has_git", lambda: True)
    cfg = Config(home=tmp_path, source="gemini", agents=GOOGLE)
    expected = [
        ["install", "--formula", "gemini-cli"],
        ["-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"],
    ]
    assert [step.command[1:] for step in setup.plan(cfg)] == expected
    gemini = write(tmp_path, "Cellar/gemini-cli/1/bin/gemini", "#!/bin/sh\n")
    gemini.chmod(0o755)
    (bin_dir / "gemini").symlink_to(gemini)
    write(tmp_path, "bin/agy", "#!/bin/sh\n").chmod(0o755)
    write(tmp_path, ".gemini/settings.json", "{}")
    assert tools.version("agy", tmp_path) == "installed, never started in this account"
    (tmp_path / ".gemini/antigravity-cli").mkdir()
    calls = []

    def sh(args, *a, **kw):
        calls.append(args)
        return 0, "gemini-cli" if args[:2] == ["brew", "outdated"] else "1.0"

    monkeypatch.setattr(tools, "sh", sh)
    def log(*_):
        return None
    tools.update_clis(log, cfg)
    tools.update_plugins(log, home=tmp_path, enabled=GOOGLE)
    assert ["brew", "upgrade", "--formula", "gemini-cli"] in calls
    assert ["agy", "update"] in calls
    assert ["gemini", "extensions", "update", "--all"] in calls
    (bin_dir / "gemini").unlink()
    write(tmp_path, "bin/gemini", "#!/bin/sh\n").chmod(0o755)
    calls.clear()
    tools.update_clis(log, cfg)
    assert ["npm", "install", "-g", "@google/gemini-cli@latest"] in calls
    cfg.cli_commands["gemini"] = ["custom", "update"]
    calls.clear()
    tools.update_clis(log, cfg)
    assert ["custom", "update"] in calls
    assert not any(call[0] == "npm" for call in calls)


@pytest.mark.parametrize("description", [
    ">\n  Reviews code\n  for bugs: carefully.\n",
    ">-\n  Reviews code\n\n  Reports findings.\n",
    "|2+ # Keep the layout.\n  Reviews code\n    Reports findings.\n\n",
], ids=["folded", "paragraphs", "literal-with-indentation"])
def test_block_descriptions_reach_the_google_agent_formats(tmp_path, description):
    package = tmp_path / "pkg"
    field = f"description: {description}"
    write(package, "agents/helper.md", f"---\nname: helper\n{field}---\nBe helpful.\n")
    agents.mirror(package, "me/pkg", tmp_path, AgentModels(gemini="gemini-model"), enabled=GOOGLE)
    for path in (tmp_path / ".gemini/agents/helper.md", tmp_path / ".gemini/config/agents/helper.md"):
        assert field in path.read_text()
