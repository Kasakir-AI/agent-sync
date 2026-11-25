import json

import pytest

from agent_sync import backups, pending, plugins
from tests.test_inventory import codex_plugin, write
from tests.test_skills import make_skill

OLD = "netlify-skills@claude-plugins-official"
NEW = "netlify@openai-curated-remote"


def old_plugin(home, version="1.7.0"):
    root = codex_plugin(home, OLD, version, manifest=".claude-plugin/plugin.json")
    make_skill(root / "skills", "netlify-functions")
    write(home, ".codex/config.toml", f'[plugins."{OLD}"]\nenabled = true\n')
    return root


def own_plugin(home, pid=NEW, version="1.9.0", *, same_skills=True):
    root = codex_plugin(home, pid, version, remote=True, metadata={
        "interface": {"displayName": "Netlify"}, "version": version})
    if same_skills:
        make_skill(root / "skills", "netlify-functions")
    return root


def test_codex_catalog_parses_two_marketplaces():
    text = """Marketplace `claude-plugins-official` (/checkout/.claude-plugin/marketplace.json)
ID              STATUS               VERSION  SOURCE
netlify-skills  installed, enabled   1.7.0    ./plugins/netlify-skills
extra           not installed        1.0.0    ./plugins/extra

Marketplace `openai-curated-remote`
ID                  STATUS               VERSION  SOURCE
netlify             not installed        1.9.0
visualize           installed, enabled   1.0.89
other               installed, disabled  2.0.0
"""
    entries = plugins.parse_catalog(text)
    assert entries == [
        plugins.CatalogEntry(OLD, "netlify-skills", "claude-plugins-official", "1.7.0", True),
        plugins.CatalogEntry("extra@claude-plugins-official", "extra", "claude-plugins-official", "1.0.0", False),
        plugins.CatalogEntry(NEW, "netlify", "openai-curated-remote", "1.9.0", False),
        plugins.CatalogEntry("visualize@openai-curated-remote", "visualize", "openai-curated-remote", "1.0.89", True),
        plugins.CatalogEntry("other@openai-curated-remote", "other", "openai-curated-remote", "2.0.0", True),
    ]
    assert [entry.claude_format for entry in entries] == [True, True, False, False, False]


def test_switches_to_an_installed_codex_app_that_keeps_skills(tmp_path):
    old_plugin(tmp_path)
    pid = "app-deadbeef@openai-curated-remote"
    own_plugin(tmp_path, pid, "1.7.0")
    calls, logs = [], []

    def run(args):
        calls.append(args)
        return 0, ""

    state = tmp_path / "state"
    log = logs.append

    def switch(now, keep=()):
        with pending.during(state, keep, lambda *line: log(line), False, now=now):
            plugins.own_versions(tmp_path, False, lambda *line: log(line), state_dir=state,
                                 catalog=lambda: pytest.fail("already installed"), run=run)

    switch(100, [OLD])
    assert not calls and not logs
    switch(100)
    assert not calls
    assert logs[0][0] == "WARN" and f'add "plugin:codex:{OLD}"' in logs[0][1]
    assert "both" in logs[1][1]
    switch(100 + pending.WAIT - 1)
    assert not calls
    switch(100 + pending.WAIT)
    assert calls == [["codex", "plugin", "remove", OLD]]
    assert logs[-1] == ("ok", f"switched Codex from {OLD} 1.7.0 to its own {pid} 1.7.0")


@pytest.mark.parametrize(("version", "same_skills", "reason"), [
    ("1.6.0", True, "older"),
    ("unknown", True, "could not be compared"),
    ("1.9.0", False, "lacks one of its skills (netlify-functions)"),
])
def test_keeps_old_next_to_installed_app_when_replacement_would_regress(tmp_path, version, same_skills, reason):
    old_plugin(tmp_path)
    own_plugin(tmp_path, version=version, same_skills=same_skills)
    logs = []
    plugins.own_versions(tmp_path, False, lambda *line: logs.append(line), state_dir=tmp_path / "state",
                         run=lambda _: pytest.fail("would remove a plugin"))
    assert len(logs) == 1 and logs[0][0] == "info" and reason in logs[0][1]
    assert OLD in logs[0][1] and NEW in logs[0][1]


@pytest.mark.parametrize(("mode", "actions"), [
    ("success", [("add", NEW)]),
    ("unverified", [("add", NEW), ("remove", NEW)]),
    ("loses MCP", [("add", NEW), ("remove", NEW)]),
    ("loses skills", [("add", NEW), ("remove", NEW)]),
    ("add fails", [("add", NEW)]),
    ("dry", []),
    ("older", []),
    ("unknown", []),
])
def test_catalog_switch_commands_and_remembered_choices(tmp_path, mode, actions):
    root = old_plugin(tmp_path)
    if mode == "loses MCP":
        write(root, ".mcp.json", {"netlify": {"url": "https://api.example/mcp"}})
    state = tmp_path / "state"
    version = {"older": "1.6.0", "unknown": ""}.get(mode, "1.9.0")
    entry = plugins.CatalogEntry(NEW, "netlify", "openai-curated-remote", version, False)
    calls, logs = [], []

    def run(args):
        assert args[:2] == ["codex", "plugin"]
        action, pid = args[2:]
        calls.append((action, pid))
        if action == "add":
            if mode == "add fails":
                return 1, "download failed"
            if mode != "unverified":
                own_plugin(tmp_path, pid, version, same_skills=mode != "loses skills")
        elif pid == NEW:
            root = tmp_path / ".codex/plugins/cache/openai-curated-remote/netlify"
            (root / ".codex-remote-plugin-install.json").unlink(missing_ok=True)
        return 0, ""

    dry = mode == "dry"
    def log(*line):
        logs.append(line)

    config = tmp_path / ".codex/config.toml"
    before = config.read_bytes()
    with pending.during(state, [], log, dry, now=100):
        with backups.during(tmp_path, state / "backups", 30) as saved:
            plugins.own_versions(tmp_path, dry, log, state_dir=state, catalog=lambda: [entry], run=run)
    assert calls == actions
    rejections = {"loses skills": "lacks one of its skills", "loses MCP": "lacks one of its MCP servers",
                  "unverified": "could not be verified"}
    if mode in rejections:
        assert json.loads((state / "plugin-choices.json").read_text()) == {OLD: {NEW: version}}
        assert rejections[mode] in logs[-1][1]
        assert (saved.folder / ".codex/config.toml").read_bytes() == before
        plugins.own_versions(tmp_path, False, log, state_dir=state, catalog=lambda: [entry], run=run)
        assert calls == actions
        newer = plugins.CatalogEntry(NEW, "netlify", "openai-curated-remote", "2.0.0", False)
        plugins.own_versions(tmp_path, True, log, state_dir=state, catalog=lambda: [newer], run=run)
        assert "would try" in logs[-1][1] and "2.0.0" in logs[-1][1]
    else:
        assert (state / "pending-removals.json").exists() == (mode == "success")
        expected = {"success": "both", "add fails": "download failed", "dry": "would try",
                    "older": "older", "unknown": "could not be compared"}
        assert expected[mode] in logs[-1][1]


def test_twice_ignores_a_switched_off_plugin(tmp_path):
    old_plugin(tmp_path)
    own_plugin(tmp_path)
    assert plugins.twice(tmp_path) == [f"Codex loads plugin netlify twice: {OLD} 1.7.0, {NEW} 1.9.0"]
    write(tmp_path, ".codex/config.toml", f'[plugins."{OLD}"]\nenabled = false\n')
    assert plugins.twice(tmp_path) == []
