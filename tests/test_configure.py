import tomllib

from agent_sync import configure

SETTINGS = """# My settings, with a comment to keep.
source = "claude"   # Claude Code leads
warn_models = ["gemini-"]

[agent_models]
claude = "opus"
"""


def editor(home, answers, text=SETTINGS):
    path = home / ".config/agent-sync/config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text)
    said = []
    answers = iter(answers)
    ed = configure.Editor(home, ask=lambda _: next(answers), say=said.append)
    return ed, path, said


def test_desktop_notifications_default_on_and_can_be_disabled(tmp_path):
    keys = [key for key, _ in configure.PARTS]
    position = keys.index("notify") + 1 + 2 * (keys.index("notify") > keys.index("mirror_memory"))
    ed, path, _ = editor(tmp_path, ["3", str(position), "", "s", "q"])
    assert ed.cfg.notify is True
    ed.menu()
    assert tomllib.loads(path.read_text())["notify"] is False


def test_change_several_settings_and_save(tmp_path):
    answers = ["1", "2", "",                          # source: Codex
               "7", "1", "preview-", "",              # model warnings: add preview-
               "5", "3", "gpt-x", "",                 # agent models: Codex model
               "8", "3", "3", "",                     # CLI updates: all but Cursor
               "11", "1", "", "",                     # extra steps: a step with no name is skipped
               "s", "q"]
    ed, path, said = editor(tmp_path, answers)
    ed.menu()
    data = tomllib.loads(path.read_text())
    assert data["source"] == "codex"
    assert data["warn_models"] == ["gemini-", "preview-"]
    assert data["agent_models"] == {"claude": "opus", "codex": "gpt-x"}
    assert data["update_clis"] == ["claude", "codex"]
    assert "# My settings, with a comment to keep." in path.read_text()
    assert "# Claude Code leads" in path.read_text()
    assert (path.parent / "config.toml.bak").read_text() == SETTINGS


def test_quit_without_saving_changes_nothing(tmp_path):
    ed, path, said = editor(tmp_path, ["1", "3", "", "q", "n"])
    ed.menu()
    assert path.read_text() == SETTINGS and "  Nothing was saved." in said


def test_a_bad_value_is_refused_and_explained(tmp_path):
    ed, path, said = editor(tmp_path, ["2", "1", "not a package", "", "12", "2", "seven", "q"])
    ed.menu()
    assert any("owner/repo" in line for line in said) and any("whole number" in line for line in said)
    assert path.read_text() == SETTINGS


def test_commands_notes_and_steps(tmp_path):
    answers = ["9", "2", "npm install -g '@openai/codex@latest'", "",
               "6", "1", "poteto-agent", "Be brief.", "",
               "11", "1", "dotfiles", "git -C ~/dotfiles pull", "60", "",
               "s", "q"]
    ed, path, said = editor(tmp_path, answers, text=None)
    ed.menu()
    data = tomllib.loads(path.read_text())
    assert data["cli_commands"] == {"codex": ["npm", "install", "-g", "@openai/codex@latest"]}
    assert data["agent_notes"] == {"poteto-agent": "Be brief."}
    assert data["extra_steps"] == [{"name": "dotfiles", "command": ["git", "-C", f"{tmp_path}/dotfiles", "pull"],
                                    "timeout": 60}]


def test_pstack_roles_are_edited_in_their_own_file(tmp_path):
    roles = tmp_path / ".claude/rules/pstack-models.md"
    roles.parent.mkdir(parents=True)
    text = "# pstack model configuration.\nfeature, refactoring: gpt-x\nbug-fix: gpt-x\n\n# How to run\nnotes\n"
    roles.write_text(text)
    ed, path, said = editor(tmp_path, ["r", "2", "opus", "", "s", "q"])
    ed.menu()
    assert roles.read_text() == text.replace("bug-fix: gpt-x", "bug-fix: opus")
    assert path.read_text() == SETTINGS


def test_a_command_with_tilde_gets_the_real_home_folder(tmp_path):
    ed, path, said = editor(tmp_path, ["git -C ~/dotfiles pull --ff-only"])
    command = ed.ask_command("Command", "git -C ~/dotfiles pull --ff-only")
    assert command == ["git", "-C", f"{tmp_path}/dotfiles", "pull", "--ff-only"]


