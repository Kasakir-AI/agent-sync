import json

from agent_sync import pending


def approve_current(state_dir):
    path = state_dir / pending.FILENAME
    entries = json.loads(path.read_text())
    assert entries
    for item in entries.values():
        item["approved"] = True
    path.write_text(json.dumps(entries))


def test_requires_approval_respects_keep_and_expires_removal_reasons(tmp_path):
    state = tmp_path / pending.FILENAME
    logs = []

    def log(*line):
        logs.append(line)

    key, what, start = "skill:extract", "skill extract (no longer shipped)", 1_000_000
    with pending.during(tmp_path, [], log, True, now=start) as run:
        assert not pending.due(key, what)
        assert run.questions() == {key: what}
    assert not logs and not state.exists()
    with pending.during(tmp_path, [], log, False, now=start):
        assert not pending.due(key, what)
        assert not pending.due(key, what)
    assert logs == [("WARN", f"approval required to remove {what}")]
    assert json.loads(state.read_text()) == {key: {"first": start, "what": what, "approved": False}}
    logs.clear()

    later = start + 10_000_000
    with pending.during(tmp_path, [], log, False, now=later):
        assert not pending.due(key, what)
    approve_current(tmp_path)
    before = state.read_bytes()
    with pending.during(tmp_path, [], log, True, now=later):
        assert pending.due(key, what)
    assert logs.pop() == ("dry", f"would remove {what}")
    for keep in ([key], ["extract"]):
        with pending.during(tmp_path, keep, log, True, now=later):
            assert not pending.due(key, what)
        assert not logs and state.read_bytes() == before
    with pending.during(tmp_path, [], log, False, now=later):
        assert pending.due(key, what)
        pending.removed(key)
    assert json.loads(state.read_text()) == {}
    with pending.during(tmp_path, [], log, False, now=later):
        assert not pending.due(key, what)
    with pending.during(tmp_path, [], log, False, now=later):
        pass
    assert json.loads(state.read_text()) == {}


def test_legacy_pending_entries_and_changed_reasons_require_approval(tmp_path):
    key, what = "skill:extract", "old reason"
    path = tmp_path / pending.FILENAME
    path.write_text(json.dumps({key: {"what": what, "first": 100}}))
    with pending.during(tmp_path, [], lambda *_: None, False, now=10_000_000):
        assert not pending.due(key, what)
    assert json.loads(path.read_text()) == {key: {"what": what, "first": 100, "approved": False}}
    approve_current(tmp_path)
    with pending.during(tmp_path, [], lambda *_: None, False, now=10_000_001):
        assert not pending.due(key, "different reason")
    assert json.loads(path.read_text()) == {
        key: {"what": "different reason", "first": 10_000_001, "approved": False}
    }


