import re

import pytest

from agent_sync import backups, instructions, memory, pending
from agent_sync.config import config_path, load
from agent_sync.registry import TOOLS
from tests.test_notify import available, dialogs, inbox, tty
from tests.test_pending import approve_current

INDEX = "- [Workspace preferences](workspace.md) — habits to use across projects\n"


def setup(home):
    source = home / ".claude/projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(home)) / "memory"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text(INDEX)
    (source / "workspace.md").write_text("Prefer local worktrees.\n")
    files = instructions.files(home, TOOLS)
    files["claude"].parent.mkdir(exist_ok=True)
    files["claude"].write_text("Keep replies brief.\n")
    return source, files, home / ".agents/memory"


def test_dry_run_and_copy_index_for_each_distinct_enabled_tool(tmp_path):
    source, files, dest = setup(tmp_path)
    paths = [files[tool] for tool in ("codex", "cursor", "gemini")]
    expected = ["update memory copy (2 files)", *(f"update memory index in {path}" for path in paths)]
    assert memory.mirror(tmp_path, True, enabled=TOOLS) == ["would " + line for line in expected]
    assert not dest.exists()
    assert all(not path.exists() for path in paths)

    assert memory.mirror(tmp_path, False, enabled=TOOLS) == [
        "updated memory copy (2 files)", *(f"updated memory index in {path}" for path in paths)]
    assert (dest / ".agent-sync").is_file()
    assert (dest / "MEMORY.md").read_bytes() == (source / "MEMORY.md").read_bytes()
    assert (dest / "workspace.md").read_text() == "Prefer local worktrees.\n"
    assert files["claude"].read_text() == "Keep replies brief.\n"
    for path in paths:
        section = path.read_text()
        assert "## Memory (kept by Claude Code)\n" in section
        assert ("Read the matching file in ~/.agents/memory/ when an entry applies. "
                "These are facts and preferences about the user and their projects.\n") in section
        assert INDEX.replace("(workspace.md)", "(~/.agents/memory/workspace.md)") in section
    assert files["cursor"].read_text().startswith("---\ndescription:")
    assert memory.mirror(tmp_path, False, enabled=TOOLS) == []


def test_an_unowned_folder_is_left_alone(tmp_path):
    _, files, dest = setup(tmp_path)
    dest.mkdir(parents=True)
    (dest / "mine.md").write_text("Keep this.\n")

    for dry in (True, False):
        warning, = memory.mirror(tmp_path, dry, enabled=TOOLS)
        assert warning.startswith("WARN") and str(dest) in warning
        assert sorted(path.name for path in dest.iterdir()) == ["mine.md"]
        assert (dest / "mine.md").read_text() == "Keep this.\n"
        assert not files["codex"].exists()


def test_refresh_removes_stale_copies_and_replaces_the_index_in_place(tmp_path):
    source, files, dest = setup(tmp_path)
    memory.mirror(tmp_path, False, enabled=TOOLS)
    (source / "workspace.md").unlink()
    (source / "new.md").write_text("Another preference.\n")
    (source / "MEMORY.md").write_text("- [New](new.md) — another preference\n")
    codex = files["codex"]
    codex.write_text("Before.\n\n" + codex.read_text() + "\nAfter.\n")

    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False):
        assert memory.mirror(tmp_path, False, enabled=TOOLS)[0] == "updated memory copy (2 files)"
    assert (dest / "workspace.md").exists()
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        assert memory.mirror(tmp_path, False, enabled=TOOLS) == ["updated memory copy (2 files)"]
    assert not (dest / "workspace.md").exists()
    assert (dest / "new.md").read_text() == "Another preference.\n"
    text = codex.read_text()
    assert text.count("<!-- agent-sync: memory -->") == text.count("<!-- agent-sync: end memory -->") == 1
    assert "[New](~/.agents/memory/new.md) — another preference" in text
    assert "workspace.md" not in text and text.startswith("Before.\n\n") and text.endswith("\nAfter.\n")
    assert memory.mirror(tmp_path, False, enabled=TOOLS) == []


