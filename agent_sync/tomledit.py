"""Change single settings in a TOML file and keep everything else in it: comments, blank lines, order.

Python reads TOML (tomllib) but cannot write it, and rewriting the whole file would drop the user's comments. So a
change here replaces only the text of the one value (or table) it touches. Handles what config.toml uses: strings,
numbers, booleans, arrays (also across lines), tables like [agent_models] and the [[extra_steps]] array of tables.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, time

# One part of a key: bare (codex), "basic" (may have \ escapes) or 'literal' (no escapes). TOML allows all three
# in table headers and keys, so all three must be recognized, or a table would be missed and deleted with its neighbor.
PART = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\\n]|\\.)*"|'[^'\n]*')"""
DOTTED = rf"{PART}(?:[ \t]*\.[ \t]*{PART})*"
HEADER = re.compile(rf"^[ \t]*\[(\[?)[ \t]*({DOTTED})[ \t]*\]\]?[ \t]*(#.*)?$")
KEY = re.compile(rf"[ \t]*({PART})[ \t]*=[ \t]*")
ASSIGNMENT = re.compile(rf"[ \t]*({DOTTED})[ \t]*=[ \t]*")
PARTS = re.compile(PART)
BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def basic_string(value: str) -> str:
    """A TOML basic string. Characters beyond the BMP (emoji) are written as they are: json's default
    \\ud83d\\ude00 surrogate pairs are not valid in TOML. DEL is the one control character json leaves alone."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def key_text(key: str) -> str:
    return key if BARE.match(key) else basic_string(key)


def key_name(part: str) -> str:
    """The plain name of one key part: quotes taken off, escapes in a "basic" one read."""
    if part.startswith('"'):
        return json.loads(part)
    if part.startswith("'"):
        return part[1:-1]
    return part


def value_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return basic_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(value_text(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key_text(key)} = {value_text(val)}" for key, val in value.items()) + " }"
    if isinstance(value, date | time):
        return value.isoformat()
    raise TypeError(f"can't write {type(value).__name__} to TOML")


def value_end(text: str, i: int, delimiters: str = "") -> int:
    """Index just past the value starting at text[i], before any comment after it on its last line."""
    depth, last = 0, i
    while i < len(text):
        quote = next((q for q in ('"""', "'''", '"', "'") if text.startswith(q, i)), None)
        if quote:
            i += len(quote)
            while i < len(text) and not text.startswith(quote, i):
                i += 2 if quote[0] == '"' and text[i] == "\\" else 1
            i += len(quote)
            if len(quote) == 3:                 # """Say "hello"""": up to two quotes may sit before the closing three
                for _ in range(2):
                    if text.startswith(quote[0], i):
                        i += 1
            last = i
            continue
        c = text[i]
        if depth == 0 and c in delimiters:
            return last
        if c == "#" or (c == "\n" and depth <= 0):
            if depth <= 0:
                return last
            i = text.find("\n", i) if c == "#" else i + 1
            i = len(text) if i < 0 else i
            continue
        depth += c in "[{"
        depth -= c in "]}"
        i += 1
        if not c.isspace():
            last = i
    return last


@dataclass
class Section:
    name: str | None        # None: the settings before the first [table]
    array: bool             # an [[array]] table
    start: int              # where its header line starts (0 for the top)
    body: int               # where the lines after the header start
    end: int
    parts: tuple[str, ...]
    keys: list[tuple[str, int, int, int]]   # (key, line start, value start, value end)
    dotted: list[tuple[tuple[str, ...], int, int, int]] = field(default_factory=list)


def sections(text: str) -> list[Section]:
    out = [Section(None, False, 0, 0, len(text), (), [])]
    i = 0
    while i < len(text):
        eol = text.find("\n", i)
        eol = len(text) if eol < 0 else eol
        line = text[i:eol].removesuffix("\r")
        m = HEADER.match(line)
        k = ASSIGNMENT.match(text, i)
        if m:
            out[-1].end = i
            parts = tuple(key_name(part) for part in PARTS.findall(m.group(2)))
            out.append(Section(".".join(parts), bool(m.group(1)), i, min(eol + 1, len(text)), len(text), parts, []))
        elif k and k.start() == i:
            ve = value_end(text, k.end())
            parts = tuple(key_name(part) for part in PARTS.findall(k.group(1)))
            if len(parts) == 1:
                out[-1].keys.append((parts[0], i, k.end(), ve))
            else:
                out[-1].dotted.append((parts, i, k.end(), ve))
            eol = text.find("\n", ve)
            eol = len(text) if eol < 0 else eol
        i = eol + 1
    return out


