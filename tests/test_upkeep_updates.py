import json
from types import SimpleNamespace

import pytest

from agent_sync import cli, tools
from agent_sync.config import Config, parse

SKILLS_UPDATE = ["npx", "-y", "--prefer-online", "skills@latest", "update", "-g", "-y"]
BREW_UPDATE = ["brew", "update", "-q"]
BREW_FORMULAE = ("brew", "outdated", "--formula", "--quiet")
BREW_CASKS = ("brew", "outdated", "--cask", "--greedy", "--quiet")
NPM_ROOT = ("npm", "root", "-g")
NPM_OUTDATED = ("npm", "outdated", "-g", "--json")


def write(home, path, content):
    target = home / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


@pytest.fixture
def commands(tmp_path, monkeypatch):
    ran, read, lines = [], [], []
    output, responses = {}, {}
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "PIPX_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "bin"))

    def sh(args, timeout=900, cwd=None):
        ran.append(args)
        return responses.get(tuple(args), (0, ""))

    def raw(args, timeout=120, **kwargs):
        read.append(tuple(args))
        return output.get(tuple(args), "")

    def log(kind, message):
        lines.append((kind, message))

    monkeypatch.setattr(tools, "sh", sh)
    monkeypatch.setattr(tools, "raw", raw)
    return SimpleNamespace(ran=ran, read=read, lines=lines, output=output, responses=responses, log=log)


def program(home, name, actual=None):
    target = write(home, actual or f"bin/{name}", "#!/bin/sh\nexit 127\n")
    target.chmod(0o755)
    if actual:
        link = home / "bin" / name
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(target)
    return home / "bin" / name


def servers(home, path=".claude.json", **entries):
    write(home, path, json.dumps({"mcpServers": {name: {"command": cmd} for name, cmd in entries.items()}}))


@pytest.mark.parametrize("dry", [False, True])
def test_run_updates_global_skills_once_after_packages(tmp_path, monkeypatch, commands, capsys, dry):
    cfg = Config(home=tmp_path, skill_packages=["team/one", "team/two"], update_clis=False,
                 update_plugins=False, update_self=False)
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    assert cli.run(force=True, echo=True, dry=dry) == 0
    output = capsys.readouterr().out
    assert output.count("skills from every source (skills update)") == 1
    assert output.index("skills from team/two") < output.index("skills from every source")
    if dry:
        assert SKILLS_UPDATE not in commands.ran
        assert f"would run: {' '.join(SKILLS_UPDATE)}" in output
    else:
        assert commands.ran.count(SKILLS_UPDATE) == 1


@pytest.mark.parametrize(("installed", "available", "expected"), [
    ("1.9.0", "1.10.0", "install"),
    ("1.9.0", "1.9.0", "current"),
    ("1.9.0-agy.aaa", "1.9.0-agy.bbb", "install"),
    ("2.0.0", "1.99.0", None),
    ("2.0.0", None, None),
    ("2.0.0", "bad json", None),
])
def test_antigravity_plugins_use_the_installed_marketplace(tmp_path, commands, installed, available, expected):
    program(tmp_path, "agy")
    config = {"plugins": {"notes": {"installedFrom": {"marketplace": "team", "version": installed}}}}
    write(tmp_path, ".gemini/config/config.json", json.dumps(config))
    catalogs = ".gemini/antigravity-cli/cache/marketplaces"
    write(tmp_path, f"{catalogs}/other/marketplace.json",
          json.dumps({"plugins": [{"name": "notes", "version": "99.0.0"}]}))
    if available is not None:
        content = json.dumps({"plugins": [{"name": "notes", "version": available}]})
        write(tmp_path, f"{catalogs}/team/marketplace.json", content if available != "bad json" else "{")
    tools.update_plugins(commands.log, home=tmp_path, enabled=["antigravity"])
    assert commands.ran == ([["agy", "plugin", "install", "notes@team"]] if expected == "install" else [])
    if expected == "current":
        assert commands.lines == [("ok", "Antigravity plugins are current (1)")]
    elif expected is None:
        assert commands.lines == []


