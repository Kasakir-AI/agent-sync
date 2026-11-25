"""Share global instructions across enabled tools and keep file-local notes in place."""
from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher
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


def section_rows(lines: list[str]) -> dict[int, str]:
    hidden = {}
    ends = []
    for index, line in enumerate(lines):
        marker = line.strip()
        if marker in SECTION_ENDS:
            ends.append(SECTION_ENDS[marker])
        if ends:
            hidden[index] = ends[-1]
            if marker == ends[-1]:
                ends.pop()
    return hidden


def local_rows(lines: list[str]) -> set[int]:
    """Find file-local sections and credential values."""
    return set(section_rows(lines)) | secret_rows(lines).keys()


def hidden_lines(body: str) -> Counter[str]:
    lines = body.splitlines()
    return Counter(f"{section}:{line_digest(lines[index].rstrip())}"
                   for index, section in section_rows(lines).items() if meaningful(lines[index]))


def line_digest(line: str) -> str:
    return sha256(line.encode()).hexdigest()


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


def seed_body(source: str, wanted: set[str]) -> str:
    lines = source.splitlines()
    included = {row for text, rows in units(lines) if wanted.intersection(text.split("\n")) for row in rows}
    body = "\n".join(line for row, line in enumerate(lines) if row in included or not line.strip()).strip("\n")
    return body + "\n" if body else ""


def update_body(body: str, limits: dict[str, int], replacements: dict[str, str],
                additions: dict[Path, set[str]], donors: dict[Path, str], blocked: set[str]) -> str:
    """Apply deletions and rewords to the shared rows of `body`, then add each donor's new lines after the line
    they follow there. Lines count by occurrence (a second `---` is its own line) and only in shared rows."""
    lines = body.splitlines()
    hidden = local_rows(lines)
    present = {line.rstrip() for index, line in enumerate(lines) if index not in hidden}
    out = []
    remaining = limits.copy()
    for index, line in enumerate(lines):
        key = line.rstrip()
        if index in hidden:
            out.append(line)
        elif key not in remaining or remaining[key] > 0:
            if key in remaining:
                remaining[key] -= 1
            replacement = replacements.get(key)
            if replacement is None:
                out.append(line)
            elif replacement not in present:
                out.append(replacement)
                present.add(replacement)

    for donor, wanted in additions.items():
        if not wanted:
            continue
        source = donors[donor].splitlines()
        donor_units = units(source)
        target_units = units(out, local_rows(out))
        target_texts = [text for text, _ in target_units]
        ordered = SequenceMatcher(None, [text for text, _ in donor_units], target_texts, autojunk=False)
        matches = {a + offset: b + offset for a, b, size in ordered.get_matching_blocks() for offset in range(size)}
        used = set(matches.values())
        elsewhere = defaultdict(deque)
        for index, (text, _) in enumerate(target_units):
            if index not in used:
                elsewhere[text].append(index)
        for index, (text, _) in enumerate(donor_units):
            if index not in matches and elsewhere[text]:
                matches[index] = elsewhere[text].popleft()

        positions = [rows[-1] for _, rows in target_units]
        first = matches[min(matches)] if matches else None
        anchor = None
        for index, (text, rows) in enumerate(donor_units):
            if index in matches:
                anchor = positions[matches[index]]
            elif (parts := set(text.split("\n"))) & wanted and not parts & blocked:
                if anchor is not None:
                    at = anchor + 1
                elif first is not None and text in target_texts:
                    at = target_units[first][1][0]
                else:
                    at = len(out)
                out[at:at] = [source[row] for row in rows]
                positions = [position + len(rows) if position >= at else position for position in positions]
                anchor = at + len(rows) - 1

    if out == lines:
        return body
    return "\n".join(out) + "\n" if out else ""


def mirror(home: Path, source: str, dry: bool = False, baselines: Path | None = None, *,
           enabled=DEFAULT_AGENTS) -> list[str]:
    from . import writes  # writes builds on this module

    locations = files(home, enabled)
    groups = grouped_paths(locations)
    if not groups:
        return []
    written = {}
    for attempt in range(2):
        try:
            messages = _mirror_snapshot(home, locations[source], groups, dry, baselines, written)
            break
        except writes.Raced as raced:
            pending.retry("instruction", "instructions")
            if attempt:
                pending.incomplete()
                messages = [f"WARN  {raced}"]
    return [*messages, *(f"{'would update' if dry else 'updated'} {path}" for path in written)]


