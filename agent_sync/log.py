"""A small timestamped log that keeps only the last N days."""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

STAMP = "%Y-%m-%d %H:%M:%S"


class Log:
    def __init__(self, path: Path, echo: bool = False, write: bool = True):
        self.path, self.echo, self.write, self.kinds = path, echo, write, Counter()
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind: str, message: str) -> None:
        line = f"{datetime.now().strftime(STAMP)} {kind:<5} {message}"
        if self.write:
            with self.path.open("a") as f:
                f.write(line + "\n")
        self.kinds[kind] += 1
        if self.echo:
            print(line, file=sys.stdout, flush=True)

    @property
    def problems(self) -> int:
        return self.kinds["FAIL"]

    def lines(self, lines, kind: str = "sync") -> None:
        """Log the lines a mirror returned. A line may name its own kind first ("WARN  ...", "skip  ...")."""
        for line in lines:
            head, _, rest = line.partition("  ")
            if head in ("WARN", "skip", "info", "FAIL", "dry"):
                self(head, rest)
            else:
                self(kind, line)

    def summary(self) -> str:
        """What a run did, for its last line: "2 changes, 3 commands run, 1 warning, no problems". Changes are the
        files agent-sync wrote; commands are the installs, updates and extra steps it ran (or, dry, would run)."""
        def count(n: int, word: str) -> str:
            return f"{n or 'no'} {word}{'' if n == 1 else 's'}"
        commands = self.kinds["ran"] + self.kinds["dry"]
        return ", ".join((count(self.kinds["sync"], "change"), count(commands, "command") + " run",
                          count(self.kinds["WARN"], "warning"), count(self.problems, "problem")))

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
