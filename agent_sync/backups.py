"""Keep the originals of files changed during upkeep."""
from __future__ import annotations

import re
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

RUN_NAME = re.compile(r"run-\d{8}-\d{6}")


@dataclass
class Run:
    home: Path
    root: Path
    now: datetime
    folder: Path | None = None
    seen: set[Path] = field(default_factory=set)

    def save(self, path: Path) -> None:
        if path in self.seen or any(parent in self.seen for parent in path.parents):
            return
        if path.exists() or path.is_symlink():
            if not path.is_symlink() and self.root.resolve().is_relative_to(path.resolve()):
                raise OSError(f"cannot back up {path} inside itself")
            if self.folder is None:
                self.folder = new_folder(self.root, "run", self.now)
            copy(path, self.home, self.folder, seen=self.seen)
        self.seen.add(path)


current: ContextVar[Run | None] = ContextVar("backups", default=None)


def new_folder(root: Path, prefix: str, stamp: datetime | None = None) -> Path:
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


def copy(path: Path, home: Path, folder: Path, *, seen: set[Path] | None = None) -> None:
    relative = path.relative_to(home) if path.is_relative_to(home) else Path("_outside", *path.parts[1:])
    dest = folder / relative
    if not (path.exists() or path.is_symlink()):
        return
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        def ignore(source: str, names: list[str]) -> set[str]:
            return {name for name in names if Path(source) / name in (seen or ())}

        shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=seen is not None, ignore=ignore)
    elif not (dest.exists() or dest.is_symlink()):
        try:
            shutil.copy2(path, dest, follow_symlinks=False)
        except OSError:
            dest.unlink(missing_ok=True)
            raise


def save(path: Path, *, follow: bool = False) -> None:
    if run := current.get():
        resolved = path.resolve() if follow else path.parent.resolve() / path.name
        if not any(parent.is_symlink() for parent in path.parents):
            run.save(path)
        if resolved != path:
            run.save(resolved)


@contextmanager
def during(home: Path, root: Path, days: int, *, now: datetime | None = None):
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    if root.is_dir() and not root.is_symlink():
        for folder in root.iterdir():
            if not RUN_NAME.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
                continue
            try:
                stamp = datetime.strptime(folder.name, "run-%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if stamp < cutoff:
                shutil.rmtree(folder)
    run = Run(home, root, now)
    token = current.set(run)
    try:
        yield run
    finally:
        current.reset(token)
