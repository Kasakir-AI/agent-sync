"""Skills a tool sees twice: the same name from two of the places it loads skills from.

What each tool loads (only what is switched on):
- Claude Code: ~/.claude/skills and the skills of its enabled plugins.
- Codex: ~/.agents/skills, ~/.codex/skills (its built-in ones in .system) and the skills of the enabled plugins
  including its online catalog,
  minus the SKILL.md paths switched off in [[skills.config]] in ~/.codex/config.toml.
- Cursor: ~/.agents/skills, ~/.cursor/skills, its built-in ~/.cursor/skills-cursor and its plugins, and with its
  third-party setting (on unless you switched it off) also ~/.claude/skills, ~/.codex/skills and the skills of
  Claude Code's enabled plugins. Of folders at the same path under these skills folders, Cursor reads the first.
- Gemini CLI and Antigravity also load the skills of their extensions and plugins.
Tools show a plugin's skill as plugin:name, so names are compared after the last colon, ignoring case.
Two paths to one folder (a link) are one skill.
"""
from __future__ import annotations

import os
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from . import inventory, pending, skills
from .registry import DEFAULT_AGENTS, TOOLS, inside

SHOWN = 8                # names listed in a report line


@dataclass(frozen=True)
class Seen:
    name: str    # as the tool matches it: the SKILL.md name after the last colon, in lower case
    where: str   # the place it comes from, in words
    path: Path   # the skill's folder
    plugin_id: str = ""


def skill_name(d: Path) -> str:
    try:
        text = (d / "SKILL.md").read_text(errors="replace")
    except OSError:
        text = ""
    name = d.name
    if text.startswith("---"):
        for line in text.splitlines()[1:]:
            if line.strip() == "---":
                break
            if line.startswith("name:"):
                name = line.removeprefix("name:").strip().strip("\"'") or name
                break
    return name.rsplit(":", 1)[-1].lower()


def walk(root: Path, *, within: Path | None = None) -> list[Path]:
    """Every skill folder under `root`, through links, skipping hidden folders and not looking inside a skill."""
    out, seen, todo = [], set(), [root]
    while todo:
        d = todo.pop()
        if within is not None and not inside(within, d):
            continue
        real = os.path.realpath(d)
        if real in seen or not d.is_dir():
            continue
        seen.add(real)
        definition = d / "SKILL.md"
        if d != root and (within is None or inside(within, definition)) and definition.is_file():
            out.append(d)
            continue
        try:
            todo += [c for c in d.iterdir() if not c.name.startswith(".")]
        except OSError:
            pass
    return sorted(out)


