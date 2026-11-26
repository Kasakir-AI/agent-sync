import json
import urllib.request
from datetime import UTC, datetime
from io import BytesIO
from stat import S_IMODE
from types import SimpleNamespace

import pytest

from agent_sync import backups, cli, pending, tools
from agent_sync.config import Config, parse

SKILLS_UPDATE = ["npx", "-y", "--prefer-online", "skills@latest", "update", "-g", "-y"]
SKILLS_CHECK = (*SKILLS_UPDATE[:4], "check", "-g")
BREW_UPDATE = ["brew", "update", "-q"]
BREW_CASKS = ("brew", "outdated", "--cask", "--greedy", "--quiet")
BREW_JSON = ("brew", "outdated", "--json=v2")
NPM_ROOT = ("npm", "root", "-g")
NPM_OUTDATED = ("npm", "outdated", "-g", "--json")
PIPX_LIST = ("pipx", "list", "--json")
UV_LIST = ("uv", "tool", "list")
UV_DIR = ("uv", "tool", "dir")
SETTLED = "2020-01-01T00:00:00.000Z"


def npm_releases(output, packages):
    """Fake npm's answers: {package: (installed, {release: when, ...})}; the last release is the latest."""
    report = {}
    for name, (installed, releases) in packages.items():
        if installed != list(releases)[-1]:         # npm lists only outdated packages
            report[name] = {"current": installed, "latest": list(releases)[-1]}
        output[("npm", "view", name, "time", "--json")] = json.dumps(releases)
        for major in {version.split(".")[0] for version in [installed, *releases]}:
            output[("npm", "view", f"{name}@^{major}", "version", "--json")] = json.dumps(
                [version for version in releases if version.split(".")[0] == major])
    output[NPM_OUTDATED] = json.dumps(report)


def brew_outdated(formulae=(), casks=()):
    """Fake `brew outdated --json=v2`: (name, installed, current) per formula and cask."""
    return json.dumps({section: [{"name": name, "installed_versions": [installed], "current_version": current,
                                  "pinned": False} for name, installed, current in entries]
                       for section, entries in (("formulae", formulae), ("casks", casks))})


def pipx_list(**tools_installed):
    return json.dumps({"venvs": {name: {"metadata": {"main_package": {
        "package": name, "package_version": version, "package_or_url": name}}}
        for name, version in tools_installed.items()}})


