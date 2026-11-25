from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .textmerge import load_baselines, save_baselines

WAIT = 20 * 3600


@dataclass(frozen=True)
class Run:
    log: Callable[[str, str], None]
    dry: bool
    now: float
    keep: frozenset[str]
    previous: dict
    seen: dict = field(default_factory=dict)
    approvals: frozenset[str] = frozenset()

    def keeps(self, key: str) -> bool:
        return key in self.keep or key.rsplit(":", 1)[-1] in self.keep

    def due(self, key: str, what: str, *, approval: str | None = None) -> bool:
        if self.keeps(key):
            return False
        if approval is not None:
            token = f"{key}@{approval}"
            if token in self.approvals:
                return True
            if key not in self.seen:
                self.log("dry" if self.dry else "WARN",
                         f'approval required to {what}. Run `agent-sync run --force --approve-status-line {token}` '
                         f'to replace it, or add "{key}" to keep_installed to keep it.')
            self.seen[key] = {"what": what, "approval": token}
            return False
        first = self.previous.get(key, {}).get("first") if isinstance(self.previous.get(key), dict) else None
        if not isinstance(first, (int, float)):
            if key not in self.seen:
                if self.dry:
                    self.log("dry", f"would announce removal of {what}")
                else:
                    date = datetime.fromtimestamp(self.now + WAIT).strftime("%Y-%m-%d %H:%M:%S")
                    self.log("WARN", f'will remove {what} at the first run after {date}; '
                             f'to keep it, add "{key}" to keep_installed')
            self.seen[key] = {"first": self.now, "what": what}
            return False
        self.seen[key] = {"first": first, "what": what}
        if self.now - first < WAIT:
            return False
        if self.dry:
            self.log("dry", f"would remove {what}")
        return True


current: ContextVar[Run | None] = ContextVar("pending", default=None)


def keeps(key: str) -> bool:
    run = current.get()
    return run is not None and run.keeps(key)


def due(key: str, what: str, *, approval: str | None = None) -> bool:
    run = current.get()
    return run is not None and run.due(key, what, approval=approval)


@contextmanager
def during(state_dir: Path, keep: list[str], log, dry: bool, *, now: float | None = None,
           approvals: tuple[str, ...] = ()):
    path = state_dir / "pending-removals.json"
    previous = load_baselines(path)
    run = Run(log, dry, time.time() if now is None else now, frozenset(keep), previous,
              approvals=frozenset(approvals))
    token = current.set(run)
    try:
        yield run
        if not dry and run.seen != previous:
            save_baselines(path, run.seen)
    finally:
        current.reset(token)
