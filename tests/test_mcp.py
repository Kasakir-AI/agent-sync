import json
import tomllib

from agent_sync import mcp


def test_missing_servers_are_added_and_existing_ones_left_alone(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {
        "files": {"type": "stdio", "command": "npx", "args": ["-y", "files-mcp@1.0.0"], "env": {"ROOT": "/tmp"}},
        "docs": {"type": "http", "url": "https://example.com/mcp"}}}))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"docs": {"url": "https://mine.example/mcp"}}}))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('model = "x"\n')
    changes = mcp.mirror(tmp_path, "claude")
    assert "added MCP server files to cursor" in changes and "added MCP server docs to codex" in changes
    cursor = json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]
    assert cursor["docs"]["url"] == "https://mine.example/mcp"          # existing entry kept
    assert cursor["files"]["env"] == {"ROOT": "/tmp"}
    codex = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert codex["model"] == "x" and codex["mcp_servers"]["files"]["args"] == ["-y", "files-mcp@1.0.0"]
    assert mcp.mirror(tmp_path, "claude") == []