Table = str | tuple[str, ...]


def section(text: str, table: Table | None) -> Section | None:
    return next((s for s in sections(text)
                 if not s.array and (s.parts == table if isinstance(table, tuple) else s.name == table)), None)


def has_value(text: str, table: Table | None, key: str) -> bool:
    s = section(text, table)
    return bool(s and any(entry[0] == key for entry in s.keys))


def append_block(text: str, block: str) -> str:
    if not text.strip():
        return block
    return text.rstrip("\n") + "\n\n" + block


def set_value(text: str, table: Table | None, key: str | tuple[str, ...], value) -> str:
    """`key = value` at the top (table None) or in [table]: the old value replaced, or a new line added."""
    name = key_text(key) if isinstance(key, str) else ".".join(map(key_text, key))
    line = f"{name} = {value_text(value)}"
    s = section(text, table)
    if s is None:
        heading = ".".join(map(key_text, table)) if isinstance(table, tuple) else table
        return append_block(text, f"[{heading}]\n{line}\n")
    entries = s.keys if isinstance(key, str) else s.dotted
    hit = next((k for k in entries if k[0] == key), None)
    if hit:
        return text[:hit[1]] + line + text[hit[3]:]
    if s.keys:                                    # after the last setting of the section
        at = text.find("\n", s.keys[-1][3])
        at = len(text) if at < 0 else at + 1
    elif table is not None:                       # right under the [table] line
        at = s.body
    else:                                         # top: before the comments that lead into the first table
        lines = text[:s.end].splitlines(keepends=True)
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")) and s.end < len(text):
            lines.pop()
        at = sum(map(len, lines))
        if at == 0 and s.end == len(text):
            at = len(text)
    before = text[:at]
    if before and not before.endswith("\n"):
        before += "\n"
    after = text[at:]
    if table is None and not s.keys and after.strip():
        return before + line + "\n\n" + after
    return before + line + "\n" + after


def remove_value(text: str, table: Table | None, key: str) -> str:
    s = section(text, table)
    hit = next((k for k in s.keys if k[0] == key), None) if s else None
    if not hit:
        return text
    eol = text.find("\n", hit[3])
    return text[:hit[1]] + (text[eol + 1:] if eol >= 0 else "")


def remove_inline_entry(text: str, table: Table | None, key: str, name: str) -> str:
    """Remove one member of an inline table without changing its other values."""
    s = section(text, table)
    hit = next((k for k in s.keys if k[0] == key), None) if s else None
    if not hit or text[hit[2]] != "{":
        return text
    at, comma = hit[2] + 1, None
    while entry := KEY.match(text, at):
        end = value_end(text, entry.end(), ",}")
        sep = end
        while text[sep] in " \t":
            sep += 1
        more = text[sep] == ","
        if key_name(entry[1]) == name:
            if more:
                return text[:at] + text[sep + 1:]
            return text[:comma if comma is not None else at] + text[end:]
        if not more:
            break
        comma, at = sep, sep + 1
    return text


def set_table(text: str, table: Table, values: dict) -> str:
    """Make [table] hold exactly `values`: changed keys replaced in place, removed keys deleted, new ones added.
    An empty `values` removes the table's settings (and the [table] line when nothing else is under it)."""
    s = section(text, table)
    for key in [k[0] for k in s.keys] if s else []:
        if key not in values:
            text = remove_value(text, table, key)
    for key, value in values.items():
        text = set_value(text, table, key, value)
    s = section(text, table)
    if s and not values and all(not line.strip() or line.lstrip().startswith("#")
                                for line in text[s.body:s.end].splitlines()):
        text = text[:s.start] + text[s.body:]
    return text


def set_array_tables(text: str, name: str, items: list[dict]) -> str:
    """Replace every [[name]] table with `items`, written where the first one was (or at the end)."""
    block = "\n".join(f"[[{name}]]\n" + "".join(f"{key_text(k)} = {value_text(v)}\n" for k, v in item.items())
                      for item in items)
    spans = [(s.start, s.end) for s in sections(text) if s.name == name and s.array]
    if not spans:
        return append_block(text, block) if items else text
    for start, end in reversed(spans[1:]):
        text = text[:start] + text[end:]
    start, end = spans[0]
    rest = text[end:].lstrip("\n")
    head = text[:start]
    if not block:
        return head.rstrip("\n") + "\n" + ("\n" + rest if rest else "")
    return head + block + ("\n" + rest if rest else "")
