import json
from datetime import datetime

from agent_sync import pending


def test_announces_waits_respects_keep_and_expires_removal_reasons(tmp_path):
    state = tmp_path / "pending-removals.json"
    logs = []

    def log(*line):
        logs.append(line)

    key, what, start = "skill:extract", "skill extract (no longer shipped)", 1_000_000
    with pending.during(tmp_path, [], log, True, now=start):
        assert not pending.due(key, what)
    assert logs.pop() == ("dry", f"would announce removal of {what}")
    assert not state.exists()
    with pending.during(tmp_path, [], log, False, now=start):
        assert not pending.due(key, what)
        assert not pending.due(key, what)
    date = datetime.fromtimestamp(start + pending.WAIT).strftime("%Y-%m-%d %H:%M:%S")
    assert logs == [("WARN", f'will remove {what} at the first run after {date}; '
                            f'to keep it, add "{key}" to keep_installed')]
    assert json.loads(state.read_bytes()) == {key: {"first": start, "what": what}}
    before = state.read_bytes()
    logs.clear()

    with pending.during(tmp_path, [], log, False, now=start + pending.WAIT - 1):
        assert not pending.due(key, what)
    with pending.during(tmp_path, [], log, True, now=start + pending.WAIT):
        assert pending.due(key, what)
    assert logs.pop() == ("dry", f"would remove {what}")
    for keep in ([key], ["extract"]):
        with pending.during(tmp_path, keep, log, True, now=start + pending.WAIT):
            assert not pending.due(key, what)
        assert not logs and state.read_bytes() == before
    with pending.during(tmp_path, [], log, False, now=start + pending.WAIT):
        assert pending.due(key, what)
    with pending.during(tmp_path, [], log, False, now=start + pending.WAIT + 1):
        pass
    assert json.loads(state.read_bytes()) == {}
    with pending.during(tmp_path, [], log, False, now=start + pending.WAIT + 2):
        assert not pending.due(key, what)
    assert logs[0][0] == "WARN"


def test_package_skill_removals_wait_and_save_the_originals(tmp_path, monkeypatch, capsys):
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
    start = 1_000_000
    now = start
    original_during = pending.during

    def during(*args):
        return original_during(*args, now=now)

    monkeypatch.setattr(pending, "during", during)
    assert cli.run(force=True, echo=True) == 0
    output = capsys.readouterr().out
    assert "WARN  will remove skill dropped (no longer in org/second)" in output
    assert "WARN  will remove skill clash (org/second ships it, but an earlier package has that name)" in output
    assert not any("remove" in args for args in calls)
    assert not any("add" in args and ("clash" in args or "*" in args) for args in calls)
    assert all(path.read_bytes() == content for path, content in before.items())

    backup_root = cfg.state_dir / "backups"
    first_run = set(backup_root.iterdir())
    calls.clear()
    now += pending.WAIT
    assert cli.run(force=True, echo=True) == 0
    assert not dropped.exists() and not clashing.exists()
    assert [args[args.index("-s") + 1] for args in calls if "remove" in args] == ["clash", "dropped"]
    assert any("org/first" in args and "add" in args for args in calls)
    saved, = set(backup_root.iterdir()) - first_run
    for path, content in before.items():
        assert (saved / path.relative_to(tmp_path)).read_bytes() == content
