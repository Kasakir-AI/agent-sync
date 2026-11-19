"""Your own skills: the ones in the source tool's skills folder that no skill package installed.

- Claude Code reads ~/.claude/skills; Codex and Cursor both read ~/.agents/skills.
- Package skills are installed for every tool by the `skills` CLI already (recorded in ~/.agents/.skill-lock.json),
  so only the rest is copied here.
A copy carries a `.agent-sync` marker. A copy with the marker is updated when the source changes and removed when the
source skill is gone; a folder without it is yours and is never touched.
"""
from __future__ import annotations

import filecmp
import json
import shutil
from pathlib import Path

MARKER = ".agent-sync"


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


def same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b, ignore=[MARKER, ".DS_Store"])
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    if filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)[1:] != ([], []):
        return False
    return all(same_tree(a / d, b / d) for d in cmp.common_dirs)


def mirror(home: Path, source: str, skip: list[str] | None = None, dry: bool = False) -> list[str]:
    wanted = own(home, source, skip or [])
    def verb(done: str, would: str) -> str:
        return would if dry else done

    changes: list[str] = []
    targets = set(folders(home).values()) - {folders(home)[source]}
    for target in sorted(targets):
        for name, src in wanted.items():
            dest = target / name
            if dest.is_symlink() and dest.resolve() == src.resolve():
                continue                                   # already the same folder
            if dest.exists() and not (dest / MARKER).exists():
                changes.append(f"WARN  {dest} exists and is not a copy agent-sync made; left alone")
                continue
            if dest.exists() and same_tree(src, dest):
                continue
            if not dry:
                tmp = dest.with_name(f".{name}.agent-sync-new")
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.copytree(src, tmp, symlinks=True, ignore=shutil.ignore_patterns(".DS_Store"))
                (tmp / MARKER).write_text(f"copied by agent-sync from {src}\n")
                if dest.exists():
                    shutil.rmtree(dest)
                tmp.rename(dest)
            changes.append(f"{verb('copied', 'would copy')} skill {name} to {target}")
        for d in sorted(target.iterdir()) if target.is_dir() else []:
            if (d / MARKER).is_file() and not d.is_symlink() and d.name not in wanted:
                if not dry:
                    shutil.rmtree(d)
                changes.append(f"{verb('removed', 'would remove')} skill {d.name} from {target} "
                               "(no longer in the source tool)")
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
