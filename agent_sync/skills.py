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
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from . import backups, pending
from .registry import DEFAULT_AGENTS, TOOLS, grouped_paths, inside, paths

EXTRA_ROOTS = {"codex": ".codex/skills", "cursor": ".cursor/skills", "gemini": ".gemini/skills"}

MARKER = ".agent-sync"
COPY_MARK = "copied by agent-sync from "
PLUGIN_MARK = COPY_MARK + "Claude Code plugin "
REVIEW_MARK = "kept by agent-sync review\n"
DIGEST = "files: "   # a marker's second line: the digest of the files agent-sync copied


def tree_digest(folder: Path) -> str:
    """A digest of a skill folder's files (names, link targets and bytes), without the marker."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.name in (MARKER, ".DS_Store") or (path.is_dir() and not path.is_symlink()):
            continue
        digest.update(os.fsencode(path.relative_to(folder)) + b"\0")
        digest.update(os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def edited(dest: Path, home: Path) -> bool:
    """A digest or the previous source can establish that a copy is unchanged."""
    lines = marker_text(dest).splitlines()
    recorded = next((line.removeprefix(DIGEST) for line in lines[1:] if line.startswith(DIGEST)), None)
    if recorded is not None:
        return recorded != tree_digest(dest)
    if not lines or not lines[0].startswith(COPY_MARK):
        return True
    source = lines[0].removeprefix(COPY_MARK)
    if lines[0].startswith(PLUGIN_MARK):
        source = lines[0].partition(": ")[2]
    previous = Path(source)
    return not (previous.is_absolute() and inside(home, previous) and previous.resolve() != dest.resolve()
                and (previous / "SKILL.md").is_file() and tree_digest(previous) == tree_digest(dest))


def folders(home: Path, enabled=DEFAULT_AGENTS) -> dict[str, Path]:
    return paths(home, enabled, "skills_dir")


def lock_file(home: Path) -> Path:
    """Where the `skills` CLI records what it installed: under XDG_STATE_HOME when that is set."""
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    return Path(state) / "skills/.skill-lock.json" if state else home / ".agents/.skill-lock.json"


def locked(home: Path) -> dict[str, dict]:
    """The skills a package installed, from the `skills` CLI's lock file."""
    p = lock_file(home)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError, AttributeError):
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
        paths += [root / x for x in extra if isinstance(x, str) and inside(root, root / x)]
    out = {}
    for path in paths:
        if not inside(root, path):
            continue
        if (path / "SKILL.md").is_file():
            if inside(root, path / "SKILL.md"):
                out.setdefault(path.name, path)
            continue
        for d in sorted(path.iterdir()) if path.is_dir() else []:
            if (not d.name.startswith(".") and inside(root, d)
                    and inside(root, d / "SKILL.md") and (d / "SKILL.md").is_file()):
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

    def remove_copy(dest: Path, reason: str) -> None:
        key = f"skill-copy:{dest.parent.relative_to(home)}:{dest.name}"
        what = (f"skill {dest.name} from {dest.parent} "
                f"({reason}{'; you edited this copy' if edited(dest, home) else ''})")
        if pending.due(key, what) and not dry:
            backups.save(dest)
            shutil.rmtree(dest)
            pending.removed(key)
            changes.append(f"removed {what}")

    from .duplicates import doubled, elsewhere, views  # they build on this module
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
                if not ((dest / "SKILL.md").is_file()
                        and made_for_each_tool(home, name, [(roots[source_dir], src), (roots[target], dest)])):
                    changes.append(f"WARN  {dest} exists and is not a copy agent-sync made; left alone")
                continue                                   # a tool's own version (an installer's): nothing to say
            if dest.exists() and in_source and not is_plugin_copy(dest):
                changes.append(f"WARN  {dest} is a copy from an earlier source tool; left alone")
                continue
            have = elsewhere(home, seen, target, name, src, readers=roots[target])
            if have:                                       # every tool reading `target` loads this very skill already
                if dest.exists():
                    remove_copy(dest, f"already loaded from {'; '.join(have)}")
                continue
            if twice := doubled(home, seen, target, name, src, readers=roots[target]):
                left_out.setdefault((target, tuple(twice)), []).append(name)
                continue                                   # another skill has its name: an existing copy stays
            mark += f"{DIGEST}{tree_digest(src)}\n"
            if dest.exists() and same_tree(src, dest):
                if marker_text(dest) != mark and not dry:
                    backups.save(dest)
                    (dest / MARKER).write_text(mark)       # same files from a new place (a plugin's new version)
                continue
            key = f"skill-copy:{target.relative_to(home)}:{name}"
            if dest.exists() and edited(dest, home):
                if not pending.due(key, f"your edits to the copy of skill {name} in {target} (its source changed; "
                                        "the new version replaces them)") or dry:
                    continue
            if not dry:
                backups.save(dest)
                target.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(prefix=f".{name}.agent-sync-", dir=target) as staged:
                    tmp = Path(staged) / name
                    shutil.copytree(src, tmp, symlinks=True, ignore=shutil.ignore_patterns(".DS_Store"))
                    (tmp / MARKER).write_text(mark)
                    old = Path(f"{staged}.previous")
                    if dest.exists():
                        dest.rename(old)
                    try:
                        tmp.rename(dest)
                    except OSError:
                        if old.exists():
                            old.rename(dest)
                        raise
                    if old.exists():
                        shutil.rmtree(old)
                pending.removed(key)
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
            why = "agent-sync no longer copies plugin skills" if plugin else "no longer in the source tool"
            remove_copy(d, why)
    for (target, twice), names in sorted(left_out.items()):
        have = " and ".join(TOOLS[tool].name for tool in twice)
        what = f"skill {names[0]}" if len(names) == 1 else f"{len(names)} skills ({', '.join(names[:8])})"
        changes.append(f"WARN  {what}: {have} also {'loads' if len(twice) == 1 else 'load'} a different skill of "
                       f"that name, so {target} gets no new copy and keeps any it has. Rename one to load both")
    return changes


