"""Share global instructions across enabled tools and keep file-local notes in place."""
from __future__ import annotations

import re
from collections import Counter
from hashlib import sha256
from pathlib import Path

from . import pending
from .privacy import secret_rows, secret_values
from .registry import DEFAULT_AGENTS, grouped_paths, paths
from .textmerge import keys, load_baselines, meaningful, partner, save_baselines

KEY = "instructions:Global instructions"
BLOCK = re.compile(r"\n*<!-- agent-sync: begin.*?<!-- agent-sync: end -->\n?", re.S)
FRONT = re.compile(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)", re.M | re.S)
LOCAL_START = "<!-- agent-sync: local -->"
LOCAL_END = "<!-- agent-sync: end local -->"
PRIVATE_START = "<!-- agent-sync: private -->"
PRIVATE_END = "<!-- agent-sync: end private -->"
MEMORY_START = "<!-- agent-sync: memory -->"
MEMORY_END = "<!-- agent-sync: end memory -->"
SECTION_ENDS = {LOCAL_START: LOCAL_END, PRIVATE_START: PRIVATE_END, MEMORY_START: MEMORY_END}


def split_front(text: str) -> tuple[str, str]:
    match = FRONT.match(text)
    return (match[0], text[match.end():]) if match else ("", text)


def files(home: Path, enabled=DEFAULT_AGENTS) -> dict[str, Path]:
    return paths(home, enabled, "instructions")


def own_text(text: str) -> str:
    """Remove the block written by older releases."""
    return BLOCK.sub("\n", text)


def local_rows(lines: list[str]) -> set[int]:
    """Find file-local sections and credential values."""
    hidden = set(secret_rows(lines))
    ends = []
    for index, line in enumerate(lines):
        marker = line.strip()
        if marker in SECTION_ENDS:
            ends.append(SECTION_ENDS[marker])
        if ends:
            hidden.add(index)
            if marker == ends[-1]:
                ends.pop()
    return hidden


def shared_body(body: str) -> str:
    lines = body.splitlines(keepends=True)
    hidden = local_rows(lines)
    return "".join(line for index, line in enumerate(lines) if index not in hidden)


def rendered(tool: str, current: str, body: str, src: Path) -> str:
    if tool != "cursor":
        return body
    front, existing = split_front(current)
    if existing == body:
        return current
    front = front or (f"---\ndescription: Global instructions, mirrored by agent-sync from {src.name}\n"
                      "alwaysApply: true\n---\n")
    return front.rstrip("\r\n") + "\n" + (body if not body or body.endswith("\n") else body + "\n")


def rewords(tracked: list[str], present: set[str], visible: list[str], local: set[str],
            previous: dict[str, str]) -> dict[str, str]:
    """Pair missing shared lines with edits in the same file."""
    paired = {old: new for old, new in previous.items() if old not in present and new in visible}
    reserved = set(tracked) | local | set(previous.values())
    candidates = list(dict.fromkeys(line for line in visible if line not in reserved))
    for old in tracked:
        if old not in present and old not in paired and (match := partner(old, candidates)) is not None:
            paired[old] = candidates.pop(match)
    return paired


def seed_body(source: str, wanted: set[str]) -> str:
    """A new file's text: the source's shared document in order, every wanted line however often it occurs, with the
    source's paragraph breaks."""
    lines = [line for line in source.splitlines() if not line.strip() or line.rstrip() in wanted]
    body = "\n".join(lines).strip("\n")
    return body + "\n" if body else ""


FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def units(lines: list[str], hidden: set[int] | frozenset[int] = frozenset()) -> list[tuple[str, list[int]]]:
    """The shared document as units, each (its text, its rows): a meaningful line, or a fenced code block as one
    unit (blank lines in it included), so a second block is never read as lines the first one already has."""
    out, index = [], 0
    while index < len(lines):
        if index in hidden or not meaningful(lines[index]):
            index += 1
            continue
        if fence := FENCE.match(lines[index]):
            end = index + 1
            while end < len(lines) and not lines[end].strip().startswith(fence[1]):
                end += 1
            end = min(end, len(lines) - 1)
            out.append(("\n".join(line.rstrip() for line in lines[index:end + 1]), list(range(index, end + 1))))
            index = end + 1
        else:
            out.append((lines[index].rstrip(), [index]))
            index += 1
    return out