def test_memory_is_local_when_instructions_sync_runs_after_it(tmp_path):
    _, files, _ = setup(tmp_path)
    baselines = tmp_path / "state/baselines.json"
    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    memory.mirror(tmp_path, False, enabled=TOOLS)
    codex = files["codex"]
    codex.write_text("Run pytest.\n" + codex.read_text())

    instructions.mirror(tmp_path, "claude", baselines=baselines, enabled=TOOLS)
    assert files["claude"].read_text() == "Keep replies brief.\nRun pytest.\n"
    assert codex.read_text().count("<!-- agent-sync: memory -->") == 1
    assert memory.mirror(tmp_path, False, enabled=TOOLS) == []


def test_claude_home_project_memory_is_used_when_claude_is_disabled(tmp_path):
    _, files, dest = setup(tmp_path)
    memory.mirror(tmp_path, False, enabled=("codex",))
    assert (dest / "workspace.md").is_file()
    assert "kept by Claude Code" in files["codex"].read_text()


def test_without_claude_home_project_memory_nothing_is_shared(tmp_path):
    other_home = tmp_path / "another-home"
    assert memory.mirror(other_home, False, enabled=TOOLS) == []
    assert not other_home.exists()


def test_private_notes_are_removed_from_the_copy_and_indexes(tmp_path):
    source, files, dest = setup(tmp_path)
    notes = {
        "marked": "\ufeff---\nprivate: true\n---\nPersonal note.\n",
        "nested": "---\nmetadata:\n  type: user\n  private: true # keep local\n---\nPersonal note.\n",
        "inline": "---\nmetadata: {type: user, private: true}\n---\nPersonal note.\n",
        "configured": '---\nname: "Display name"\n---\nPersonal note.\n',
        "sensitive": "File the application under my legal name.\n",
        "shared": "Instructions for finding the keychain item.\n",
        "secret": "Ask about this account.\n",
    }
    for name, body in notes.items():
        (source / f"{name}.md").write_text(body)
    secret = "ghp_" + "A1" * 18
    index = INDEX + "".join(f"- [{name}](./{name}.md) index detail\n" for name in notes)
    (source / "MEMORY.md").write_text(index)
    memory.mirror(tmp_path, False, enabled=TOOLS)
    (source / "secret.md").write_text(secret + "\n")
    settings = {"enabled": TOOLS, "private_memory": ["Display name"],
                "share_memory": ["shared", "secret", "configured", "marked"]}
    with backups.during(tmp_path, tmp_path / "backups") as run:
        messages = memory.mirror(tmp_path, False, **settings)
        assert (run.folder / ".agents/memory/secret.md").read_text() == notes["secret"]

    info, warning, *_ = messages
    assert info.startswith("kept 6 memory notes private to Claude Code")
    for detail in ("marked (marked private)", "nested (marked private)", "inline (marked private)",
                   "Display name (listed in private_memory)", "sensitive (mentions a legal name)",
                   "secret (contains a secret (GitHub token))"):
        assert detail in info
    assert warning == ("WARN  memory note secret contains what looks like a secret (GitHub token); "
                       "it stays in Claude Code only. Consider moving the secret out of memory.")
    assert secret not in "\n".join(messages)
    assert set(path.name for path in dest.glob("*.md")) == {"MEMORY.md", "shared.md", "workspace.md"}
    assert (source / "MEMORY.md").read_text() == index
    assert (source / "secret.md").read_text() == secret + "\n"
    for path in (dest / "MEMORY.md", files["codex"], files["cursor"], files["gemini"]):
        text = path.read_text()
        assert "shared.md" in text and "workspace.md" in text
        assert all(f"{name}.md" not in text for name in notes if name != "shared")
    assert "(~/.agents/memory/shared.md)" in files["codex"].read_text()
    assert memory.mirror(tmp_path, False, **settings) == [info, warning]


