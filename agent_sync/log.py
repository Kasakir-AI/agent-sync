"""A small timestamped log that keeps only the last N days."""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from .textmerge import load_baselines, save_baselines

STAMP = "%Y-%m-%d %H:%M:%S"
LOG_STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d"
WARNING = re.compile(rf"^({LOG_STAMP}) WARN  ([^\n]*(?:\n\t[^\n]*)*)$", re.MULTILINE)
RUN_END = re.compile(rf"^{LOG_STAMP} done  agent-sync finished[^\n]*$", re.MULTILINE)
ACK_AGE = 30 * 24 * 3600
ACK_FILE = "acknowledged-warnings.json"


class WarningAcks:
    def __init__(self, state_dir: Path):
        self.path = state_dir / ACK_FILE
        cutoff = time.time() - ACK_AGE
        self.entries = {
            key: stamp for key, stamp in ({} if self.path.is_symlink() else load_baselines(self.path)).items()
            if type(stamp) in (int, float) and stamp > cutoff
        }

    @staticmethod
    def key(warning: str) -> str:
        return sha256(warning.encode()).hexdigest()

    def unseen(self, warnings: list[str]) -> list[str]:
        return [warning for warning in dict.fromkeys(warnings) if self.key(warning) not in self.entries]

    def save(self, warnings: list[str]) -> None:
        self.entries.update(dict.fromkeys(map(self.key, warnings), time.time()))
        if not self.path.is_symlink() and (self.entries or self.path.exists()):
            save_baselines(self.path, self.entries)


class Log:
    def __init__(self, path: Path, echo: bool = False, write: bool = True):
        self.path, self.echo, self.write, self.kinds = path, echo, write, Counter()
        self.warnings: list[str] = []
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind: str, message: str) -> None:
        if kind == "WARN":
            self.warnings.append(message)
            message = message.replace("\n", "\n\t")
        line = f"{datetime.now().strftime(STAMP)} {kind:<5} {message}"
        if self.write:
            with self.path.open("a") as f:
                f.write(line + "\n")
        self.kinds[kind] += 1
        if self.echo and kind != "WARN":
            print(line, file=sys.stdout, flush=True)

    def recent_warnings(self, days: int) -> list[str]:
        """Warnings from the latest finished run and anything logged since. A run repeats every warning that still
        holds, so an older run's warning that the latest run did not repeat has been resolved."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return []
        if len(ends := [match.end() for match in RUN_END.finditer(text)]) > 1:
            text = text[ends[-2]:]
        cutoff = (datetime.now() - timedelta(days=days)).strftime(STAMP)
        return [match[2].replace("\n\t", "\n") for match in WARNING.finditer(text) if match[1] >= cutoff]

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

    def prune(self, days: int, now: datetime | None = None, *, dry: bool = False) -> int:
        """Drop lines older than `days`. Rewrites the file in place (same inode, so a viewer keeps following)."""
        if self.path.is_symlink():
            self("WARN", f"skipped symlink {self.path}")
            return 0
        if not self.path.is_file():
            return 0
        cutoff = ((now or datetime.now()) - timedelta(days=days)).strftime(STAMP)
        lines = self.path.read_text().splitlines(keepends=True)
        kept = []
        retain = True
        for line in lines:
            if re.match(LOG_STAMP, line):
                retain = line[:19] >= cutoff
            if retain:
                kept.append(line)
        if not dry and len(kept) != len(lines):
            with self.path.open("r+") as f:
                f.write("".join(kept))
                f.truncate()
                os.fsync(f.fileno())
        return len(lines) - len(kept)
