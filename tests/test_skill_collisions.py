"""A package install never replaces a skill that package did not install, until you approve it."""
import json
import os

from agent_sync import cli, pending, skills, tools
from agent_sync.config import Config
from agent_sync.log import Log
from tests.test_pending import approve_current
from tests.test_skills import make_skill

PACKAGE = "org/pack"


def installer(home, commands, also=None):
    """Acts like `skills add`: writes each skill to ~/.agents/skills, links it from ~/.claude/skills, and records it
    in the lock file, whatever was there before."""
    def sh(args, timeout):
        commands.append(args)
        selected = args[args.index("-s") + 1:args.index("-a")]
        names = ["notes", "tips"] if selected == ["*"] else selected
        lock_path = home / ".agents/.skill-lock.json"
        lock = json.loads(lock_path.read_text()) if lock_path.exists() else {"version": 3, "skills": {}}
        for name in names:
            canonical = home / ".agents/skills" / name
            canonical.mkdir(parents=True, exist_ok=True)
            (canonical / "SKILL.md").write_text(f"---\nname: {name}\ndescription: from {PACKAGE}\n---\n")
            link = home / ".claude/skills" / name
            if link.is_symlink() or link.is_file():
                link.unlink()
            elif link.is_dir():
                import shutil
                shutil.rmtree(link)
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(f"../../.agents/skills/{name}")
            lock["skills"][name] = {"source": PACKAGE, "skillPath": f"skills/{name}/SKILL.md"}
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock, indent=2))
        if also:
            also()
        return 0, ""
    return sh


def setup(tmp_path, monkeypatch, also=None):
    cfg = Config(home=tmp_path, skill_packages=[PACKAGE])
    make_skill(tmp_path / ".claude/skills", "notes", "My own notes skill.")

    def checkout(package, folder):
        if not folder.exists():
            make_skill(folder / "skills", "notes")
            make_skill(folder / "skills", "tips")
        return 0, ""

    commands = []
    monkeypatch.setattr(tools, "checkout", checkout)
    monkeypatch.setattr(tools, "sh", installer(tmp_path, commands, also))
    return cfg, commands


def run(cfg, log):
    with pending.during(cfg.state_dir, [], log, False):
        cli.sync_packages(cfg, log, cfg.state_dir, False)
    return pending.load(cfg.state_dir)


def own_notes(home):
    folder = home / ".claude/skills/notes"
    return not folder.is_symlink() and "My own notes skill." in (folder / "SKILL.md").read_text()


def test_an_install_that_replaces_your_skill_is_put_back_and_asked_about(tmp_path, monkeypatch):
    cfg, commands = setup(tmp_path, monkeypatch)
    log = Log(tmp_path / "log", write=False)
    make_skill(cfg.state_dir / "skill-guard.killed", "notes")        # left by a run that was killed

    asked = run(cfg, log)
    assert own_notes(tmp_path)
    assert not (tmp_path / ".agents/skills/notes").exists()          # nothing of that name was there before
    assert (tmp_path / ".agents/skills/tips/SKILL.md").is_file()       # the rest of the package installed
    assert set(skills.locked(tmp_path)) == {"tips"}                     # the lock no longer credits it to the package
    assert list(asked) == [f"skill:notes@{PACKAGE}"]
    assert asked[f"skill:notes@{PACKAGE}"]["what"] == f"replace skill notes with the one from {PACKAGE}"
    assert not list(cfg.state_dir.glob("skill-guard.*"))

    # Until you answer, later runs leave that name out, so nothing is replaced and put back again.
    assert list(run(cfg, log)) == [f"skill:notes@{PACKAGE}"]
    assert commands[-1] == tools.skill_args(cfg.skills_cli, PACKAGE, ["tips"], enabled=cfg.agents)
    assert own_notes(tmp_path)

    approve_current(cfg.state_dir)
    assert run(cfg, log) == {}
    assert commands[-1] == tools.skill_args(cfg.skills_cli, PACKAGE, None, enabled=cfg.agents)
    assert (tmp_path / ".claude/skills/notes").is_symlink()
    assert set(skills.locked(tmp_path)) == {"notes", "tips"}


def test_changes_the_install_does_not_claim_are_left_alone(tmp_path, monkeypatch):
    """A skill you edit while the installer runs keeps your edit: only folders the lock now credits to the package
    are put back."""
    mine = tmp_path / ".claude/skills/draft"

    def edit():
        (mine / "SKILL.md").write_text("---\nname: draft\ndescription: edited during the install\n---\n")

    cfg, _ = setup(tmp_path, monkeypatch, also=edit)
    make_skill(tmp_path / ".claude/skills", "draft", "First version.")
    run(cfg, Log(tmp_path / "log", write=False))
    assert "edited during the install" in (mine / "SKILL.md").read_text()


def test_skills_the_package_installed_before_update_without_a_question(tmp_path, monkeypatch):
    cfg, commands = setup(tmp_path, monkeypatch)
    log = Log(tmp_path / "log", write=False)
    run(cfg, log)
    approve_current(cfg.state_dir)
    run(cfg, log)                              # the package now owns notes

    # A new package version: its own folders change, and that is no collision.
    os.utime(tmp_path / ".agents/skills/notes/SKILL.md", (0, 0))
    (tmp_path / ".agents/skills/notes/SKILL.md").write_text("old version\n")
    assert run(cfg, log) == {}
    assert "from org/pack" in (tmp_path / ".agents/skills/notes/SKILL.md").read_text()


