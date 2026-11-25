import json
from stat import S_IMODE
from types import SimpleNamespace

import pytest

from agent_sync import backups, cli, tools
from agent_sync.config import Config, parse

SKILLS_UPDATE = ["npx", "-y", "--prefer-online", "skills@latest", "update", "-g", "-y"]
SKILLS_CHECK = (*SKILLS_UPDATE[:4], "check", "-g")
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
    write(tmp_path, ".agents/.skill-lock.json", '{"skills":{"example":{}}}')
    commands.output[SKILLS_CHECK] = "Checking for skill updates…\n↑ example has an update\n"
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
        assert commands.ran.count([*SKILLS_UPDATE, "example"]) == 1


@pytest.mark.parametrize(("cli_name", "report"), [
    ("skills@latest", "Checking for skill updates…\n✓ All global skills are up to date\n"),
    ("skills@1.4.5", "Checking for skill updates...\n\nChecking 1 skill(s) for updates...\n\n"
                     "✓ All skills are up to date\n\n"),
])
def test_global_skills_up_to_date_never_open_a_backup(tmp_path, commands, cli_name, report):
    write(tmp_path, ".agents/.skill-lock.json", '{"skills":{"current":{}}}')
    write(tmp_path, ".agents/skills/current/SKILL.md", "unchanged")
    check = (*SKILLS_CHECK[:3], cli_name, "check", "-g")
    commands.output[check] = report
    root = tmp_path / "backups"

    with backups.during(tmp_path, root):
        tools.update_skills(commands.log, False, cli_name, home=tmp_path)
        assert not root.exists()

    assert commands.read == [check]
    assert commands.ran == []


def test_global_skills_check_failure_leaves_installed_skills_alone(tmp_path, commands):
    write(tmp_path, ".agents/.skill-lock.json", '{"skills":{"current":{}}}')
    original = write(tmp_path, ".agents/skills/current/SKILL.md", "unchanged")
    commands.output[SKILLS_CHECK] = "Checking for skill updates…\n✗ Failed to check skills from team/pack\n"
    root = tmp_path / "backups"

    with backups.during(tmp_path, root):
        tools.update_skills(commands.log, False, "skills@latest", home=tmp_path)
        assert not root.exists()

    assert commands.ran == []
    assert commands.lines[-1][0] == "FAIL"
    assert original.read_text() == "unchanged"


def test_global_skill_update_backs_up_only_reported_installs(tmp_path, monkeypatch, commands):
    write(tmp_path, ".agents/.skill-lock.json", '{"skills":{"renamed":{},"current":{}}}')
    changed = write(tmp_path, ".agents/skills/renamed/SKILL.md", "original")
    current = write(tmp_path, ".agents/skills/current/SKILL.md", "unchanged")
    commands.output[SKILLS_CHECK] = (
        "Checking for skill updates…\n\nFetching 1 source…\nChecking skills from source: team/pack\n"
        "↑ renamed has an update\n\n1 skill(s) can be updated; run npx skills update to install\n\n"
    )
    root = tmp_path / "backups"

    def sh(args, timeout):
        assert args == [*SKILLS_UPDATE, "renamed"]
        folder, = root.iterdir()
        assert not (folder / current.relative_to(tmp_path)).exists()
        assert (folder / changed.relative_to(tmp_path)).read_text() == "original"
        changed.write_text("updated")
        return 0, ""

    monkeypatch.setattr(tools, "sh", sh)
    with backups.during(tmp_path, root):
        tools.update_skills(commands.log, False, "skills@latest", home=tmp_path)

    assert changed.read_text() == "updated"


