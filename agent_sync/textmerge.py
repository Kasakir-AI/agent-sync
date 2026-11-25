"""Compare and merge texts line by line, the way agent-sync keeps one version of a setting in every tool: a line the
kept version changed is replaced, a new one is added, and a line only the other file has stays as that tool's own,
unless the last synced version had it and the kept one deleted it since. The lines last synced are stored as
baselines (a JSON file in agent-sync's state folder)."""
from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path


def comment(line: str) -> bool:
    """A line that is only an HTML comment."""
    text = line.strip()
    return len(text) >= 7 and text.startswith("<!--") and text.endswith("-->")


def meaningful(line: str) -> bool:
    return bool(line.strip()) and not comment(line)


def keys(text: str) -> list[str]:
    """The lines that carry meaning: no blank lines, no lines that are only an HTML comment."""
    return [line.rstrip() for line in text.splitlines() if meaningful(line)]


def normalized(body: str) -> str:
    text = "\n".join(line.rstrip() for line in body.splitlines() if not comment(line))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extends(longer: str, base: str) -> bool:
    """True when `longer` has every line of `base`, in the same order, plus perhaps lines of its own."""
    lines = iter(keys(longer))
    return all(line in lines for line in keys(base))


def partner(line: str, candidates: list[str]) -> int | None:
    """The first candidate that reads like an edit of `line`, or None when `line` is unlike them all."""
    text = line.strip()
    return next((index for index, other in enumerate(candidates)
                 if SequenceMatcher(None, text, other.strip()).ratio() >= 0.6), None)


def rows(target: str, winner: str) -> list[tuple[str, str]]:
    """What keeping `winner` does to `target`, line by line: " " stays, "+" comes from the winner,
    "-" is an edited line the winner's replaces, "=" is the target's own line and stays.
    The winner's lines that are only an HTML comment are metadata of its own file and are not added."""
    return [(tag, line) for tag, line, _ in marked_rows(target, winner)]


def marked_rows(target: str, winner: str, *, protected: set[int] | None = None) -> list[tuple[str, str, int | None]]:
    """`rows`, with the position in `winner` of the line that replaces each "-" line (None on the other rows)."""
    old, new = target.splitlines(), winner.splitlines()
    if not protected and extends(target, winner):
        wanted, found, out = keys(winner), 0, []
        for line in old:
            matched = meaningful(line) and found < len(wanted) and wanted[found] == line.rstrip()
            found += matched
            out.append((" " if matched or not meaningful(line) else "=", line, None))
        return out
    out = []
    old_keys = [None if protected and index in protected else line.rstrip() for index, line in enumerate(old)]
    match = SequenceMatcher(None, old_keys, [line.rstrip() for line in new], autojunk=False)
    for tag, i1, i2, j1, j2 in match.get_opcodes():
        if tag == "equal":
            out += [(" ", line, None) for line in old[i1:i2]]
            continue
        added = [(at, line) for at, line in enumerate(new[j1:j2], j1) if not comment(line)]
        start = 0
        for at, line in enumerate(old[i1:i2], i1):
            if protected and at in protected:
                out.append((" ", line, None))
                continue
            index = partner(line, [other for _, other in added[start:]])
            if index is None:
                out.append(("=", line, None))
            else:
                out += [("-", line, added[start + index][0])]
                out += [("+", other, None) for _, other in added[start:start + index + 1]]
                start += index + 1
        out += [("+", line, None) for _, line in added[start:]]
    return out


def merge(target: str, winner: str, drop: frozenset[str] = frozenset(), *, protected: set[int] | None = None) -> str:
    """Write the winner's lines into `target` and keep the lines only `target` has, except those in `drop`."""
    marked = marked_rows(target, winner, protected=protected)
    lines = [line for tag, line, _ in marked if tag != "-" and not (tag == "=" and line.rstrip() in drop)]
    if lines == target.splitlines():
        return target
    return "\n".join(lines) + "\n" if lines else ""


def reworded(target: str, winner: str, synced: Iterable[str]) -> dict[int, str]:
    """The lines `target` reworded since the last sync while `winner` still has them as synced: the position of the
    winner's line -> the target's wording. Without a record of the last sync, a reworded line can't be told from a
    stale one: none."""
    synced, lines = set(synced), winner.splitlines()
    return {at: line for tag, line, at in marked_rows(target, winner)
            if tag == "-" and line.rstrip() not in synced and lines[at].rstrip() in synced}


def own_lines(target: str, winner: str) -> int:
    return sum(tag == "=" and meaningful(line) for tag, line in rows(target, winner))


def removed_lines(target: str, winner: str, baseline: Iterable[str]) -> set[str]:
    """The lines only `target` has that the last synced version had and `winner` no longer has:
    deleted from the winner since then, so they go. Every other line only `target` has is its own and stays."""
    synced, kept = set(baseline), set(keys(winner))
    return {line.rstrip() for tag, line in rows(target, winner)
            if tag == "=" and line.rstrip() in synced and line.rstrip() not in kept}


def load_baselines(path: Path) -> dict:
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_baselines(path: Path, stored: dict) -> None:
    text = json.dumps(stored, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_bytes().decode("utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    temp.write_text(text)
    temp.replace(path)