def test_package_skill_removals_require_approval_and_save_the_originals(tmp_path, monkeypatch, capsys):
    from agent_sync import cli, tools
    from agent_sync.config import Config
    from tests.test_inventory import write
    from tests.test_skills import make_skill

    cfg = Config(home=tmp_path, skill_packages=["org/first", "org/second"], update_plugins=False,
                 update_mcp_programs=False, update_clis=False, update_self=False)
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(tools, "PATH", str(tmp_path / "no-bin"))
    make_skill(tmp_path / ".agents/skills", "clash", "Old package copy")
    dropped = write(tmp_path, ".agents/skills/dropped/SKILL.md", "Old dropped copy")
    clashing = dropped.parent.parent / "clash/SKILL.md"
    lock = write(tmp_path, ".agents/.skill-lock.json", {"skills": {
        "dropped": {"source": "org/second"}, "clash": {"source": "org/second"}}})
    before = {path: path.read_bytes() for path in (dropped, clashing, lock)}

    def checkout(package, folder):
        if not folder.exists():
            make_skill(folder / "skills", "clash", package)
            make_skill(folder / "skills", package.split("/")[1], package)
        return 0, ""

    calls = []

    def run(args, timeout=900):
        calls.append(args)
        if "remove" in args:
            name = args[args.index("-s") + 1]
            kept = json.loads(lock.read_text())
            del kept["skills"][name]
            lock.write_text(json.dumps(kept))
            (dropped.parent.parent / name / "SKILL.md").unlink()
        return 0, ""

    monkeypatch.setattr(tools, "checkout", checkout)
    monkeypatch.setattr(tools, "sh", run)
    monkeypatch.setattr(tools, "raw", lambda *args, **kwargs: "Checking for skill updates…\n")
    start = 1_000_000
    now = start
    original_during = pending.during

    def during(*args, **kwargs):
        return original_during(*args, now=now, **kwargs)

    monkeypatch.setattr(pending, "during", during)
    assert cli.run(force=True, echo=True) == 0
    output = capsys.readouterr().out
    assert "WARN  approval required to remove skill dropped (no longer in org/second)" in output
    assert ("WARN  approval required to remove skill clash "
            "(org/second ships it, but an earlier package has that name)") in output
    assert not any("remove" in args for args in calls)
    assert not any("add" in args and ("clash" in args or "*" in args) for args in calls)
    assert all(path.read_bytes() == content for path, content in before.items())

    backup_root = cfg.state_dir / "backups"
    first_run = set(backup_root.iterdir())
    calls.clear()
    now += 1
    assert cli.main(["pending", "--approve", "skill:clash", "skill:dropped"]) == 0
    assert cli.run(force=True, echo=True) == 0
    assert not dropped.exists() and not clashing.exists()
    assert [args[args.index("-s") + 1] for args in calls if "remove" in args] == ["clash", "dropped"]
    assert any("org/first" in args and "add" in args for args in calls)
    saved, = set(backup_root.iterdir()) - first_run
    for path, content in before.items():
        assert (saved / path.relative_to(tmp_path)).read_bytes() == content
    assert json.loads((cfg.state_dir / pending.FILENAME).read_text()) == {}


def test_keep_matches_exact_key_last_segment_and_colon_delimited_prefix(tmp_path):
    key = "instructions:0123456789ab"
    for keep in (key, "0123456789ab", "instructions", "instructions:"):
        with pending.during(tmp_path, [keep], lambda *_: None, True) as run:
            assert not pending.due(key, "deletion batch")
            assert run.questions() == {}
    with pending.during(tmp_path, ["instruction"], lambda *_: None, True) as run:
        assert not pending.due(key, "deletion batch")
        assert run.questions() == {key: "deletion batch"}


def test_upgrade_questions_name_the_upgrade_and_keep_their_approval_by_key(tmp_path):
    logs = []
    key = "upgrade:npm:wrangler@5"
    first = "upgrade wrangler 4.1.0 -> 5.0.0 (a new major version, which can include breaking changes)"
    moved = "upgrade wrangler 4.2.0 -> 5.0.1 (a new major version, which can include breaking changes)"
    with pending.during(tmp_path, [], lambda *line: logs.append(line), False):
        assert not pending.due(key, first, by_key=True)
    assert logs == [("WARN", f"approval required to {first}")]
    assert pending.question(first) == f"U{first[1:]}?"
    approve_current(tmp_path)
    with pending.during(tmp_path, [], lambda *line: logs.append(line), False):
        assert pending.due(key, moved, by_key=True)
    assert json.loads((tmp_path / pending.FILENAME).read_text())[key]["what"] == moved
    with pending.during(tmp_path, [], lambda *line: logs.append(line), False):
        assert not pending.due("skill:extract", "skill extract")
    assert logs[-1] == ("WARN", "approval required to remove skill extract")
    assert pending.question("MCP server cf") == "Remove MCP server cf?"


def test_a_package_prefix_keeps_every_major_version_of_it(tmp_path):
    for keep, kept in ((["upgrade:npm:wrangler"], True), (["upgrade:npm:wrangler@5"], True),
                       (["upgrade:npm:wrangler@6"], False), (["upgrade:npm:wrang"], False)):
        with pending.during(tmp_path, keep, lambda *_: None, True):
            assert pending.keeps("upgrade:npm:wrangler@5") is kept
