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


def test_emoji_in_a_server_makes_valid_codex_toml():
    block = mcp.codex_block("party", {"command": "run", "args": ["🚀"], "env": {"MOOD 😀": "😀\x7f"}})
    server = tomllib.loads(block)["mcp_servers"]["party"]
    assert server["args"] == ["🚀"] and server["env"] == {"MOOD 😀": "😀\x7f"}


def test_an_empty_mcp_file_counts_as_no_servers(tmp_path):
    # Antigravity creates ~/.gemini/config/mcp_config.json empty until a server is added.
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"files": {"command": "npx", "args": ["f"]}}}))
    target = tmp_path / ".gemini/config/mcp_config.json"
    target.parent.mkdir(parents=True)
    target.write_text("")
    assert mcp.read(tmp_path, "antigravity") == {}
    changes = mcp.mirror(tmp_path, "claude", enabled=("claude", "antigravity"))
    assert changes == ["added MCP server files to antigravity"]
    assert json.loads(target.read_text())["mcpServers"]["files"] == {"command": "npx", "args": ["f"]}


def test_mcp_dedupe_preserves_unrelated_toml_bytes_and_backs_up_once(tmp_path):
    from tests.test_inventory import codex_plugin, write

    plugin = codex_plugin(tmp_path, "cloudflare@claude-plugins-official", "1.0.0",
                          manifest=".claude-plugin/plugin.json")
    write(plugin, ".mcp.json", {"cloudflare": {"type": "http", "url": "https://mcp.cloudflare.com/mcp"}})
    config = write(tmp_path, ".codex/config.toml", "")
    beginning = b'# Keep my spacing\r\nmodel   = "example"\r\n\r\n'
    first = b'[mcp_servers."cloud.flare"] # duplicate\r\nurl = "https://mcp.cloudflare.com/mcp/"\r\n'
    second = b'[mcp_servers."cloud.flare".env]\r\nTHING = "value"\r\n'
    middle = b'\r\n# Unrelated table\r\n[mcp_servers."cloud.flare.extra"]\r\ncommand = "different"\r\n'
    third = b'[[mcp_servers."cloud.flare".tools]]\r\nname = "test"\r\n'
    end = b'\r\n[plugins."cloudflare@claude-plugins-official"]\r\nenabled = true\r\n'
    before = beginning + first + second + middle + third + end
    config.write_bytes(before)
    backups = tmp_path / "state/backups"
    message = ("MCP server cloud.flare from Codex settings: "
               "plugin cloudflare@claude-plugins-official brings the same server")
    assert mcp.dedupe(tmp_path, True, backups, enabled=("codex",)) == ["would remove " + message]
    assert config.read_bytes() == before and not backups.exists()
    assert mcp.dedupe(tmp_path, False, backups, enabled=("codex",)) == ["removed " + message]
    assert config.read_bytes() == beginning + middle + end
    saved, = backups.glob("dedupe-*/.codex/config.toml")
    assert saved.read_bytes() == before
    assert mcp.dedupe(tmp_path, False, backups, enabled=("codex",)) == []
    assert list(backups.iterdir()) == [saved.parents[1]]


def test_mcp_dedupe_keeps_json_keys_order_and_unicode(tmp_path):
    from tests.test_inventory import write

    write(tmp_path, ".gemini/config/config.json", {"plugins": {
        "cloudflare": {"installedFrom": {"marketplace": "agy", "version": "1.0.0"}}}})
    write(tmp_path, ".gemini/config/plugins/cloudflare/plugin.json", {"version": "1.0.0"})
    write(tmp_path, ".gemini/config/plugins/cloudflare/mcp.json",
          {"mcpServers": {"cloudflare": {"url": "https://mcp.cloudflare.com/mcp"}}})
    entries = {"other": "étoile", "mcpServers": {
        "cloudflare": {"serverUrl": "https://mcp.cloudflare.com/mcp"},
        "tools": {"command": "python3", "args": ["é.py"]}}, "after": ["🦊"]}
    path = write(tmp_path, ".gemini/config/mcp_config.json", json.dumps(entries, ensure_ascii=False))
    before = path.read_bytes()
    backups = tmp_path / "backups"
    assert mcp.dedupe(tmp_path, False, backups, enabled=("antigravity",)) == [
        "removed MCP server cloudflare from Antigravity settings: plugin cloudflare@agy brings the same server"]
    del entries["mcpServers"]["cloudflare"]
    assert path.read_text() == json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    saved, = backups.glob("dedupe-*/.gemini/config/mcp_config.json")
    assert saved.read_bytes() == before


def test_mirror_skips_both_plugin_name_and_endpoint(tmp_path):
    from tests.test_inventory import codex_plugin, write

    codex = codex_plugin(tmp_path, "cloudflare@remote", "1.0.0", remote=True)
    write(codex, ".mcp.json", {"mcpServers": {"cf": {"url": "https://cf.example/mcp"},
                                           "same": {"url": "https://plugin.example"}}})
    write(tmp_path, ".claude.json", {"mcpServers": {
        "by-endpoint": {"url": "https://cf.example/mcp/"},
        "same": {"url": "https://user.example"},
        "new": {"url": "https://new.example"},
    }})
    assert mcp.mirror(tmp_path, "claude", enabled=("claude", "codex")) == ["added MCP server new to codex"]
    assert mcp.read(tmp_path, "codex") == {"new": {"url": "https://new.example"}}


