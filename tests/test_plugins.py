import json

import pytest

from agent_sync import backups, pending, plugins
from tests.test_inventory import codex_plugin, write
from tests.test_pending import approve_current
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
    assert logs[0][0] == "WARN" and f'approval required to remove Codex plugin {OLD}' in logs[0][1]
    assert "both" in logs[1][1]
    switch(10_000_000)
    assert not calls
    approve_current(state)
    switch(10_000_001)
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
    rejections = {"loses skills": "lacks one of its skills", "loses MCP": "lacks one of its MCP servers",
                  "unverified": "could not be verified"}
    with pending.during(state, [], log, dry, now=100):
        with backups.during(tmp_path, state / "backups") as saved:
            plugins.own_versions(tmp_path, dry, log, state_dir=state, catalog=lambda: [entry], run=run)
            if mode in rejections:
                assert (saved.folder / ".codex/config.toml").read_bytes() == before
    assert calls == actions
    if mode in rejections:
        assert json.loads((state / "plugin-choices.json").read_text()) == {OLD: {NEW: version}}
        assert rejections[mode] in logs[-1][1]
        assert config.read_bytes() == before and saved.folder is None
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


def test_a_kept_plugin_is_never_replaced_from_the_catalog(tmp_path):
    old_plugin(tmp_path)
    entry = plugins.CatalogEntry(NEW, "netlify", "openai-curated-remote", "1.9.0", False)
    with pending.during(tmp_path / "state", [f"plugin:codex:{OLD}"], lambda *_: None, False):
        plugins.own_versions(tmp_path, False, lambda *_: None, state_dir=tmp_path / "state",
                             catalog=lambda: [entry], run=lambda _: pytest.fail("would add a plugin"))


def test_a_codex_app_with_another_name_counts_when_it_has_half_the_skills(tmp_path):
    root = old_plugin(tmp_path)
    make_skill(root / "skills", "netlify-deploy")
    app = codex_plugin(tmp_path, "app-0a1b@openai-curated-remote", "1.7.0", remote=True,
                       metadata={"interface": {"displayName": "Deploy Tools"}, "version": "1.7.0"})
    make_skill(app / "skills", "netlify-functions")
    logs = []
    with pending.during(tmp_path / "state", [], lambda *line: logs.append(line), False):
        plugins.own_versions(tmp_path, False, lambda *line: logs.append(line), state_dir=tmp_path / "state",
                             catalog=lambda: pytest.fail("already installed"), run=lambda _: (0, ""))
    assert any("lacks one of its skills (netlify-deploy)" in message for _, message in logs)


def test_a_replacement_that_lacks_a_command_or_hooks_is_not_taken(tmp_path):
    root = old_plugin(tmp_path)
    write(root, "commands/deploy.md", "deploy")
    write(root, "hooks/hooks.json", {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "start"}]}]}})
    own_plugin(tmp_path)
    logs = []
    plugins.own_versions(tmp_path, False, lambda *line: logs.append(line), state_dir=tmp_path / "state",
                         run=lambda _: pytest.fail("would remove a plugin"))
    assert "lacks 2 of its commands, agents or hooks (command deploy, hook SessionStart)" in logs[0][1]


@pytest.mark.parametrize("second_command", ["second", "first"], ids=["distinct", "repeated"])
def test_switch_requires_every_invocation_even_for_the_same_event_and_matcher(tmp_path, second_command):
    root = old_plugin(tmp_path)
    new = own_plugin(tmp_path)
    hooks = [{"type": "command", "command": command} for command in ("first", second_command)]
    write(root, "hooks/hooks.json", {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}})
    manifest = ".codex-plugin/plugin.json"
    metadata = json.loads((new / manifest).read_text())
    write(new, manifest, {**metadata, "hooks": ["./hooks/custom.json"]})
    write(new, "hooks/custom.json", {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks[:1]}]}})
    logs = []

    def switch():
        plugins.own_versions(tmp_path, False, lambda *line: logs.append(line), state_dir=tmp_path / "state",
                             run=lambda _: pytest.fail("would remove a plugin"))

    with pending.during(tmp_path / "state", [], lambda *line: logs.append(line), False) as run:
        switch()
        assert list(run.questions()) == []
        assert "lacks one of its commands, agents or hooks" in logs[-1][1]
        assert "PreToolUse" in logs[-1][1]
        write(new, "hooks/custom.json", {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}})
        switch()
        assert list(run.questions()) == [f"plugin:codex:{OLD}"]


def test_twice_says_once_that_a_pending_removal_waits_for_approval(tmp_path):
    old_plugin(tmp_path)
    own_plugin(tmp_path)
    with pending.during(tmp_path / "state", [], lambda *_: None, False):
        assert not pending.due(f"plugin:codex:{OLD}", f"Codex plugin {OLD}")
        assert plugins.twice(tmp_path) == [
            f"info  Codex loads plugin netlify twice until you approve removing {OLD}: waiting for your approval"]


def test_a_hook_file_named_in_the_manifest_and_at_the_default_path_counts_once(tmp_path):
    root = old_plugin(tmp_path)
    new = own_plugin(tmp_path)
    hooks = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "audit"}]}]}}
    write(root, "hooks/hooks.json", hooks)
    claude = ".claude-plugin/plugin.json"
    write(root, claude, {**json.loads((root / claude).read_text()), "hooks": "./hooks/hooks.json"})
    codex = ".codex-plugin/plugin.json"
    write(new, codex, {**json.loads((new / codex).read_text()), "hooks": ["./hooks/hooks.json"]})
    write(new, "hooks/hooks.json", hooks)
    logs = []
    with pending.during(tmp_path / "state", [], lambda *line: logs.append(line), False) as run:
        plugins.own_versions(tmp_path, False, lambda *line: logs.append(line), state_dir=tmp_path / "state",
                             run=lambda _: pytest.fail("would remove a plugin"))
        assert list(run.questions()) == [f"plugin:codex:{OLD}"]