def update_body(body: str, drop: set[str], replacements: dict[str, str],
                additions: dict[Path, set[str]], donors: dict[Path, str]) -> str:
    """Apply deletions and rewords to the shared rows of `body`, then add each donor's new lines after the line
    they follow there. Lines count by occurrence (a second `---` is its own line) and only in shared rows."""
    lines = body.splitlines()
    hidden = local_rows(lines)
    present = {line.rstrip() for index, line in enumerate(lines) if index not in hidden}
    out = []
    for index, line in enumerate(lines):
        key = line.rstrip()
        if index in hidden:
            out.append(line)
        elif key not in drop:
            replacement = replacements.get(key)
            if replacement is None:
                out.append(line)
            elif replacement not in present:
                out.append(replacement)
                present.add(replacement)

    for donor, wanted in additions.items():
        source = donors[donor].splitlines()
        counts: Counter[str] = Counter()
        keyed = []                                    # (text, its occurrence number, rows) of each donor unit
        for text, rows in units(source):
            counts[text] += 1
            keyed.append((text, counts[text], rows))
        have = Counter(text for text, _ in units(out, local_rows(out)))
        missing = [number > have[text] for text, number, _ in keyed]
        new = [gone and any(part.rstrip() in wanted for part in text.split("\n"))
               for (text, _, _), gone in zip(keyed, missing, strict=True)]
        for index, (_, number, _) in enumerate(keyed):   # a repeated `---` or `- [ ]` goes with the new lines by it
            if missing[index] and number > 1 and any(new[i] for i in (index - 1, index + 1) if 0 <= i < len(new)):
                new[index] = True
        anchor = None
        for (text, number, rows), add in zip(keyed, new, strict=True):
            if not add:
                found = [rows for unit, rows in units(out, local_rows(out)) if unit == text]
                if len(found) >= number:
                    anchor = found[number - 1][-1]
                continue
            at = len(out) if anchor is None else anchor + 1
            out[at:at] = [source[row] for row in rows]
            anchor = at + len(rows) - 1

    if out == lines:
        return body
    return "\n".join(out) + "\n" if out else ""


MASS_DELETION = (3, 0.10)   # more lines than this, or this share of the shared lines, deleted at once: one question


