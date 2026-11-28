"""Keep upkeep and review writes from running at the same time."""
from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .textmerge import private_file


@contextmanager
def run_lock(path: Path) -> Iterator[bool]:
    """Yield whether the caller holds the lock. Recover empty directories left by crashed runs."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            # Hold this through upkeep and its dialogs.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        os.fchmod(fd, 0o700)
        for entry in path.parent.iterdir():
            private_file(entry)
        review = path.parent / "review"
        if not review.is_symlink() and review.is_dir():
            review.chmod(0o700)
            private_file(review / "baselines.json")
        if not path.is_symlink() and path.is_dir():
            with suppress(FileNotFoundError):
                path.rmdir()
        try:
            path.mkdir()
        except FileExistsError:
            yield False
            return
        try:
            yield True
        finally:
            with suppress(OSError):
                path.rmdir()
    finally:
        os.close(fd)
