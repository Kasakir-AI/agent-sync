import json

import pytest

from agent_sync import backups, review, writes
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

    root = cfg.state_dir / "backups"
    with backups.during(tmp_path, root) as run:
        lines = writes.repair_mcp(tmp_path, cfg)
    assert (run.folder / ".claude.json").read_text() == before
    assert list(root.iterdir()) == [run.folder]
    assert "Claude Code and Cursor pointed at files that no longer exist; now Codex's version" in lines[0]
    for name in (".claude.json", ".cursor/mcp.json"):
        assert json.loads((tmp_path / name).read_text())["mcpServers"]["repl"]["args"] == [f"{new}/service.mjs"]
    assert 'enabled_tools = ["run"]' in (tmp_path / ".codex/config.toml").read_text()
    group, = review.scan(tmp_path, cfg).groups
    assert group.status == "same"
    assert writes.repair_mcp(tmp_path, cfg) == []


def test_upkeep_leaves_stale_mcp_copies_with_local_credentials_alone(tmp_path):
    old, new = versioned(tmp_path, "1.0", exists=False), versioned(tmp_path, "2.0")
    setup(tmp_path, old, new)
    source, target = tmp_path / ".codex/config.toml", tmp_path / ".claude.json"
    source.write_text(source.read_text() + 'API_KEY = "fixture-credential"\n')
    data = json.loads(target.read_text())
    data["mcpServers"]["repl"]["env"]["API_KEY"] = "fixture-credential"
    target.write_text(json.dumps(data))
    before = target.read_bytes()

    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []
    assert target.read_bytes() == before


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


@pytest.mark.parametrize("create_log_directory", [False, True])
def test_copies_that_differ_only_in_a_log_file_are_left_for_review(tmp_path, create_log_directory):
    there = versioned(tmp_path, "2.0")
    claude_log, cursor_log = tmp_path / "claude-logs/new.log", tmp_path / "cursor-logs/current.log"
    cursor_log.parent.mkdir()
    cursor_log.touch()
    if create_log_directory:
        claude_log.parent.mkdir()

    def settings(log):
        config = {"command": "node", "args": [f"{there}/service.mjs"], "env": {"LOG_FILE": str(log)}}
        return json.dumps({"mcpServers": {"repl": config}})

    claude = tmp_path / ".claude.json"
    claude.write_text(settings(claude_log))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text(settings(cursor_log))

    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []
    assert claude.read_text() == settings(claude_log)


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


@pytest.mark.parametrize("port", ["", ":8000"])
def test_addresses_are_never_taken_for_versions(tmp_path, port):
    there = versioned(tmp_path, "2.0")
    def server_at(host):
        return json.dumps({"mcpServers": {"repl": {
            "command": "node", "args": [f"{there}/service.mjs", "--upstream", f"http://{host}{port}/mcp"],
        }}})
    claude = tmp_path / ".claude.json"
    claude.write_text(server_at("10.0.0.1"))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text(server_at("10.0.0.2"))
    cfg = Config(home=tmp_path)

    assert writes.repair_mcp(tmp_path, cfg) == []
    assert claude.read_text() == server_at("10.0.0.1")


def test_repair_replans_instead_of_overwriting_a_concurrent_server_edit(tmp_path, monkeypatch):
    old, new = versioned(tmp_path, "1.0", exists=False), versioned(tmp_path, "2.0")
    setup(tmp_path, old, new)
    claude = tmp_path / ".claude.json"
    initial_edits = writes.edits
    edited = False

    def concurrent_edit(steps):
        nonlocal edited
        if not edited:
            edited = True
            claude.write_text(json.dumps({"mcpServers": {"repl": {"url": "https://local.example/mcp"}}}))
        return initial_edits(steps)

    monkeypatch.setattr(writes, "edits", concurrent_edit)
    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []
    assert json.loads(claude.read_text())["mcpServers"]["repl"] == {"url": "https://local.example/mcp"}


def test_review_and_repair_warn_and_skip_malformed_mcp_settings(tmp_path, monkeypatch, capsys):
    from agent_sync import cli

    old, new = versioned(tmp_path, "1.0", exists=False), versioned(tmp_path, "2.0")
    setup(tmp_path, old, new, cursor_dir=old)
    cursor = tmp_path / ".cursor/mcp.json"
    cursor.write_text('{"mcpServers":[]}')
    config = Config(home=tmp_path)
    monkeypatch.setattr(cli, "load", lambda: config)
    assert cli.main(["review", "--list"]) == 0
    assert "WARN  MCP servers" in capsys.readouterr().err

    lines = writes.repair_mcp(tmp_path, config)
    assert any(line.startswith("WARN") and str(cursor) in line for line in lines)
    assert any("updated ~/.claude.json" in line for line in lines)
    assert cursor.read_text() == '{"mcpServers":[]}'


