"""Keep the originals of files changed during upkeep."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISLNK, S_ISREG

FOLDER_NAME = re.compile(r"(?:run|review|repair|dedupe|pins|restore|skill)-([0-9]{8}-[0-9]{6})")
MIB = 1024 * 1024


@dataclass
class Run:
    home: Path
    root: Path
    now: datetime
    folder: Path | None = None
    seen: set[Path] = field(default_factory=set)
    prefix: str = "run"
    originals: dict[Path, Path] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        if path in self.seen or any(parent in self.seen for parent in path.parents):
            return
        if path.exists() or path.is_symlink():
            if not path.is_symlink() and self.root.resolve().is_relative_to(path.resolve()):
                raise OSError(f"cannot back up {path} inside itself")
            if self.folder is None:
                self.folder = new_folder(self.root, self.prefix, self.now)
            self.originals[self.folder / relative_path(path, self.home)] = path
            copy(path, self.home, self.folder, seen=self.seen)
        self.seen.add(path)

    def finish(self) -> None:
        if self.folder is None or self.root.is_symlink() or self.folder.is_symlink():
            return
        discard_unchanged(self.folder, None, self.originals)
        if not self.folder.exists():
            self.folder = None


current: ContextVar[Run | None] = ContextVar("backups", default=None)


def new_folder(root: Path, prefix: str, stamp: datetime | None = None) -> Path:
    if root.is_symlink():
        raise OSError(f"cannot write backups to symlink {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = stamp or datetime.now()
    while True:
        folder = root / f"{prefix}-{stamp:%Y%m%d-%H%M%S}"
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            stamp += timedelta(seconds=1)
        else:
            return folder


def relative_path(path: Path, home: Path) -> Path:
    original = path.parent.resolve() / path.name if path.is_symlink() else path.resolve()
    resolved_home = home.resolve()
    return (original.relative_to(resolved_home) if original.is_relative_to(resolved_home)
            else Path("_outside", *original.parts[1:]))


def copy(path: Path, home: Path, folder: Path, *, seen: set[Path] | None = None) -> None:
    dest = folder.resolve() / relative_path(path, home)
    if not (path.exists() or path.is_symlink()):
        return
    if folder.is_symlink() or dest.parent.resolve() != dest.parent:
        raise OSError(f"cannot copy {path} through a symlink inside its backup")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        inside = [other for other in seen or () if other.is_relative_to(path) and other != path]
        if not inside and not (dest.exists() or dest.is_symlink()) and clone(path, dest):
            return

        def ignore(source: str, names: list[str]) -> set[str]:
            return {name for name in names if Path(source) / name in (seen or ())}

        shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=seen is not None, ignore=ignore)
    elif not (dest.exists() or dest.is_symlink()):
        try:
            shutil.copy2(path, dest, follow_symlinks=False)
        except OSError:
            dest.unlink(missing_ok=True)
            raise


def clone(path: Path, dest: Path) -> bool:
    """Copy a folder as an APFS clone (macOS clonefile: the copy shares the original's blocks, so it costs almost no
    space until one of them changes). False when it can't, after removing any part copy."""
    if sys.platform != "darwin":
        return False
    try:
        done = subprocess.run(["/bin/cp", "-c", "-R", "-P", str(path), str(dest)], capture_output=True,
                              timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        done = False
    if not done and (dest.exists() or dest.is_symlink()):
        shutil.rmtree(dest, ignore_errors=True)
    return done


def save(path: Path, *, follow: bool = False) -> None:
    if run := current.get():
        original = path.parent.resolve() / path.name
        run.save(original)
        if follow and original.is_symlink():
            run.save(original.resolve())


@contextmanager
def during(home: Path, root: Path, *, now: datetime | None = None, prefix: str = "run"):
    run = Run(home, root, now or datetime.now(), prefix=prefix)
    token = current.set(run)
    try:
        yield run
    finally:
        current.reset(token)
        run.finish()


def discard_unchanged(saved: Path, original: Path | None, originals: dict[Path, Path]) -> None:
    if saved in originals:
        original = originals[saved]
        if original.parent.resolve() != original.parent:
            original = None
    saved_stat = saved.lstat()
    try:
        live_stat = original.lstat() if original is not None else None
    except (FileNotFoundError, NotADirectoryError):
        live_stat = None
    if S_ISDIR(saved_stat.st_mode):
        live_dir = original if live_stat and S_ISDIR(live_stat.st_mode) else None
        for child in saved.iterdir():
            discard_unchanged(child, live_dir / child.name if live_dir is not None else None, originals)
        if not any(saved.iterdir()):
            saved.rmdir()
    elif original is not None and live_stat is not None:
        if S_ISLNK(saved_stat.st_mode) and S_ISLNK(live_stat.st_mode):
            if saved.readlink() == original.readlink():
                saved.unlink()
        elif (S_ISREG(saved_stat.st_mode) and S_ISREG(live_stat.st_mode)
              and S_IMODE(saved_stat.st_mode) == S_IMODE(live_stat.st_mode)
              and saved_stat.st_size == live_stat.st_size and same_bytes(saved, original)):
            saved.unlink()


def same_bytes(saved: Path, original: Path) -> bool:
    with saved.open("rb") as copy_file, original.open("rb") as live:
        while chunk := copy_file.read(MIB):
            if chunk != live.read(MIB):
                return False
        return not live.read(1)


def folder_entries(root: Path, warn: Callable[[str], None] | None = None) -> list[tuple[Path, bool, int]]:
    if root.is_symlink():
        if warn:
            warn(f"skipped symlink {root}")
        return []
    if not root.is_dir():
        return []
    entries = []
    for path in root.iterdir():
        info = path.lstat()
        if S_ISLNK(info.st_mode):
            if warn:
                warn(f"skipped symlink {path}")
            continue
        folder = S_ISDIR(info.st_mode)
        entries.append((path, folder, tree_size(path) if folder else info.st_size))
    return entries


def tree_size(folder: Path) -> int:
    size = 0
    for path in folder.iterdir():
        info = path.lstat()
        if S_ISDIR(info.st_mode):
            size += tree_size(path)
        elif S_ISREG(info.st_mode):
            size += info.st_size
    return size


def prune(root: Path, days: int, max_mb: int, current: Path | None, dry: bool, log,
          *, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now()) - timedelta(days=days)
    entries = folder_entries(root, lambda message: log("WARN", message))
    total = sum(size for _, _, size in entries)
    candidates = []
    for path, folder, size in entries:
        if folder and path != current and (match := FOLDER_NAME.fullmatch(path.name)):
            try:
                stamp = datetime.strptime(match[1], "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            candidates.append((stamp, path, size))
    limit = max_mb * MIB

    def eviction_order(entry):
        stamp, path, size = entry
        return (stamp >= cutoff, stamp >= cutoff and size <= limit // 10, stamp, path)

    for stamp, path, size in sorted(candidates, key=eviction_order):
        if stamp >= cutoff and total <= limit:
            break
        if path.is_symlink():
            log("WARN", f"skipped symlink {path}")
            continue
        if not dry:
            shutil.rmtree(path)
        log("info", f"{'Would delete' if dry else 'Deleted'} backup {path}")
        total -= size


def backup_folders(root: Path) -> list[Path]:
    """Backup folders agent-sync made, newest first."""
    found = [folder for folder in root.iterdir() if folder.is_dir() and not folder.is_symlink()
             and FOLDER_NAME.fullmatch(folder.name)] if root.is_dir() else []
    return sorted(found, key=lambda folder: (FOLDER_NAME.fullmatch(folder.name)[1], folder.name), reverse=True)


def original_path(saved: Path, folder: Path, home: Path) -> Path:
    """Where a saved file came from: under your home folder, or the absolute path kept under _outside/."""
    relative = saved.relative_to(folder)
    return Path("/", *relative.parts[1:]) if relative.parts[0] == "_outside" else home.resolve() / relative


def saved_files(folder: Path) -> list[Path]:
    """The files and links a backup holds. Folders are restored file by file, so nothing added since is removed."""
    return sorted(path for path in folder.rglob("*") if path.is_symlink() or path.is_file())


def restore(folder: Path, home: Path, root: Path, only: list[str], dry: bool) -> list[str]:
    """Put a backup's files back where they came from. The files they replace are saved first, in a restore-
    backup of their own, so a restore can be undone the same way. `only`: paths (or folders) to limit it to."""
    wanted = [Path(os.path.realpath(Path(item).expanduser())) for item in only]
    changes = []
    with during(home, root, prefix="restore"):
        for saved in saved_files(folder):
            target = original_path(saved, folder, home)
            if wanted and not any(target == item or target.is_relative_to(item) for item in wanted):
                continue
            if target.parent.resolve() != target.parent:      # a folder on the way is a link to elsewhere now
                changes.append(f"WARN  skipped {target}: a folder in its path is now a link")
                continue
            if target.is_symlink() == saved.is_symlink() and (
                    os.readlink(target) == os.readlink(saved) if saved.is_symlink()
                    else target.is_file() and same_bytes(saved, target)
                    and S_IMODE(target.stat().st_mode) == S_IMODE(saved.stat().st_mode)):
                continue
            if dry:
                changes.append(f"would restore {target}")
                continue
            if target.is_dir() and not target.is_symlink():
                changes.append(f"WARN  skipped {target}: a folder is there now")
                continue
            try:
                save(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = target.with_name(f".{target.name}.agent-sync-restore")
                if staged.exists() or staged.is_symlink():
                    staged.unlink()
                if saved.is_symlink():
                    os.symlink(os.readlink(saved), staged)
                else:
                    shutil.copy2(saved, staged)
                os.replace(staged, target)
            except OSError as error:          # a folder on the way is a file now, say: skip it, restore the rest
                changes.append(f"WARN  skipped {target}: {error.strerror or error}")
                continue
            changes.append(f"restored {target}")
    return changes