def test_a_skill_the_package_owns_elsewhere_is_still_protected_where_you_wrote_your_own(tmp_path, monkeypatch):
    """The package installed notes for Codex and Cursor; Claude Code, enabled later, has a notes skill of yours."""
    cfg, commands = setup(tmp_path, monkeypatch)
    make_skill(tmp_path / ".agents/skills", "notes", "from org/pack")
    lock = {"version": 3, "skills": {"notes": {"source": PACKAGE, "skillPath": "skills/notes/SKILL.md"}}}
    (tmp_path / ".agents/.skill-lock.json").write_text(json.dumps(lock))

    asked = run(cfg, Log(tmp_path / "log", write=False))
    assert own_notes(tmp_path)
    assert list(asked) == [f"skill:notes@{PACKAGE}"]
    assert skills.locked(tmp_path)["notes"]["source"] == PACKAGE      # the package still owns its own install


def test_keep_stops_the_install_from_trying_again(tmp_path, monkeypatch):
    cfg, commands = setup(tmp_path, monkeypatch)
    cfg.keep_installed = [f"skill:notes@{PACKAGE}"]
    log = Log(tmp_path / "log", write=False)
    with pending.during(cfg.state_dir, cfg.keep_installed, log, False):
        cli.sync_packages(cfg, log, cfg.state_dir, False)
    assert commands == [tools.skill_args(cfg.skills_cli, PACKAGE, ["tips"], enabled=cfg.agents)]
    assert own_notes(tmp_path)


def test_names_match_without_regard_to_case_where_folders_do(tmp_path, monkeypatch):
    probe = tmp_path / "Probe"
    probe.mkdir()
    if not (tmp_path / "probe").exists():
        import pytest
        pytest.skip("this file system tells Notes and notes apart")
    cfg, _ = setup(tmp_path, monkeypatch)
    (tmp_path / ".claude/skills/notes").rename(tmp_path / ".claude/skills/Notes")
    run(cfg, Log(tmp_path / "log", write=False))
    names = [entry.name for entry in (tmp_path / ".claude/skills").iterdir()]
    assert [name for name in names if name.lower() == "notes"] == ["Notes"]
    assert "My own notes skill." in (tmp_path / ".claude/skills/Notes/SKILL.md").read_text()


def test_a_failed_install_puts_back_what_it_changed_even_without_a_lock_entry(tmp_path):
    """The installer writes files before its lock file; one that fails in between has replaced your skill but
    recorded nothing."""
    make_skill(tmp_path / ".claude/skills", "notes", "My own notes skill.")
    with skills.guard_installs(tmp_path, ("claude",), {PACKAGE}, set(), tmp_path / "state") as guard:
        (tmp_path / ".claude/skills/notes/SKILL.md").write_text("half-written package copy\n")
        guard.failed = True
    assert own_notes(tmp_path)
    assert guard.restored == [("notes", PACKAGE)]


def test_an_earlier_question_holds_a_name_in_any_case(tmp_path, monkeypatch):
    cfg, commands = setup(tmp_path, monkeypatch)
    log = Log(tmp_path / "log", write=False)
    with pending.during(cfg.state_dir, [], log, False):
        pending.due(f"skill:Notes@{PACKAGE}", cli.replace_skill("Notes", PACKAGE))
    run(cfg, log)
    assert commands == [tools.skill_args(cfg.skills_cli, PACKAGE, ["tips"], enabled=cfg.agents)]


def test_skills_update_leaves_held_names_out(tmp_path, monkeypatch):
    lock = {"skills": {name: {"source": PACKAGE} for name in ("notes", "tips")}}
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents/.skill-lock.json").write_text(json.dumps(lock))
    ran = []
    monkeypatch.setattr(tools, "raw", lambda *args, **kwargs: "↑ notes has an update\n↑ tips has an update\n")
    monkeypatch.setattr(tools, "sh", lambda args, timeout: (ran.append(args) or 0, ""))
    log = Log(tmp_path / "log", write=False)

    assert tools.update_skills(log, False, "skills", home=tmp_path, hold={"notes"})
    assert ran[-1][-4:] == ["update", "-g", "-y", "tips"]
    ran.clear()
    assert tools.update_skills(log, False, "skills", home=tmp_path, hold={"notes", "tips"})
    assert ran == []                                     # nothing left to update, and never everything at once


def test_a_renamed_install_owns_only_its_new_name(tmp_path):
    lock = {"pack-notes": {"source": PACKAGE, "skillName": "notes"}}
    assert set(skills.package_folders(lock, PACKAGE)) == {"pack-notes"}


def test_a_snapshot_left_by_a_killed_run_is_kept_in_the_backups(tmp_path):
    from agent_sync import backups
    state = tmp_path / "state"
    make_skill(state / "skill-guard.killed/0", "notes", "The only copy left.")
    with backups.during(tmp_path, tmp_path / "backups") as run:
        with skills.guard_installs(tmp_path, ("claude",), {PACKAGE}, set(), state):
            pass
    assert not (state / "skill-guard.killed").exists()
    assert "The only copy left." in (run.folder / "state/skill-guard.killed/0/notes/SKILL.md").read_text()


def test_skills_update_never_updates_everything_while_a_name_is_held(tmp_path, monkeypatch):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents/.skill-lock.json").write_text(json.dumps({"skills": {"notes": {"source": PACKAGE}}}))
    ran = []
    monkeypatch.setattr(tools, "sh", lambda args, timeout: (ran.append(args) or 0, ""))
    log = Log(tmp_path / "log", write=False)
    for report in ("  ↑ notes\n", "something new this CLI prints\n"):          # legacy list, unreadable list
        monkeypatch.setattr(tools, "raw", lambda *args, report=report, **kwargs: report)
        assert tools.update_skills(log, False, "skills", home=tmp_path, hold={"notes"})
    assert ran == []


def test_package_names_match_in_any_case(tmp_path):
    assert set(skills.package_folders({"notes": {"source": "Org/Pack"}}, "org/pack")) == {"notes"}
