import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from agent_sync.statusline import PARSERS, Limit, Status, render

ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOW = datetime(2026, 11, 25, 11).timestamp()
TODAY = datetime(2026, 11, 25, 12, 42).timestamp()
TUESDAY = datetime(2026, 11, 24, 22, 29).timestamp()


@pytest.mark.parametrize("width, rows", [(140, 1), (110, 1), (95, 1), (80, 2), (60, 2), (45, 3)])
def test_richest_layout_with_fewest_rows(width, rows):
    status = Status(
        "Gemini 3 Pro", "plan", Limit(21, TODAY), Limit(48, TUESDAY), 86, 123_000, 1_000_000, 1.02, 2, 3, width
    )
    plain = ANSI.sub("", render(status, NOW))
    lines = plain.splitlines()
    assert len(lines) == rows
    assert max(map(len, lines)) <= width - 4
    assert all(line == line.strip() and not line.startswith("│") and not line.endswith("│") for line in lines)
    segments = re.split(r" │ |\n", plain)
    assert len(segments) == 7
    assert segments[0] == "Gemini 3 Pro [plan]"
    assert re.fullmatch(r"5h: ?21% ?\((rst )?12:42pm\)", segments[1])
    assert re.fullmatch(r"wk: ?48% ?\((rst )?Tue 10:29pm\)", segments[2])
    assert segments[3].startswith(("ctx:86%", "ctx: 86% left"))
    assert segments[4] == "$1.02"
    assert segments[5] in ("agents:2", "a:2")
    assert segments[6] in ("tasks:3", "t:3")
    assert ("(123k/1.0M)" in segments[3]) == (width in (140, 80))


@pytest.mark.parametrize("used, color", [(49, 32), (50, 33), (79, 33), (80, 31)])
def test_limit_usage_colors(used, color):
    styled = render(Status("Claude", five_hour=Limit(used)), NOW)
    assert f"\x1b[{color}m{used}%\x1b[0m" in styled


@pytest.mark.parametrize("left, color", [(19, 31), (20, 33), (49, 33), (50, 32)])
def test_context_remaining_colors(left, color):
    styled = render(Status("Claude", context_left=left), NOW)
    assert f"\x1b[{color}m{left}% left\x1b[0m" in styled


def test_wide_and_combining_characters_use_terminal_cells():
    status = Status("光光e\u0301", context_left=60, width=25)
    assert ANSI.sub("", render(status, NOW)) == "光光e\u0301 │ ctx: 60% left"
    status = Status("光光e\u0301", context_left=60, width=18)
    assert ANSI.sub("", render(status, NOW)) == "光光e\u0301\nctx: 60% left"


def test_very_narrow_terminal_truncates_long_segments():
    assert ANSI.sub("", render(Status("光光光", width=8), NOW)) == "光…"


def test_shorter_labels_prevent_clipping_when_row_count_is_tied():
    status = Status("Claude", five_hour=Limit(20, TODAY), width=22)
    assert ANSI.sub("", render(status, NOW)) == "Claude\n5h:20%(12:42pm)"


def test_claude_parses_limits_context_and_explicit_cost():
    status = PARSERS["claude"](
        {
            "model": {"display_name": "Claude Opus", "id": "opus"},
            "rate_limits": {
                "five_hour": {"used_percentage": 51, "resets_at": TODAY},
                "seven_day": {"used_percentage": 0, "resets_at": TUESDAY},
            },
            "cost": {"total_cost_usd": 0},
            "context_window": {
                "used_percentage": 12,
                "context_window_size": 200_000,
                "current_usage": {"input_tokens": 4000, "cache_read_input_tokens": 20_000},
            },
            "terminal_width": 140,
        }
    )
    assert status == Status(
        "Claude Opus",
        five_hour=Limit(51, TODAY),
        week=Limit(0, TUESDAY),
        context_left=88,
        context_used=24_000,
        context_size=200_000,
        cost=0,
        width=140,
    )
    assert PARSERS["claude"]({"model": {"id": "opus"}}).model == "opus"
    assert PARSERS["claude"]({}).cost is None


def test_cursor_parses_model_mode_and_context_without_cost_or_limits():
    status = PARSERS["cursor"](
        {
            "model": {"id": "claude-opus", "param_summary": "High", "max_mode": True},
            "render_width_chars": 80,
            "context_window": {
                "used_percentage": 40,
                "remaining_percentage": 61,
                "context_window_size": 200_000,
                "total_input_tokens": 48_000,
                "total_output_tokens": 5000,
            },
            "cost": {"total_cost_usd": 100},
            "rate_limits": {"five_hour": {"used_percentage": 10}},
        }
    )
    assert status == Status(
        "claude-opus", "High, Max", context_left=61, context_used=53_000, context_size=200_000, width=80
    )
    within_context = PARSERS["cursor"](
        {
            "model": {"display_name": "Sonnet", "max_mode": True},
            "context_window": {
                "current_usage": {"input_tokens": 32},
                "context_window_size": 100,
                "total_input_tokens": 99,
                "total_output_tokens": 70,
                "used_percentage": 32,
            },
        }
    )
    assert within_context.model == "Sonnet"
    assert within_context.mode == "Max"
    assert within_context.context_left == 68
    assert within_context.context_used == 32