@pytest.mark.parametrize("report", [
    pytest.param(
        "\x1b[38;5;145mChecking for skill updates...\x1b[0m\n\n"
        "\x1b[38;5;102mChecking 2 skill(s) for updates...\x1b[0m\n\n"
        "\x1b[38;5;145m1 update(s) available:\x1b[0m\n\n"
        "  \x1b[38;5;145m↑\x1b[0m renamed\n"
        "    \x1b[38;5;102msource: team/pack\x1b[0m\n\n"
        "\x1b[38;5;102mRun\x1b[0m \x1b[38;5;145mnpx skills update\x1b[0m "
        "\x1b[38;5;102mto update all skills\x1b[0m\n\n",
        id="legacy-indented-rows",
    ),
    pytest.param("Checking for skill updates…\nChanges found in team/pack\n", id="unknown-output"),
    pytest.param("", id="empty-output"),
])
def test_global_skill_bulk_update_backs_up_all_locked_installs(tmp_path, monkeypatch, commands, report):
    lock = write(tmp_path, ".agents/.skill-lock.json", '{"skills":{"renamed":{},"current":{}}}')
    changed = write(tmp_path, ".agents/skills/renamed/SKILL.md", "original")
    current = write(tmp_path, ".agents/skills/current/SKILL.md", "unchanged")
    unmanaged = write(tmp_path, ".agents/skills/unmanaged/SKILL.md", "unmanaged")
    update = [*SKILLS_UPDATE[:3], "skills@1.4.5", "update", "-g", "-y"]
    commands.output[(*update[:4], "check", "-g")] = report

    def sh(args, timeout):
        assert args == update
        assert any(level == "info" for level, _ in commands.lines)
        for original in (lock, changed, current):
            assert (run.folder / original.relative_to(tmp_path)).read_bytes() == original.read_bytes()
        assert not (run.folder / unmanaged.relative_to(tmp_path)).exists()
        changed.write_text("updated")
        return 0, ""

    monkeypatch.setattr(tools, "sh", sh)
    with backups.during(tmp_path, tmp_path / "backups") as run:
        tools.update_skills(commands.log, False, "skills@1.4.5", home=tmp_path)

    assert changed.read_text() == "updated"


def test_gemini_update_backs_up_only_updatable_extension_folders(tmp_path, monkeypatch, commands):
    program(tmp_path, "gemini")
    changed = write(tmp_path, ".gemini/extensions/changed/gemini-extension.json", '{"name":"changed"}')
    write(changed.parent, ".gemini-extension-install.json", '{"type":"git"}')
    current = write(tmp_path, ".gemini/extensions/current/gemini-extension.json", '{"name":"current"}')
    write(current.parent, ".gemini-extension-install.json", '{"type":"local"}')
    linked = write(tmp_path, ".gemini/extensions/linked/gemini-extension.json", '{"name":"linked"}')
    write(linked.parent, ".gemini-extension-install.json", '{"type":"link"}')
    write(tmp_path, ".gemini/extensions/unrelated/archive", "unrelated")
    root = tmp_path / "backups"

    def sh(args, timeout):
        assert args == ["gemini", "extensions", "update", "--all"]
        folder, = root.iterdir()
        saved = folder / ".gemini/extensions"
        assert {path.name for path in saved.iterdir()} == {"changed", "current"}
        changed.write_text('{"name":"changed","version":"2.0.0"}')
        return 0, ""

    monkeypatch.setattr(tools, "sh", sh)
    with backups.during(tmp_path, root):
        tools.update_plugins(commands.log, home=tmp_path, enabled=["gemini"])

    folder, = root.iterdir()
    assert (folder / changed.relative_to(tmp_path)).read_text() == '{"name":"changed"}'
    assert not (folder / current.parent.relative_to(tmp_path)).exists()


