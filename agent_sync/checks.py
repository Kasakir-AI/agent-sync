"""Read-only checks, reported as WARN lines."""
from __future__ import annotations

import re
from pathlib import Path

from . import roles
from .registry import DEFAULT_AGENTS, TOOLS


def model_files(home: Path, *, enabled=DEFAULT_AGENTS) -> list[Path]:
    """Where the enabled tools name the models they run."""
    settings = {"codex": TOOLS["codex"].mcp, "cursor": TOOLS["cursor"].first_run}
    files = list(roles.files(home, enabled).values())
    for tool in TOOLS.values():
        if tool.id in enabled:
            if filename := settings.get(tool.id):
                files.append(home / filename)
            if pattern := tool.agent_glob:
                files.extend(sorted(home.glob(pattern)))
    return list(dict.fromkeys(files))


def unwanted_models(home: Path, words: list[str], *, enabled=DEFAULT_AGENTS) -> list[str]:
    """One line per file that names a model containing one of `words` (for models you have no subscription for)."""
    if not words:
        return []
    pattern = re.compile("|".join(re.escape(w) for w in words), re.I)
    out = []
    for f in model_files(home, enabled=enabled):
        try:
            hits = sorted({m.group(0).lower() for m in pattern.finditer(f.read_text())})
        except OSError:
            continue
        if hits:
            out.append(f"{f} names {', '.join(hits)} (listed in warn_models)")
    return out