def test_cli_upkeep_shares_instructions_then_memory(tmp_path, monkeypatch, capsys):
    from agent_sync import cli
    from agent_sync.config import config_path

    source, files, dest = setup(tmp_path)
    (source / "private.md").write_text("Personal note.\n")
    (source / "approved.md").write_text("Find the keychain item in Settings.\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test version"))
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("skill_packages = []\nmirror_instructions = true\nmirror_memory = true\n"
                      'private_memory = ["private"]\nshare_memory = ["approved"]\n'
                      "mirror_mcp = false\nmirror_roles = false\nmirror_skills = false\n"
                      "update_plugins = false\nupdate_clis = false\nupdate_self = false\n")
    assert cli.main(["run", "--force", "--echo"]) == 0
    output = capsys.readouterr().out
    assert output.index("global instructions mirrored") < output.index("updated memory copy")
    assert (dest / "workspace.md").is_file()
    assert (dest / "approved.md").is_file() and not (dest / "private.md").exists()
    codex = files["codex"].read_text()
    assert codex.startswith("Keep replies brief.\n\n<!-- agent-sync: memory -->\n")
    assert "[Workspace preferences](~/.agents/memory/workspace.md)" in codex


def test_only_copies_agent_sync_wrote_are_replaced_or_removed(tmp_path):
    source, files, dest = setup(tmp_path)
    (source / "gone.md").write_text("Old note.\n")
    memory.mirror(tmp_path, False, enabled=TOOLS)
    (dest / "mine.md").write_text("The user's own note.\n")          # never listed in the marker
    (dest / "gone.md").write_text("Edited copy.\n")
    (source / "gone.md").unlink()
    (source / "mine.md").write_text("A new note of the same name.\n")
    state = tmp_path / "state"
    with pending.during(state, [], lambda *_: None, False):
        lines = memory.mirror(tmp_path, False, enabled=TOOLS)
    assert f"WARN  {dest / 'mine.md'} is not a copy agent-sync made; left alone" in lines
    assert (dest / "mine.md").read_text() == "The user's own note.\n"
    assert "you edited this copy" in pending.load(state)["memory:gone.md"]["what"]
    approve_current(state)
    with pending.during(state, [], lambda *_: None, False):
        memory.mirror(tmp_path, False, enabled=TOOLS)
    assert not (dest / "gone.md").exists() and (dest / "mine.md").exists()


def test_an_empty_memory_index_removes_the_memory_section(tmp_path):
    source, files, _ = setup(tmp_path)
    memory.mirror(tmp_path, False, enabled=("claude", "codex"))
    assert "<!-- agent-sync: memory -->" in files["codex"].read_text()
    (source / "MEMORY.md").write_text("\n")
    memory.mirror(tmp_path, False, enabled=("claude", "codex"))
    assert "agent-sync: memory" not in files["codex"].read_text()
    (source / "MEMORY.md").unlink()
    assert memory.mirror(tmp_path, False, enabled=("claude", "codex")) == []


def test_a_codex_file_linked_to_claude_md_gets_no_memory_section(tmp_path):
    _, files, _ = setup(tmp_path)
    files["codex"].parent.mkdir(parents=True, exist_ok=True)
    files["codex"].symlink_to(files["claude"])
    memory.mirror(tmp_path, False, enabled=("claude", "codex"))
    assert files["claude"].read_text() == "Keep replies brief.\n"


def test_the_project_folder_name_replaces_every_non_ascii_character(tmp_path):
    home = tmp_path / "café user"
    home.mkdir()
    source = home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(home)) / "memory"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text(INDEX)
    (source / "workspace.md").write_text("Prefer local worktrees.\n")
    memory.mirror(home, False, enabled=("claude", "codex"))
    assert (home / ".agents/memory/workspace.md").exists()


