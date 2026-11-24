import json

from agent_sync import agents, mcp, roles, tools
from agent_sync.config import AgentModels, load
from tests.test_agents import package


def test_config_customization(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('update_clis = ["codex"]\n[cli_commands]\ncodex = ["echo", "hi"]\n'
                        '[[extra_steps]]\nname = "hello"\ncommand = ["echo", "hello"]\n')
    cfg = load(cfg_file, home=tmp_path)
    assert cfg.wants_cli("codex") and not cfg.wants_cli("claude")
    assert cfg.cli_commands["codex"] == ["echo", "hi"] and cfg.extra_steps[0]["name"] == "hello"


def test_extra_steps_run_and_dry_run_only_prints(tmp_path):
    lines = []
    log = lambda kind, msg: lines.append((kind, msg))  # noqa: E731
    out = tmp_path / "made-by-step"
    steps = [{"name": "make a file", "command": ["touch", str(out)]}]
    tools.extra_steps(log, steps, dry=True)
    assert not out.exists() and lines[-1][0] == "dry"
    tools.extra_steps(log, steps)
    assert out.exists() and lines[-1] == ("ran", "make a file")


def test_dry_run_writes_nothing(tmp_path):
    home = tmp_path / "home"
    changes = agents.mirror(package(tmp_path), "someone/pack", home, AgentModels(), dry=True)
    assert changes and all(c.startswith("would") for c in changes) and not home.exists()
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
    assert mcp.mirror(tmp_path, "claude", dry=True) == ["would add MCP server x to codex",
                                                        "would add MCP server x to cursor"]
    assert not (tmp_path / ".cursor").exists() and not (tmp_path / ".codex").exists()
    assert roles.mirror(tmp_path, "claude", dry=True) == [f"skip  no {roles.files(tmp_path)['claude']}"]
