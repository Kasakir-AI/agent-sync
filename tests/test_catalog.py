import json

from agent_sync import catalog, cli, tools
from agent_sync.catalog import Catalog, Combo, Skill, Technology


def skill(name, source="org/skills"):
    return Skill(name, source, f"skills/{name}", name.title(), f"About {name}.", "a" * 40)


CATALOG = Catalog(
    skills={name: skill(name) for name in ("fastapi-patterns", "docker-expert", "rust-async", "htmx-fastapi", "go")},
    technologies=(
        Technology("fastapi", "FastAPI", ("fastapi-patterns",), packages=frozenset({"fastapi"})),
        Technology("docker", "Docker", ("docker-expert",), files=("Dockerfile", "compose.yaml")),
        Technology("rust", "Rust", ("rust-async",), files=("Cargo.toml",)),
        Technology("htmx", "htmx", (), content_files=("base.html",), content_patterns=(r"htmx\.org",)),
        Technology("go", "Go", ("go",), extensions=frozenset({".go"})),
    ),
    combos=(Combo("htmx-fastapi", "htmx + FastAPI", frozenset({"htmx", "fastapi"}), ("htmx-fastapi",)),),
)


def test_suggest_spots_technologies_from_manifests_files_contents_and_extensions(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["FastAPI[all]>=0.110"]\n')
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/Dockerfile").write_text("FROM python:3.13-slim\n")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/base.html").write_text('<script src="https://unpkg.com/htmx.org"></script>')
    (tmp_path / "node_modules/x").mkdir(parents=True)
    (tmp_path / "node_modules/x/Cargo.toml").write_text("")        # dependencies' own files don't count

    found = catalog.detect(CATALOG, catalog.scan(tmp_path))
    assert found == {"fastapi": "depends on fastapi", "docker": "has deploy/Dockerfile",
                     "htmx": "templates/base.html mentions htmx"}
    suggested = {s.skill.id: s.reasons for s in catalog.suggest(CATALOG, found)}
    assert suggested == {"fastapi-patterns": ("FastAPI (depends on fastapi)",),
                         "docker-expert": ("Docker (has deploy/Dockerfile)",),
                         "htmx-fastapi": ("FastAPI + htmx",)}


def test_dependencies_are_read_from_each_ecosystem_s_manifest(tmp_path):
    files = {
        "package.json": json.dumps({"dependencies": {"React": "19"}, "devDependencies": {"vitest": "3"}}),
        "requirements-dev.txt": "# tools\nDjango==5.2\n-r base.txt\npytest_asyncio>=1\n",
        "Cargo.toml": '[dependencies]\ntokio = "1"\n',
        "go.mod": "module x\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.10.0\n)\n",
        "Gemfile": "gem 'rails', '~> 8.0'\n",
        "composer.json": json.dumps({"require": {"laravel/framework": "^12"}}),
        "Pipfile": '[packages]\nflask = "*"\n',
    }
    found = set()
    for name, text in files.items():
        (tmp_path / name).write_text(text)
        found |= catalog.dependencies(tmp_path / name)
    assert found == {"react", "vitest", "django", "pytest-asyncio", "tokio", "github.com/gin-gonic/gin", "rails",
                     "laravel/framework", "flask"}


