"""A small timestamped log that keeps only the last N days."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

STAMP = "%Y-%m-%d %H:%M:%S"


class Log:
    def __init__(self, path: Path, echo: bool = False, write: bool = True):
        self.path, self.echo, self.write, self.problems = path, echo, write, 0
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind: str, message: str) -> None:
        line = f"{datetime.now().strftime(STAMP)} {kind:<5} {message}"
        if self.write:
            with self.path.open("a") as f:
                f.write(line + "\n")
        if kind == "FAIL":
            self.problems += 1
        if self.echo:
            print(line, file=sys.stdout, flush=True)

    def prune(self, days: int, now: datetime | None = None) -> int:
        """Drop lines older than `days`. Rewrites the file in place (same inode, so a viewer keeps following)."""
        if not self.path.exists():
            return 0
        cutoff = ((now or datetime.now()) - timedelta(days=days)).strftime(STAMP)
        lines = self.path.read_text().splitlines(keepends=True)
        kept = [l for l in lines if l[:19] >= cutoff]
        if len(kept) != len(lines):
            with self.path.open("r+") as f:
                f.write("".join(kept))
                f.truncate()
                os.fsync(f.fileno())
        return len(lines) - len(kept)
