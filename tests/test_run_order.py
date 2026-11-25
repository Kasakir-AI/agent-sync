from agent_sync import cli, inventory
from agent_sync.config import Config
from tests.test_inventory import codex_plugin


def test_plugins_update_before_the_checks_and_one_snapshot_serves_them(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path, skill_packages=[], update_clis=False, update_mcp_programs=False, update_self=False)
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "test"))
    monkeypatch.setattr(cli.notify, "deliver", lambda *args: (set(), False))
    monkeypatch.setattr(cli.tools, "PATH", str(tmp_path / "bin"))
    order, described = [], []

    def update_plugins(*args, **kwargs):
        order.append("update plugins")
        codex_plugin(tmp_path, "fresh@remote", "1.0.0", remote=True)      # installed by the update

    def report(*args, **kwargs):
        order.append("duplicate check")
        return []

    describe = inventory.describe

    def counted(tool, pid, *args):
        described.append((tool, pid))
        return describe(tool, pid, *args)

    monkeypatch.setattr(cli.tools, "update_plugins", update_plugins)
    monkeypatch.setattr(cli.duplicates, "report", report)
    monkeypatch.setattr(inventory, "describe", counted)
    assert cli.run(force=True, echo=False) == 0
    assert order == ["update plugins", "duplicate check"]
    assert described.count(("codex", "fresh@remote")) == 1           # read once, after the update


def test_inventory_uses_one_snapshot_for_loaded_disabled_and_duplicate_plugins(tmp_path, monkeypatch, capsys):
    cfg = Config(home=tmp_path, agents=["codex"])
    monkeypatch.setattr(cli, "load", lambda: cfg)
    codex_plugin(tmp_path, "fresh@remote", "1.0.0", remote=True)
    codex_plugin(tmp_path, "disabled@local", "1.0.0")
    (tmp_path / ".codex/config.toml").write_text(
        '[plugins."fresh@remote"]\nenabled = true\n[plugins."disabled@local"]\nenabled = false\n')
    read = inventory.READERS["codex"]
    traversals = []

    def counted(home):
        traversals.append(home)
        yield from read(home)

    monkeypatch.setitem(inventory.READERS, "codex", counted)
    assert cli.show_inventory() == 0
    output = capsys.readouterr().out
    assert "fresh@remote" in output and "disabled@local" in output and "(not loaded)" in output
    assert "Problems found" in output
    assert traversals == [tmp_path]