def _mirror_snapshot(home: Path, src: Path, groups: dict[Path, tuple[str, ...]], dry: bool,
                     baselines: Path | None, written: dict[Path, tuple[bytes | None, bytes]]) -> list[str]:
    from . import writes

    names = {path: str(path.relative_to(home)) for path in groups}
    snapshot, original = {}, {}
    current = {}
    for path in groups:
        try:
            before = snapshot[path] = writes.read_bytes(path)
            if path in written and before == written[path][1]:
                before = written[path][0]
            original[path] = before
            current[path] = (before or b"").decode().replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeDecodeError) as error:
            if path != src:
                raise
            pending.incomplete()
            return [f"WARN  {src} could not be read ({error}); instructions are shared again once it can"]
    bodies = {path: own_text(split_front(text)[1] if "cursor" in groups[path] else text)
              for path, text in current.items()}
    public = {path: shared_body(body) for path, body in bodies.items()}
    visible = {path: keys(body) for path, body in public.items()}

    def check_snapshot() -> None:
        for path, before in snapshot.items():
            if writes.read_bytes(path) != before:
                raise writes.Raced(path)

    stored = load_baselines(baselines) if baselines else {}
    entry = stored.get(KEY) if isinstance(stored.get(KEY), dict) else {}
    synced = line_list(entry.get("lines"))
    if original[src] is None or (not visible[src] and not synced and "local" not in entry):
        pending.incomplete()
        return [f"WARN  {src} is missing or has no shared lines; agent-sync changes no instructions until it has"]

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
    limits, replacements, held = {}, {}, {}
    removal_key = None
    synced_counts = Counter(synced)
    tracked = list(dict.fromkeys((*synced, *line_list(entry.get("conflicts")),
                                 *(line for item in previous.values()
                                   for line in (*item.get("missing", []), *item.get("reworded", {}))))))
    counts = {path: Counter(lines) for path, lines in visible.items()}
    present = {path: set(rows) for path, rows in counts.items()}
    tracked_counts = synced_counts | Counter(dict.fromkeys(tracked, 1))
    hidden = {path: hidden_lines(body) for path, body in bodies.items()}
    last_hidden = entry.get("hidden") or {}
    last_moves = entry.get("moved") or {}
    moves = {path: Counter() for path in groups}
    missing_counts = {}
    moved = {path: Counter() for path in groups}
    for path, rows in hidden.items():
        name = names[path]
        previous_hidden = set(line_list(last_hidden.get(name)))
        active_moves = Counter(line_list(last_moves.get(name))) & rows
        new_hidden = Counter({token: count for token, count in rows.items() if token not in previous_hidden})
        available = active_moves | new_hidden
        missing = tracked_counts - counts[path]
        for line, count in missing.items():
            digest = line_digest(line)
            for section in SECTION_ENDS.values():
                token = f"{section}:{digest}"
                if n := min(count, available[token]):
                    moves[path][token] = n
                    moved[path][line] += n
                    count -= n
        missing_counts[path] = missing - moved[path]
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
        paired = {path: rewords(tracked, present[path], visible[path], known[path],
                               previous.get(names[path], {}).get("reworded", {}))
                  for path in known if path not in new_files}
        replacements = paired.get(src, {})
        for old, new in replacements.items():
            messages.append(f'info  {names[src]} rewords "{old}" -> "{new}"; the other tools get the new wording')
        reserved = set(tracked) | {new for changes in paired.values() for new in changes.values()}
        for path, name in names.items():
            prior_rewords = previous.get(name, {}).get("reworded", {})
            kept_local = set(prior_rewords.values()) & present[path]
            reserved.update(kept_local)
            known[path].update(kept_local)
        deleted = missing_counts[src] - Counter({line: missing_counts[src][line] for line in replacements})
        if deleted:
            if not dry:
                check_snapshot()
            ordered = sorted(deleted.elements())
            deletion_count = len(ordered)
            digest = line_digest("\n".join(ordered))[:12]
            key = f"instructions:{digest}"
            if pending.keeps(key):
                restore = counts[src] + deleted
                donor = []
                for line in (*synced, *tracked):
                    if restore[line] > 0:
                        donor.append(line)
                        restore[line] -= 1
                bodies[src] = update_body(bodies[src], {}, {}, {src: set(deleted)}, {src: "\n".join(donor)}, set())
                visible[src] = keys(shared_body(bodies[src]))
                present[src] = set(visible[src])
                counts[src] = Counter(visible[src])
            else:
                noun = "line" if deletion_count == 1 else "lines"
                what = (f"{deletion_count} shared instruction {noun} from the other tools "
                        f'({names[src]}, first: "{ordered[0]}")')
                if not pending.due(key, what, warn=False):
                    return [f"WARN  instructions are on hold until you approve or keep the deletion of "
                            f"{deletion_count} line(s): run agent-sync pending"]
                limits = {line: tracked_counts[line] - count for line, count in deleted.items()}
                removal_key = key
        wanted = tracked_counts & counts[src]
        for path in (path for path in paired if path != src):
            changes = {replacements.get(old, old): new for old, new in paired[path].items()
                       if old not in limits and replacements.get(old) != new
                       and (old in present[src] or old in replacements)}
            missing = [line for line in (wanted - counts[path] - moved[path]).elements()
                       if line not in paired[path]]
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
        for path, body in public.items():
            additions[path].update(line for line, rows in units(body.splitlines()) if len(rows) == 1
                                   and line in synced_counts and counts[path][line] > synced_counts[line]
                                   and line not in known[path])
        if new_files or set(groups) - paired.keys():
            additions[src].update(set(visible[src]) & set(tracked))
    seed = seed_body(public[src], additions[src])
    resulting = {}

    for path in groups:
        holds = held.get(names[path], {})
        excluded = set(holds.get("missing", [])) | holds.get("reworded", {}).keys() | moved[path].keys()
        incoming = {donor: lines - excluded if donor != path else lines for donor, lines in additions.items()}
        body = bodies[path] or ("" if excluded else seed)
        resulting[path] = update_body(body, limits, replacements, incoming, public, limits.keys() | moved[path].keys())
        after = rendered(groups[path][0], current[path], resulting[path], src)
        before = snapshot[path]
        data = original[path] if after == current[path] else after.encode()
        if data == before:
            continue
        data = data or b""
        if not dry:
            def change(seen: bytes | None, before=before, path=path, data=data) -> bytes:
                if seen != before:
                    raise writes.Raced(path)
                return data

            writes.update(path, change, check=check_snapshot)
            snapshot[path] = data
        written[path] = original[path], data

    if not dry:
        check_snapshot()

    if baselines and not dry:
        visible = {path: keys(shared_body(body)) for path, body in resulting.items()}
        shared_counts = Counter(visible[src])
        for rows in visible.values():
            shared_counts &= Counter(rows)
        shared = shared_counts.keys()
        held_lines = {line for item in held.values() for line in (*item["missing"], *item["reworded"])}
        visible_lines = {line for rows in visible.values() for line in rows}
        held_lines |= {line for rows in moved.values() for line in rows} & visible_lines
        retained = shared_counts | Counter({line: max(1, tracked_counts[line]) for line in held_lines})
        for line, limit in limits.items():
            retained[line] = min(retained[line], limit)
        lines = []
        for line in (*visible[src], *synced, *tracked):
            if retained[line] > 0:
                retained[line] -= 1
                lines.append(line)
        local = {names[path]: sorted((set(rows) & known.get(path, set())) - shared)
                 for path, rows in visible.items()}
        updated = {**entry, "lines": lines, "local": local,
                   "hidden": {names[path]: sorted(hidden[path].elements()) for path in groups},
                   "moved": {names[path]: sorted(rows.elements()) for path, rows in moves.items() if rows}}
        updated.pop("conflicts", None)
        if held:
            updated["held"] = held
        else:
            updated.pop("held", None)
        updated.pop("deletion_batch", None)
        if updated != entry:
            save_baselines(baselines, {**stored, KEY: updated})
    if removal_key:
        pending.removed(removal_key)
    return messages


def line_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(line, str) for line in value):
        return []
    return [line for line in value if not secret_values(line)]
