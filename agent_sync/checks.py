"""Read-only checks, reported as WARN lines."""
from __future__ import annotations

import re
from pathlib import Path

from . import roles


def model_files(home: Path) -> list[Path]:
    """Where the three tools name the models they run."""
    return [*roles.files(home).values(), home / ".codex/config.toml", home / ".cursor/cli-config.json",
            *sorted((home / ".claude/agents").glob("*.md")), *sorted((home / ".codex/agents").glob("*.toml")),
            *sorted((home / ".cursor/plugins/local").glob("*/agents/*.md"))]


def unwanted_models(home: Path, words: list[str]) -> list[str]:
    """One line per file that names a model containing one of `words` (for models you have no subscription for)."""
    if not words:
        return []
    pattern = re.compile("|".join(re.escape(w) for w in words), re.I)
    out = []
    for f in model_files(home):
        try:
            hits = sorted({m.group(0).lower() for m in pattern.finditer(f.read_text())})
        except OSError:
            continue
        if hits:
            out.append(f"{f} names {', '.join(hits)} (listed in warn_models)")
    return out