@pytest.mark.parametrize("same_bytes", [False, True])
def test_failed_standalone_update_preserves_resolved_executable(tmp_path, monkeypatch, commands, same_bytes):
    link = program(tmp_path, "standalone", "libexec/standalone")
    real = link.resolve()
    original = real.read_bytes()
    servers(tmp_path, own="standalone")
    cfg = Config(home=tmp_path, program_updates={"standalone": ["standalone", "update"]})
    root = tmp_path / "backups"

    def sh(args, timeout):
        assert args == ["standalone", "update"]
        if not same_bytes:
            real.write_text("broken")
        real.chmod(0o644)
        return 1, "update failed"

    monkeypatch.setattr(tools, "sh", sh)
    with backups.during(tmp_path, root):
        tools.update_mcp_programs(commands.log, cfg, False)

    assert commands.lines[-1][0] == "FAIL"
    folder, = root.iterdir()
    saved = folder / real.relative_to(tmp_path)
    assert saved.read_bytes() == original
    assert S_IMODE(saved.stat().st_mode) == 0o755


@pytest.mark.parametrize(("installed", "available", "expected"), [
    ("1.9.0", "1.10.0", "install"),
    ("1.9.0", "1.9.0", "current"),
    ("1.9.0-agy.aaa", "1.9.0-agy.bbb", "install"),
    ("1.9.0-beta.1", "1.9.0", "install"),
    ("1.9.0", "1.9.0-agy.1", None),
    ("1.9.0", "1.10.0-beta.1", None),
    ("1.9.0", "1.9.0-rc.1", None),
    ("1.9.0-agy.2", "1.9.0", None),
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


def test_mcp_program_updates_report_non_object_settings(tmp_path, commands):
    malformed = write(tmp_path, ".claude.json", "[]")
    tools.update_mcp_programs(commands.log, Config(home=tmp_path), True)

    assert commands.read == commands.ran == []
    assert any(level == "WARN" and str(malformed) in message for level, message in commands.lines)


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("custom_pipx_home", [False, True])
def test_mcp_resolves_npm_pipx_uv_and_listed_own_updaters(tmp_path, monkeypatch, commands, dry, custom_pipx_home):
    pipx = "pipx-other" if custom_pipx_home else ".local/pipx"
    if custom_pipx_home:
        monkeypatch.setenv("PIPX_HOME", str(tmp_path / pipx))
    program(tmp_path, "npm")
    program(tmp_path, "one", "global/one/bin/one")
    program(tmp_path, "two", "global/@org/two/bin/two")
    program(tmp_path, "current", "global/current/bin/current")
    program(tmp_path, "pix", f"{pipx}/venvs/pix-package/bin/pix")
    program(tmp_path, "tool-uv", ".local/share/uv/tools/tool-uv-package/bin/tool-uv")
    own = program(tmp_path, "plannotator", ".local/bin/plannotator").resolve()
    program(tmp_path, "standalone")
    (tmp_path / "bin/alias").symlink_to(own)
    servers(tmp_path, one="one", two="two", current="current", pipx="pix", uv="tool-uv", own="plannotator",
            repeated="alias", standalone="standalone")
    commands.output.update({
        NPM_ROOT: str(tmp_path / "global") + "\n",
        NPM_OUTDATED: json.dumps({"one": {}, "@org/two": {}}),
    })
    cfg = Config(home=tmp_path, program_updates={"plannotator": ["plannotator", "update"]})
    tools.update_mcp_programs(commands.log, cfg, dry)
    if dry:                                            # a dry run runs nothing, not even a lookup
        assert commands.read == commands.ran == []
        assert ("dry", "would run: plannotator update  (plannotator update (MCP server own))") in commands.lines
        assert any("would update pix-package (pipx)" in message for _, message in commands.lines)
        return
    assert commands.read == [NPM_ROOT, NPM_OUTDATED]   # no program is probed with --help
    assert commands.ran == [
        ["npm", "install", "-g", "one@latest"], ["npm", "install", "-g", "@org/two@latest"],
        ["pipx", "upgrade", "pix-package"], ["uv", "tool", "upgrade", "tool-uv-package"],
        ["plannotator", "update"],
    ]


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
    assert SKILLS_UPDATE not in commands.ran


@pytest.mark.parametrize(("setting", "dry", "expected"), [
    (False, False, []),
    (True, False, [["npm", "install", "-g", "one@latest"], ["pipx", "upgrade-all"],
                   ["uv", "tool", "upgrade", "--all"]]),
    (["homebrew"], False, [BREW_UPDATE, ["brew", "upgrade"]]),
    (["homebrew", "npm", "pipx", "uv"], True, []),
])
def test_global_tools_update_only_selected_managers(tmp_path, commands, setting, dry, expected):
    for manager in ("npm", "pipx", "uv", "brew"):
        program(tmp_path, manager)
    commands.output[NPM_OUTDATED] = '{"one": {}, "already-updated": {}}'
    state = tools.UpdateState(npm_upgraded={"already-updated"})
    cfg = Config(home=tmp_path, update_global_tools=setting)
    tools.update_global_tools(commands.log, cfg, dry, state=state)
    assert commands.ran == expected
    assert commands.read == ([NPM_OUTDATED] if setting is True else [])
    if dry:
        assert [message for kind, message in commands.lines if kind == "dry"] == [
            "would run: npm outdated -g --json  (global npm tools)",
            "would run: pipx upgrade-all  (global pipx tools)",
            "would run: uv tool upgrade --all  (global uv tools)",
            "would run: brew upgrade  (global homebrew tools)",
        ]


def test_global_tools_run_between_mcp_and_clis_and_reuse_manager_state(tmp_path, monkeypatch, commands):
    program(tmp_path, "npm")
    program(tmp_path, "brew", "brew/bin/brew")
    program(tmp_path, "mcp-npm", "global/mcp-npm/bin/mcp-npm")
    program(tmp_path, "mcp-brew", "brew/Cellar/mcp-brew/1/bin/mcp-brew")
    program(tmp_path, "claude")
    servers(tmp_path, npm="mcp-npm", brew="mcp-brew")
    commands.output.update({NPM_ROOT: str(tmp_path / "global"),
                            NPM_OUTDATED: '{"mcp-npm": {}, "npm": {}}',
                            BREW_FORMULAE: "mcp-brew\n"})
    cfg = Config(home=tmp_path, agents=["claude"], skill_packages=[], update_plugins=False, update_self=False,
                 update_global_tools=["npm", "homebrew"])
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    assert cli.run(force=True, echo=False) == 0
    assert commands.ran.count(BREW_UPDATE) == commands.read.count(NPM_OUTDATED) == 1
    assert [args for args in commands.ran if args[:2] in (
        ["npm", "install"], ["brew", "upgrade"], ["claude", "update"]
    )] == [
        ["npm", "install", "-g", "mcp-npm@latest"], ["brew", "upgrade", "--formula", "mcp-brew"],
        ["npm", "install", "-g", "npm@latest"], ["brew", "upgrade"], ["claude", "update"],
    ]


def test_mcp_programs_never_update_interpreters_or_switched_off_servers(tmp_path, commands):
    for name in ("python3.13", "java", "ruby", "bash"):
        program(tmp_path, name, f"brew/Cellar/{name}/1/bin/{name}")
    program(tmp_path, "off", "brew/Cellar/off/1/bin/off")
    program(tmp_path, "disabled", "brew/Cellar/disabled/1/bin/disabled")
    program(tmp_path, "python", "brew/Cellar/python@3.13/3.13.1/bin/python3.13")   # a link named python
    servers(tmp_path, py="python3.13", java="java", ruby="ruby", sh="bash", link="python", disabled="disabled")
    write(tmp_path, ".codex/config.toml", '[mcp_servers.off]\ncommand = "off"\nenabled = false\n')
    data = json.loads((tmp_path / ".claude.json").read_text())
    data["mcpServers"]["disabled"]["disabled"] = True
    (tmp_path / ".claude.json").write_text(json.dumps(data))
    commands.output[BREW_FORMULAE] = "python@3.13\njava\nruby\nbash\noff\ndisabled\n"
    tools.update_mcp_programs(commands.log, Config(home=tmp_path), False)
    assert commands.read == commands.ran == []


def test_global_homebrew_tools_skip_another_accounts_homebrew(tmp_path, monkeypatch, commands):
    program(tmp_path, "brew", "brew/bin/brew")
    monkeypatch.setattr(tools, "brew_writable", lambda prefix: False)
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["homebrew"]), False,
                              state=tools.UpdateState())
    assert commands.ran == [] and "another account" in commands.lines[0][1]