def moved_checkout(tmp_path):
    """A checkout moved from ~/tool to ~/Projects/tool: Claude Code and Codex name the new place, Cursor the old."""
    new = tmp_path / "Projects/tool/bin"
    new.mkdir(parents=True)
    (new / "tool.js").write_text("")
    old = tmp_path / "tool/bin"

    def entry(folder):
        return {"command": "node", "args": [str(folder / "tool.js"), "mcp"]}
    return old, new, entry


def test_upkeep_repairs_copies_left_at_a_checkout_s_old_folder(tmp_path):
    old, new, entry = moved_checkout(tmp_path)
    setup(tmp_path, new, new, cursor_dir=old)
    for name, folder in ((".claude.json", new), (".cursor/mcp.json", old)):
        (tmp_path / name).write_text(json.dumps({"mcpServers": {"repl": entry(folder)}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/tool.js", "mcp"]\nenabled_tools = ["run"]\n')
    cfg = Config(home=tmp_path)

    lines = writes.repair_mcp(tmp_path, cfg)
    assert [line.split(" (")[0] for line in lines] == ["updated ~/.cursor/mcp.json"]
    assert "Cursor pointed at files that no longer exist" in lines[0]
    assert json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]["repl"]["args"] == entry(new)["args"]
    assert writes.repair_mcp(tmp_path, cfg) == []


@pytest.mark.parametrize("stale", [
    {"command": "node", "args": ["{old}/tool.js", "serve"]},          # other settings differ too
    {"command": "node", "args": ["{old}/other.js", "mcp"]},           # a different file
])
def test_a_stale_copy_that_differs_by_more_than_the_folder_is_left_for_review(tmp_path, stale):
    old, new, entry = moved_checkout(tmp_path)
    setup(tmp_path, new, new, cursor_dir=old)
    stale = json.loads(json.dumps(stale).replace("{old}", str(old)))
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"repl": entry(new)}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/tool.js", "mcp"]\n')
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"repl": stale}}))
    before = (tmp_path / ".cursor/mcp.json").read_bytes()

    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []
    assert (tmp_path / ".cursor/mcp.json").read_bytes() == before


def test_upkeep_repairs_a_moved_path_inside_an_argument(tmp_path):
    old, new, _ = moved_checkout(tmp_path)
    setup(tmp_path, new, new, cursor_dir=old)
    for name, folder in ((".claude.json", new), (".cursor/mcp.json", old)):
        (tmp_path / name).write_text(json.dumps({"mcpServers": {"repl": {
            "command": "node", "args": [f"--script={folder}/tool.js"]}}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["--script={new}/tool.js"]\n')

    assert [line.split(" (")[0] for line in writes.repair_mcp(tmp_path, Config(home=tmp_path))] == [
        "updated ~/.cursor/mcp.json"]
    assert json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]["repl"]["args"] == [
        f"--script={new}/tool.js"]


def test_a_repair_needs_every_changed_path_to_be_the_same_move(tmp_path):
    """The checkout moved, and a separate output folder is gone too: the working copy's output folder is a
    different place, not the same move, so the stale copy is left for review."""
    old, new, _ = moved_checkout(tmp_path)
    (tmp_path / "elsewhere/out").mkdir(parents=True)
    setup(tmp_path, new, new, cursor_dir=old)

    def entry(folder, out):
        return {"command": "node", "args": [str(folder / "tool.js")], "env": {"OUTPUT_DIR": str(out)}}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"repl": entry(new, tmp_path / "elsewhere/out")}}))
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"repl": entry(old, tmp_path / "gone/out")}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/tool.js"]\n\n'
        f'[mcp_servers.repl.env]\nOUTPUT_DIR = "{tmp_path}/elsewhere/out"\n')
    before = (tmp_path / ".cursor/mcp.json").read_bytes()

    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []
    assert (tmp_path / ".cursor/mcp.json").read_bytes() == before