def newest(folder: Path) -> Path | None:
    """A plugin cache keeps one folder per version; the newest is the one in use."""
    try:
        versions = [d for d in folder.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        return None
    return max(versions, key=lambda d: d.stat().st_mtime, default=None)


def shared_where(home: Path, d: Path, locked: dict[str, dict] | None = None) -> str:
    """Where a skill in ~/.agents/skills came from (`locked`: the skills lock, read once by the caller)."""
    rel = d.relative_to(home / ".agents/skills")
    if rel.parts[0] == "synced":
        return "~/.agents/skills (claude.ai synced skills)"
    source = ((skills.locked(home) if locked is None else locked).get(d.name) or {}).get("source")
    if source and len(rel.parts) == 1:
        return f"~/.agents/skills (package {source})"
    if skills.is_plugin_copy(d):
        pid = skills.marker_text(d).removeprefix(skills.PLUGIN_MARK).split(":", 1)[0]
        return f"~/.agents/skills (agent-sync copy from Claude Code plugin {pid})"
    if (d / skills.MARKER).exists():
        return "~/.agents/skills (agent-sync copy)"
    return "~/.agents/skills"


def claude_where(home: Path, d: Path, locked: dict[str, dict] | None = None) -> str:
    """Where a skill in ~/.claude/skills came from; a link into ~/.agents/skills is named after its source."""
    real, shared = Path(os.path.realpath(d)), Path(os.path.realpath(home / ".agents/skills"))
    if real.is_relative_to(shared):
        return shared_where(home, home / ".agents/skills" / real.relative_to(shared), locked).replace(
            "~/.agents/skills", "~/.claude/skills", 1)
    return "~/.claude/skills (claude.ai synced skills)" if "synced" in d.parts else "~/.claude/skills"


def codex_config(home: Path) -> dict:
    try:
        return tomllib.loads((home / ".codex/config.toml").read_text())
    except (OSError, ValueError):
        return {}


def codex_off(config: dict) -> set[str]:
    """The skill folders switched off in Codex's [[skills.config]]."""
    return {os.path.realpath(Path(e["path"]).parent) for e in (config.get("skills") or {}).get("config") or []
            if isinstance(e, dict) and e.get("enabled") is False and isinstance(e.get("path"), str)}


def views(home: Path, *, enabled=DEFAULT_AGENTS) -> dict[str, list[Seen]]:
    """tool -> every skill it loads."""
    locked = skills.locked(home)
    shared = [Seen(skill_name(d), shared_where(home, d, locked), d) for d in walk(home / ".agents/skills")]

    enabled = tuple(enabled)
    loaded = inventory.plugins(home, (*enabled, "claude") if "cursor" in enabled else enabled)

    def plugin_skills(tool: str) -> list[Seen]:
        return [Seen(skill_name(d), f"{TOOLS[tool].name} plugin {p.id.removesuffix('@local')}", d, p.id)
                for p in loaded.get(tool, []) for d in p.skill_dirs]

    claude = [Seen(skill_name(d), claude_where(home, d, locked), d) for d in walk(home / ".claude/skills")]
    claude += plugin_skills("claude")

    off = codex_off(codex_config(home))
    codex = shared + [Seen(skill_name(d), "~/.codex/skills", d) for d in walk(home / ".codex/skills")]
    codex += [Seen(skill_name(d), "Codex built-in", d) for d in walk(home / ".codex/skills/.system")]
    codex += plugin_skills("codex")
    codex = [s for s in codex if os.path.realpath(s.path) not in off]

    cursor = shared + [Seen(skill_name(d), "~/.cursor/skills", d) for d in walk(home / ".cursor/skills")]
    cursor += [Seen(skill_name(d), "Cursor built-in", d) for d in walk(home / ".cursor/skills-cursor")]
    cursor += plugin_skills("cursor")
    third_party = [s for s in claude + codex
                   if s.where.startswith(("~/.claude/skills", "~/.codex/skills", "Claude Code plugin"))]
    cursor = unique(cursor + third_party)
    order = {"~/.cursor/skills": 0, "~/.claude/skills": 1, "~/.codex/skills": 2, "~/.agents/skills": 3}
    kept, rels = [], set()
    for s in sorted(cursor, key=lambda s: order.get(s.where.split(" (")[0], 9)):
        root = s.where.split(" (")[0]
        rel = s.path.relative_to(home / root.removeprefix("~/")) if root in order else None
        if rel is None or rel not in rels:   # Cursor reads one folder per path under its skills folders
            rels.add(rel)
            kept.append(s)

    found = {"claude": unique(claude), "codex": unique(codex), "cursor": kept}
    if "gemini" in enabled:
        alias_names = {skill.name for skill in shared}
        native = skills.EXTRA_ROOTS["gemini"]
        found["gemini"] = unique(shared + [Seen(skill_name(d), f"~/{native}", d)
                                           for d in walk(home / native) if skill_name(d) not in alias_names]
                                 + plugin_skills("gemini"))
    if "antigravity" in enabled:
        root = TOOLS["antigravity"].skills_dir
        found["antigravity"] = unique([Seen(skill_name(d), f"~/{root}", d) for d in walk(home / root)]
                                      + plugin_skills("antigravity"))
    return {tool: found[tool] for tool in TOOLS if tool in enabled}


def unique(seen: list[Seen]) -> list[Seen]:
    out, paths = [], set()
    for s in seen:
        real = os.path.realpath(s.path)
        if real not in paths:
            paths.add(real)
            out.append(s)
    return out


def within(path: Path, folder: str) -> bool:
    """`path` is `folder` or inside it, links resolved (foo-bar is not inside foo)."""
    return Path(os.path.realpath(path)).is_relative_to(folder)


def elsewhere(home: Path, seen: dict[str, list[Seen]], target: Path, name: str, src: Path, *,
              readers: tuple[str, ...] | None = None, off: set[str] | None = None) -> list[str]:
    """Where each tool reads an identical skill elsewhere, or deliberately disabled the copy a second tool reads.
    Empty when a reader would lose its only identical copy.
    `off`: Codex's switched-off skill folders, read once by the caller."""
    if readers is None:
        readers = tuple(tool for tool, folder in skills.folders(home).items() if folder == target)
    mine = os.path.realpath(target / name)
    want = skills.tree_digest(src)
    name = skill_name(src)
    switched_off = off if off is not None else codex_off(codex_config(home))
    places = []
    for tool in readers:
        hit = next((s for s in seen.get(tool, []) if not within(s.path, mine) and s.name == name
                    and (s.path / "SKILL.md").is_file() and skills.tree_digest(s.path) == want), None)
        if hit:
            places.append(f"{TOOLS[tool].name}: {hit.where}")
        elif tool == "codex" and mine in switched_off and len(readers) > 1:
            places.append("Codex: switched off there")
        else:
            return []
    return places


CURSOR_FOLDERS = (".cursor/skills", ".claude/skills", ".codex/skills", ".agents/skills")


def doubled(home: Path, seen: dict[str, list[Seen]], target: Path, name: str, src: Path, *,
              readers: tuple[str, ...] | None = None) -> list[str]:
    """The tools reading `target` that a copy at target/name would make load the skill in `src` twice: they load a
    skill of its name from elsewhere already. For Cursor, a folder at the same path under another of its skills
    folders does not count: it reads only one of those."""
    if readers is None:
        readers = tuple(tool for tool, folder in skills.folders(home).items() if folder == target)
    mine, wanted = os.path.realpath(target / name), skill_name(src)
    shadows = {home / folder / name for folder in CURSOR_FOLDERS}
    return [tool for tool in readers
            if any(s.name == wanted and not within(s.path, mine)
                   and not (tool == "cursor" and s.path in shadows)
                   and not (tool == "gemini" and s.where == f"~/{skills.EXTRA_ROOTS['gemini']}")
                   for s in seen.get(tool, []))]


def report(home: Path, allow: list[str] | None = None, *, enabled=DEFAULT_AGENTS) -> list[str]:
    """One line per tool and pair of places that give it the same skills, naming the skills."""
    allowed = {a.lower() for a in allow or []}
    run = pending.current.get()
    pairs = run.plugin_pairs if run is not None else set()
    lines = []
    for tool, seen in views(home, enabled=enabled).items():
        by_name: dict[str, list[Seen]] = defaultdict(list)
        for s in seen:
            by_name[s.name].append(s)
        groups: dict[tuple[bool, tuple[str, ...]], list[str]] = defaultdict(list)
        for name, found in sorted(by_name.items()):
            if len(found) <= 1 or name in allowed:
                continue
            related = {(a, b) for a, b in combinations(found, 2)
                       if (tool, frozenset((a.plugin_id, b.plugin_id))) in pairs}
            if not related:
                groups[False, tuple(sorted({s.where for s in found}))].append(name)
                continue
            for waiting, places in {(pair in related, tuple(sorted({s.where for s in pair})))
                                    for pair in combinations(found, 2)}:
                groups[waiting, places].append(name)
        for (waiting, places), names in sorted(groups.items()):
            where = "; ".join(places) if len(places) > 1 else f"{places[0]}, twice"
            head = "info  " if waiting else ""
            tail = " (waiting for your approval)" if waiting else ""
            if len(names) == 1:
                lines.append(f"{head}{TOOLS[tool].name} loads {names[0]} more than once, from {where}{tail}")
                continue
            shown = ", ".join(names[:SHOWN]) + (f" and {len(names) - SHOWN} others" if len(names) > SHOWN else "")
            lines.append(f"{head}{TOOLS[tool].name} loads {len(names)} skills more than once, from {where}: "
                         f"{shown}{tail}")
    return lines