def test_antigravity_missing_config_or_disabled(tmp_path, commands):
    program(tmp_path, "agy")
    tools.update_plugins(commands.log, home=tmp_path, enabled=["antigravity"])
    config = {"plugins": {"notes": {"installedFrom": {"marketplace": "team", "version": "1.0.0"}}}}
    write(tmp_path, ".gemini/config/config.json", json.dumps(config))
    write(tmp_path, ".gemini/antigravity-cli/cache/marketplaces/team/marketplace.json",
          json.dumps({"plugins": [{"name": "notes", "version": "2.0.0"}]}))
    tools.update_plugins(commands.log, home=tmp_path, enabled=["claude"])
    tools.update_plugins(commands.log, True, tmp_path, enabled=["antigravity"])
    assert commands.ran == []
    assert commands.lines == [("dry", "would run: agy plugin install notes@team  (Antigravity plugin notes)")]


def test_mcp_brew_uses_package_identity_and_outdated_listings(tmp_path, commands):
    program(tmp_path, "mcp-safari", "brew/Cellar/mcp-safari-server/0.3.2/bin/mcp-safari")
    program(tmp_path, "mcp-helper", "brew/Cellar/mcp-safari-server/0.3.2/bin/mcp-helper")
    program(tmp_path, "current", "brew/Cellar/current/1/bin/current")
    program(tmp_path, "cask-mcp", "brew/Caskroom/cask-mcp/1/cask-mcp")
    servers(tmp_path, safari="mcp-safari", helper="mcp-helper", current="current")
    servers(tmp_path, ".cursor/mcp.json", other="mcp-safari", cask="cask-mcp")
    commands.output[BREW_FORMULAE] = "team/tap/mcp-safari-server\nunrelated\n"
    commands.output[BREW_CASKS] = "cask-mcp\n"
    tools.update_mcp_programs(commands.log, Config(home=tmp_path), False)
    assert commands.read == [BREW_FORMULAE, BREW_CASKS]
    assert commands.ran == [BREW_UPDATE, ["brew", "upgrade", "--formula", "team/tap/mcp-safari-server"],
                            ["brew", "upgrade", "--cask", "cask-mcp"]]
    assert ("ran", "mcp-safari-server upgrade (Homebrew, for MCP server safari)") in commands.lines


def test_mcp_brew_skips_another_accounts_install(tmp_path, monkeypatch, commands):
    program(tmp_path, "mcp-safari", "brew/Cellar/mcp-safari-server/0.3.2/bin/mcp-safari")
    servers(tmp_path, safari="mcp-safari")
    monkeypatch.setattr(tools, "brew_writable", lambda prefix: False)
    tools.update_mcp_programs(commands.log, Config(home=tmp_path), False)
    assert commands.read == commands.ran == []
    assert len(commands.lines) == 1 and "another account owns" in commands.lines[0][1]


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("custom_pipx_home", [False, True])
def test_mcp_resolves_npm_pipx_uv_and_own_updaters(tmp_path, monkeypatch, commands, dry, custom_pipx_home):
    pipx = "pipx-other" if custom_pipx_home else ".local/pipx"
    if custom_pipx_home:
        monkeypatch.setenv("PIPX_HOME", str(tmp_path / pipx))
    program(tmp_path, "npm")
    program(tmp_path, "one", "global/one/bin/one")
    program(tmp_path, "two", "global/@org/two/bin/two")
    program(tmp_path, "current", "global/current/bin/current")
    program(tmp_path, "pix", f"{pipx}/venvs/pix-package/bin/pix")
    program(tmp_path, "tool-uv", ".local/share/uv/tools/tool-uv-package/bin/tool-uv")
    own = program(tmp_path, "plannotator", ".local/bin/plannotator")
    standalone = program(tmp_path, "standalone")
    unowned = program(tmp_path, "unowned")
    (tmp_path / "bin/alias").symlink_to(own)
    servers(tmp_path, one="one", two="two", current="current", pipx="pix", uv="tool-uv", own="plannotator",
            repeated="alias", standalone="standalone", unowned="unowned")
    commands.output.update({
        NPM_ROOT: str(tmp_path / "global") + "\n",
        NPM_OUTDATED: json.dumps({"one": {}, "@org/two": {}}),
        (str(own), "--help"): "Usage\n  plannotator update [--check]\n",
        (str(standalone), "--help"): "Commands\n  standalone self-update\n",
        (str(unowned), "--help"): "Usage: updates are available elsewhere\n",
    })
    tools.update_mcp_programs(commands.log, Config(home=tmp_path), dry)
    expected = [
        ["npm", "install", "-g", "one@latest"], ["npm", "install", "-g", "@org/two@latest"],
        ["pipx", "upgrade", "pix-package"], ["uv", "tool", "upgrade", "tool-uv-package"],
        [str(own), "update"], [str(standalone), "self-update"],
    ]
    assert commands.read == [NPM_ROOT, NPM_OUTDATED, (str(own), "--help"),
                             (str(standalone), "--help"), (str(unowned), "--help")]
    if dry:
        assert commands.ran == []
        assert [message.split("  (")[0] for kind, message in commands.lines if kind == "dry"] == [
            f"would run: {' '.join(args)}" for args in expected
        ]
    else:
        assert commands.ran == expected


