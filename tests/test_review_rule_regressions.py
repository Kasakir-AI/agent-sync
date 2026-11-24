import pytest

from agent_sync import review, writes
from agent_sync.config import Config


def rule(home, tool, name, body, front=""):
    suffix = ".md" if tool == "claude" else ".mdc"
    path = home / f".{tool}/rules/{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}" if front else body)
    return path


@pytest.mark.parametrize("scoped", [False, True])
def test_similar_rules_from_one_tool_stay_separate(tmp_path, scoped):
    shared = "Keep changes small.\nReview each diff.\nAdd focused tests.\n"
    python = rule(tmp_path, "claude", "python", shared + "Run pytest.\n",
                  'paths: ["**/*.py"]' if scoped else "")
    js = rule(tmp_path, "claude", "js", shared + "Run npm test.\n",
              'paths:\n  - "**/*.js"' if scoped else "")

    scanned = review.scan(tmp_path, Config(home=tmp_path))

    assert {tuple(version.path for version in group.versions) for group in scanned.groups} == {(python,), (js,)}
    if scoped:
        for group in scanned.groups:
            assert group.status == "local" and not group.missing
            with pytest.raises(writes.ChoiceError):
                writes.plan(tmp_path, scanned, {group.key: group.versions[0].id}, "claude")


def test_cross_tool_match_does_not_pick_up_a_second_rule_from_the_same_tool(tmp_path):
    shared = "Keep changes small.\nReview each diff.\nAdd focused tests.\n"
    first = rule(tmp_path, "claude", "first", shared + "Use pytest.\n")
    second = rule(tmp_path, "claude", "second", shared + "Use npm.\n")
    cursor = rule(tmp_path, "cursor", "first", shared, "alwaysApply: true")

    scanned = review.scan(tmp_path, Config(home=tmp_path))

    assert {frozenset(version.path for version in group.versions) for group in scanned.groups} == {
        frozenset((first, cursor)), frozenset((second,)),
    }


def test_scoped_rules_with_the_same_name_do_not_pair(tmp_path):
    shared = "Keep changes small.\nReview each diff.\nAdd focused tests.\n"
    python = rule(tmp_path, "claude", "style", shared, 'paths: ["**/*.py"]')
    js = rule(tmp_path, "cursor", "style", shared, "globs: **/*.js\nalwaysApply: false")

    scanned = review.scan(tmp_path, Config(home=tmp_path))

    assert {tuple(version.path for version in group.versions) for group in scanned.groups} == {(python,), (js,)}
    assert len({group.key for group in scanned.groups}) == 2
    assert all(not group.missing for group in scanned.groups)


def test_global_rule_cannot_overwrite_a_scoped_rule_with_the_same_name(tmp_path):
    global_rule = rule(tmp_path, "claude", "style", "Keep changes small.\nReview each diff.\n")
    scoped = rule(tmp_path, "cursor", "style", "Use named exports.\n", "globs: **/*.js\nalwaysApply: false")
    original = scoped.read_bytes()

    scanned = review.scan(tmp_path, Config(home=tmp_path))
    group = next(group for group in scanned.groups if group.versions[0].path == global_rule)
    assert group.status == "local" and not group.missing
    with pytest.raises(writes.ChoiceError):
        writes.plan(tmp_path, scanned, {group.key: group.versions[0].id}, "claude")
    assert scoped.read_bytes() == original