def installed_name(name: str, meta: dict) -> str:
    return str(meta.get("skillName") or name).lower()


def install_name(name: str) -> str:
    return re.sub(r"[^a-z0-9._]+", "-", name.lower()).strip(".-")[:255] or "unnamed-skill"


def install_roots(home: Path, enabled) -> set[Path]:
    roots = set(folders(home, enabled).values())
    roots.add(home / ".agents/skills")
    roots.update(home / root for tool, root in EXTRA_ROOTS.items() if tool in enabled)
    if "claude" in enabled and (claude_home := os.environ.get("CLAUDE_CONFIG_DIR", "").strip()):
        roots.add(Path(claude_home) / "skills")
    return roots


def backup_installs(home: Path, enabled, names) -> None:
    backups.save(lock_file(home), follow=True)
    roots = install_roots(home, enabled)
    for name in names:
        if name in {"", ".", ".."} or "/" in name:
            continue
        for root in sorted(roots):
            backups.save(root / name)


def entry_state(path: Path) -> str | None:
    """What a skill folder entry holds: nothing, a link's target, or a digest of its files."""
    if path.is_symlink():
        return "link " + os.readlink(path)
    if path.is_dir():
        return "folder " + tree_digest(path)
    return "file " + hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def package_folders(lock: dict[str, dict], package: str) -> dict[str, str]:
    """folder name -> lock name of each skill the lock says `package` installed."""
    out = {}
    for name, meta in lock.items():
        if str(meta.get("source", "")).casefold() == package.casefold():   # GitHub names ignore case
            # the lock key is the installed name, even after --rename
            out[name] = out[install_name(name)] = name
    return out


def put_back(path: Path, saved: Path | None) -> None:
    """Replace what is at `path` with the saved copy, or remove it when there was nothing; never leaves it missing."""
    backups.save(path)
    aside = path.with_name(f".{path.name}.agent-sync-old")
    staged = path.with_name(f".{path.name}.agent-sync-new")
    for leftover in (aside, staged):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover)
        elif leftover.exists() or leftover.is_symlink():
            leftover.unlink()
    if saved is not None:
        copy_entry(saved, staged)
    if path.exists() or path.is_symlink():
        path.rename(aside)
    if saved is not None:
        staged.rename(path)
    if aside.is_dir() and not aside.is_symlink():
        shutil.rmtree(aside)
    elif aside.exists() or aside.is_symlink():
        aside.unlink()


def copy_entry(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        os.symlink(os.readlink(src), dest)
    elif src.is_dir():
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest)


def unlock(home: Path, before: dict[str, dict], names: set[str]) -> None:
    """Give the lock file's entries for `names` back the values they had before the install."""
    path = lock_file(home)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    entries = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return
    for name in names:
        if name in before:
            entries[name] = before[name]
        else:
            entries.pop(name, None)
    target = path.resolve()
    staged = target.with_name(f".{target.name}.agent-sync")
    staged.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(staged, target.stat().st_mode & 0o777)
    staged.replace(target)