def test_same_server_twice_warns_without_writing(tmp_path):
    from tests.test_inventory import write

    path = write(tmp_path, ".claude.json", {"mcpServers": {
        "a": {"url": "https://example.com/mcp"}, "b": {"url": "https://example.com/mcp/"},
        "c": {"httpUrl": "https://example.com/mcp"}, "other": {"url": "https://other.example"},
    }})
    before = path.read_bytes()
    assert mcp.same_server_twice(tmp_path, enabled=("claude",)) == [
        "Claude Code settings list the same MCP server more than once: a, b, c"]
    assert path.read_bytes() == before


def test_pins_change_only_selected_toml_and_json_strings(tmp_path):
    from tests.test_inventory import write

    old, new = "rea-agents@6.8.0", "rea-agents@6.10.1"
    toml_before = (
        f'note = "{old}"\r\n'
        '[mcp_servers.rea]\r\ncommand = "/opt/bin/npx"\r\n'
        f'args  = [ "-y", # {old}\r\n  \'{old}\', "mcp" ]\r\n'
        f'[mcp_servers.rea.env]\r\nEXAMPLE = "{old}"\r\n'
        f'[mcp_servers.hold]\r\ncommand = "npx"\r\nargs = ["{old}", "mcp"]\r\n'
        '[mcp_servers.ahead]\r\ncommand = "bunx"\r\nargs = ["rea-agents@7.0.0"]\r\n'
        f'[mcp_servers.other]\r\ncommand = "python"\r\nargs = ["{old}"]\r\n')
    json_before = ('{ "note":"@org/pack@1.2.3-rc.1", "mcpServers": {'
                   '"scoped": {"command":"pnpm","args":["dlx","@org/pack@1.2.3-rc.1","mcp"]},'
                   f'"rea":{{"command":"npm","args":["exec","{old}","mcp"]}},'
                   '"missing-dlx":{"command":"pnpm","args":["@org/pack@1.2.3-rc.1"]}}}')
    codex = write(tmp_path, ".codex/config.toml", toml_before)
    cursor = write(tmp_path, ".cursor/mcp.json", json_before)
    calls = []

    def latest(package):
        calls.append(package)
        return {"rea-agents": "6.10.1", "@org/pack": "1.2.3"}[package]

    enabled = ("codex", "cursor")
    backups = tmp_path / "backups"
    expected = ["updated MCP server rea in Codex settings: rea-agents 6.8.0 -> 6.10.1",
                "updated MCP server scoped in Cursor settings: @org/pack 1.2.3-rc.1 -> 1.2.3",
                "updated MCP server rea in Cursor settings: rea-agents 6.8.0 -> 6.10.1"]
    assert mcp.pins(tmp_path, True, enabled=enabled, keep=["hold"], latest=latest, backup_dir=backups) == [
        line.replace("updated", "would update", 1) for line in expected]
    assert codex.read_bytes() == toml_before.encode() and cursor.read_bytes() == json_before.encode()
    assert not backups.exists()
    calls.clear()
    assert mcp.pins(tmp_path, False, enabled=enabled, keep=["hold"], latest=latest, backup_dir=backups) == expected
    assert calls == ["rea-agents", "@org/pack"]
    assert codex.read_bytes() == toml_before.replace(f"'{old}'", f"'{new}'").encode()
    assert cursor.read_text() == json_before.replace('"dlx","@org/pack@1.2.3-rc.1"', '"dlx","@org/pack@1.2.3"').replace(
        f'"exec","{old}"', f'"exec","{new}"')
    saved, = backups.iterdir()
    assert (saved / ".codex/config.toml").read_bytes() == toml_before.encode()
    assert (saved / ".cursor/mcp.json").read_bytes() == json_before.encode()
    assert mcp.pins(tmp_path, False, enabled=enabled, keep=["hold"], latest=latest, backup_dir=backups) == []
    assert list(backups.iterdir()) == [saved]


def test_shared_settings_use_plugins_from_every_reader(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_sync.registry import TOOLS
    from tests.test_inventory import write

    monkeypatch.setitem(TOOLS, "gemini", replace(TOOLS["gemini"], mcp=TOOLS["cursor"].mcp))
    write(tmp_path, ".gemini/extensions/pack/gemini-extension.json", {
        "mcpServers": {"alias": {"httpUrl": "https://example.com/mcp"}}})
    write(tmp_path, ".claude.json", {"mcpServers": {"cf": {"url": "https://example.com/mcp"}}})
    enabled = ("claude", "cursor", "gemini")
    assert mcp.mirror(tmp_path, "claude", enabled=enabled) == []
    path = write(tmp_path, ".cursor/mcp.json", {"mcpServers": {"cf": {"url": "https://example.com/mcp"}}})
    assert mcp.dedupe(tmp_path, False, tmp_path / "backups", enabled=enabled) == [
        "removed MCP server cf from Cursor, Gemini CLI settings: plugin pack@extensions brings the same server"]
    assert json.loads(path.read_text())["mcpServers"] == {}


def test_dedupe_handles_inline_codex_entries_without_reformatting_others(tmp_path):
    from tests.test_inventory import codex_plugin, write

    root = codex_plugin(tmp_path, "pack@remote", "1.0.0", remote=True)
    write(root, ".mcp.json", {"same": {"url": "https://same.example"}})
    before = ('mcp_servers = { first={ url="https://first.example" }, same = {url="https://same.example"},'
              ' last={command="run",args=["a,b", "c"]} } # preserve\n')
    path = write(tmp_path, ".codex/config.toml", before)
    assert mcp.dedupe(tmp_path, False, tmp_path / "backups", enabled=("codex",)) == [
        "removed MCP server same from Codex settings: plugin pack@remote brings the same server"]
    assert path.read_text() == before.replace(' same = {url="https://same.example"},', "")
