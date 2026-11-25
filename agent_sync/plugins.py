"""Report duplicate plugins and prefer Codex's own plugin when it covers the same skills."""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import inventory, tools
from .registry import DEFAULT_AGENTS, TOOLS

MARKETPLACE = re.compile(r"^\s*Marketplace\s+`([^`]+)`")
CATALOG_ROW = re.compile(r"^\s*(\S+)\s+(installed,\s*enabled|installed,\s*disabled|installed|not installed)"
                         r"(?=\s|$)\s*(\S+)?(?:\s+(.*))?$")
DOTTED = re.compile(r"\d+(?:\.\d+)*")


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    marketplace: str
    version: str
    installed: bool
    claude_format: bool = field(default=False, compare=False)


def parse_catalog(text: str) -> list[CatalogEntry]:
    entries = []
    market, claude_markets = "", set()
    for line in text.splitlines():
        if header := MARKETPLACE.match(line):
            market = header[1]
        if market and ".claude-plugin/marketplace.json" in line:
            claude_markets.add(market)
        if market and (row := CATALOG_ROW.match(line)):
            pid, status, version, _ = row.groups()
            name, _, marketplace = pid.partition("@")
            entries.append(CatalogEntry(
                pid if marketplace else f"{name}@{market}", name, marketplace or market,
                "" if version in (None, "-", "—") else version, status != "not installed",
            ))
    return [CatalogEntry(e.id, e.name, e.marketplace, e.version, e.installed, e.marketplace in claude_markets)
            for e in entries]


def codex_catalog() -> list[CatalogEntry]:
    return parse_catalog(tools.raw(["codex", "plugin", "list"]))


def twice(home: Path, *, enabled=DEFAULT_AGENTS) -> list[str]:
    lines = []
    for tool, found in inventory.plugins(home, enabled).items():
        families: dict[str, list[inventory.Plugin]] = {}
        for plugin in found:
            families.setdefault(plugin.family, []).append(plugin)
        for family, group in families.items():
            if len({p.marketplace for p in group}) > 1:
                copies = ", ".join(f"{p.id} {p.version}".rstrip() for p in group)
                lines.append(f"{TOOLS[tool].name} loads plugin {family} twice: {copies}")
    return lines


def numbers(version: str) -> tuple[int, ...]:
    return tuple(map(int, version.split("."))) if DOTTED.fullmatch(version) else ()


def version_problem(current: str, candidate: str) -> str:
    if candidate == current:
        return ""
    old, new = numbers(current), numbers(candidate)
    if not old or not new:
        return "its version could not be compared"
    if new < old:
        return f"it is older ({candidate} < {current})"
    return ""


def label(plugin: inventory.Plugin | CatalogEntry) -> str:
    return f"{plugin.id} {plugin.version}".rstrip()


def skill_problem(old: inventory.Plugin, candidate: inventory.Plugin) -> str:
    missing = sorted(old.skill_names - candidate.skill_names)
    if not missing:
        return ""
    names = ", ".join(missing[:8]) + (", ..." if len(missing) > 8 else "")
    if len(missing) == 1:
        return f"it lacks one of its skills ({names})"
    return f"it lacks {len(missing)} of its skills ({names})"


def remember(path: Path, choices: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(choices, file, indent=2)
            file.write("\n")
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def own_versions(home: Path, dry: bool, log, *, enabled=DEFAULT_AGENTS, state_dir: Path,
                 catalog=codex_catalog, run=tools.sh) -> None:
    if "codex" not in enabled:
        return
    loaded = inventory.plugins(home, ("codex",))["codex"]
    rejected = state_dir / "plugin-choices.json"
    choices = inventory.settings(rejected)
    available = None

    def command(action: str, pid: str) -> bool:
        code, output = run(["codex", "plugin", action, pid])
        if code:
            log("FAIL", f"Codex could not {action} {pid}: {output}")
        return code == 0

    for old in loaded:
        if old.made_for != "claude":
            continue
        skipped = choices.get(old.id, {})
        own = [p for p in loaded if p.family == old.family and p.made_for == "codex"]
        if own:
            own.sort(key=lambda p: numbers(p.version), reverse=True)
            options = [(p, version_problem(old.version, p.version) or skill_problem(old, p)) for p in own]
            candidate, reason = next(((p, why) for p, why in options if not why), options[0])
            if reason:
                log("info", f"Codex keeps {old.id} next to its own {candidate.id}: {reason}")
            elif dry:
                log("dry", f"would remove {old.id} from Codex; its own {label(candidate)} covers the same skills")
            elif command("remove", old.id):
                log("ok", f"switched Codex from {label(old)} to its own {label(candidate)}")
            continue

        if available is None:
            available = catalog()
        options = [entry for entry in available if not entry.installed and not entry.claude_format
                   and entry.marketplace != old.marketplace and inventory.family(entry.name) == old.family
                   and (not isinstance(skipped, dict) or skipped.get(entry.id) != entry.version)]
        if not options:
            continue
        entry = max(options, key=lambda e: (not version_problem(old.version, e.version), numbers(e.version)))
        if reason := version_problem(old.version, entry.version):
            log("info", f"Codex keeps {old.id} over its own {entry.id}: {reason}")
            continue
        if dry:
            log("dry", f"would try Codex's own {label(entry)} in place of {label(old)}")
            continue
        if not command("add", entry.id):
            continue
        refreshed = inventory.plugins(home, ("codex",))["codex"]
        candidate = next((p for p in refreshed if p.id == entry.id and p.made_for == "codex"), None)
        reason = (version_problem(old.version, candidate.version) or skill_problem(old, candidate)
                  if candidate else "the Codex-format install could not be verified")
        if reason:
            if command("remove", entry.id) and candidate and old.skill_names - candidate.skill_names:
                choices[old.id] = {**(skipped if isinstance(skipped, dict) else {}), entry.id: entry.version}
                remember(rejected, choices)
            log("info", f"Codex keeps {old.id} over its own {entry.id}: {reason}")
        else:
            loaded.append(candidate)
            if command("remove", old.id):
                log("ok", f"switched Codex from {label(old)} to its own {label(candidate)}")
