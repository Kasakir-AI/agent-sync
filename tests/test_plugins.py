import json

import pytest

from agent_sync import plugins
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

    plugins.own_versions(tmp_path, False, lambda *line: logs.append(line), state_dir=tmp_path / "state",
                         catalog=lambda: pytest.fail("already installed"), run=run)
    assert calls == [["codex", "plugin", "remove", OLD]]
    assert logs == [("ok", f"switched Codex from {OLD} 1.7.0 to its own {pid} 1.7.0")]


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
    ("success", [("add", NEW), ("remove", OLD)]),
    ("loses skills", [("add", NEW), ("remove", NEW)]),
    ("add fails", [("add", NEW)]),
    ("dry", []),
    ("older", []),
    ("unknown", []),
])
def test_catalog_switch_commands_and_remembered_choices(tmp_path, mode, actions):
    old_plugin(tmp_path)
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
            own_plugin(tmp_path, pid, version, same_skills=mode != "loses skills")
        elif pid == NEW:
            root = tmp_path / ".codex/plugins/cache/openai-curated-remote/netlify"
            (root / ".codex-remote-plugin-install.json").unlink()
        return 0, ""

    plugins.own_versions(tmp_path, mode == "dry", lambda *line: logs.append(line), state_dir=state,
                         catalog=lambda: [entry], run=run)
    assert calls == actions
    if mode == "loses skills":
        assert json.loads((state / "plugin-choices.json").read_text()) == {OLD: {NEW: version}}
        assert "lacks one" in logs[-1][1]
        plugins.own_versions(tmp_path, False, lambda *line: logs.append(line), state_dir=state,
                             catalog=lambda: [entry], run=run)
        assert calls == actions
        newer = plugins.CatalogEntry(NEW, "netlify", "openai-curated-remote", "2.0.0", False)
        plugins.own_versions(tmp_path, True, lambda *line: logs.append(line), state_dir=state,
                             catalog=lambda: [newer], run=run)
        assert "would try" in logs[-1][1] and "2.0.0" in logs[-1][1]
    else:
        assert not state.exists()
        expected = {"success": "switched", "add fails": "download failed", "dry": "would try",
                    "older": "older", "unknown": "could not be compared"}
        assert expected[mode] in logs[-1][1]


def test_twice_ignores_a_switched_off_plugin(tmp_path):
    old_plugin(tmp_path)
    own_plugin(tmp_path)
    assert plugins.twice(tmp_path) == [f"Codex loads plugin netlify twice: {OLD} 1.7.0, {NEW} 1.9.0"]
    write(tmp_path, ".codex/config.toml", f'[plugins."{OLD}"]\nenabled = false\n')
    assert plugins.twice(tmp_path) == []
