import json

from agent_sync import cli, inventory, mcp
from tests.test_skills import make_skill


def write(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


def codex_plugin(home, pid, version, *, manifest=".codex-plugin/plugin.json", metadata=None, remote=False):
    name, market = pid.split("@")
    root = home / ".codex/plugins/cache" / market / name / (version or "cache")
    write(root, manifest, metadata if metadata is not None else {"name": name, "version": version})
    if remote:
        write(root.parent, ".codex-remote-plugin-install.json", {"schema_version": 1})
    return root


def test_inventory_reads_real_plugin_layouts(tmp_path):
    codex = codex_plugin(tmp_path, "app-abcd@remote", "2.0.0", remote=True, metadata={
        "interface": {"displayName": "Adobe-Plugin"}, "name": "other", "mcpServers": "./extra.json"})
    write(codex, ".app.json", {"apps": {"Wrong display": {}}})
    write(codex, "extra.json", {"mcpServers": {"adobe": {"url": "https://adobe.example/mcp"}}})
    make_skill(codex / "skills/nested", "resize")
    other = codex_plugin(tmp_path, "app-ef01@remote", "1.0.0", remote=True)
    write(other, ".app.json", {"apps": {"Visualize": {}}})
    codex_plugin(tmp_path, "bad@remote", "1.0.0", remote=True, metadata="{")
    codex_plugin(tmp_path, "off@remote", "1.0.0", remote=True)
    write(tmp_path, ".codex/config.toml", '[plugins."off@remote"]\nenabled = false\n')

    claude = tmp_path / "claude-store/1.3.0"
    make_skill(claude / "custom", "check")
    write(claude, ".claude-plugin/plugin.json", {"skills": ["./custom"], "mcpServers": {
        "checks": {"command": "${CLAUDE_PLUGIN_ROOT}/run"}}})
    write(tmp_path, ".claude/plugins/installed_plugins.json", {"plugins": {
        "checks@official": [{"scope": "user", "installPath": str(claude), "version": "1.3.0"}]}})

    write(tmp_path, ".cursor/plugins/cache/cursor/cloudflare/2.1.0/mcp.json",
          {"mcpServers": {"cf": {"url": "https://cf.example/mcp"}}})
    write(tmp_path, ".cursor/plugins/local/localpack/.cursor-plugin/plugin.json",
          {"version": "3.1.0", "mcpServers": {"local": {"command": "run"}}})

    write(tmp_path, ".gemini/extensions/pack/gemini-extension.json",
          {"version": "2.2.0", "mcpServers": {"pack": {"httpUrl": "https://pack.example/mcp"}}})
    make_skill(tmp_path / ".gemini/extensions/pack/skills", "pack-skill")

    write(tmp_path, ".gemini/config/config.json", {"plugins": {
        "cloudflare": {"installedFrom": {"marketplace": "agy", "version": "2.0.0"}},
        "off": {"installedFrom": {"marketplace": "agy", "version": "2.0.0"}, "enabled": False},
    }})
    agy = tmp_path / ".gemini/config/plugins/cloudflare"
    write(agy, "plugin.json", {"version": "2.0.0"})
    write(agy, "mcp_config.json", {"mcpServers": {"cloudflare": {"serverUrl": "https://cf.example/mcp"}}})
    make_skill(agy / "skills", "cloudflare-audit")
    write(tmp_path, ".gemini/config/plugins/off/plugin.json", {})

    loaded = inventory.plugins(tmp_path, ("claude", "codex", "cursor", "gemini", "antigravity"))
    adobe, visualize = loaded["codex"]
    assert (adobe.family, adobe.id, adobe.version, adobe.root, adobe.made_for, adobe.loads) == (
        "adobe", "app-abcd@remote", "2.0.0", codex, "codex", True)
    assert adobe.skill_names == frozenset({"resize"}) and adobe.skills == 1
    assert adobe.servers == (("adobe", {"url": "https://adobe.example/mcp"}),)
    assert visualize.family == "visualize"
    checks, = loaded["claude"]
    assert checks.skill_names == frozenset({"check"})
    assert checks.servers == (("checks", {"command": f"{claude}/run", "args": []}),)
    assert [p.id for p in loaded["cursor"]] == ["cloudflare@cursor", "localpack@local"]
    assert loaded["cursor"][1].servers == (("local", {"command": "run", "args": []}),)
    assert loaded["gemini"][0].servers == (("pack", {"url": "https://pack.example/mcp"}),)
    assert loaded["gemini"][0].skill_names == frozenset({"pack-skill"})
    cloudflare, = loaded["antigravity"]
    assert cloudflare.id == "cloudflare@agy" and cloudflare.made_for == "antigravity"
    assert cloudflare.servers == (("cloudflare", {"url": "https://cf.example/mcp"}),)


def test_endpoint_normalizes_raw_urls_and_exact_npm_versions():
    assert inventory.endpoint({"serverUrl": "https://example.com/mcp/"}) == "https://example.com/mcp"
    assert inventory.endpoint({"httpUrl": "https://example.com/mcp/"}) == "https://example.com/mcp"
    assert inventory.endpoint({"command": "npx", "args": ["-y", "@org/pkg@1.2.3-beta.1", "mcp"]}) == (
        "npx -y @org/pkg mcp")


def test_inventory_command_is_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(mcp, "npm_latest", lambda pkg: "6.10.1" if pkg == "rea-agents" else "")
    monkeypatch.setattr(mcp, "npm_released", lambda pkg: {"6.10.1": "2026-01-01T00:00:00Z"})
    plugin = codex_plugin(tmp_path, "cloudflare@remote", "2.0.0", remote=True)
    make_skill(plugin / "skills", "workers")
    write(plugin, ".mcp.json", {"cloudflare": {"url": "https://cf.example/mcp"}})
    codex_plugin(tmp_path, "off@remote", "1.0.0", remote=True)
    write(tmp_path, ".codex/config.toml", '[plugins."off@remote"]\nenabled = false\n'
          '[mcp_servers.cf]\nurl = "https://cf.example/mcp"\n'
          '[mcp_servers.rea]\ncommand = "npx"\nargs = ["-y", "rea-agents@6.8.0", "mcp"]\n')

    def snapshot():
        return {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}

    before = snapshot()
    assert cli.main(["inventory"]) == 0
    assert snapshot() == before
    output = capsys.readouterr().out
    assert "Plugin cloudflare@remote 2.0.0 (made for Codex)" in output
    assert "Plugin off@remote 1.0.0 (made for Codex) (not loaded)" in output
    assert "Skills loaded: 1" in output
    assert "MCP server cloudflare from plugin cloudflare@remote" in output
    assert "MCP server rea from settings (rea-agents 6.8.0, 6.10.1 is out)" in output
    assert "approval required to remove MCP server cf from Codex settings" in output
    assert "would update MCP server rea in Codex settings" in output


def test_gemini_extensions_switched_off_for_the_home_folder_do_not_load(tmp_path):
    for name in ("on", "off", "back", "elsewhere"):
        write(tmp_path, f".gemini/extensions/{name}/gemini-extension.json", {"name": name})
    write(tmp_path, ".gemini/extensions/extension-enablement.json", {
        "off": {"overrides": [f"!{tmp_path}/*"]},
        "back": {"overrides": [f"!{tmp_path}/*", f"{tmp_path}/*"]},
        "elsewhere": {"overrides": ["!/somewhere/else/*"]},
    })
    loaded = inventory.plugins(tmp_path, ("gemini",))["gemini"]
    assert sorted(plugin.name for plugin in loaded) == ["back", "elsewhere", "on"]
    every = inventory.plugins(tmp_path, ("gemini",), include_disabled=True)["gemini"]
    assert [plugin.loads for plugin in every if plugin.name == "off"] == [False]


def test_a_broken_mcp_or_app_file_skips_that_file_not_the_plugin(tmp_path):
    root = codex_plugin(tmp_path, "pack@remote", "1.0.0", remote=True, metadata={
        "name": "pack", "version": "1.0.0", "mcpServers": "../../outside.json"})
    write(root, ".mcp.json", "{broken")
    write(root, ".app.json", "[")
    write(root, "mcp.json", {"good": {"url": "https://good.example"}})
    write(root.parent.parent, "outside.json", {"evil": {"url": "https://evil.example"}})
    make_skill(root / "skills", "kept")
    plugin, = inventory.plugins(tmp_path, ("codex",))["codex"]
    assert plugin.skill_names == {"kept"} and [name for name, _ in plugin.servers] == ["good"]