def mirror(home: Path, source: str, dry: bool = False, baselines: Path | None = None, *,
           enabled=DEFAULT_AGENTS) -> list[str]:
    from . import writes  # writes builds on this module

    locations = files(home, enabled)
    groups = grouped_paths(locations)
    if not groups:
        return []
    src = locations[source]
    names = {path: str(path.relative_to(home)) for path in groups}
    current = {}
    for path in groups:
        try:
            current[path] = path.read_text() if path.exists() else ""
        except (OSError, UnicodeDecodeError) as error:
            if path != src:
                raise
            return [f"WARN  {src} could not be read ({error}); instructions are shared again once it can"]
    bodies = {path: own_text(split_front(text)[1] if "cursor" in groups[path] else text)
              for path, text in current.items()}
    public = {path: shared_body(body) for path, body in bodies.items()}
    visible = {path: keys(body) for path, body in public.items()}
    if not visible[src]:
        return [f"WARN  {src} is missing or has no shared lines; agent-sync changes no instructions until it has"]
    stored = load_baselines(baselines) if baselines else {}
    entry = stored.get(KEY) if isinstance(stored.get(KEY), dict) else {}
    previous = {
        name: {"missing": line_list(item.get("missing")),
               "reworded": {old: new for old, new in item.get("reworded", {}).items()
                            if not (secret_values(old) or secret_values(new))}}
        for name, item in (entry.get("held") or {}).items()
    }
    recorded = entry.get("local")
    kept = entry.get("kept") if isinstance(entry.get("kept"), dict) else {}
    messages = []
    for path, text in current.items():
        if rows := secret_rows(text.splitlines()):
            kinds = ", ".join(sorted({kind for row in rows.values() for kind in row}))
            messages.append(f"WARN  {path} has {len(rows)} line(s) that look like secrets ({kinds}); "
                            "they stay in that file only.")
    drop, replacements, held = set(), {}, {}
    removal_keys = []
    tracked = list(dict.fromkeys((*line_list(entry.get("lines")), *line_list(entry.get("conflicts")),
                                 *(line for item in previous.values()
                                   for line in (*item.get("missing", []), *item.get("reworded", {}))))))
    present = {path: set(lines) for path, lines in visible.items()}    # shared rows: what a tool shares
    has = {path: set(keys(body)) for path, body in bodies.items()}       # any row: a line moved to a local section
    if not tracked and recorded is None:
        reserved = set(tracked) | set(visible[src])
        known = {path: set(lines) - reserved for path, lines in visible.items()}
        additions = {src: set(visible[src])}
    else:
        if recorded is None:
            known = {path: set(lines) - set(tracked) for path, lines in visible.items()}
            new_files = set()
        else:
            known = {path: set(line_list(recorded.get(name))) for path, name in names.items()
                     if isinstance(recorded, dict) and name in recorded}
            new_files = set(groups) - known.keys()
            for path in new_files:   # a tool added since: what its file has already is its own, as on a first run
                known[path] = set(visible[path]) - set(tracked) - set(visible[src])
        for path in groups:          # lines the review page kept as this tool's own
            known[path] |= set(line_list(kept.get(str(path.resolve())))) & present[path]
        for path in groups:          # an edited local line stays local
            gone = [line for line in known[path] if line not in present[path]]
            for line in visible[path]:
                if gone and line not in known[path] and line not in tracked and (
                        match := partner(line, gone)) is not None:
                    known[path].discard(gone.pop(match))
                    known[path].add(line)
        paired = {path: rewords(tracked, has[path], visible[path], known[path],
                               previous.get(names[path], {}).get("reworded", {}))
                  for path in known if path not in new_files}
        replacements = paired.get(src, {})
        for old, new in replacements.items():
            messages.append(f'info  {names[src]} rewords "{old}" -> "{new}"; the other tools get the new wording')
        reserved = set(tracked) | {new for changes in paired.values() for new in changes.values()}
        for path, name in names.items():
            prior_rewords = previous.get(name, {}).get("reworded", {})
            kept_local = set(prior_rewords.values()) & has[path]
            reserved.update(kept_local)
            known[path].update(kept_local)
        deleted = [line for line in tracked
                   if line not in present[src] and line not in replacements
                   and any(line in other for path, other in present.items() if path != src)]
        limit, share = MASS_DELETION
        if len(deleted) > 1 and (len(deleted) > limit or len(deleted) > share * len(tracked)):
            what = (f"{len(deleted)} shared instruction lines from the other tools ({names[src]} no longer has "
                    f'them; first: "{deleted[0]}")')
            if pending.due("instructions", what) and not dry:
                drop.update(deleted)
                removal_keys.append("instructions")
            else:
                held.setdefault(names[src], {"missing": [], "reworded": {}})["missing"].extend(deleted)
        else:
            for line in deleted:
                key = f"instruction:{sha256(line.encode()).hexdigest()}"
                if pending.due(key, f'instruction "{line}" from the other tools') and not dry:
                    drop.add(line)
                    removal_keys.append(key)
                else:
                    held.setdefault(names[src], {"missing": [], "reworded": {}})["missing"].append(line)
        for path in (path for path in paired if path != src):
            changes = {replacements.get(old, old): new for old, new in paired[path].items()
                       if old not in drop and replacements.get(old) != new
                       and (old in present[src] or old in replacements)}
            missing = [line for line in tracked if line not in has[path] and line not in paired[path]
                       and line in present[src]]
            if changes:
                old, new = next(iter(changes.items()))
                messages.append(f'WARN  {path} rewords {len(changes)} shared line(s) (first: "{old}" -> "{new}"); '
                                'the other tools keep the old wording. Run agent-sync review to choose.')
            if missing:
                messages.append(f'WARN  {path} no longer has {len(missing)} shared line(s) (first: "{missing[0]}"); '
                                'the other tools keep them. Run agent-sync review to remove them everywhere '
                                'or put them back.')
            if missing or changes:
                held[names[path]] = {"missing": missing, "reworded": changes}
        additions = {path: (set(lines) - reserved - known[path]) for path, lines in visible.items()}
        if new_files or set(groups) - paired.keys():
            additions[src].update(set(visible[src]) & set(tracked))
    seed = seed_body(public[src], additions[src])
    resulting = {}
    for path in groups:
        holds = held.get(names[path], {})
        excluded = set(holds.get("missing", [])) | holds.get("reworded", {}).keys()
        incoming = {donor: lines - excluded if donor != path else lines for donor, lines in additions.items()}

        def change(text: str, path=path, excluded=excluded, incoming=incoming) -> str:
            body = own_text(split_front(text)[1] if "cursor" in groups[path] else text)
            resulting[path] = update_body(body if body or excluded else seed, drop, replacements, incoming, public)
            return rendered(groups[path][0], text, resulting[path], src)

        if dry:
            changed = change(current[path]) != current[path]
        else:
            try:
                changed = writes.update_text(path, change)
            except writes.Raced as raced:
                messages.append(f"WARN  {raced}")
                resulting[path] = bodies[path]
                continue
        if changed:
            messages.append(f"{'would update' if dry else 'updated'} {path}")
        resulting.setdefault(path, bodies[path])

    if baselines and not dry:
        visible = {path: keys(shared_body(body)) for path, body in resulting.items()}
        shared = set.intersection(*(set(lines) for lines in visible.values()))
        held_lines = {line for item in held.values() for line in (*item["missing"], *item["reworded"])}
        lines = list(dict.fromkeys(line for line in (*visible[src], *tracked) if line in shared or line in held_lines))
        local = {names[path]: sorted((set(keys(body)) & known.get(path, set())) - shared)
                 for path, body in resulting.items()}
        updated = {**entry, "lines": lines, "local": local}
        updated.pop("conflicts", None)
        if held:
            updated["held"] = held
        else:
            updated.pop("held", None)
        if updated != entry:
            save_baselines(baselines, {**stored, KEY: updated})
    for key in removal_keys:
        pending.removed(key)
    return messages


def line_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(line, str) for line in value):
        return []
    return [line for line in value if not secret_values(line)]
