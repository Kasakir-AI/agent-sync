"""Keep upkeep and review writes from running at the same time."""
from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

MAX_AGE = 7200


@contextmanager
def run_lock(path: Path) -> Iterator[bool]:
    """Yield whether the caller holds the lock. Recover empty directories left by crashed runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            # Hold this through upkeep and its dialogs. Only abandoned directories can age out.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            age = time.time() - path.lstat().st_mtime
        except FileNotFoundError:
            pass
        else:
            if age < MAX_AGE:
                yield False
                return
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