def test_antigravity_prefers_family_then_lowest_remaining_fraction_per_window():
    status = PARSERS["antigravity"](
        {
            "model": {"display_name": "GPT Model", "id": "gemini"},
            "cycle_mode": "plan",
            "quota": {
                "claude-5h": {"remaining_fraction": 0.01},
                "gpt-five-hour": {"remaining_fraction": 0.5, "reset_in_seconds": 108_000},
                "GPT-rolling": {"remaining_fraction": 0.25, "reset_in_seconds": 90},
                "gpt-short-disabled": {"remaining_fraction": 0.09, "disabled": True},
                "credits": {"remaining_amount": 200},
                "gemini-week": {"remaining_fraction": 0.125, "reset_in_seconds": 45},
                "weekly-shared": {"remaining_fraction": 0.25},
                "gpt unspecified": {"remaining_fraction": 0.01},
                "gpt-week": {"remaining_fraction": 0.75, "reset_time": "2026-11-28T15:00:00.900Z"},
            },
            "context_window": {
                "remaining_percentage": 72,
                "used_percentage": 28,
                "context_window_size": 100_000,
            },
            "cost": {"total_usd": 1.2},
            "subagents": [{"status": "running"}, {"status": "done"}, 42, {"status": "running"}],
            "task_count": 3,
            "terminal_width": 60,
        },
        NOW,
    )
    reset = datetime(2026, 11, 28, 15, microsecond=900_000, tzinfo=UTC).timestamp()
    assert status == Status(
        "GPT Model", "plan", Limit(75, NOW + 90), Limit(25, reset), 72, 28_000, 100_000, 1.2, 2, 3, 60
    )


def test_antigravity_falls_back_to_shared_quota_and_reset_duration():
    status = PARSERS["antigravity"](
        {
            "model": {"id": "gemini-4"},
            "quota": {
                "agnostic-short": {
                    "remaining_fraction": 0.25,
                    "reset_in_seconds": 0,
                    "reset_time": "2026-11-25T20:00:00+02:00",
                },
                "shared": {"remaining_fraction": 0.125, "reset_in_seconds": 21_600},
                "another": {"remaining_fraction": 0.5, "reset_in_seconds": 21_600.5},
            },
        },
        NOW,
    )
    assert status.model == "Antigravity"
    assert status.five_hour == Limit(87.5, NOW + 21_600)
    assert status.week == Limit(50)
    status = PARSERS["antigravity"](
        {"quota": {"shared-short": {"remaining_fraction": 0.25, "reset_time": "2026-11-25T20:00:00+02:00"}}}, NOW
    )
    assert status.five_hour == Limit(75, datetime(2026, 11, 25, 18, tzinfo=UTC).timestamp())


@pytest.mark.parametrize("provider", PARSERS)
def test_malformed_fields_never_create_metrics(provider):
    status = PARSERS[provider](
        {
            "model": {"display_name": False, "id": [], "param_summary": 4, "max_mode": 1},
            "rate_limits": {"five_hour": {"used_percentage": True}, "seven_day": 1},
            "context_window": {"used_percentage": float("nan"), "remaining_percentage": [], "context_window_size": {}},
            "cost": {"total_usd": float("inf"), "total_cost_usd": "123"},
            "quota": {"claude-short": [2], "claude-5h": {"remaining_fraction": "1"}},
            "subagents": "running",
            "task_count": -3,
            "mode": {},
            "cycle_mode": [],
        },
        NOW,
    )
    assert status == Status(status.model, width=status.width)
    assert PARSERS[provider]([0], NOW) == status


def test_generic_accepts_plain_model_and_discards_terminal_control_characters():
    status = PARSERS["generic"]({"model": "\x1b[31mOne\nTwo\x07", "mode": "plan"})
    assert ANSI.sub("", render(status, NOW)) == "OneTwo [plan]"


def test_payload_width_then_columns_then_terminal_size(monkeypatch):
    monkeypatch.setenv("COLUMNS", "84")
    assert PARSERS["cursor"]({"terminal_width": 50, "render_width_chars": 60}).width == 60
    assert PARSERS["generic"]({"terminal_width": True}).width == 84
    monkeypatch.delenv("COLUMNS")
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((101, 24)))
    assert PARSERS["generic"]({}).width == 101

    def unavailable(fd):
        raise OSError("no terminal")

    monkeypatch.setattr(os, "get_terminal_size", unavailable)
    for bad_value in ("nope", "-40", "9" * 5000):
        monkeypatch.setenv("COLUMNS", bad_value)
        assert PARSERS["generic"]({}).width == 120


@pytest.mark.parametrize("payload", ["", "{", "[]", "null", '"word"'])
def test_module_command_handles_invalid_input(payload):
    run = subprocess.run(
        [sys.executable, "-m", "agent_sync.statusline", "claude"],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == "agent-sync status line: no data\n"
    assert not run.stderr


def test_module_command_renders_a_payload():
    run = subprocess.run(
        [sys.executable, "-m", "agent_sync.statusline", "claude"],
        input=json.dumps(
            {
                "model": {"display_name": "Claude"},
                "rate_limits": {"five_hour": {"used_percentage": 62}},
                "terminal_width": 120,
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert ANSI.sub("", run.stdout) == "Claude │ 5h: 62%\n"
    assert not run.stderr


def test_import_does_not_load_other_agent_sync_modules():
    run = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import agent_sync.statusline"],
        capture_output=True,
        text=True,
        check=True,
    )
    imports = [line.rsplit("|", 1)[-1].strip() for line in run.stderr.splitlines() if line.startswith("import time:")]
    assert {name for name in imports if name.startswith("agent_sync")} == {"agent_sync", "agent_sync.statusline"}