@pytest.fixture
def memory_upkeep(tmp_path, monkeypatch):
    from agent_sync import cli

    _, _, dest = setup(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test"))
    monkeypatch.setattr(cli.membership, "available", lambda _: {})
    config = config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(
        'agents = ["claude", "codex"]\nskill_packages = []\nmirror_roles = false\n'
        "mirror_skills = false\nmirror_mcp = false\nstatus_line = false\nupdate_plugins = false\n"
        "update_clis = false\nupdate_mcp_programs = false\nupdate_self = false\n"
    )
    return cli.main, config, dest


@pytest.mark.parametrize(("reply", "shared"), [("y", True), ("", False)])
def test_terminal_memory_choice_is_saved_once(memory_upkeep, monkeypatch, reply, shared):
    main, config, dest = memory_upkeep
    before = config.read_bytes()
    prompts = tty(monkeypatch, [reply])
    assert main(["run", "--force"]) == 0
    assert load().mirror_memory is shared
    assert dest.exists() is shared
    assert main(["run", "--force"]) == 0
    assert prompts == ["Share Claude Code's memory notes with Codex? "
                       "Notes about credentials, keys and personal details stay in Claude Code. [y/N] "]
    root = load().state_dir / "backups"
    assert [path.read_bytes() for path in root.glob("*/.config/agent-sync/config.toml")] == [before]


def test_dry_run_does_not_ask_and_eof_asks_again(memory_upkeep, monkeypatch, capsys):
    main, _, dest = memory_upkeep
    prompts = tty(monkeypatch, [EOFError(), "no"])
    assert main(["run", "--dry-run"]) == 0
    assert "Would ask: Share Claude Code's memory notes with Codex?" in capsys.readouterr().out
    assert not prompts and load().mirror_memory is None
    assert main(["run", "--force", "--echo"]) == 0
    assert load().mirror_memory is None and not dest.exists()
    assert capsys.readouterr().out.count("Memory sharing is waiting for your choice.") == 1
    assert main(["run", "--force"]) == 0
    assert len(prompts) == 2 and load().mirror_memory is False


@pytest.mark.parametrize("shared", [True, False])
def test_inbox_buttons_save_memory_choice(memory_upkeep, monkeypatch, shared):
    from agent_sync import notify

    main, _, dest = memory_upkeep
    monkeypatch.setattr(notify, "in_terminal", lambda: False)
    available(monkeypatch, "plannotator")
    processes = inbox(monkeypatch, {"result": {}}, {"result": {}})
    assert main(["run", "--force"]) == 0
    assert main(["run", "--force"]) == 0
    assert load().mirror_memory is None and not dest.exists()
    assert len(processes) == 2
    body = processes[0].messages[-1]["params"]["arguments"]["body"]
    assert ":::run\nShare" in body and ":::run\nDon't share" in body
    verb = "--approve" if shared else "--keep"
    command = next(line.split() for line in body.splitlines() if line.startswith(f"agent-sync pending {verb}"))
    assert main(command[1:]) == 0
    assert load().mirror_memory is shared and not dest.exists()
    assert main(["run", "--force"]) == 0
    assert dest.exists() is shared and len(processes) == 2


def test_macos_memory_window_retries_after_dismissal(memory_upkeep, monkeypatch):
    from agent_sync import notify

    main, _, dest = memory_upkeep
    monkeypatch.setattr(notify, "in_terminal", lambda: False)
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    available(monkeypatch, "osascript")
    calls = dialogs(monkeypatch, (1, ""), (0, "Don't share\n"))
    assert main(["run", "--force"]) == 0
    assert load().mirror_memory is None and not dest.exists()
    assert main(["run", "--force"]) == 0
    assert load().mirror_memory is False and not dest.exists()
    assert main(["run", "--force"]) == 0
    assert len(calls) == 2
    assert all('buttons {"Don\'t share", "Share"}' in args[2] for args, _ in calls)


@pytest.mark.parametrize("marker", ["", "[]", '{"files":{"workspace.md":"bad"}}'])
def test_malformed_manifest_owns_no_notes_even_if_bytes_match(tmp_path, marker):
    import json

    source, _, dest = setup(tmp_path)
    dest.mkdir(parents=True)
    (dest / memory.MARKER).write_text(marker)
    note = dest / "workspace.md"
    note.write_bytes((source / note.name).read_bytes())
    lines = memory.mirror(tmp_path, False, enabled=TOOLS)
    assert any(line.startswith("WARN") and "manifest" in line for line in lines)
    assert note.name not in json.loads((dest / memory.MARKER).read_text())["files"]
    (source / note.name).write_text("Updated source\n")
    memory.mirror(tmp_path, False, enabled=TOOLS)
    assert note.read_text() == "Prefer local worktrees.\n"


def test_a_byte_identical_note_in_a_valid_manifest_folder_is_never_claimed(tmp_path):
    import json

    source, _, dest = setup(tmp_path)
    dest.mkdir(parents=True)
    (dest / memory.MARKER).write_text('{"files":{}}')
    note = dest / "workspace.md"
    note.write_bytes((source / note.name).read_bytes())
    memory.mirror(tmp_path, False, enabled=TOOLS)
    assert note.name not in json.loads((dest / memory.MARKER).read_text())["files"]


def test_removing_claudes_memory_folder_clears_and_backs_up_the_managed_index(tmp_path):
    source, files, _ = setup(tmp_path)
    codex = files["codex"]
    codex.parent.mkdir()
    codex.write_text("My own instructions.\n\n")
    memory.mirror(tmp_path, False, enabled=TOOLS)
    before = codex.read_bytes()
    for note in source.iterdir():
        note.unlink()
    source.rmdir()

    with backups.during(tmp_path, tmp_path / "backups") as run:
        memory.mirror(tmp_path, False, enabled=TOOLS)
    assert codex.read_text() == "My own instructions.\n\n"
    assert (run.folder / ".codex/AGENTS.md").read_bytes() == before


@pytest.mark.parametrize("removed_at_source", [False, True])
def test_managed_memory_symlink_is_left_alone_during_updates_and_removals(tmp_path, removed_at_source):
    source, _, dest = setup(tmp_path)
    memory.mirror(tmp_path, False, enabled=TOOLS)
    note = dest / "workspace.md"
    unrelated = tmp_path / "outside.md"
    unrelated.write_bytes(note.read_bytes())
    state = tmp_path / "state"
    if removed_at_source:
        (source / note.name).unlink()
        with pending.during(state, [], lambda *_: None, False):
            memory.mirror(tmp_path, False, enabled=TOOLS)
        approve_current(state)
    else:
        (source / note.name).write_text("New source\n")
    note.unlink()
    note.symlink_to(unrelated)
    with pending.during(state, [], lambda *_: None, False):
        lines = memory.mirror(tmp_path, False, enabled=TOOLS)
    assert note.is_symlink() and unrelated.read_text() == "Prefer local worktrees.\n"
    assert any(line.startswith("WARN") and "symlink" in line for line in lines)


@pytest.mark.parametrize("when", ["before_read", "during_stage"])
def test_memory_preserves_an_edit_arriving_before_each_write_attempt(tmp_path, monkeypatch, when):
    from agent_sync import writes

    source, _, dest = setup(tmp_path)
    memory.mirror(tmp_path, False, enabled=TOOLS)
    note = dest / "workspace.md"
    (source / note.name).write_text("New source\n")
    update, stage = writes.update, writes.stage

    def edit_before_read(path, *args, **kwargs):
        if path == note:
            note.write_text("Concurrent edit\n")
        return update(path, *args, **kwargs)

    def edit_during_stage(path, *args):
        if path == note.resolve():
            note.write_text("Concurrent edit\n")
        return stage(path, *args)

    if when == "before_read":
        monkeypatch.setattr(writes, "update", edit_before_read)
    else:
        monkeypatch.setattr(writes, "stage", edit_during_stage)
    lines = memory.mirror(tmp_path, False, enabled=TOOLS)
    assert note.read_text() == "Concurrent edit\n"
    assert any(line.startswith("WARN") and "changed while" in line for line in lines)