@pytest.fixture
def pypi(monkeypatch):
    """Fake PyPI: releases[name] = {version: upload time or file metadata}."""
    releases = {}

    def urlopen(url, *, timeout):
        name = url.split("/")[-2]
        return BytesIO(json.dumps({"releases": {
            version: files if isinstance(files, list) else [{"upload_time_iso_8601": files}]
            for version, files in releases.get(name, {}).items()
        }}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return releases


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
    commands.output[BREW_JSON] = brew_outdated([("team/tap/mcp-safari-server", "0.3.2", "0.3.3"),
                                                ("unrelated", "1.0", "1.1")], [("cask-mcp", "1", "1.1")])
    tools.update_mcp_programs(commands.log, Config(home=tmp_path), False)
    assert commands.read == [BREW_JSON]
    assert commands.ran == [BREW_UPDATE, ["brew", "upgrade", "--formula", "team/tap/mcp-safari-server"],
                            ["brew", "upgrade", "--cask", "cask-mcp"]]
    assert ("ran", "mcp-safari-server 0.3.2 -> 0.3.3 (Homebrew, for MCP server safari)") in commands.lines


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
def test_mcp_resolves_npm_pipx_uv_and_listed_own_updaters(tmp_path, monkeypatch, commands, pypi, dry,
                                                          custom_pipx_home):
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
    commands.output[NPM_ROOT] = str(tmp_path / "global") + "\n"
    npm_releases(commands.output, {"one": ("1.0.0", {"1.0.0": SETTLED, "1.1.0": SETTLED}),
                                   "@org/two": ("2.0.0", {"2.0.0": SETTLED, "2.0.1": SETTLED})})
    commands.output[PIPX_LIST] = pipx_list(**{"pix-package": "3.0.0"})
    commands.output[UV_LIST] = "tool-uv-package v0.4.0\n- tool-uv\n"
    pypi.update({"pix-package": {"3.0.0": SETTLED, "3.1.0": SETTLED},
                 "tool-uv-package": {"0.4.0": SETTLED, "0.5.0": SETTLED}})
    cfg = Config(home=tmp_path, program_updates={"plannotator": ["plannotator", "update"]})
    tools.update_mcp_programs(commands.log, cfg, dry)
    upgrades = [["npm", "install", "-g", "one@1.1.0"], ["npm", "install", "-g", "@org/two@2.0.1"],
                ["pipx", "install", "--force", "pix-package==3.1.0"],
                ["uv", "tool", "upgrade", "tool-uv-package==0.5.0"], ["plannotator", "update"]]
    assert not any(read[-1] == "--help" for read in commands.read)   # no program is probed with --help
    if dry:                                            # a dry run looks versions up and runs nothing
        assert commands.ran == []
        assert [message.split("  ")[0] for kind, message in commands.lines if kind == "dry"] == [
            f"would run: {' '.join(args)}" for args in upgrades]
        return
    assert commands.ran == upgrades


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

    answers = {NPM_OUTDATED: (1, '{"npm": {"current": "11.0.0", "latest": "11.1.0"}}'),
               ("npm", "view", "npm@^11", "version", "--json"): (0, '["11.0.0", "11.1.0"]'),
               ("npm", "view", "npm", "time", "--json"): (0, json.dumps({"11.1.0": SETTLED}))}

    def subprocess_run(args, **kwargs):
        code, stdout = answers[tuple(args)]
        return SimpleNamespace(returncode=code, stdout=stdout)

    monkeypatch.setattr(tools.subprocess, "run", subprocess_run)
    tools.update_prerequisites(lambda *_: None)
    assert ran == [["npm", "install", "-g", "npm@11.1.0"]]


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
        BREW_JSON: brew_outdated([("mcp-safari-server", "1", "1.1"), ("git", "2.50", "2.51"),
                                  ("node", "25.1", "25.2")]),
        NPM_ROOT: str(tmp_path / "global"),
    })
    npm_releases(commands.output, {"npm": ("11.0.0", {"11.0.0": SETTLED, "11.1.0": SETTLED}),
                                   "npm-mcp": ("1.0.0", {"1.0.0": SETTLED, "1.1.0": SETTLED})})
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
        assert upgrades == ["mcp-safari-server", "npm-mcp@1.1.0", "cursor-cli", "git", "node",
                            "npm@11.1.0", "agent-sync"]


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


NPM_ONE = ["npm", "install", "-g", "one@1.1.0"]
PIPX_PIX = ["pipx", "install", "--force", "pix==2.1.0"]
UV_TUV = ["uv", "tool", "upgrade", "tuv==0.3.0"]
BREW_WGET = ["brew", "upgrade", "--formula", "wget"]


@pytest.mark.parametrize(("setting", "dry", "expected"), [
    (False, False, []),
    (True, False, [NPM_ONE, PIPX_PIX, UV_TUV]),
    (["homebrew"], False, [BREW_UPDATE, BREW_WGET]),
    (["homebrew", "npm", "pipx", "uv"], True, []),
])
def test_global_tools_update_only_selected_managers(tmp_path, commands, pypi, setting, dry, expected):
    for manager in ("npm", "pipx", "uv", "brew"):
        program(tmp_path, manager)
    npm_releases(commands.output, {"one": ("1.0.0", {"1.0.0": SETTLED, "1.1.0": SETTLED}),
                                   "already-updated": ("1.0.0", {"1.0.0": SETTLED, "1.1.0": SETTLED})})
    commands.output.update({PIPX_LIST: pipx_list(pix="2.0.0"), UV_LIST: "tuv v0.2.0\n- tuv\n",
                            BREW_JSON: brew_outdated([("wget", "1.24", "1.25")])})
    pypi.update({"pix": {"2.0.0": SETTLED, "2.1.0": SETTLED}, "tuv": {"0.2.0": SETTLED, "0.3.0": SETTLED}})
    state = tools.UpdateState(handled={("npm", "already-updated")})
    cfg = Config(home=tmp_path, update_global_tools=setting)
    tools.update_global_tools(commands.log, cfg, dry, state=state)
    assert commands.ran == expected
    if dry:                                     # it looks versions up and says what a real run would install
        assert [message for kind, message in commands.lines if kind == "dry"] == [
            f"would run: {' '.join(args)}  ({label} global tool))" for args, label in (
                (NPM_ONE, "one 1.0.0 -> 1.1.0 (npm,"), (PIPX_PIX, "pix 2.0.0 -> 2.1.0 (pipx,"),
                (UV_TUV, "tuv 0.2.0 -> 0.3.0 (uv,"), (BREW_WGET, "wget 1.24 -> 1.25 (Homebrew,"))]