def test_antigravity_ids_that_could_be_flags_or_paths_are_never_passed_on(tmp_path, commands):
    program(tmp_path, "agy")
    config = {"plugins": {"-rf": {"installedFrom": {"marketplace": "team", "version": "1.0.0"}},
                          "notes": {"installedFrom": {"marketplace": "../escape", "version": "1.0.0"}}}}
    write(tmp_path, ".gemini/config/config.json", json.dumps(config))
    for market in ("team", "escape"):
        write(tmp_path, f".gemini/antigravity-cli/cache/marketplaces/{market}/marketplace.json",
              json.dumps({"plugins": [{"name": "-rf", "version": "2.0.0"}, {"name": "notes", "version": "2.0.0"}]}))
    tools.update_plugins(commands.log, home=tmp_path, enabled=["antigravity"])
    assert commands.ran == []


def test_claude_plugins_update_only_at_user_scope_and_back_up_their_state(tmp_path, monkeypatch, commands):
    from datetime import datetime

    from agent_sync import backups
    from tests.test_skills import make_plugin

    program(tmp_path, "claude")
    write(tmp_path, ".claude.json", "{}")
    root = make_plugin(tmp_path, "notes@team", ["write"])
    write(root, "big.bin", "x" * 1000)
    other = make_plugin(tmp_path, "other@team", ["read"])
    commands.output[("claude", "plugin", "list", "--json")] = json.dumps([
        {"id": "notes@team", "scope": "user"}, {"id": "proj@team", "scope": "project"},
        {"id": "--help", "scope": "user"}, {"id": "a/b@team", "scope": "user"}])
    with backups.during(tmp_path, tmp_path / "backups", now=datetime(2026, 11, 1)) as run:
        tools.update_plugins(commands.log, home=tmp_path, enabled=["claude"])
        saved = run.folder
        assert (saved / ".claude.json").exists() and (saved / ".claude/plugins/installed_plugins.json").exists()
        assert (saved / root.relative_to(tmp_path) / "big.bin").read_text() == "x" * 1000
        assert not (saved / other.relative_to(tmp_path)).exists()
        assert not (saved / ".claude/plugins/marketplaces").exists()
    assert commands.ran == [["claude", "plugin", "marketplace", "update"], ["claude", "plugin", "update", "notes@team"]]
    assert run.folder is None and not saved.exists()


def test_standalone_updater_backs_up_its_install_folder(tmp_path, monkeypatch, commands):
    link = program(tmp_path, "standalone", ".local/share/standalone/bin/standalone")
    real = link.resolve()
    support = real.parent.parent / "support.txt"
    support.write_text("before")
    unrelated = tmp_path / ".local/share/unrelated.txt"
    unrelated.write_text("unrelated")
    servers(tmp_path, own="standalone")
    cfg = Config(home=tmp_path, program_updates={"standalone": ["standalone", "update"]})

    def sh(args, timeout):
        assert args == ["standalone", "update"]
        support.write_text("after")
        unrelated.write_text("independent edit")
        real.write_text("new executable")
        return 1, "update failed"

    monkeypatch.setattr(tools, "sh", sh)
    with backups.during(tmp_path, tmp_path / "backups") as run:
        tools.update_mcp_programs(commands.log, cfg, False)
    assert (run.folder / support.relative_to(tmp_path)).read_text() == "before"
    assert (run.folder / real.relative_to(tmp_path)).is_file()
    assert not (run.folder / unrelated.relative_to(tmp_path)).exists()