def test_mcp_ignores_runners_agent_clis_apps_and_disabled_agents(tmp_path, commands):
    for name in ("codex", "agy", "npx", "node", "python3", "npm"):
        program(tmp_path, name)
    program(tmp_path, "node_repl", "Applications/ChatGPT.app/Contents/bin/node_repl")
    disabled = program(tmp_path, "disabled")
    commands.output[(str(disabled), "--help")] = "disabled update"
    servers(tmp_path, codex="codex", agy="agy", npx="npx", node="node", python="python3", embedded="node_repl")
    servers(tmp_path, ".cursor/mcp.json", other="disabled")
    tools.update_mcp_programs(commands.log, Config(home=tmp_path, agents=["claude"]), False)
    assert commands.read == commands.ran == []


def test_npm_outdated_reads_json_when_npm_exits_one(tmp_path, monkeypatch):
    program(tmp_path, "npm")
    ran = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(tools, "sh", lambda args, timeout: ran.append(args) or (0, ""))

    def subprocess_run(args, **kwargs):
        assert tuple(args) == NPM_OUTDATED
        return SimpleNamespace(returncode=1, stdout='{"npm": {"current": "11.0.0", "latest": "11.1.0"}}')

    monkeypatch.setattr(tools.subprocess, "run", subprocess_run)
    tools.update_prerequisites(lambda *_: None)
    assert ran == [["npm", "install", "-g", "npm@latest"]]


def test_run_shares_brew_refresh_and_updates_npm_after_node(tmp_path, monkeypatch, commands):
    program(tmp_path, "npm")
    brew = program(tmp_path, "brew", "brew/bin/brew")
    program(tmp_path, "cursor-agent", "brew/Caskroom/cursor-cli/1/cursor-agent")
    program(tmp_path, "mcp-safari", "brew/Cellar/mcp-safari-server/1/mcp-safari")
    program(tmp_path, "npm-mcp", "global/npm-mcp/bin/npm-mcp")
    servers(tmp_path, ".cursor/mcp.json", safari="mcp-safari", npm="npm-mcp")
    write(tmp_path, ".cursor/cli-config.json", "{}")
    cfg = Config(home=tmp_path, source="cursor", agents=["cursor"], skill_packages=[], update_plugins=False)
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(tools, "how_installed", lambda: ("homebrew", str(brew.resolve().parents[1])))
    commands.output.update({
        BREW_FORMULAE: "mcp-safari-server\ngit\nnode\n",
        NPM_ROOT: str(tmp_path / "global"),
        NPM_OUTDATED: '{"npm": {}, "npm-mcp": {}}',
    })
    commands.responses[("brew", "outdated", "--cask", "--greedy", "cursor-cli")] = (1, "cursor-cli")
    commands.responses[("brew", "outdated", "--formula", "agent-sync")] = (1, "agent-sync")
    assert cli.run(force=True, echo=False, dry=True) == 0
    assert not any(args[0] in ("brew", "npm") and args[1] != "outdated" for args in commands.ran)
    for _ in range(2):
        commands.ran.clear()
        commands.read.clear()
        assert cli.run(force=True, echo=False) == 0
        assert commands.ran.count(BREW_UPDATE) == 1
        assert commands.read.count(NPM_OUTDATED) == 1
        upgrades = [args[-1] for args in commands.ran if args[1:2] in (["upgrade"], ["install"])]
        assert upgrades == ["mcp-safari-server", "npm-mcp@latest", "cursor-cli", "git", "node",
                            "npm@latest", "agent-sync"]


def test_run_can_disable_mcp_program_updates(tmp_path, monkeypatch, commands):
    own = program(tmp_path, "plannotator")
    servers(tmp_path, own="plannotator")
    commands.output[(str(own), "--help")] = "plannotator update"
    cfg = parse("update_mcp_programs = false", tmp_path / "config.toml", tmp_path)
    cfg.skill_packages = []
    cfg.update_plugins = cfg.update_clis = cfg.update_self = False
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    assert cli.run(force=True, echo=False) == 0
    assert (str(own), "--help") not in commands.read
    assert SKILLS_UPDATE in commands.ran
