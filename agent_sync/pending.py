"""Require approval before upkeep removes an item or installs a new major version."""
from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from .textmerge import load_baselines, save_baselines

FILENAME = "pending-removals.json"


class Removal(TypedDict):
    first: float
    what: str
    approved: bool


def load(state_dir: Path) -> dict[str, Removal]:
    return {
        key: Removal(first=item["first"], what=item["what"], approved=item.get("approved") is True)
        for key, item in load_baselines(state_dir / FILENAME).items()
        if isinstance(item, dict) and type(item.get("first")) in (int, float)
        and isinstance(item.get("what"), str)
    }


@dataclass
class Run:
    log: Callable[[str, str], None]
    dry: bool
    now: float
    keep: frozenset[str]
    previous: dict[str, Removal]
    seen: dict[str, Removal] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    completed: set[str] | None = None
    step_incomplete: bool = False
    plugin_pairs: set[tuple[str, frozenset[str]]] = field(default_factory=set)

    def entries(self) -> dict[str, Removal]:
        """Keep unobserved items until their owning step has completed."""
        if self.completed is None:
            return self.seen
        kept = {key: item for key, item in self.previous.items()
                if key not in self.removed
                and not any(key == owner or key.startswith(f"{owner}:") for owner in self.completed)}
        return {**kept, **self.seen}

    def keeps(self, key: str) -> bool:
        return (key in self.keep or key.rsplit(":", 1)[-1] in self.keep
                or any(prefix and (key.startswith(prefix) if prefix.endswith(":") else
                                   key.startswith((f"{prefix}:", f"{prefix}@")))
                       for prefix in self.keep))

    def due(self, key: str, what: str, *, warn: bool = True, by_key: bool = False) -> bool:
        """The change is approved. `by_key`: an approval stays when `what` changes (an upgrade's versions move)."""
        if self.keeps(key) or key in self.removed:
            return False
        item = self.seen.get(key) or self.previous.get(key)
        if item is None or item["what"] != what and not by_key:
            item = Removal(first=self.now, what=what, approved=False)
            if warn and not self.dry:
                self.log("WARN", f"approval required to {change(what)}")
        self.seen[key] = Removal(first=item["first"], what=what, approved=item["approved"])
        if self.dry and item["approved"]:
            self.log("dry", f"would {change(what)}")
        return item["approved"]

    def questions(self) -> dict[str, str]:
        return {key: item["what"] for key, item in self.seen.items() if not item["approved"]}


def change(what: str) -> str:
    """What approving a question does: an upgrade names itself; anything else is a removal."""
    return what if what.startswith("upgrade ") else f"remove {what}"


def question(what: str) -> str:
    text = change(what)
    return f"{text[:1].upper()}{text[1:]}?"


current: ContextVar[Run | None] = ContextVar("pending", default=None)


def incomplete() -> None:
    if (run := current.get()) is not None:
        run.step_incomplete = True


def complete(owner: str) -> None:
    if (run := current.get()) is not None and run.completed is not None:
        run.completed.add(owner)


def retry(*namespaces: str) -> None:
    if (run := current.get()) is not None:
        for key in list(run.seen):
            if key.partition(":")[0] in namespaces:
                del run.seen[key]


def plugin_replacement(tool: str, old: str, candidate: str) -> None:
    if (run := current.get()) is not None:
        run.plugin_pairs.add((tool, frozenset((old, candidate))))


def keeps(key: str) -> bool:
    run = current.get()
    return run is not None and run.keeps(key)


def asked(key: str) -> bool:
    """This run asked for approval to remove `key` and has none yet."""
    run = current.get()
    return run is not None and key in run.seen and not run.seen[key]["approved"]


def due(key: str, what: str, *, warn: bool = True, by_key: bool = False) -> bool:
    run = current.get()
    return run is not None and run.due(key, what, warn=warn, by_key=by_key)


def removed(key: str) -> None:
    if (run := current.get()) is not None and not run.dry:
        run.seen.pop(key, None)
        run.removed.add(key)


@contextmanager
def during(state_dir: Path, keep: list[str], log, dry: bool, *, now: float | None = None, scoped: bool = False):
    run = Run(log, dry, time.time() if now is None else now, frozenset(keep), load(state_dir))
    if scoped:
        run.completed = set()
    token = current.set(run)
    try:
        yield run
        path = state_dir / FILENAME
        if not dry and (run.seen or path.exists()):
            save_baselines(path, run.entries())
    finally:
        current.reset(token)