def test_upkeep_repairs_a_server_naming_a_moved_folder_and_a_file_inside_it(tmp_path):
    old, new, _ = moved_checkout(tmp_path)
    setup(tmp_path, new, new, cursor_dir=old)

    def entry(folder):
        return {"command": "node", "args": [str(folder / "tool.js")], "env": {"TOOL_HOME": str(folder)}}
    for name, folder in ((".claude.json", new), (".cursor/mcp.json", old)):
        (tmp_path / name).write_text(json.dumps({"mcpServers": {"repl": entry(folder)}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/tool.js"]\n\n'
        f'[mcp_servers.repl.env]\nTOOL_HOME = "{new}"\n')

    assert [line.split(" (")[0] for line in writes.repair_mcp(tmp_path, Config(home=tmp_path))] == [
        "updated ~/.cursor/mcp.json"]
    assert json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]["repl"]["env"] == {
        "TOOL_HOME": str(new)}


def test_a_different_log_path_alone_is_not_a_move(tmp_path):
    old, new, _ = moved_checkout(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/run.log").write_text("")
    setup(tmp_path, new, new, cursor_dir=new)

    def entry(log):
        return {"command": "node", "args": [str(new / "tool.js")], "env": {"LOG_FILE": str(log)}}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"repl": entry(tmp_path / "logs/run.log")}}))
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"repl": entry(tmp_path / "gone/run.log")}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/tool.js"]\n\n'
        f'[mcp_servers.repl.env]\nLOG_FILE = "{tmp_path}/logs/run.log"\n')
    before = (tmp_path / ".cursor/mcp.json").read_bytes()

    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []
    assert (tmp_path / ".cursor/mcp.json").read_bytes() == before


def test_a_folder_setting_follows_the_move_of_the_script_inside_it(tmp_path):
    """~/tool moved to ~/Projects/tool: TOOL_HOME=~/tool is not 'missing' (its parent, home, exists), but it
    follows the same move as the script, so the whole copy is repaired."""
    new = tmp_path / "Projects/tool"
    new.mkdir(parents=True)
    (new / "server.js").write_text("")
    old = tmp_path / "tool"
    setup(tmp_path, new, new, cursor_dir=old)

    def entry(folder):
        return {"command": "node", "args": [str(folder / "server.js")], "env": {"TOOL_HOME": str(folder)}}
    for name, folder in ((".claude.json", new), (".cursor/mcp.json", old)):
        (tmp_path / name).write_text(json.dumps({"mcpServers": {"repl": entry(folder)}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/server.js"]\n\n'
        f'[mcp_servers.repl.env]\nTOOL_HOME = "{new}"\n')

    assert [line.split(" (")[0] for line in writes.repair_mcp(tmp_path, Config(home=tmp_path))] == [
        "updated ~/.cursor/mcp.json"]
    assert json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]["repl"]["env"] == {
        "TOOL_HOME": str(new)}


def test_a_live_path_under_the_old_parent_is_not_moved_along(tmp_path):
    """tool/ moved into Projects/, but logs/ beside it did not: a stale copy logging there keeps that for review."""
    old, new, _ = moved_checkout(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/out.log").write_text("")
    (tmp_path / "Projects/logs").mkdir()
    (tmp_path / "Projects/logs/out.log").write_text("")
    setup(tmp_path, new, new, cursor_dir=old)

    def entry(folder, log):
        return {"command": "node", "args": [str(folder / "tool.js")], "env": {"LOG_FILE": str(log)}}
    for name, server in ((".claude.json", entry(new, tmp_path / "Projects/logs/out.log")),
                         (".cursor/mcp.json", entry(old, tmp_path / "logs/out.log"))):
        (tmp_path / name).write_text(json.dumps({"mcpServers": {"repl": server}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/tool.js"]\n\n'
        f'[mcp_servers.repl.env]\nLOG_FILE = "{tmp_path}/Projects/logs/out.log"\n')
    before = (tmp_path / ".cursor/mcp.json").read_bytes()
    assert writes.repair_mcp(tmp_path, Config(home=tmp_path)) == []
    assert (tmp_path / ".cursor/mcp.json").read_bytes() == before


def test_a_folder_argument_follows_the_move_of_the_script(tmp_path):
    new = tmp_path / "Projects/tool"
    new.mkdir(parents=True)
    (new / "run.js").write_text("")
    old = tmp_path / "tool"
    setup(tmp_path, new, new, cursor_dir=old)

    def entry(folder):
        return {"command": "node", "args": [str(folder / "run.js"), "--cwd", str(folder)]}
    for name, folder in ((".claude.json", new), (".cursor/mcp.json", old)):
        (tmp_path / name).write_text(json.dumps({"mcpServers": {"repl": entry(folder)}}))
    (tmp_path / ".codex/config.toml").write_text(
        f'[mcp_servers.repl]\ncommand = "node"\nargs = ["{new}/run.js", "--cwd", "{new}"]\n')
    assert [line.split(" (")[0] for line in writes.repair_mcp(tmp_path, Config(home=tmp_path))] == [
        "updated ~/.cursor/mcp.json"]
