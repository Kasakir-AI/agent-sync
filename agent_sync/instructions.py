"""Share global instructions across enabled tools and keep file-local notes in place."""
from __future__ import annotations

import re
from pathlib import Path

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


def edits(synced: list[str], known: dict[Path, set[str]], bodies: dict[Path, str],
          visible: dict[Path, list[str]]) -> tuple[dict[str, dict[Path, str]], set[str]]:
    """Pair each missing shared line with at most one new line per file."""
    changes: dict[str, dict[Path, str]] = {}
    deleted = set()
    synced_keys = set(synced)
    for path, lines in visible.items():
        local = known.get(path)
        if not lines or local is None:
            continue
        present = set(keys(bodies[path]))
        candidates = list(dict.fromkeys(line for line in lines if line not in synced_keys and line not in local))
        for line in synced:
            if line in present:
                continue
            match = partner(line, candidates)
            if match is None:
                deleted.add(line)
            else:
                changes.setdefault(line, {})[path] = candidates.pop(match)
    return changes, deleted - changes.keys()


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
    synced = list(dict.fromkeys((*line_list(entry.get("lines")), *line_list(entry.get("conflicts")))))
    recorded = entry.get("local")
    messages = []
    conflicts = {}
    replacements = {}
    if isinstance(recorded, dict):
        known = {path: set(line_list(recorded[name])) for path, name in names.items() if name in recorded}
        changes, drop = edits(synced, known, bodies, visible)
        for old, edits_by_path in changes.items():
            wordings = set(edits_by_path.values())
            if len(wordings) == 1:
                replacements[old] = next(iter(wordings))
            else:
                conflicts[old] = wordings
                paths_in_conflict = ", ".join(str(path) for path in edits_by_path)
                messages.append(f"WARN  conflicting rewords of {old!r} in {paths_in_conflict}")
        reserved = set(synced).union(*conflicts.values())
        kept = set(synced) - drop - replacements.keys() - conflicts.keys()
        additions = {path: (set(lines) - reserved - known.get(path, set())) | (set(lines) & kept)
                     for path, lines in visible.items()}
    else:
        reserved = set(synced).union(visible[src])
        known = {path: set(lines) - reserved for path, lines in visible.items()}
        drop = set(synced) - set(visible[src]) - set(keys(bodies[src])) if visible[src] else set()
        additions = {src: set(visible[src])}

    resulting = {}
    seed = seed_body(public[src], additions[src])
    for path, body in bodies.items():
        new_body = update_body(body or seed, drop, replacements, additions, visible)
        result = rendered(groups[path][0], current[path], new_body, src)
        resulting[path] = new_body
        if result == current[path]:
            continue
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result)
        messages.append(f"{'would update' if dry else 'updated'} {path}")

    if baselines and not dry:
        visible = {path: keys(shared_body(body)) for path, body in resulting.items()}
        shared = set.intersection(*(set(lines) for lines in visible.values()))
        lines = list(dict.fromkeys(line for line in visible[src] if line in shared))
        local = {names[path]: sorted((set(keys(body)) & known.get(path, set())) - shared)
                 for path, body in resulting.items()}
        updated = {**entry, "lines": lines, "local": local}
        if conflicts:
            updated["conflicts"] = list(conflicts)
        else:
            updated.pop("conflicts", None)
        if updated != entry:
            save_baselines(baselines, {**stored, KEY: updated})
    return messages


def line_list(value: object) -> list[str]:
    return value if isinstance(value, list) and all(isinstance(line, str) for line in value) else []