def test_a_broken_manifest_gives_no_dependencies(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert catalog.dependencies(tmp_path / "package.json") == set()


def fake_skills_cli(home, ran, writes=True):
    """Acts like `skills add`: records the command and, unless told not to, writes the skill for each agent."""
    def sh(args, timeout=900, cwd=None):
        ran.append((args, cwd))
        if writes:
            name = args[4].rstrip("/").rpartition("/")[2] if args[4].startswith("https://") else args[6]
            base = cwd if cwd is not None else home
            for folder in (".claude/skills", ".agents/skills"):
                (base / folder / name).mkdir(parents=True, exist_ok=True)
                (base / folder / name / "SKILL.md").write_text("---\nname: x\n---\n")
        return 0, ""
    return sh


def test_install_pins_each_skill_to_its_checked_commit(tmp_path, monkeypatch):
    ran = []
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(tools, "sh", fake_skills_cli(home, ran))
    cfg = cli.Config(home=home, agents=("claude", "codex"))
    assert cli.install_skills(cfg, [skill("docker-expert")], project) == 0
    (args, cwd), = ran
    assert args[:4] == ["npx", "-y", cfg.skills_cli, "add"]
    assert args[4] == f"https://github.com/org/skills/tree/{'a' * 40}/skills/docker-expert"
    assert "-g" not in args and cwd == project                    # into the project by default

    ran.clear()
    cli.install_skills(cfg, [skill("docker-expert")], None)
    assert ran[0][0][-1] == "-g"                                    # --global: for your account


def test_the_shipped_catalog_is_consistent():
    data = catalog.load()
    assert len(data.skills) > 100
    for item in data.skills.values():
        assert tools.safe_id(item.id) and len(item.commit) == 40 and item.source.count("/") == 1
    combo_needs = {tech for combo in data.combos for tech in combo.requires}
    for tech in data.technologies:
        assert (tech.skills or tech.id in combo_needs) and set(tech.skills) <= set(data.skills), tech.id
    for combo in data.combos:
        assert set(combo.skills) <= set(data.skills) and combo.requires <= {t.id for t in data.technologies}


def test_the_readme_counts_match_the_catalog():
    from pathlib import Path
    data = catalog.load()
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    skills, technologies = len(data.skills) // 10 * 10, len(data.technologies) // 10 * 10
    assert f"catalog of {skills}+ skills for {technologies}+ technologies" in readme


def test_restore_puts_a_backup_back_and_backs_up_what_it_replaces(tmp_path):
    from agent_sync import backups
    home, root = tmp_path, tmp_path / "state/backups"
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("original\n")
    with backups.during(home, root) as run:
        backups.save(settings)
        settings.write_text("changed by upkeep\n")
    (home / ".claude/new.json").write_text("added since\n")

    assert backups.restore(run.folder, home, root, [], dry=True) == [f"would restore {settings}"]
    assert backups.restore(run.folder, home, root, [], dry=False) == [f"restored {settings}"]
    assert settings.read_text() == "original\n"
    assert (home / ".claude/new.json").exists()                   # nothing added since is removed
    undo, = [f for f in backups.backup_folders(root) if f.name.startswith("restore-")]
    assert (undo / ".claude/settings.json").read_text() == "changed by upkeep\n"
    assert backups.restore(run.folder, home, root, [], dry=True) == []


def test_an_install_the_cli_skipped_is_reported_as_failed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tools, "sh", fake_skills_cli(tmp_path, [], writes=False))
    cfg = cli.Config(home=tmp_path, agents=("claude",))
    assert cli.install_skills(cfg, [skill("docker-expert")], tmp_path) == 1
    assert "FAILED docker-expert" in capsys.readouterr().out


def test_a_skill_counts_as_installed_only_where_every_agent_can_load_it(tmp_path):
    (tmp_path / ".claude/skills/docker-expert").mkdir(parents=True)
    (tmp_path / ".claude/skills/docker-expert/SKILL.md").write_text("")
    (tmp_path / ".agents/skills/empty").mkdir(parents=True)          # a folder with no SKILL.md doesn't count
    assert catalog.installed(tmp_path, tmp_path, ("claude",)) == {"docker-expert"}
    assert catalog.installed(tmp_path, tmp_path, ("claude", "codex")) == set()


def test_a_skill_the_catalog_does_not_list_installs_from_its_repository(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "sh", fake_skills_cli(tmp_path, ran))
    cfg = cli.Config(home=tmp_path, agents=("claude",))
    assert cli.install_skills(cfg, [cli.direct_skill("org/tools/linter")], tmp_path) == 0
    assert ran[0][0][4:7] == ["org/tools", "-s", "linter"]
    assert cli.direct_skill("org/tools") is None and cli.direct_skill("-x/y/z") is None


def test_folder_markers_count(tmp_path):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    tech = Technology("actions", "GitHub Actions", ("x",), files=(".github/workflows/",))
    assert catalog.evidence_for(tech, catalog.scan(tmp_path)) == "has .github/workflows"


def test_suggest_refuses_json_with_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load", lambda: cli.Config(home=tmp_path))
    assert cli.main(["skill", "suggest", "--install", "--json", "--path", str(tmp_path)]) == 2


def test_catalog_skills_install_under_distinct_names():
    names = [skill.name.lower() for skill in catalog.load().skills.values()]
    assert len(names) == len(set(names))


