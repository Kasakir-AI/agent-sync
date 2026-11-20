import os
import stat
import subprocess

from agent_sync import review, skills
from agent_sync.config import Config


def apply_claude_skill(home):
    scan = review.scan(home, Config(home=home))
    group, = scan.groups
    winner = next(version for version in group.versions if version.tools == ("claude",))
    steps = review.plan(home, scan, {group.key: winner.id}, "claude")
    review.apply(home, steps, home / "backup")


def test_review_preserves_permissions_when_copying_a_new_skill_file(tmp_path):
    source = skills.folders(tmp_path)["claude"] / "example"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Run the helper.\n")
    script = source / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)

    apply_claude_skill(tmp_path)

    installed = skills.folders(tmp_path)["codex"] / "example/run.sh"
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    subprocess.run([installed], check=True)


def test_review_updates_skill_files_without_changing_existing_permissions(tmp_path):
    source = skills.folders(tmp_path)["claude"] / "example"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Run the helper.\n")
    helper = source / "run.sh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    skills.mirror(tmp_path, "claude")

    target = skills.folders(tmp_path)["codex"] / "example"
    installed = target / "run.sh"
    installed.chmod(0o700)
    helper.write_text("#!/bin/sh\nexit 1\n")

    apply_claude_skill(tmp_path)

    assert installed.read_text() == helper.read_text()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o700


def test_reviewed_skill_keeps_its_own_content_during_upkeep(tmp_path):
    source = skills.folders(tmp_path)["claude"] / "example"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("File a note weekly.\n")
    skills.mirror(tmp_path, "claude")

    target = skills.folders(tmp_path)["codex"] / "example"
    marker = target / skills.MARKER
    old_marker = marker.read_text()
    prompt = target / "SKILL.md"
    prompt.write_text("File a note weekly.\nUse the Codex folder.\n")
    edited = marker.stat().st_mtime + 3
    os.utime(prompt, (edited, edited))
    (target / "local.md").write_text("Only in Codex.\n")
    (source / "SKILL.md").write_text("File a note daily.\n")

    apply_claude_skill(tmp_path)

    expected = "File a note daily.\nUse the Codex folder.\n"
    assert prompt.read_text() == expected
    skills.mirror(tmp_path, "claude")
    assert prompt.read_text() == expected
    assert (target / "local.md").read_text() == "Only in Codex.\n"
    backup = tmp_path / "backup" / target.relative_to(tmp_path) / marker.name
    assert backup.read_text() == old_marker

    (source / "SKILL.md").rename(source / "retired.md")
    skills.mirror(tmp_path, "claude")
    assert prompt.read_text() == expected


def test_review_can_keep_local_skill_files_without_changing_any_instructions(tmp_path):
    source = skills.folders(tmp_path)["claude"] / "example"
    source.mkdir(parents=True)
    original = "File a note weekly.\n"
    (source / "SKILL.md").write_text(original)
    skills.mirror(tmp_path, "claude")

    target = skills.folders(tmp_path)["codex"] / "example"
    local = target / "local.md"
    local.write_text("Only in Codex.\n")
    apply_claude_skill(tmp_path)

    assert skills.mirror(tmp_path, "claude") == []
    assert skills.mirror(tmp_path, "codex") == []
    assert (target / "SKILL.md").read_text() == original
    assert local.read_text() == "Only in Codex.\n"
