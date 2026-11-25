"""Render the status JSON supplied on stdin by a supported CLI."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Limit:
    used: float
    resets_at: float | None = None


@dataclass(frozen=True)
class Status:
    model: str
    mode: str | None = None
    five_hour: Limit | None = None
    week: Limit | None = None
    context_left: float | None = None
    context_used: int | None = None
    context_size: int | None = None
    cost: float | None = None
    agents: int = 0
    tasks: int = 0
    width: int = 120


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RESET = "\x1b[0m"
_SEPARATOR = " \x1b[2m│\x1b[0m "
_FAMILIES = (
    ("claude", re.compile(r"claude|sonnet|opus|haiku")),
    ("gpt", re.compile(r"gpt|openai|o1|o3|o4|codex")),
    ("gemini", re.compile(r"gemini")),
)
_SHORT_BUCKET = re.compile(r"5h|5-h|five|hour|short|rolling")
_WEEK_BUCKET = re.compile(r"week|7d|7-d|7day")


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    if isinstance(value, str):
        clean = "".join(c for c in _ANSI.sub("", value) if c == "\u200d" or unicodedata.category(c)[0] != "C")
        return clean.strip() or None
    return None


def _number(value: object) -> float | None:
    if type(value) in (int, float):
        try:
            if math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    return None


def _count(value: object) -> int | None:
    number = _number(value)
    if number is not None and number >= 0 and number.is_integer():
        return int(value)
    return None


def _percent(number: float) -> float:
    return max(0.0, min(number, 100.0))


def _width(value: object) -> int:
    width = _count(value)
    if width:
        return width
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        columns = 0
    if columns > 0:
        return columns
    return shutil.get_terminal_size(fallback=(120, 24)).columns or 120


def _epoch(value: object) -> float | None:
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
            return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return _number(value)


def _limit(value: object) -> Limit | None:
    payload = _object(value)
    used = _number(payload.get("used_percentage"))
    if used is not None:
        return Limit(_percent(used), _epoch(payload.get("resets_at")))
    return None


def _context(payload: dict, *, totals: bool = False) -> tuple[float | None, int | None, int | None]:
    context = _object(payload.get("context_window"))
    left = _number(context.get("remaining_percentage"))
    used = _number(context.get("used_percentage"))
    if left is None and used is not None:
        left = 100 - used
    if left is not None:
        left = _percent(left)

    size = _count(context.get("context_window_size"))
    current = _object(context.get("current_usage"))
    usage_keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    counts = [_count(current.get(key)) for key in usage_keys]
    if all(count is None for count in counts) and totals:
        counts = [_count(context.get(key)) for key in ("total_input_tokens", "total_output_tokens")]
    if any(count is not None for count in counts):
        tokens = sum(count or 0 for count in counts)
    elif size and used is not None:
        tokens = int(size * (_percent(used) / 100))
    else:
        tokens = None
    return left, tokens, size


def _claude(value: object, now: float | None = None) -> Status:
    payload = _object(value)
    model = _object(payload.get("model"))
    limits = _object(payload.get("rate_limits"))
    left, used, size = _context(payload)
    return Status(
        _text(model.get("display_name")) or _text(model.get("id")) or "Claude",
        five_hour=_limit(limits.get("five_hour")),
        week=_limit(limits.get("seven_day")),
        context_left=left,
        context_used=used,
        context_size=size,
        cost=_number(_object(payload.get("cost")).get("total_cost_usd")),
        width=_width(payload.get("terminal_width")),
    )


def _cursor(value: object, now: float | None = None) -> Status:
    payload = _object(value)
    model = _object(payload.get("model"))
    mode = _text(model.get("param_summary"))
    if model.get("max_mode") is True:
        mode = f"{mode}, Max" if mode else "Max"
    left, used, size = _context(payload, totals=True)
    return Status(
        _text(model.get("display_name")) or _text(model.get("id")) or "Cursor",
        mode=mode,
        context_left=left,
        context_used=used,
        context_size=size,
        width=_width(payload.get("render_width_chars")),
    )


def _antigravity(value: object, now: float | None = None) -> Status:
    payload = _object(value)
    model = _object(payload.get("model"))
    display_name = _text(model.get("display_name"))
    model_name = (display_name or _text(model.get("id")) or "").lower()
    family = next((name for name, pattern in _FAMILIES if pattern.search(model_name)), "")
    now = time.time() if now is None else now
    buckets = {"short": [], "week": []}
    for key, value in _object(payload.get("quota")).items():
        bucket = _object(value)
        remaining = _number(bucket.get("remaining_fraction"))
        if not isinstance(key, str) or bucket.get("disabled") is True or remaining is None:
            continue
        key = key.lower()
        seconds = _number(bucket.get("reset_in_seconds")) or 0.0
        if _SHORT_BUCKET.search(key):
            window = "short"
        elif _WEEK_BUCKET.search(key):
            window = "week"
        elif seconds > 0:
            window = "short" if seconds <= 21600 else "week"
        else:
            continue
        reset = now + seconds if seconds > 0 and seconds.is_integer() else _epoch(bucket.get("reset_time"))
        buckets[window].append((bool(family and family in key), remaining, reset))

    limits = {}
    for window, candidates in buckets.items():
        if candidates:
            _, remaining, reset = min(candidates, key=lambda item: (not item[0], item[1]))
            limits[window] = Limit(_percent((1 - remaining) * 100), reset)

    left, used, size = _context(payload)
    subagents = payload.get("subagents")
    running = 0
    if isinstance(subagents, list):
        running = sum(_object(agent).get("status") == "running" for agent in subagents)
    return Status(
        display_name or "Antigravity",
        mode=_text(payload.get("cycle_mode")),
        five_hour=limits.get("short"),
        week=limits.get("week"),
        context_left=left,
        context_used=used,
        context_size=size,
        cost=_number(_object(payload.get("cost")).get("total_usd")),
        agents=running,
        tasks=_count(payload.get("task_count")) or 0,
        width=_width(payload.get("terminal_width")),
    )


def _generic(value: object, now: float | None = None) -> Status:
    payload = _object(value)
    raw_model = payload.get("model")
    model = _object(raw_model)
    left, used, size = _context(payload)
    return Status(
        _text(raw_model) or _text(model.get("display_name")) or _text(model.get("id")) or "agent-sync",
        mode=_text(payload.get("mode")),
        context_left=left,
        context_used=used,
        context_size=size,
        width=_width(payload.get("terminal_width")),
    )


PARSERS = {"claude": _claude, "cursor": _cursor, "antigravity": _antigravity, "generic": _generic}


def _styled(text: str, color: int) -> str:
    return f"\x1b[{color}m{text}{_RESET}"


def _reset_time(epoch: float | None, now: float) -> str:
    if epoch is None:
        return ""
    try:
        stamp = datetime.fromtimestamp(epoch)
        today = datetime.fromtimestamp(now).date()
    except (ValueError, OverflowError, OSError):
        return ""
    clock = f"{stamp.hour % 12 or 12}:{stamp.minute:02d}{'am' if stamp.hour < 12 else 'pm'}"
    return clock if stamp.date() == today else f"{stamp:%a} {clock}"


def _tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.0f}k"
    return str(count)


def _segments(status: Status, now: float, tier: int) -> list[str]:
    segments = [_styled(status.model, 1)]
    if status.mode:
        segments[0] += " " + _styled(f"[{status.mode}]", 36)
    for label, limit in (("5h", status.five_hour), ("wk", status.week)):
        if limit is None:
            continue
        color = 31 if limit.used >= 80 else 33 if limit.used >= 50 else 32
        space = " " if tier < 2 else ""
        segment = f"{label}:{space}" + _styled(f"{limit.used:.0f}%", color)
        reset = _reset_time(limit.resets_at, now)
        if reset:
            caption = f"rst {reset}" if tier < 2 else reset
            segment += space + _styled(f"({caption})", 2)
        segments.append(segment)
    if status.context_left is not None:
        left = status.context_left
        color = 31 if left < 20 else 33 if left < 50 else 32
        segment = "ctx: " + _styled(f"{left:.0f}% left", color) if tier < 2 else "ctx:" + _styled(f"{left:.0f}%", color)
        if tier == 0 and status.context_used is not None and status.context_size:
            segment += " " + _styled(f"({_tokens(status.context_used)}/{_tokens(status.context_size)})", 2)
        segments.append(segment)
    if status.cost is not None:
        segments.append(f"${status.cost:.2f}")
    if status.agents:
        label = "agents" if tier < 3 else "a"
        segments.append(_styled(f"{label}:{status.agents}", 35))
    if status.tasks:
        label = "tasks" if tier < 3 else "t"
        segments.append(_styled(f"{label}:{status.tasks}", 36))
    return segments


def _cells(text: str) -> Iterator[tuple[str, int]]:
    cluster = ""
    columns = 0
    for char in text:
        category = unicodedata.category(char)
        if category[0] == "M" or char == "\u200d" or (columns == 2 and "\U0001f3fb" <= char <= "\U0001f3ff"):
            cluster += char
            if char == "\ufe0f" and columns:
                columns = max(columns, 2)
        elif cluster.endswith("\u200d"):
            cluster += char
            columns = max(columns, 2)
        else:
            if cluster:
                yield cluster, columns
            cluster = char
            columns = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    if cluster:
        yield cluster, columns


def _wrap(segments: list[str], width: int) -> tuple[list[str], int]:
    lines: list[str] = []
    line_width = 0
    clipped = 0
    for segment in segments:
        cells = list(_cells(_ANSI.sub("", segment)))
        size = sum(columns for _, columns in cells)
        if size > width:
            clipped += 1
            prefix = ""
            size = 1
            for cluster, columns in cells:
                if size + columns > width:
                    break
                prefix += cluster
                size += columns
            segment = prefix + "…"
        if lines and line_width + 3 + size <= width:
            lines[-1] += _SEPARATOR + segment
            line_width += 3 + size
        else:
            lines.append(segment)
            line_width = size
    return lines, clipped


def render(status: Status, now: float) -> str:
    width = max(1, status.width - 4)
    best, best_clipped = _wrap(_segments(status, now, 0), width)
    for tier in (1, 2, 3):
        if len(best) == 1 and not best_clipped:
            break
        lines, clipped = _wrap(_segments(status, now, tier), width)
        if (len(lines), clipped) < (len(best), best_clipped):
            best, best_clipped = lines, clipped
    return "\n".join(best)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["-h"] or args == ["--help"]:
        print("usage: agent-sync-statusline [claude|cursor|antigravity|generic]")
        return 0
    provider = args[0] if args else "generic"
    if provider not in PARSERS or len(args) > 1:
        print("usage: agent-sync-statusline [claude|cursor|antigravity|generic]", file=sys.stderr)
        return 2
    try:
        value = json.load(sys.stdin)
    except (ValueError, OSError, RecursionError):
        value = None
    if not isinstance(value, dict):
        print("agent-sync status line: no data")
        return 0
    now = time.time()
    print(render(PARSERS[provider](value, now), now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