def test_global_tools_run_between_mcp_and_clis_and_reuse_manager_state(tmp_path, monkeypatch, commands):
    program(tmp_path, "npm")
    program(tmp_path, "brew", "brew/bin/brew")
    program(tmp_path, "mcp-npm", "global/mcp-npm/bin/mcp-npm")
    program(tmp_path, "mcp-brew", "brew/Cellar/mcp-brew/1/bin/mcp-brew")
    program(tmp_path, "claude")
    servers(tmp_path, npm="mcp-npm", brew="mcp-brew")
    commands.output.update({NPM_ROOT: str(tmp_path / "global"),
                            BREW_JSON: brew_outdated([("mcp-brew", "1", "1.1"), ("wget", "1.24", "1.25")])})
    npm_releases(commands.output, {"mcp-npm": ("1.0.0", {"1.0.0": SETTLED, "1.1.0": SETTLED}),
                                   "npm": ("11.0.0", {"11.0.0": SETTLED, "11.1.0": SETTLED})})
    cfg = Config(home=tmp_path, agents=["claude"], skill_packages=[], update_plugins=False, update_self=False,
                 update_global_tools=["npm", "homebrew"])
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    assert cli.run(force=True, echo=False) == 0
    assert commands.ran.count(BREW_UPDATE) == 1
    assert commands.read.count(NPM_OUTDATED) == commands.read.count(BREW_JSON) == 1
    assert [args for args in commands.ran if args[:2] in (
        ["npm", "install"], ["brew", "upgrade"], ["claude", "update"]
    )] == [
        ["npm", "install", "-g", "mcp-npm@1.1.0"], ["brew", "upgrade", "--formula", "mcp-brew"],
        ["npm", "install", "-g", "npm@11.1.0"], BREW_WGET, ["claude", "update"],
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
    commands.output[BREW_JSON] = brew_outdated([(name, "1", "1.1") for name in
                                                ("python@3.13", "java", "ruby", "bash", "off", "disabled")])
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


# ---------- the upgrade guard: same major after 2 days, a new major only after approval ----------

FRESH = datetime.now(UTC).isoformat()
WRANGLER_5 = "upgrade:npm:wrangler@5"
ASK_5 = "upgrade wrangler 4.1.0 -> 5.0.0 (a new major version, which can include breaking changes)"


def global_npm(tmp_path, commands, releases, *, keep=(), majors=False, dry=False, scoped=False):
    """One global npm tool, wrangler 4.1.0, with these releases; returns the questions the run asks."""
    program(tmp_path, "npm")
    npm_releases(commands.output, {"wrangler": ("4.1.0", releases)})
    cfg = Config(home=tmp_path, update_global_tools=["npm"], global_major_updates=majors)
    with pending.during(cfg.state_dir, list(keep), commands.log, dry, scoped=scoped) as run:
        tools.update_global_tools(commands.log, cfg, dry, state=tools.UpdateState())
    return run.questions()


@pytest.mark.parametrize(("released", "installs", "waits"), [
    (SETTLED, True, None),
    (FRESH, False, "it is under 2 days old"),
    (None, False, "its release time could not be looked up"),
])
def test_same_major_release_installs_once_it_is_two_days_old(tmp_path, commands, released, installs, waits):
    assert global_npm(tmp_path, commands, {"4.1.0": SETTLED, "4.3.0": released}) == {}
    assert commands.ran == ([["npm", "install", "-g", "wrangler@4.3.0"]] if installs else [])
    if waits:
        assert ("info", f"wrangler 4.3.0 (npm) waits: {waits}") in commands.lines


def test_new_major_is_asked_about_and_only_the_same_major_installs(tmp_path, commands):
    questions = global_npm(tmp_path, commands, {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": SETTLED})
    assert questions == {WRANGLER_5: ASK_5}
    assert commands.ran == [["npm", "install", "-g", "wrangler@4.3.0"]]
    assert ("WARN", f"approval required to {ASK_5}") in commands.lines


def test_new_major_under_two_days_old_is_not_asked_about_yet(tmp_path, commands):
    assert global_npm(tmp_path, commands, {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": FRESH}) == {}
    assert commands.ran == [["npm", "install", "-g", "wrangler@4.3.0"]]
    assert ("info", "wrangler 5.0.0 (npm) waits: it is under 2 days old") in commands.lines


def test_approved_new_major_installs_and_its_question_is_done(tmp_path, commands):
    state = Config(home=tmp_path).state_dir
    state.mkdir(parents=True)
    earlier = "upgrade wrangler 4.0.0 -> 5.0.0 (a new major version, which can include breaking changes)"
    (state / pending.FILENAME).write_text(json.dumps({WRANGLER_5: {"first": 1, "what": earlier, "approved": True}}))
    assert global_npm(tmp_path, commands, {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": SETTLED}) == {}
    assert commands.ran == [["npm", "install", "-g", "wrangler@5.0.0"]]
    assert json.loads((state / pending.FILENAME).read_text()) == {}


@pytest.mark.parametrize(("keep", "asked"), [
    (["upgrade:npm:wrangler"], {}),
    ([WRANGLER_5], {}),
    (["upgrade:npm:wrangler@4"], {WRANGLER_5: ASK_5}),
])
def test_keep_installed_keeps_the_installed_major(tmp_path, commands, keep, asked):
    releases = {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": SETTLED}
    assert global_npm(tmp_path, commands, releases, keep=keep) == asked
    assert commands.ran == [["npm", "install", "-g", "wrangler@4.3.0"]]


def test_a_later_major_is_a_new_question_after_keeping_one(tmp_path, commands):
    releases = {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": SETTLED, "6.0.0": SETTLED}
    assert list(global_npm(tmp_path, commands, releases, keep=[WRANGLER_5])) == ["upgrade:npm:wrangler@6"]


def test_the_newest_release_in_the_major_wins_over_a_later_published_backport(tmp_path, commands):
    global_npm(tmp_path, commands, {"4.1.0": SETTLED, "4.3.0": SETTLED, "4.1.2": SETTLED, "5.0.0": SETTLED})
    assert commands.ran == [["npm", "install", "-g", "wrangler@4.3.0"]]


@pytest.mark.parametrize(("releases", "left"), [
    ({"4.1.0": SETTLED}, {}),                                                   # current now
    ({"4.1.0": SETTLED, "6.0.0": SETTLED}, {"upgrade:npm:wrangler@6"}),        # a newer major replaces it
])
def test_an_earlier_major_question_ends_when_it_no_longer_applies(tmp_path, commands, releases, left):
    state = Config(home=tmp_path).state_dir
    state.mkdir(parents=True)
    (state / pending.FILENAME).write_text(json.dumps({WRANGLER_5: {"first": 1, "what": ASK_5, "approved": False}}))
    global_npm(tmp_path, commands, releases, scoped=True)
    assert set(json.loads((state / pending.FILENAME).read_text())) == set(left)


def test_an_approved_major_stays_approved_while_its_newest_release_settles(tmp_path, commands):
    state = Config(home=tmp_path).state_dir
    state.mkdir(parents=True)
    saved = {WRANGLER_5: {"first": 1, "what": ASK_5, "approved": True}}
    (state / pending.FILENAME).write_text(json.dumps(saved))
    global_npm(tmp_path, commands, {"4.1.0": SETTLED, "5.0.0": SETTLED, "5.0.1": FRESH}, scoped=True)
    assert commands.ran == []
    assert json.loads((state / pending.FILENAME).read_text()) == saved


def test_a_failed_listing_ends_no_question(tmp_path, commands):
    state = Config(home=tmp_path).state_dir
    state.mkdir(parents=True)
    saved = {WRANGLER_5: {"first": 1, "what": ASK_5, "approved": True}}
    (state / pending.FILENAME).write_text(json.dumps(saved))
    program(tmp_path, "npm")
    cfg = Config(home=tmp_path, update_global_tools=["npm"])
    with pending.during(cfg.state_dir, [], commands.log, False, scoped=True):
        tools.update_global_tools(commands.log, cfg, False, state=tools.UpdateState())
    assert json.loads((state / pending.FILENAME).read_text()) == saved


def test_global_major_updates_installs_a_new_major_without_asking(tmp_path, commands):
    assert global_npm(tmp_path, commands, {"4.1.0": SETTLED, "5.0.0": SETTLED}, majors=True) == {}
    assert commands.ran == [["npm", "install", "-g", "wrangler@5.0.0"]]
    assert not any(kind == "WARN" for kind, _ in commands.lines)


@pytest.mark.parametrize("keep", ["upgrade:npm:wrangler", WRANGLER_5])
def test_global_major_updates_respects_keep_installed(tmp_path, commands, keep):
    releases = {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": SETTLED}
    assert global_npm(tmp_path, commands, releases, keep=[keep], majors=True) == {}
    assert commands.ran == [["npm", "install", "-g", "wrangler@4.3.0"]]


def test_homebrew_upgrades_now_asks_for_a_new_major_and_never_waits(tmp_path, commands):
    program(tmp_path, "brew", "brew/bin/brew")
    commands.output[BREW_JSON] = brew_outdated(
        [("big", "1.9", "2.0"), ("small", "1.2.0", "1.2.1_1"), ("odd", "HEAD-abc", "HEAD-def")],
        [("tool", "abc", "def")])
    cfg = Config(home=tmp_path, update_global_tools=["homebrew"])
    with pending.during(cfg.state_dir, [], commands.log, False) as run:
        tools.update_global_tools(commands.log, cfg, False, state=tools.UpdateState())
    assert run.questions() == {
        "upgrade:homebrew:big@2": "upgrade big 1.9 -> 2.0 (a new major version, which can include breaking changes)"}
    assert commands.ran == [BREW_UPDATE, ["brew", "upgrade", "--formula", "odd"],
                            ["brew", "upgrade", "--formula", "small"], ["brew", "upgrade", "--cask", "tool"]]
    assert commands.read == [BREW_JSON]          # no release-time lookups for Homebrew


def test_dry_run_says_what_it_would_upgrade_and_ask_and_records_nothing(tmp_path, monkeypatch, commands, capsys):
    program(tmp_path, "npm")
    npm_releases(commands.output, {"wrangler": ("4.1.0", {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": SETTLED})})
    cfg = Config(home=tmp_path, skill_packages=[], update_plugins=False, update_clis=False, update_self=False,
                 update_mcp_programs=False, update_global_tools=["npm"])
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    assert cli.run(force=True, echo=True, dry=True) == 0
    output = capsys.readouterr().out
    assert "would run: npm install -g wrangler@4.3.0" in output
    assert f"Would ask: U{ASK_5[1:]}? [y/N]" in output
    assert not any(args[:2] == ["npm", "install"] for args in commands.ran)
    assert not (cfg.state_dir / pending.FILENAME).exists()


def test_one_run_shares_one_npm_cache_for_mcp_pins_and_tools(tmp_path, monkeypatch, commands):
    import os

    program(tmp_path, "npm")
    write(tmp_path, ".claude.json", json.dumps({"mcpServers": {"pin": {"command": "npx",
                                                                        "args": ["-y", "pinned@1.0.0"]}}}))
    npm_releases(commands.output, {"wrangler": ("4.1.0", {"4.1.0": SETTLED, "4.3.0": SETTLED, "5.0.0": SETTLED})})
    caches = []
    fake = tools.raw

    def raw(args, timeout=120, **kwargs):
        if args[:2] == ["npm", "view"]:
            caches.append((args[2], os.environ.get("npm_config_cache")))
        return fake(args, timeout, **kwargs)

    monkeypatch.setattr(tools, "raw", raw)
    cfg = Config(home=tmp_path, agents=["claude"], skill_packages=[], update_plugins=False, update_clis=False,
                 update_self=False, update_mcp_programs=False, update_global_tools=["npm"])
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    cli.run(force=True, echo=False)
    assert {package for package, _ in caches} >= {"pinned", "wrangler", "wrangler@^4"}
    assert len({cache for _, cache in caches}) == 1 and None not in {cache for _, cache in caches}


def test_uv_upgrades_the_existing_tool_at_the_guarded_version(tmp_path, commands, pypi):
    program(tmp_path, "uv")
    commands.output[UV_LIST] = "demo v1.0.0\n- demo\n"
    pypi["demo"] = {"1.1.0": SETTLED, "2.0.0": SETTLED}
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["uv"]), False,
                              state=tools.UpdateState())
    assert commands.ran == [["uv", "tool", "upgrade", "demo==1.1.0"]]


@pytest.mark.parametrize("receipt_variant", ["bare", "extras", "custom"])
def test_uv_updates_a_pinned_tool_with_its_installation_requirements(tmp_path, commands, pypi, receipt_variant):
    program(tmp_path, "uv")
    python = program(tmp_path, "python3.10")
    tool_dir = tmp_path / "uv tools"
    interpreter = tool_dir / "demo/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(python)
    commands.output[UV_DIR] = str(tool_dir)
    commands.output[UV_LIST] = "demo v1.0.0\n- demo\n"
    requirements = '{ name = "demo", specifier = "==1.0.0" }'
    if receipt_variant != "bare":
        requirements = """
    { name = "demo", extras = ["feature"], specifier = "==1.0.0" },
    { name = "plugin", extras = ["cli"], specifier = ">=5" }
"""
    receipt = f"[tool]\nrequirements = [{requirements}]\n"
    if receipt_variant == "custom":
        receipt += 'entrypoints = [{ name = "plugin", install-path = "/bin/plugin", from = "plugin" }]\n'
    write(tmp_path, "uv tools/demo/uv-receipt.toml", receipt)
    pypi["demo"] = {"1.1.0": SETTLED, "2.0.0": SETTLED}
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["uv"]), False,
                              state=tools.UpdateState())
    install = ["uv", "tool", "install", "--force", "--python", str(python)]
    if receipt_variant == "bare":
        assert commands.ran == [[*install, "demo==1.1.0"]]
    elif receipt_variant == "extras":
        assert commands.ran == [[*install, "--with=plugin[cli]>=5", "demo[feature]==1.1.0"]]
    else:
        assert commands.ran == []
        assert any("requires a manual upgrade" in message for _, message in commands.lines)


def test_pipx_keeps_extras_and_leaves_tools_from_a_url_alone(tmp_path, commands, pypi):
    program(tmp_path, "pipx")
    commands.output[PIPX_LIST] = json.dumps({"venvs": {
        "black": {"metadata": {"main_package": {"package": "black", "package_version": "24.1.0",
                                                "package_or_url": "black[d]"}}},
        "fork": {"metadata": {"main_package": {"package": "fork", "package_version": "1.0.0",
                                               "package_or_url": "git+https://example.com/fork.git"}}}}})
    pypi.update({"black": {"24.1.0": SETTLED, "24.2.0": SETTLED}, "fork": {"1.0.0": SETTLED, "1.1.0": SETTLED}})
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["pipx"]), False,
                              state=tools.UpdateState())
    assert commands.ran == [["pipx", "install", "--force", "black[d]==24.2.0"]]


def test_pipx_upgrades_the_suffixed_environment_in_place(tmp_path, commands, pypi):
    program(tmp_path, "pipx")
    commands.output[PIPX_LIST] = json.dumps({"venvs": {
        "demo": {"metadata": {"main_package": {"package": "demo", "package_version": "2.0.0",
                                             "package_or_url": "demo", "suffix": ""}}},
        "demo-test": {"metadata": {"main_package": {"package": "demo", "package_version": "1.0.0",
                                                  "package_or_url": "demo[feature]", "suffix": "-test"}}},
    }})
    pypi["demo"] = {"1.1.0": SETTLED, "2.0.0": SETTLED}
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["pipx"]), False,
                              state=tools.UpdateState())
    assert commands.ran == [["pipx", "install", "--force", "--suffix=-test", "demo[feature]==1.1.0"]]


@pytest.mark.parametrize(("manager", "expected"), [
    ("pipx", ["pipx", "install", "--force", "--python={python}", "demo==1.1.0"]),
    ("uv", ["uv", "tool", "upgrade", "demo==1.1.0"]),
])
def test_python_tool_upgrades_to_the_newest_compatible_release(tmp_path, commands, pypi, manager, expected):
    program(tmp_path, manager)
    python = str(program(tmp_path, "python3.10"))
    expected = [part.format(python=python) for part in expected]
    commands.output[PIPX_LIST] = json.dumps({"venvs": {"demo": {"metadata": {
        "python_version": "Python 3.10.9",
        "source_interpreter": {"__type__": "Path", "__Path__": python},
        "main_package": {"package": "demo", "package_version": "1.0.0", "package_or_url": "demo"},
    }}}})
    commands.output[UV_LIST] = "demo v1.0.0\n- demo\n"
    commands.output[UV_DIR] = str(tmp_path / "uv tools")
    write(tmp_path, "uv tools/demo/pyvenv.cfg", "version_info = 3.10.9.final.0\n")
    pypi["demo"] = {
        "1.1.0": [
            {"requires_python": ">=3.12", "upload_time_iso_8601": SETTLED},
            {"requires_python": "~=3.9,!=3.10.8", "upload_time_iso_8601": SETTLED},
        ],
        "1.2.0": [
            {"requires_python": ">=3.9,!=3.10.*", "upload_time_iso_8601": SETTLED},
            {"requires_python": "==3.11.*", "upload_time_iso_8601": SETTLED},
            {"requires_python": "invalid", "upload_time_iso_8601": SETTLED},
        ],
        "2.0.0": [{"requires_python": ">=3.11", "upload_time_iso_8601": SETTLED}],
    }
    cfg = Config(home=tmp_path, update_global_tools=[manager], global_major_updates=True)
    tools.update_global_tools(commands.log, cfg, False, state=tools.UpdateState())
    assert commands.ran == [expected]


def test_pypi_upgrade_waits_for_a_compatible_distribution_to_settle(tmp_path, commands, pypi):
    program(tmp_path, "uv")
    commands.output[UV_LIST] = "demo v1.0.0\n- demo\n"
    commands.output[UV_DIR] = str(tmp_path / "uv tools")
    write(tmp_path, "uv tools/demo/pyvenv.cfg", "version_info = 3.10.9.final.0\n")
    pypi["demo"] = {"1.1.0": [
        {"requires_python": ">=3.11", "upload_time_iso_8601": SETTLED},
        {"requires_python": ">=3.10", "upload_time_iso_8601": FRESH},
    ]}
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["uv"]), False,
                              state=tools.UpdateState())
    assert commands.ran == []
    assert ("info", "demo 1.1.0 (uv) waits: it is under 2 days old") in commands.lines


@pytest.mark.parametrize(("installed", "releases", "target"), [
    ("1.2.0.post1", {"1.2.0": SETTLED}, None),          # a post release is newer than its base release
    ("1.0", {"1.0.0": SETTLED}, None),                  # the same release, spelled two ways
    ("1.0.0rc1", {"1.0.0": SETTLED}, "1.0.0"),          # a final release is newer than its release candidate
])
def test_pipx_never_reinstalls_the_same_or_an_older_release(tmp_path, commands, pypi, installed, releases, target):
    program(tmp_path, "pipx")
    commands.output[PIPX_LIST] = pipx_list(tool=installed)
    pypi["tool"] = releases
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["pipx"]), False,
                              state=tools.UpdateState())
    assert commands.ran == ([["pipx", "install", "--force", f"tool=={target}"]] if target else [])


def test_a_failed_pypi_lookup_skips_the_tool_with_information_only(tmp_path, commands, pypi):
    program(tmp_path, "uv")
    commands.output[UV_LIST] = "tuv v0.2.0\n- tuv\n"
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["uv"]), False,
                              state=tools.UpdateState())
    assert commands.ran == []
    assert commands.lines == [("info", "tuv (uv) is skipped this run: its releases could not be looked up")]


