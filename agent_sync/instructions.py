"""Share global instructions across enabled tools and keep file-local notes in place."""
from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from . import backups, pending
from .registry import DEFAULT_AGENTS, grouped_paths, paths
from .textmerge import keys, load_baselines, meaningful, partner, save_baselines

KEY = "instructions:Global instructions"
BLOCK = re.compile(r"\n*<!-- agent-sync: begin.*?<!-- agent-sync: end -->\n?", re.S)
FRONT = re.compile(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)", re.M | re.S)
LOCAL_START = "<!-- agent-sync: local -->"
LOCAL_END = "<!-- agent-sync: end local -->"
MEMORY_START = "<!-- agent-sync: memory -->"
MEMORY_END = "<!-- agent-sync: end memory -->"
SECTION_ENDS = {LOCAL_START: LOCAL_END, MEMORY_START: MEMORY_END}


def split_front(text: str) -> tuple[str, str]:
    match = FRONT.match(text)
    return (match[0], text[match.end():]) if match else ("", text)


def files(home: Path, enabled=DEFAULT_AGENTS) -> dict[str, Path]:
    return paths(home, enabled, "instructions")


def own_text(text: str) -> str:
    """Remove the block written by older releases."""
    return BLOCK.sub("\n", text)


def local_rows(lines: list[str]) -> set[int]:
    """Find the rows inside either kind of file-local section, including its markers."""
    hidden = set()
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
    """Keep the source's paragraph breaks when creating a file."""
    remaining = wanted.copy()
    lines = []
    for line in source.splitlines():
        key = line.rstrip()
        if key in remaining:
            lines.append(line)
            remaining.remove(key)
        elif not key:
            lines.append("")
    body = "\n".join(lines).strip("\n")
    return body + "\n" if body else ""


def update_body(body: str, drop: set[str], replacements: dict[str, str],
                additions: dict[Path, set[str]], visible: dict[Path, list[str]]) -> str:
    lines = body.splitlines()
    hidden = local_rows(lines)
    present = set(keys(body))
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

    for donor, additions_from_donor in additions.items():
        anchor = None
        hidden = local_rows(out)
        anchors = {line.rstrip(): index for index, line in enumerate(out)
                   if index not in hidden and meaningful(line)}
        present = {line.rstrip() for line in out}
        for line in visible[donor]:
            if line in anchors:
                anchor = anchors[line]
            elif line in additions_from_donor and line not in present:
                anchor = len(out) if anchor is None else anchor + 1
                out.insert(anchor, line)
                present.add(line)
                anchors = {key: index + (index >= anchor) for key, index in anchors.items()}
                anchors[line] = anchor

    if out == lines:
        return body
    return "\n".join(out) + "\n" if out else ""


def mirror(home: Path, source: str, dry: bool = False, baselines: Path | None = None, *,
           enabled=DEFAULT_AGENTS) -> list[str]:
    locations = files(home, enabled)
    groups = grouped_paths(locations)
    if not groups:
        return []
    src = locations[source]
    names = {path: str(path.relative_to(home)) for path in groups}
    current = {path: path.read_text() if path.exists() else "" for path in groups}
    bodies = {path: own_text(split_front(text)[1] if "cursor" in groups[path] else text)
              for path, text in current.items()}
    public = {path: shared_body(body) for path, body in bodies.items()}
    visible = {path: keys(body) for path, body in public.items()}
    stored = load_baselines(baselines) if baselines else {}
    entry = stored.get(KEY) if isinstance(stored.get(KEY), dict) else {}
    previous = entry.get("held") or {}
    recorded = entry.get("local")
    messages = []
    drop, replacements, held = set(), {}, {}
    removal_keys = []
    tracked = list(dict.fromkeys((*line_list(entry.get("lines")), *line_list(entry.get("conflicts")),
                                 *(line for item in previous.values()
                                   for line in (*item.get("missing", []), *item.get("reworded", {}))))))
    known = {path: set(line_list(recorded.get(name))) for path, name in names.items()
             if isinstance(recorded, dict) and name in recorded}
    present = {path: set(keys(body)) for path, body in bodies.items()}
    if not tracked and recorded is None:
        reserved = set(tracked) | set(visible[src])
        known = {path: set(lines) - reserved for path, lines in visible.items()}
        additions = {src: set(visible[src])}
    else:
        if recorded is None:
            known = {path: set(lines) - set(tracked) for path, lines in visible.items()}
        paired = {path: rewords(tracked, present[path], visible[path], known[path],
                               previous.get(names[path], {}).get("reworded", {}))
                  for path in known}
        replacements = paired.get(src, {})
        reserved = set(tracked) | {new for changes in paired.values() for new in changes.values()}
        for path, name in names.items():
            prior_rewords = previous.get(name, {}).get("reworded", {})
            kept_local = set(prior_rewords.values()) & present[path]
            reserved.update(kept_local)
            known.setdefault(path, set()).update(kept_local)
        for line in tracked:
            if (line not in present[src] and line not in replacements
                    and any(line in other for path, other in present.items() if path != src)):
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
            missing = [line for line in tracked if line not in present[path] and line not in paired[path]
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
        new_files = set(groups) - paired.keys()
        if new_files:
            additions[src].update(set(visible[src]) & set(tracked))
    seed = seed_body(public[src], additions[src])
    resulting = {}
    for path, body in bodies.items():
        holds = held.get(names[path], {})
        excluded = set(holds.get("missing", [])) | holds.get("reworded", {}).keys()
        incoming = {donor: lines - excluded if donor != path else lines for donor, lines in additions.items()}
        new_body = update_body(body if body or excluded else seed, drop, replacements, incoming, visible)
        result = rendered(groups[path][0], current[path], new_body, src)
        resulting[path] = new_body
        if result == current[path]:
            continue
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            backups.save(path, follow=True)
            path.write_text(result)
        messages.append(f"{'would update' if dry else 'updated'} {path}")

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
    return value if isinstance(value, list) and all(isinstance(line, str) for line in value) else []
