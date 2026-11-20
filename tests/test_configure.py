import tomllib

from agent_sync import configure
from tests.test_skills import make_plugin

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


def test_change_several_settings_and_save(tmp_path):
    make_plugin(tmp_path, "writing@market", ["outline"])
    answers = ["1", "2", "",                          # source: Codex
               "3", "1", "",                          # plugin skills: switch writing@market on
               "8", "1", "preview-", "",              # model warnings: add preview-
               "6", "3", "gpt-x", "",                 # agent models: Codex model
               "9", "3", "3", "",                     # CLI updates: all but Cursor
               "12", "1", "", "",                     # extra steps: a step with no name is skipped
               "s", "q"]
    ed, path, said = editor(tmp_path, answers)
    ed.menu()
    data = tomllib.loads(path.read_text())
    assert data["source"] == "codex" and data["plugin_skills"] == ["writing@market"]
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
    ed, path, said = editor(tmp_path, ["2", "1", "not a package", "", "13", "2", "seven", "q"])
    ed.menu()
    assert any("owner/repo" in line for line in said) and any("whole number" in line for line in said)
    assert path.read_text() == SETTINGS


def test_commands_notes_and_steps(tmp_path):
    answers = ["10", "2", "npm install -g '@openai/codex@latest'", "",
               "7", "1", "poteto-agent", "Be brief.", "",
               "12", "1", "dotfiles", "git -C ~/dotfiles pull", "60", "",
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


def test_switching_off_the_last_missing_plugin_is_saved(tmp_path):
    ed, path, said = editor(tmp_path, ["1"], text='plugin_skills = ["gone@market"]\n' + SETTINGS)
    ed.plugin_skills()
    assert ed.cfg.plugin_skills == []


def test_extra_step_prompt_tells_the_real_time_limit(tmp_path):
    ed, path, said = editor(tmp_path, [], text=None)
    asked = []
    answers = iter(["1", "slow", "sleep 1", "", ""])
    ed.ask_line = lambda prompt: asked.append(prompt) or next(answers)
    ed.steps()
    assert any("900" in p for p in asked) and not any("no limit" in p for p in asked)