def test_pypi_releases_skip_pre_releases_and_yanked_files(tmp_path, commands, pypi):
    program(tmp_path, "pipx")
    commands.output[PIPX_LIST] = pipx_list(black="1.0.0")
    pypi["black"] = {
        "1.1.0": [{"upload_time_iso_8601": FRESH}, {"upload_time_iso_8601": SETTLED}],
        "1.2.0": [{"upload_time_iso_8601": SETTLED, "yanked": True}],
        "2.0.0rc1": SETTLED,
        "1.0.1": [],
    }
    cfg = Config(home=tmp_path, update_global_tools=["pipx"], global_major_updates=True)
    tools.update_global_tools(commands.log, cfg, False, state=tools.UpdateState())
    assert commands.ran == [["pipx", "install", "--force", "black==1.1.0"]]


def test_an_unknown_interpreter_version_does_not_block_upgrades(tmp_path, commands, pypi):
    program(tmp_path, "pipx")
    commands.output[PIPX_LIST] = json.dumps({"venvs": {"demo": {"metadata": {
        "main_package": {"package": "demo", "package_version": "1.0.0", "package_or_url": "demo"},
    }}}})
    pypi["demo"] = {"1.1.0": [{"requires_python": ">=3.9", "upload_time_iso_8601": SETTLED}]}
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["pipx"]), False,
                              state=tools.UpdateState())
    assert commands.ran == [["pipx", "install", "--force", "demo==1.1.0"]]


