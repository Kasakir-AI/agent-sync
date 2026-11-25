"""Your own skills: the ones in the source tool's skills folder that no skill package installed.

- Claude Code reads ~/.claude/skills; Codex and Cursor both read ~/.agents/skills.
- Package skills are installed for every tool by the `skills` CLI already (recorded in ~/.agents/.skill-lock.json),
  so only the rest is copied here.
A copy carries a `.agent-sync` marker. Upkeep replaces or removes exact copies as their source changes. It keeps
copies where review preserved local content and leaves folders without a marker alone.

A copy is left out (and an old one removed) when every tool reading the target folder already loads the same
skill from somewhere else, so agent-sync never makes a tool load a skill twice.

A copy is also left out when a tool reading the target folder loads a skill of that name from elsewhere, so it
would load it twice. Skills inside Claude Code plugins are not copied: Cursor loads them from Claude Code already, so a
copy in ~/.agents/skills would load twice there. Copies earlier versions made (their markers name the plugin) are
removed.
"""
from __future__ import annotations

import filecmp
import json
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from . import backups
from .registry import DEFAULT_AGENTS, TOOLS, grouped_paths, paths

EXTRA_ROOTS = {"codex": ".codex/skills", "cursor": ".cursor/skills", "gemini": ".gemini/skills"}

MARKER = ".agent-sync"
COPY_MARK = "copied by agent-sync from "
PLUGIN_MARK = COPY_MARK + "Claude Code plugin "
REVIEW_MARK = "kept by agent-sync review\n"


def folders(home: Path, enabled=DEFAULT_AGENTS) -> dict[str, Path]:
    return paths(home, enabled, "skills_dir")


def locked(home: Path) -> dict[str, dict]:
    """The skills a package installed, from the `skills` CLI's lock file."""
    p = home / ".agents/.skill-lock.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    entries = data.get("skills") if isinstance(data, dict) else None
    return {name: meta for name, meta in entries.items() if isinstance(meta, dict)} if isinstance(entries, dict) else {}


def own(home: Path, source: str, skip: list[str]) -> dict[str, Path]:
    """name -> folder of every skill in the source tool that came from no package and is not itself a copy."""
    root, pkg = home / TOOLS[source].skills_dir, locked(home)
    roots = [root]
    if source == "gemini":
        roots.append(home / EXTRA_ROOTS[source])
    out = {}
    seen = set()
    for folder in roots:
        for d in sorted(folder.iterdir()) if folder.is_dir() else []:
            if d.name.startswith(".") or d.name in seen or not (d / "SKILL.md").is_file():
                continue
            seen.add(d.name)
            if d.name not in pkg and d.name not in skip and not d.is_symlink() and not (d / MARKER).exists():
                out[d.name] = d
    return out


def plugin_installs(home: Path) -> dict[str, tuple[Path, str, bool]]:
    """The path, version and enabled state of each installed Claude Code user plugin."""
    try:
        installed = json.loads((home / ".claude/plugins/installed_plugins.json").read_text()).get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    try:
        enabled = json.loads((home / ".claude/settings.json").read_text()).get("enabledPlugins") or {}
    except (OSError, ValueError, AttributeError):
        enabled = {}
    out = {}
    for pid, installs in sorted(installed.items()) if isinstance(installed, dict) else ():
        for meta in installs if isinstance(installs, list) else ():
            if not isinstance(meta, dict) or meta.get("scope") != "user":
                continue
            install_path = meta.get("installPath")
            if not isinstance(install_path, str) or not install_path:
                continue
            root = Path(install_path)
            if root.is_dir():
                loads = not isinstance(enabled, dict) or enabled.get(pid) is not False
                out[pid] = root, str(meta.get("version") or ""), loads
    return out


def plugins(home: Path) -> dict[str, Path]:
    """The loaded Claude Code user plugins and their paths."""
    return {pid: root for pid, (root, _, loads) in plugin_installs(home).items() if loads}


