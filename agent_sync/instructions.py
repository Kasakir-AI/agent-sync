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


def update_body(body: str, drop: set[str], replacements: dict[str, str],
                additions: dict[Path, set[str]], donors: dict[Path, str], blocked: set[str]) -> str:
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


MASS_DELETION = (3, 0.10)   # more lines than this, or this share of the shared lines, deleted at once: one question


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
    if not visible[src]:
        pending.incomplete()
        return [f"WARN  {src} is missing or has no shared lines; agent-sync changes no instructions until it has"]

    def check_snapshot() -> None:
        for path, before in snapshot.items():
            if writes.read_bytes(path) != before:
                raise writes.Raced(path)

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
    held_batch = set()
    batch_key = None
    removal_keys = []
    synced = line_list(entry.get("lines"))
    synced_counts = Counter(synced)
    tracked = list(dict.fromkeys((*synced, *line_list(entry.get("conflicts")),
                                 *(line for item in previous.values()
                                   for line in (*item.get("missing", []), *item.get("reworded", {}))))))
    present = {path: set(lines) for path, lines in visible.items()}
    moved = {path: (set(keys(body)) - present[path]).intersection(tracked) for path, body in bodies.items()}
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
        deleted = [line for line in tracked
                   if line not in present[src] and line not in moved[src] and line not in replacements
                   and any(line in other for path, other in present.items() if path != src)]
        limit, share = MASS_DELETION
        tracked_count = sum(max(1, synced_counts[line]) for line in tracked)
        candidates = {}
        kept_deletions = set()
        for line in deleted:
            key = f"instruction:{sha256(line.encode()).hexdigest()}"
            if pending.keeps(key):
                kept_deletions.add(line)
            else:
                candidates[key] = line
        deleted_count = sum(max(1, synced_counts[line]) for line in candidates.values())
        if candidates and not dry:
            check_snapshot()
        ordered = sorted(candidates.values())
        digest = sha256("\n".join(ordered).encode()).hexdigest()[:12]
        candidate_batch = f"instructions:{digest}"
        mass = (deleted_count > limit or (tracked_count >= 5 or deleted_count > 1)
                and deleted_count > share * tracked_count)
        if candidates and (mass or entry.get("deletion_batch") == candidate_batch):
            noun = "line" if deleted_count == 1 else "lines"
            what = (f"{deleted_count} shared instruction {noun} from the other tools "
                    f'({names[src]}, first: "{ordered[0]}")')
            if not pending.due(candidate_batch, what):
                held_batch.update(ordered)
                kept_deletions.update(ordered)
                batch_key = candidate_batch
            else:
                drop.update(ordered)
                removal_keys.append(candidate_batch)
        else:
            for key, line in candidates.items():
                if pending.due(key, f'instruction "{line}" from the other tools') and not dry:
                    drop.add(line)
                    removal_keys.append(key)
                else:
                    kept_deletions.add(line)
        if kept_deletions:
            held[names[src]] = {"missing": [line for line in deleted if line in kept_deletions], "reworded": {}}
        for path in (path for path in paired if path != src):
            changes = {replacements.get(old, old): new for old, new in paired[path].items()
                       if old not in drop and replacements.get(old) != new
                       and (old in present[src] or old in replacements)}
            missing = [line for line in tracked if line not in present[path] and line not in moved[path]
                       and line not in paired[path]
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
        for path, body in public.items():
            counts = Counter(visible[path])
            additions[path].update(line for line, rows in units(body.splitlines()) if len(rows) == 1
                                   and line in synced_counts and counts[line] > synced_counts[line]
                                   and line not in known[path])
        if new_files or set(groups) - paired.keys():
            additions[src].update(set(visible[src]) & set(tracked))
    seed = seed_body(public[src], additions[src])
    resulting = {}

    for path in groups:
        holds = held.get(names[path], {})
        excluded = set(holds.get("missing", [])) | holds.get("reworded", {}).keys() | moved[path]
        incoming = {donor: lines - excluded if donor != path else lines for donor, lines in additions.items()}
        body = bodies[path] or ("" if excluded else seed)
        resulting[path] = update_body(body, drop, replacements, incoming, public, held_batch | drop | moved[path])
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
        held_lines |= set.union(*moved.values()) & visible_lines
        kept_lines = dict.fromkeys(line for line in (*visible[src], *tracked) if line in shared or line in held_lines)
        lines = [line for line in kept_lines for _ in range(
            max(1, shared_counts[line], synced_counts[line] if line in held_lines else 0))]
        local = {names[path]: sorted((set(keys(body)) & known.get(path, set())) - shared)
                 for path, body in resulting.items()}
        updated = {**entry, "lines": lines, "local": local}
        updated.pop("conflicts", None)
        if held:
            updated["held"] = held
        else:
            updated.pop("held", None)
        if batch_key:
            updated["deletion_batch"] = batch_key
        else:
            updated.pop("deletion_batch", None)
        if updated != entry:
            save_baselines(baselines, {**stored, KEY: updated})
    for key in removal_keys:
        pending.removed(key)
    return messages


def line_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(line, str) for line in value):
        return []
    return [line for line in value if not secret_values(line)]