class Guard:
    """What a guarded install did: `restored` holds (lock name, package) for each skill put back. Set `failed`
    when the install failed: the installer writes files before its lock file, so then every entry it changed is
    put back, credited or not."""

    def __init__(self) -> None:
        self.restored: list[tuple[str, str]] = []
        self.failed = False


@contextmanager
def guard_installs(home: Path, enabled, packages: set[str], allowed: set[str], work: Path) -> Iterator[Guard]:
    """Keep an install from `packages` from replacing skills those packages did not install, such as your own.
    The `skills` CLI decides which folders it writes, so this doesn't predict them: it saves every other skill
    entry first, and afterwards puts back each one the install changed and the lock file now credits to one of
    `packages`. `allowed`: names they may replace (you approved it). Names match without regard to case, as macOS
    folders do."""
    before = locked(home)
    roots = sorted(install_roots(home, enabled))
    canonical = home / ".agents/skills"
    owned = {name.casefold() for package in packages for name in package_folders(before, package)}
    free = {name.casefold() for name in allowed} | {install_name(name).casefold() for name in allowed}
    entries = {root: [entry for entry in root.iterdir() if not entry.name.startswith(".")] if root.is_dir() else []
               for root in roots}
    foreign = {entry.name for found in entries.values() for entry in found} - {
        entry.name for found in entries.values() for entry in found if entry.name.casefold() in owned | free}

    def package_copy(entry: Path) -> bool:
        """The package's own install: its folder in ~/.agents/skills, a link to it, an exact copy of it, or the
        only copy (an install for one agent copies straight into that agent's folder)."""
        own = canonical / entry.name
        return (entry.parent == canonical or entry.resolve() == own.resolve() or not own.exists()
                or own.is_dir() and entry_state(entry) == entry_state(own))

    watched = {root / name for root in roots for name in foreign}
    watched |= {entry for root in roots for entry in entries[root]
                if entry.name.casefold() in owned - free and not package_copy(entry)}
    guard = Guard()
    work.mkdir(parents=True, exist_ok=True)
    for leftover in work.glob("skill-guard.*"):        # from a run that was killed; runs never overlap
        if leftover.is_dir() and not leftover.is_symlink():
            backups.save(leftover)                      # it may hold the only copy of a skill the install replaced
            shutil.rmtree(leftover)
    with TemporaryDirectory(dir=work, prefix="skill-guard.") as tmp:
        saved: dict[Path, tuple[str | None, Path | None]] = {}
        for index, path in enumerate(sorted(watched)):
            state = entry_state(path)
            copy = Path(tmp) / str(index) / path.name if state is not None else None
            if copy is not None:
                copy_entry(path, copy)
            saved[path] = state, copy
        try:
            yield guard
        finally:
            after = locked(home)
            credited = {name.casefold(): (key, package) for package in sorted(packages)
                        for name, key in package_folders(after, package).items()}
            changed = {path.name.casefold() for path, (state, _) in saved.items()
                       if (guard.failed or path.name.casefold() in credited) and entry_state(path) != state}
            for path, (state, copy) in sorted(saved.items()):
                if path.name.casefold() in changed and entry_state(path) != state:
                    put_back(path, copy)
            if new := {credited[name][0] for name in changed - owned if name in credited}:
                unlock(home, before, new)
            sole = next(iter(packages)) if len(packages) == 1 else None
            guard.restored += sorted({credited[name] if name in credited else (name, sole) for name in changed
                                      if name in credited or sole})


def shipped(checkout: Path) -> list[str]:
    from .duplicates import skill_name, walk

    root = checkout / "skills" if (checkout / "skills").is_dir() else checkout
    found = walk(root) or ([root] if (root / "SKILL.md").is_file() else [])
    return sorted({skill_name(d) for d in found if not internal(d)})


def internal(folder: Path) -> bool:
    """A skill marked `internal: true`, which the skills CLI installs only with INSTALL_INTERNAL_SKILLS set."""
    if os.environ.get("INSTALL_INTERNAL_SKILLS") in {"1", "true"}:
        return False
    try:
        text = (folder / "SKILL.md").read_text(errors="replace").lstrip("\ufeff")
    except OSError:
        return False
    front = re.match(r"^---\n(.*?)\n---", text, re.S)
    metadata = front and re.search(r"(?m)^metadata:[ \t]*(\{[^\n]*\}|\n(?:[ \t]+[^\n]*\n?)*)", front[1])
    return bool(metadata and re.search(r"""(?:^|[\s{,])internal\s*:\s*["']?(?:true|1)\b""", metadata[1]))


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