def plugin_skill_dirs(root: Path) -> dict[str, Path]:
    """name -> folder of each skill in a plugin: its skills/ folder plus the `skills` paths in its plugin.json."""
    paths = [root / "skills"]
    try:
        extra = json.loads((root / ".claude-plugin/plugin.json").read_text()).get("skills") or []
    except (OSError, ValueError):
        extra = []
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, list):
        paths += [root / x for x in extra if isinstance(x, str)]
    out = {}
    for path in paths:
        if (path / "SKILL.md").is_file():
            out.setdefault(path.name, path)
            continue
        for d in sorted(path.iterdir()) if path.is_dir() else []:
            if not d.name.startswith(".") and (d / "SKILL.md").is_file():
                out.setdefault(d.name, d)
    return out


def marker_text(d: Path) -> str:
    try:
        return (d / MARKER).read_text()
    except OSError:
        return ""


def is_plugin_copy(d: Path) -> bool:
    return marker_text(d).startswith(PLUGIN_MARK)


def same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b, ignore=[MARKER, ".DS_Store"])
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    if filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)[1:] != ([], []):
        return False
    return all(same_tree(a / d, b / d) for d in cmp.common_dirs)


def mirror(home: Path, source: str, skip: list[str] | None = None, dry: bool = False, *,
           enabled=DEFAULT_AGENTS) -> list[str]:
    skip = skip or []
    def verb(done: str, would: str) -> str:
        return would if dry else done

    # target folder -> name -> (the folder copied, its marker text, a note for the change line)
    plan: dict[Path, dict[str, tuple[Path, str, str]]] = {}
    mine = own(home, source, skip)
    roots = grouped_paths(folders(home, enabled))
    source_dir = folders(home, enabled)[source]
    for target in roots.keys() - {source_dir}:
        plan[target] = {n: (d, f"{COPY_MARK}{d}\n", "") for n, d in mine.items()}
    if source_dir == home / TOOLS["codex"].skills_dir:
        plan.setdefault(source_dir, {})  # Clean up old plugin copies in the shared directory.
    changes: list[str] = []

    from .duplicates import doubled, elsewhere, views  # builds on this module, so it is imported here
    seen = views(home, enabled=enabled)
    left_out: dict[tuple[Path, tuple[str, ...]], list[str]] = {}   # (target, tools that have them) -> skill names
    for target in sorted(plan):
        wanted, in_source = plan[target], target == source_dir
        for name, (src, mark, note) in wanted.items():
            dest = target / name
            if dest.is_symlink():
                if os.path.realpath(dest) != os.path.realpath(src):   # realpath, unlike resolve(), allows loops
                    changes.append(f"WARN  {dest} is a link agent-sync did not make; left alone")
                continue                                   # a link to the source is already the same folder
            if marker_text(dest).startswith(REVIEW_MARK):
                continue
            if dest.exists() and not (dest / MARKER).exists():
                from .review import made_for_each_tool  # review builds on this module, so it is imported here
                if not made_for_each_tool(home, name, [(roots[source_dir], src), (roots[target], dest)]):
                    changes.append(f"WARN  {dest} exists and is not a copy agent-sync made; left alone")
                continue                                   # a tool's own version (an installer's): nothing to say
            if dest.exists() and in_source and not is_plugin_copy(dest):
                changes.append(f"WARN  {dest} is a copy from an earlier source tool; left alone")
                continue
            have = elsewhere(home, seen, target, name, src, readers=roots[target])
            if have:                                       # every tool reading `target` loads it already
                if dest.exists():
                    if not dry:
                        backups.save(dest)
                        shutil.rmtree(dest)
                    changes.append(f"{verb('removed', 'would remove')} skill {name}{note} from {target} "
                                   f"(already loaded from {'; '.join(have)})")
                continue
            if twice := doubled(home, seen, target, name, src, readers=roots[target]):
                if dest.exists():
                    if not dry:
                        backups.save(dest)
                        shutil.rmtree(dest)
                    changes.append(f"{verb('removed', 'would remove')} skill {name}{note} from {target} "
                                   f"({' and '.join(TOOLS[tool].name for tool in twice)} would load it twice)")
                left_out.setdefault((target, tuple(twice)), []).append(name)
                continue
            if dest.exists() and same_tree(src, dest):
                if marker_text(dest) != mark and not dry:
                    backups.save(dest)
                    (dest / MARKER).write_text(mark)       # same files from a new place (a plugin's new version)
                continue
            if not dry:
                backups.save(dest)
                target.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(prefix=f".{name}.agent-sync-", dir=target) as staged:
                    tmp = Path(staged) / name
                    shutil.copytree(src, tmp, symlinks=True, ignore=shutil.ignore_patterns(".DS_Store"))
                    (tmp / MARKER).write_text(mark)
                    if dest.exists():
                        shutil.rmtree(dest)
                    tmp.rename(dest)
            changes.append(f"{verb('copied', 'would copy')} skill {name}{note} to {target}")
        for d in sorted(target.iterdir()) if target.is_dir() else []:
            if not (d / MARKER).is_file() or d.is_symlink() or d.name in wanted:
                continue
            marker = marker_text(d)
            if marker.startswith(REVIEW_MARK):
                continue
            plugin = marker.startswith(PLUGIN_MARK)
            if in_source and not plugin:
                continue                                   # a copy from an earlier source tool; not ours to judge
            if not dry:
                backups.save(d)
                shutil.rmtree(d)
            why = "agent-sync no longer copies plugin skills" if plugin else "no longer in the source tool"
            changes.append(f"{verb('removed', 'would remove')} skill {d.name} from {target} ({why})")
    for (target, twice), names in sorted(left_out.items()):
        have = " and ".join(TOOLS[tool].name for tool in twice)
        what = f"skill {names[0]}" if len(names) == 1 else f"{len(names)} skills ({', '.join(names[:8])})"
        changes.append(f"WARN  {what} not copied to {target}: {have} already "
                       f"{'loads' if len(twice) == 1 else 'load'} a skill of that name, so a copy would load twice")
    return changes