def test_install_never_replaces_a_skill_of_yours_without_asking(tmp_path, monkeypatch, capsys):
    mine = tmp_path / ".claude/skills/docker-expert"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own\n")
    ran = []
    monkeypatch.setattr(tools, "sh", fake_skills_cli(tmp_path, ran))
    cfg = cli.Config(home=tmp_path, agents=("claude",))
    assert cli.install_skills(cfg, [skill("docker-expert")], tmp_path) == 0      # not a terminal: never replaces
    assert ran == [] and (mine / "SKILL.md").read_text() == "my own\n"
    assert "kept your docker-expert" in capsys.readouterr().out

    (tmp_path / "skills-lock.json").write_text(json.dumps({"skills": {"docker-expert": {"source": "org/skills"}}}))
    assert cli.install_skills(cfg, [skill("docker-expert")], tmp_path) == 0      # an earlier install of it: updates
    assert len(ran) == 1


def test_restore_puts_back_permissions_and_skips_paths_behind_a_new_link(tmp_path):
    import os

    from agent_sync import backups
    home, root = tmp_path / "home", tmp_path / "state/backups"
    config = home / ".config/tool/settings.json"
    config.parent.mkdir(parents=True)
    config.write_text("x\n")
    config.chmod(0o600)
    with backups.during(home, root) as run:
        backups.save(config)
        config.chmod(0o644)
    assert backups.restore(run.folder, home, root, [], dry=False) == [f"restored {config.resolve()}"]
    assert config.stat().st_mode & 0o777 == 0o600

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (config.parent).rename(tmp_path / "moved")
    os.symlink(elsewhere, config.parent)
    lines = backups.restore(run.folder, home, root, [], dry=True)
    assert lines and lines[0].startswith("WARN  skipped") and not (elsewhere / "settings.json").exists()


def test_a_replaced_skill_s_backup_survives_the_install(tmp_path, monkeypatch):
    """Even an update of an earlier install of the same skill keeps a copy of what was there (you may have edited
    it)."""
    from agent_sync import backups
    mine = tmp_path / ".claude/skills/docker-expert"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my edits\n")
    (tmp_path / "skills-lock.json").write_text(json.dumps({"skills": {"docker-expert": {"source": "org/skills"}}}))
    monkeypatch.setattr(tools, "sh", fake_skills_cli(tmp_path, []))
    cfg = cli.Config(home=tmp_path, agents=("claude",))
    assert cli.install_skills(cfg, [skill("docker-expert")], tmp_path) == 0
    saved, = [f for f in backups.backup_folders(cfg.state_dir / "backups") if f.name.startswith("skill-")]
    assert (saved / ".claude/skills/docker-expert/SKILL.md").read_text() == "my edits\n"


def test_restore_skips_a_path_it_cannot_write_and_restores_the_rest(tmp_path):
    from agent_sync import backups
    home, root = tmp_path / "home", tmp_path / "state/backups"
    first, second = home / "a/settings.json", home / "b/settings.json"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_text("original\n")
    with backups.during(home, root) as run:
        backups.save(first)
        backups.save(second)
        first.write_text("changed\n")
        second.write_text("changed\n")
    import shutil
    shutil.rmtree(home / "a")
    (home / "a").write_text("a file where the folder was\n")
    lines = backups.restore(run.folder, home, root, [], dry=False)
    assert lines[0].startswith("WARN  skipped") and lines[1] == f"restored {second.resolve()}"
    assert second.read_text() == "original\n"


def test_a_linked_skill_s_body_is_backed_up_too(tmp_path, monkeypatch):
    import os

    from agent_sync import backups
    body = tmp_path / ".agents/skills/docker-expert"
    body.mkdir(parents=True)
    (body / "SKILL.md").write_text("my edits\n")
    (tmp_path / ".claude/skills").mkdir(parents=True)
    os.symlink("../../.agents/skills/docker-expert", tmp_path / ".claude/skills/docker-expert")
    (tmp_path / "skills-lock.json").write_text(json.dumps({"skills": {"docker-expert": {"source": "org/skills"}}}))

    def replace_body(args, timeout=900, cwd=None):
        (body / "SKILL.md").write_text("package version\n")
        return 0, ""
    monkeypatch.setattr(tools, "sh", replace_body)
    cfg = cli.Config(home=tmp_path, agents=("claude",))
    cli.install_skills(cfg, [skill("docker-expert")], tmp_path)
    saved, = [f for f in backups.backup_folders(cfg.state_dir / "backups") if f.name.startswith("skill-")]
    assert (saved / ".agents/skills/docker-expert/SKILL.md").read_text() == "my edits\n"
