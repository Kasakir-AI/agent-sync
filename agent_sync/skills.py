"""Your own skills: the ones in the source tool's skills folder that no skill package installed.

- Claude Code reads ~/.claude/skills; Codex and Cursor both read ~/.agents/skills.
- Package skills are installed for every tool by the `skills` CLI already (recorded in ~/.agents/.skill-lock.json),
  so only the rest is copied here.
A copy carries a `.agent-sync` marker. Upkeep replaces or removes exact copies as their source changes. It keeps
copies where review preserved local content and leaves folders without a marker alone.

Skills inside Claude Code plugins are copied to ~/.agents/skills (Codex and Cursor) only for the plugins you name in
`plugin_skills`: most plugins only make sense in Claude Code. Their markers name the plugin.
"""
from __future__ import annotations

import filecmp
import json
import os
import shutil
from pathlib import Path

MARKER = ".agent-sync"
COPY_MARK = "copied by agent-sync from "
PLUGIN_MARK = COPY_MARK + "Claude Code plugin "
REVIEW_MARK = "kept by agent-sync review\n"


def folders(home: Path) -> dict[str, Path]:
    return {"claude": home / ".claude/skills", "codex": home / ".agents/skills", "cursor": home / ".agents/skills"}


def locked(home: Path) -> dict[str, dict]:
    """The skills a package installed, from the `skills` CLI's lock file."""
    p = home / ".agents/.skill-lock.json"
    try:
        return json.loads(p.read_text()).get("skills") or {}
    except (OSError, ValueError):
        return {}


def own(home: Path, source: str, skip: list[str]) -> dict[str, Path]:
    """name -> folder of every skill in the source tool that came from no package and is not itself a copy."""
    folder, pkg = folders(home)[source], locked(home)
    out = {}
    for d in sorted(folder.iterdir()) if folder.is_dir() else []:
        if d.name.startswith(".") or d.name in pkg or d.name in skip or d.is_symlink():
            continue
        if (d / "SKILL.md").is_file() and not (d / MARKER).exists():
            out[d.name] = d
    return out


def plugins(home: Path) -> dict[str, Path]:
    """plugin@marketplace -> folder of every Claude Code plugin installed for your user and not switched off."""
    try:
        installed = json.loads((home / ".claude/plugins/installed_plugins.json").read_text()).get("plugins") or {}
    except (OSError, ValueError):
        return {}
    try:
        enabled = json.loads((home / ".claude/settings.json").read_text()).get("enabledPlugins") or {}
    except (OSError, ValueError):
        enabled = {}
    out = {}
    for pid, installs in sorted(installed.items()):
        for meta in installs if isinstance(installs, list) else []:
            root = Path(meta.get("installPath") or "")
            if meta.get("scope") == "user" and enabled.get(pid) is not False and root.is_dir():
                out[pid] = root
    return out


def plugin_skill_dirs(root: Path) -> dict[str, Path]:
    """name -> folder of each skill in a plugin: its skills/ folder plus the `skills` paths in its plugin.json."""
    paths = [root / "skills"]
    try:
        extra = json.loads((root / ".claude-plugin/plugin.json").read_text()).get("skills") or []
    except (OSError, ValueError):
        extra = []
    paths += [root / x for x in ([extra] if isinstance(extra, str) else extra) if isinstance(x, str)]
    out = {}
    for path in paths:
        if (path / "SKILL.md").is_file():
            out.setdefault(path.name, path)
            continue
        for d in sorted(path.iterdir()) if path.is_dir() else []:
            if not d.name.startswith(".") and (d / "SKILL.md").is_file():
                out.setdefault(d.name, d)
    return out


def from_plugins(home: Path, chosen: list[str], skip: list[str]) -> tuple[dict[str, tuple[str, Path]], list[str]]:
    """The skills of the plugins in `chosen` (plugin@marketplace, or just the plugin's name):
    name -> (plugin, folder), and a WARN line for each choice that matches nothing or name used twice."""
    found, warnings, pkg = plugins(home), [], locked(home)
    out: dict[str, tuple[str, Path]] = {}
    for choice in chosen:
        hits = [pid for pid in found if choice in (pid, pid.split("@")[0])]
        if not hits:
            warnings.append(f"WARN  plugin_skills: {choice} is not a Claude Code plugin installed for your user "
                            "(or it is switched off)")
        for pid in hits:
            for name, d in plugin_skill_dirs(found[pid]).items():
                if name in skip or name in pkg:
                    continue
                if name in out and out[name][0] != pid:
                    warnings.append(f"WARN  skill {name} is in plugins {out[name][0]} and {pid}; "
                                    f"only the one from {out[name][0]} is copied")
                    continue
                out[name] = (pid, d)
    return out, warnings


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


def mirror(home: Path, source: str, skip: list[str] | None = None, dry: bool = False,
           plugin_skills: list[str] | None = None) -> list[str]:
    skip = skip or []
    def verb(done: str, would: str) -> str:
        return would if dry else done

    # target folder -> name -> (the folder copied, its marker text, a note for the change line)
    plan: dict[Path, dict[str, tuple[Path, str, str]]] = {}
    mine = own(home, source, skip)
    for target in set(folders(home).values()) - {folders(home)[source]}:
        plan[target] = {n: (d, f"{COPY_MARK}{d}\n", "") for n, d in mine.items()}
    shared = folders(home)["codex"]
    offered, changes = from_plugins(home, plugin_skills or [], skip)
    plan.setdefault(shared, {})
    for name, (pid, d) in offered.items():
        if name in plan[shared]:
            changes.append(f"WARN  skill {name} from plugin {pid} has the name of one of your own skills; "
                           "only your own is copied")
        else:
            plan[shared][name] = (d, f"{PLUGIN_MARK}{pid}: {d}\n", f" (from plugin {pid})")

    for target in sorted(plan):
        wanted, in_source = plan[target], target == folders(home)[source]
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
                if not made_for_each_tool(home, name, [(("claude",), src), (("codex", "cursor"), dest)]):
                    changes.append(f"WARN  {dest} exists and is not a copy agent-sync made; left alone")
                continue                                   # a tool's own version (an installer's): nothing to say
            if dest.exists() and in_source and not is_plugin_copy(dest):
                changes.append(f"WARN  {dest} is a copy from an earlier source tool; left alone")
                continue
            if dest.exists() and same_tree(src, dest) and (dest / MARKER).read_text() == mark:
                continue
            if not dry:
                tmp = dest.with_name(f".{name}.agent-sync-new")
                shutil.rmtree(tmp, ignore_errors=True)
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
                shutil.rmtree(d)
            why = "its plugin is no longer chosen or installed" if plugin else "no longer in the source tool"
            changes.append(f"{verb('removed', 'would remove')} skill {d.name} from {target} ({why})")
    return changes


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
