import json

from agent_sync import review, writes
from agent_sync.config import Config


def server(path):
    return {"command": "node", "args": [str(path / "service.mjs")], "env": {"MODE": "prod"}}


def setup(tmp_path, claude_dir, codex_dir, cursor_dir=None):
    """A Codex-owned server whose service lives in a versioned folder; copies name whichever folder they saw."""
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"repl": server(claude_dir)}}))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(
        '[mcp_servers.repl]\ncommand = "node"\n'
        f'args = ["{codex_dir}/service.mjs"]\nenabled_tools = ["run"]\n\n[mcp_servers.repl.env]\nMODE = "prod"\n')
    if cursor_dir:
        (tmp_path / ".cursor").mkdir()
        (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"repl": server(cursor_dir)}}))


def versioned(tmp_path, version, exists=True):
    path = tmp_path / "app" / version
    if exists:
        path.mkdir(parents=True)
        (path / "service.mjs").write_text("")
    return path


def test_a_copy_naming_a_folder_that_is_gone_is_never_recommended(tmp_path):
    old, new = versioned(tmp_path, "1.0", exists=False), versioned(tmp_path, "2.0")
    setup(tmp_path, old, new)
    group, = review.scan(tmp_path, Config(home=tmp_path)).groups
    winner, reasons = review.recommend(group, "claude")
    assert winner.tools == ("codex",)
    assert reasons == ["the other copies point at files that no longer exist"]
    stale = next(version for version in group.versions if version.tools == ("claude",))
    assert review.cautions(stale, winner) == [f"stale: {old}/service.mjs no longer exists"]


def test_upkeep_replaces_stale_copies_with_the_working_one_and_keeps_tool_settings(tmp_path):
    old, new = versioned(tmp_path, "1.0", exists=False), versioned(tmp_path, "2.0")
    setup(tmp_path, old, new, cursor_dir=old)
    cfg = Config(home=tmp_path)
    before = (tmp_path / ".claude.json").read_text()
    dry = writes.repair_mcp(tmp_path, cfg, dry=True)
    assert [line.split(" (")[0] for line in dry] == ["would update ~/.claude.json", "would update ~/.cursor/mcp.json"]
    assert (tmp_path / ".claude.json").read_text() == before

    lines = writes.repair_mcp(tmp_path, cfg)
    assert "Claude Code and Cursor pointed at files that no longer exist; now Codex's version" in lines[0]
    assert lines[-1].startswith("originals backed up in ~/")
    for name in (".claude.json", ".cursor/mcp.json"):
        assert json.loads((tmp_path / name).read_text())["mcpServers"]["repl"]["args"] == [f"{new}/service.mjs"]
    assert 'enabled_tools = ["run"]' in (tmp_path / ".codex/config.toml").read_text()
    group, = review.scan(tmp_path, cfg).groups
    assert group.status == "same"
    assert writes.repair_mcp(tmp_path, cfg) == []


def test_working_copies_that_disagree_are_left_for_review(tmp_path):
    old, new = versioned(tmp_path, "1.0", exists=False), versioned(tmp_path, "2.0")
    other = versioned(tmp_path, "custom")
    setup(tmp_path, old, new, cursor_dir=other)
    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []


def test_paths_inside_values_are_checked_but_urls_and_package_names_are_not(tmp_path):
    there = versioned(tmp_path, "2.0")
    value = {"command": "node", "args": ["@scope/pkg/service", "https://example.com/a/b"],
             "env": {"SERVICES": json.dumps({"web": f"{there}/service.mjs", "gone": f"{tmp_path}/nowhere/x.mjs"}),
                     "PATHS": f"{there}:{tmp_path}/gone-dir/bin", "HOME_DIR": f"{tmp_path}/Folder With Spaces"}}
    (tmp_path / "Folder With Spaces").mkdir()
    assert review.missing_paths(value) == (f"{tmp_path}/nowhere/x.mjs", f"{tmp_path}/gone-dir/bin")


