import tomllib
from pathlib import Path

import pytest

from agent_sync import tomledit

EXAMPLE = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()

MINE = """# My settings.

source = "claude"   # Claude Code leads
skill_packages = [
    "backnotprop/pstack",  # the main one
    "me/mine",
]

# No Gemini here.
warn_models = ["gemini-"]

[agent_models]
claude = "opus"

[agent_notes]
poteto-agent = "a note with # and \\" in it"

[[extra_steps]]
name = "one"
command = ["true"]
"""


def test_replaces_only_the_value_and_keeps_comments():
    out = tomledit.set_value(MINE, None, "skill_packages", ["a/b"])
    assert 'skill_packages = ["a/b"]\n' in out and "the main one" not in out
    assert "# Claude Code leads" in out and "# No Gemini here." in out
    out = tomledit.set_value(out, None, "source", "codex")
    assert 'source = "codex"   # Claude Code leads' in out
    assert tomllib.loads(out)["source"] == "codex"


def test_adds_new_settings_in_the_right_place():
    out = tomledit.set_value(MINE, None, "log_days", 3)
    data = tomllib.loads(out)
    assert data["log_days"] == 3 and "log_days" not in data["agent_models"]
    out = tomledit.set_value(out, "agent_models", "codex", "gpt-x")
    assert tomllib.loads(out)["agent_models"] == {"claude": "opus", "codex": "gpt-x"}
    out = tomledit.set_value(out, "cli_commands", "codex", ["npm", "i", "-g", "@openai/codex"])
    assert tomllib.loads(out)["cli_commands"] == {"codex": ["npm", "i", "-g", "@openai/codex"]}


def test_a_key_inside_a_comment_or_string_is_not_a_key():
    text = '# source = "cursor"\nnote = "source = x"\n'
    out = tomledit.set_value(text, None, "source", "codex")
    assert out.startswith('# source = "cursor"\n') and tomllib.loads(out) == {"note": "source = x", "source": "codex"}


def test_remove_and_tables():
    out = tomledit.remove_value(MINE, None, "warn_models")
    assert "warn_models" not in tomllib.loads(out) and "# No Gemini here." in out
    out = tomledit.set_table(out, "agent_notes", {"other": "x"})
    assert tomllib.loads(out)["agent_notes"] == {"other": "x"}
    out = tomledit.set_table(out, "agent_notes", {})
    assert "agent_notes" not in tomllib.loads(out)


def test_array_tables():
    out = tomledit.set_array_tables(MINE, "extra_steps", [{"name": "a", "command": ["x"]},
                                                          {"name": "b", "command": ["y"], "timeout": 5}])
    assert tomllib.loads(out)["extra_steps"] == [{"name": "a", "command": ["x"]},
                                                 {"name": "b", "command": ["y"], "timeout": 5}]
    out = tomledit.set_array_tables(out, "extra_steps", [])
    assert "extra_steps" not in tomllib.loads(out)
    assert tomllib.loads(tomledit.set_array_tables("", "extra_steps", [{"name": "a", "command": ["x"]}]))


def test_the_example_file_edits_cleanly():
    out = EXAMPLE
    for key, value in [("source", "codex"), ("update_clis", ["claude"]), ("plugin_skills", ["writing"]),
                       ("skip_skills", ["x", "y"]), ("log_days", 14)]:
        out = tomledit.set_value(out, None, key, value)
    data = tomllib.loads(out)
    assert data["source"] == "codex" and data["update_clis"] == ["claude"] and data["plugin_skills"] == ["writing"]
    assert data["agent_models"]["claude"] == "opus" and "plugin_skills" not in data["agent_models"]
    comments = [line for line in EXAMPLE.splitlines() if line.lstrip().startswith("#")]
    assert all(c in out for c in comments)


hypothesis = pytest.importorskip("hypothesis")
st = hypothesis.strategies

VALUE = st.one_of(st.booleans(), st.integers(0, 99), st.text("ab #\"\\'=[]\n", max_size=6),
                  st.lists(st.text("ab #\"", max_size=4), max_size=3))
EDIT = st.one_of(
    st.tuples(st.just("set"), st.sampled_from([None, "agent_models", "agent_notes"]),
              st.sampled_from(["source", "k", "skill_packages", "new-key", "a b"]), VALUE),
    st.tuples(st.just("remove"), st.sampled_from([None, "agent_models", "agent_notes"]),
              st.sampled_from(["source", "k", "skill_packages", "poteto-agent", "claude"]), st.none()),
    st.tuples(st.just("steps"), st.none(), st.none(),
              st.lists(st.fixed_dictionaries({"name": st.text("ab", max_size=3), "command": st.lists(
                  st.text("ab", max_size=2), min_size=1, max_size=2)}), max_size=2)))


@hypothesis.settings(max_examples=300, deadline=None)
@hypothesis.given(st.sampled_from([MINE, EXAMPLE, ""]), st.lists(EDIT, max_size=6))
def test_any_edits_give_exactly_the_expected_settings(start, edits):
    text, want = start, tomllib.loads(start)
    for op, table, key, value in edits:
        part = want if table is None else want.setdefault(table, {})
        if op == "set":
            text = tomledit.set_value(text, table, key, value)
            part[key] = value
        elif op == "remove":
            text = tomledit.remove_value(text, table, key)
            part.pop(key, None)
        else:
            text = tomledit.set_array_tables(text, "extra_steps", value)
            want.pop("extra_steps", None)
            if value:
                want["extra_steps"] = value
        want = {k: v for k, v in want.items() if v != {} or k in tomllib.loads(text)}
        assert tomllib.loads(text) == want
    for line in start.splitlines():
        if line.startswith("#"):
            assert line in text