@pytest.mark.parametrize("receipt", [
    '[tool]\nrequirements = [{ name = "demo", specifier = "<2" }]\n',
    '[tool]\nrequirements = [{ name = "demo", specifier = "==1.0.0" }]\nexcludes = ["heavy"]\n',
])
def test_uv_leaves_ranged_or_excluding_installs_to_a_manual_upgrade(tmp_path, commands, pypi, receipt):
    program(tmp_path, "uv")
    python = program(tmp_path, "python3.10")
    tool_dir = tmp_path / "uv tools"
    interpreter = tool_dir / "demo/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(python)
    commands.output[UV_DIR] = str(tool_dir)
    commands.output[UV_LIST] = "demo v1.0.0\n- demo\n"
    write(tmp_path, "uv tools/demo/uv-receipt.toml", receipt)
    pypi["demo"] = {"1.1.0": SETTLED}
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["uv"]), False,
                              state=tools.UpdateState())
    assert commands.ran == []
    assert any("requires a manual upgrade" in message for _, message in commands.lines)


def test_pipx_leaves_out_an_interpreter_that_moved(tmp_path, commands, pypi):
    program(tmp_path, "pipx")
    commands.output[PIPX_LIST] = json.dumps({"venvs": {"demo": {"metadata": {
        "python_version": "Python 3.10.9",
        "source_interpreter": {"__type__": "Path", "__Path__": str(tmp_path / "gone/python3.10")},
        "main_package": {"package": "demo", "package_version": "1.0.0", "package_or_url": "demo"},
    }}}})
    pypi["demo"] = {"1.1.0": SETTLED}
    tools.update_global_tools(commands.log, Config(home=tmp_path, update_global_tools=["pipx"]), False,
                              state=tools.UpdateState())
    assert commands.ran == [["pipx", "install", "--force", "demo==1.1.0"]]


def test_uv_does_not_ask_about_a_major_it_cannot_install(tmp_path, commands, pypi):
    program(tmp_path, "uv")
    python = program(tmp_path, "python3.10")
    tool_dir = tmp_path / "uv tools"
    interpreter = tool_dir / "demo/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(python)
    commands.output[UV_DIR] = str(tool_dir)
    commands.output[UV_LIST] = "demo v1.0.0\n- demo\n"
    write(tmp_path, "uv tools/demo/uv-receipt.toml", '[tool]\nrequirements = [{ name = "demo", specifier = "<3" }]\n')
    pypi["demo"] = {"2.0.0": SETTLED}
    cfg = Config(home=tmp_path, update_global_tools=["uv"])
    with pending.during(cfg.state_dir, [], commands.log, False) as run:
        tools.update_global_tools(commands.log, cfg, False, state=tools.UpdateState())
    assert run.questions() == {}
    assert commands.ran == []
    assert any("requires a manual upgrade" in message for _, message in commands.lines)