def test_only_the_servers_lines_change_in_a_settings_file_with_other_text(tmp_path):
    old, new = versioned(tmp_path, "1.0", exists=False), versioned(tmp_path, "2.0")
    setup(tmp_path, old, new)
    claude = tmp_path / ".claude.json"
    data = json.loads(claude.read_text())
    data["mcpServers"]["repl"]["type"] = "stdio"                       # as Claude Code writes it
    claude.write_text(json.dumps({"tip": "Say what you're doing — then continue.", **data}, indent=2,
                                 ensure_ascii=False) + "\n")
    before = claude.read_text().splitlines()
    writes.repair_mcp(tmp_path, Config(home=tmp_path))
    after = claude.read_text().splitlines()
    assert after[1] == before[1] == '  "tip": "Say what you\'re doing — then continue.",'
    assert sum(a != b for a, b in zip(before, after, strict=True)) == 1          # only the args line


def test_a_file_the_server_creates_in_a_folder_that_exists_is_not_stale(tmp_path):
    there = versioned(tmp_path, "2.0")
    (tmp_path / "logs").mkdir()
    for log in ("new.log", "other.log"):
        assert review.missing_paths({"command": "node", "args": [f"{there}/service.mjs"],
                                     "env": {"LOG_FILE": f"{tmp_path}/logs/{log}"}}) == ()
    assert review.missing_paths({"command": f"{tmp_path}/gone/bin/node", "args": []}) == (f"{tmp_path}/gone/bin/node",)


def test_copies_that_differ_only_in_a_log_file_are_left_for_review(tmp_path):
    there = versioned(tmp_path, "2.0")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/claude.log").write_text("")
    def server_with(log):
        return {"command": "node", "args": [f"{there}/service.mjs"], "env": {"LOG_FILE": f"{tmp_path}/logs/{log}"}}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"repl": server_with("claude.log")}}))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"repl": server_with("cursor.log")}}))
    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []


def test_a_copy_an_app_version_behind_is_brought_up_to_the_newest(tmp_path):
    old, new = versioned(tmp_path, "26.1117.31558"), versioned(tmp_path, "26.1120.41243")   # both folders still exist
    setup(tmp_path, old, new, cursor_dir=old)
    cfg = Config(home=tmp_path)
    group, = review.scan(tmp_path, cfg).groups
    winner, reasons = review.recommend(group, "claude")
    assert winner.tools == ("codex",) and reasons == ["the newest version (26.1120.41243)"]
    lines = writes.repair_mcp(tmp_path, cfg)
    assert "Claude Code and Cursor had an older version; now Codex's version (26.1120.41243)" in lines[0]
    for name in (".claude.json", ".cursor/mcp.json"):
        assert json.loads((tmp_path / name).read_text())["mcpServers"]["repl"]["args"] == [f"{new}/service.mjs"]


def test_copies_that_differ_in_more_than_version_numbers_are_left_for_review(tmp_path):
    one, other = versioned(tmp_path, "1.0"), versioned(tmp_path, "2.0")
    setup(tmp_path, one, other)
    data = json.loads((tmp_path / ".claude.json").read_text())
    data["mcpServers"]["repl"]["env"]["MODE"] = "debug"                   # a setting of its own, not a version
    (tmp_path / ".claude.json").write_text(json.dumps(data))
    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []


def test_addresses_are_never_taken_for_versions(tmp_path):
    there = versioned(tmp_path, "2.0")
    def server_at(host):
        return {"command": "node", "args": [f"{there}/service.mjs", "--upstream", f"http://{host}:8000/mcp"],
                "env": {"BIND": host}}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"repl": server_at("10.0.0.1")}}))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"repl": server_at("10.0.0.2")}}))
    cfg = Config(home=tmp_path)
    assert writes.repair_mcp(tmp_path, cfg) == []
    group, = review.scan(tmp_path, cfg).groups
    assert review.newest(group.versions) is None