def installed_name(name: str, meta: dict) -> str:
    return str(meta.get("skillName") or name).lower()


def backup_installs(home: Path, enabled, names) -> None:
    backups.save(home / ".agents/.skill-lock.json", follow=True)
    roots = set(folders(home, enabled).values())
    roots.add(home / ".agents/skills")
    roots.update(home / root for tool, root in EXTRA_ROOTS.items() if tool in enabled)
    for name in names:
        if name in {"", ".", ".."} or "/" in name:
            continue
        for root in sorted(roots):
            backups.save(root / name)


def shipped(checkout: Path) -> list[str]:
    from .duplicates import skill_name, walk

    root = checkout / "skills" if (checkout / "skills").is_dir() else checkout
    found = walk(root) or ([root] if (root / "SKILL.md").is_file() else [])
    return sorted({skill_name(d) for d in found})


def package_choice(home: Path, package: str, checkout: Path, taken: set[str]) -> tuple[list[str] | None, list[str]]:
    """Which skills of `package` to install, given the names packages listed before it already ship:
    (None for all of them, or the names to install) and the installed ones to remove because an earlier
    package has that name. Adds this package's names to `taken`."""
    ships = shipped(checkout)
    clash = [n for n in ships if n in taken]
    taken.update(ships)
    if not clash:
        return None, []
    gone = [name for name, meta in sorted(locked(home).items()) if meta.get("source") == package
            and installed_name(name, meta) in clash]
    return [n for n in ships if n not in clash], gone


def dropped(home: Path, package: str, checkout: Path) -> list[str]:
    """Skills installed from `package` that its current checkout no longer ships."""
    out = []
    for name, meta in sorted(locked(home).items()):
        if meta.get("source") != package:
            continue
        path = meta.get("skillPath") or f"skills/{name}/SKILL.md"
        if not (checkout / path).is_file():
            out.append(name)
    return out