def test_changing_the_source_in_the_editor_moves_the_roles_file(tmp_path, monkeypatch):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents/pstack-models.md").write_text("bug-fix: gpt-x\n")
    def edit(path, say=print):
        path.write_text(path.read_text().replace('source = "claude"', 'source = "codex"'))
    monkeypatch.setattr(configure, "open_in_editor", edit)
    ed, path, said = editor(tmp_path, [])
    ed.settings_file()
    assert ed.roles_path == tmp_path / ".agents/pstack-models.md"
    assert ed.roles_text == "bug-fix: gpt-x\n"


def test_extra_step_prompt_tells_the_real_time_limit(tmp_path):
    ed, path, said = editor(tmp_path, [], text=None)
    asked = []
    answers = iter(["1", "slow", "sleep 1", "", ""])
    ed.ask_line = lambda prompt: asked.append(prompt) or next(answers)
    ed.steps()
    assert any("900" in p for p in asked) and not any("no limit" in p for p in asked)


def test_agents_menu_only_offers_enabled_sources(tmp_path, monkeypatch):
    from agent_sync import tools

    monkeypatch.setattr(tools, "PATH", str(tmp_path / "bin"))
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini/settings.json").write_text("{}")
    answers = ["14", "1", "4", "2", "3", "",        # refuse to remove source, add Gemini, remove Codex and Cursor
               "1", "2",                            # Gemini is now source
               "14", "1", "", "s", "q"]              # remove Claude, save
    ed, path, said = editor(tmp_path, answers)
    ed.menu()
    data = tomllib.loads(path.read_text())
    assert data["source"] == "gemini"
    assert data["agents"] == ["gemini"]
    assert any("Change source before removing it" in line for line in said)
    assert any("Gemini CLI (installed)" in line for line in said)
    first = said.index(next(line for line in said if "Now: Claude Code" in line))
    assert "Codex" not in "\n".join(said[first:first + 4])


def test_keep_mcp_versions_can_be_edited_and_reloaded(tmp_path):
    ed, path, _ = editor(tmp_path, ["15", "1", "rea", "", "s", "q"])
    assert ed.cfg.keep_mcp_versions == []
    ed.menu()
    assert ed.cfg.keep_mcp_versions == ["rea"]
    assert tomllib.loads(path.read_text())["keep_mcp_versions"] == ["rea"]


def test_shared_memory_shows_three_states(tmp_path):
    ed, path, said = editor(tmp_path, ["3", "5", "1", "5", "2", "", "s", "q"])
    assert ed.cfg.mirror_memory is None
    ed.menu()
    for state in ("not asked yet", "on", "off"):
        assert any(f"Share Claude Code's home-project memory with the other tools ({state})" in line for line in said)
    assert ed.cfg.mirror_memory is False
    assert tomllib.loads(path.read_text())["mirror_memory"] is False


def test_memory_lists_can_be_edited_and_reloaded(tmp_path):
    answers = ["3", "6", "1", "seller", "", "7", "1", "shared-safely", "", "", "s", "q"]
    ed, path, _ = editor(tmp_path, answers)
    assert ed.cfg.private_memory == ed.cfg.share_memory == []
    ed.menu()
    saved = tomllib.loads(path.read_text())
    assert ed.cfg.private_memory == saved["private_memory"] == ["seller"]
    assert ed.cfg.share_memory == saved["share_memory"] == ["shared-safely"]


def test_upkeep_safety_and_global_tools_settings_can_be_saved(tmp_path):
    answers = ["12", "3", "45", "16", "1", "mcp:codex:extract", "",
               "17", "3", "4", "", "s", "q"]
    ed, path, _ = editor(tmp_path, answers)
    assert ed.cfg.backup_days == 30
    assert ed.cfg.keep_installed == []
    assert ed.cfg.update_global_tools is False
    ed.menu()
    data = tomllib.loads(path.read_text())
    assert data["backup_days"] == 45
    assert data["keep_installed"] == ["mcp:codex:extract"]
    assert data["update_global_tools"] == ["homebrew"]
