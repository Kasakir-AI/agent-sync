import json
from datetime import datetime

from agent_sync import backups, cli, mcp, schedule
from agent_sync.config import Config
from tests.test_agents import HELPER
from tests.test_inventory import write
from tests.test_memory import setup as memory_setup
from tests.test_skills import make_skill


def test_save_keeps_first_bytes_links_and_outside_paths(tmp_path):
    home, outside = tmp_path / "home", tmp_path / "outside"
    nested = write(home, "tree/file", "first")
    neighbor = write(home, "tree/neighbor", "original")
    outside.write_text("outside original")
    link = home / "tree/link"
    link.symlink_to(outside)
    root = tmp_path / "state/backups"

    with backups.during(home, root, 30, now=datetime(2026, 10, 1, 12)):
        created = home / "tree/new"
        backups.save(created)
        created.write_text("new")
        assert not root.exists()
        backups.save(nested)
        nested.write_text("second")
        backups.save(nested)
        nested.write_text("third")
        backups.save(home / "tree")
        neighbor.unlink()
        backups.save(outside)
    saved, = root.iterdir()
    assert saved.name == "run-20261001-120000"
    assert (saved / "tree/file").read_text() == "first"
    assert (saved / "tree/neighbor").read_text() == "original"
    assert (saved / "tree/link").readlink() == outside
    assert not (saved / "tree/new").exists()
    assert (saved / "_outside" / outside.relative_to("/")).read_text() == "outside original"


def test_retention_uses_only_valid_run_directories_and_leaves_no_empty_run(tmp_path):
    root = tmp_path / "backups"
    names = {"run-20250801-000000", "run-20250901-000000", "review-20250201-100000",
             "run-20250201-100000-extra", "run-20250230-100000"}
    for name in names:
        (root / name).mkdir(parents=True)
    (root / "run-20250201-000000").write_text("file")
    (root / "run-20250202-000000").symlink_to(root / "review-20250201-100000", target_is_directory=True)
    with backups.during(tmp_path, root, 30, now=datetime(2025, 10, 1)) as run:
        assert run.folder is None
    assert {path.name for path in root.iterdir()} == ((names - {"run-20250801-000000"})
                                                    | {"run-20250201-000000", "run-20250202-000000"})


def test_upkeep_writers_share_one_pre_run_backup(tmp_path, monkeypatch, capsys):
    cfg = Config(home=tmp_path, skill_packages=["example/pack"], mirror_instructions=True, mirror_memory=True,
                 launcher=True, update_plugins=False, update_mcp_programs=False, update_clis=False,
                 update_self=False)
    monkeypatch.setattr(Config, "macos", property(lambda _: True))
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.setattr(cli, "online", lambda: True)
    monkeypatch.setattr(cli.tools, "skills", lambda *args: (0, "fake"))
    monkeypatch.setattr(cli.tools, "PATH", "")
    monkeypatch.setattr(cli.tools, "sh", lambda *args: (0, ""))

    def checkout(package, folder):
        assert package == "example/pack"
        write(folder, "agents/helper.md", HELPER)
        return 0, ""

    monkeypatch.setattr(cli.tools, "checkout", checkout)
    monkeypatch.setattr(mcp, "npm_latest", lambda _: "1.1.0")
    monkeypatch.setattr(mcp, "npm_released", lambda _: {"1.1.0": "2020-01-01T00:00:00Z"})
    _, files, memory_dir = memory_setup(tmp_path)
    files["claude"].write_text("Include the date.\n")
    old = [
        write(tmp_path, files["codex"].relative_to(tmp_path), "Codex note.\n"),
        write(tmp_path, ".agents/pstack-models.md", "feature: old\n"),
        write(tmp_path, ".claude/agents/helper.md", "old agent\n"),
        write(tmp_path, ".cursor/plugins/local/pack-agents/agents/dropped.md", "old copied agent\n"),
        write(memory_dir, "workspace.md", "old memory\n"),
        write(memory_dir, "gone.md", "removed memory\n"),
        write(tmp_path, schedule.launcher_path(tmp_path).relative_to(tmp_path), schedule.LAUNCHER_MARK + "\n"),
        write(tmp_path, ".cursor/mcp.json", {"mcpServers": {"pin": {"command": "npx", "args": ["pack@1.0.0"]}}}),
        write(tmp_path, ".codex/config.toml", '[mcp_servers.pin]\ncommand = "npx"\nargs = ["pack@1.0.0"]\n'),
    ]
    (memory_dir / ".agent-sync").touch()
    write(tmp_path, ".claude/rules/pstack-models.md", "feature: current\n")
    write(tmp_path, ".claude.json", {"mcpServers": {"mirror": {"url": "https://example.invalid/mcp"}}})
    make_skill(tmp_path / ".claude/skills", "edited", "New skill")
    make_skill(tmp_path / ".agents/skills", "edited", "Old skill")
    old.append(write(tmp_path, ".agents/skills/edited/.agent-sync", "old marker\n"))
    old.append(tmp_path / ".agents/skills/edited/SKILL.md")
    before = {file: file.read_bytes() for file in old}
    root = cfg.state_dir / "backups"

    assert cli.run(force=True, echo=True, dry=True) == 0
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not root.exists() and not (cfg.state_dir / "pending-removals.json").exists()
    capsys.readouterr()

    assert cli.run(force=True, echo=True) == 0
    saved, = root.iterdir()
    awaiting_approval = {
        tmp_path / ".cursor/plugins/local/pack-agents/agents/dropped.md", memory_dir / "gone.md",
    }
    for path, content in before.items():
        if path in awaiting_approval:
            assert path.read_bytes() == content and not (saved / path.relative_to(tmp_path)).exists()
            continue
        assert (saved / path.relative_to(tmp_path)).read_bytes() == content, path
        assert not path.exists() or path.read_bytes() != content, path
    assert f"backups: {saved}" in capsys.readouterr().out
    assert "Include the date." in files["codex"].read_text()
    assert "<!-- agent-sync: memory -->" in files["codex"].read_text()
    cursor = json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]
    assert cursor == {"pin": {"command": "npx", "args": ["pack@1.1.0"]},
                      "mirror": {"url": "https://example.invalid/mcp"}}
    assert cli.main(["pending", "--approve", "agent:.cursor/plugins/local/pack-agents/agents:dropped",
                     "memory:gone.md"]) == 0
    assert cli.run(force=True, echo=True) == 0
    removal_backup, = set(root.iterdir()) - {saved}
    for path in awaiting_approval:
        assert not path.exists()
        assert (removal_backup / path.relative_to(tmp_path)).read_bytes() == before[path]
