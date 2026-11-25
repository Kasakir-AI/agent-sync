"""Upgrade installed packages (npm, pipx, uv, Homebrew) with one guard:

- the newest release in the installed major version, once it is SETTLE_DAYS old (Homebrew reviews its releases
  before it publishes them, so they need no wait);
- a newer major version only after you approve it (`upgrade:<manager>:<package>@<major>`), or with
  `global_major_updates`. Keeping that key keeps the installed major; keeping `upgrade:<manager>:<package>` keeps
  it for good.

A lookup that fails skips that package for this run.
"""
from __future__ import annotations

import functools
import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from . import mcp, pending, tools


@dataclass(frozen=True)
class Package:
    manager: str          # a key of MANAGERS
    name: str             # what the manager installs it as (a tap formula's full name)
    installed: str
    latest: str = ""      # the newest release the manager's listing names ("" for PyPI: look it up)
    kind: str = ""        # Homebrew: "formula" or "cask"
    extras: str = ""      # pipx: the extras it was installed with, "[a,b]"


@dataclass
class UpdateState:
    """What one run's update steps share: a listing or lookup is made once, and a package is looked at once."""
    brew_updated: bool = False
    found: dict[tuple, object] = field(default_factory=dict)
    handled: set[tuple[str, str]] = field(default_factory=set)

    @functools.cached_property
    def npm(self) -> mcp.NpmLookup:
        return mcp.NpmLookup()

    def once(self, key: tuple, read: Callable[[], object]):
        if key not in self.found:
            self.found[key] = read()
        return self.found[key]


NUMBERS = re.compile(r"v?(\d+(?:\.\d+)*)")
FINAL = re.compile(r"\d+(?:\.\d+)*")          # a PyPI release that is not a pre-, post- or dev release


def numbers(version: str) -> tuple[int, ...]:
    match = NUMBERS.match(version)
    return tuple(map(int, match[1].split("."))) if match else ()


def major(version: str) -> int | None:
    return found[0] if (found := numbers(version)) else None


def forward(installed: str, target: str) -> bool:
    """`target` is a move up from `installed` (never down); versions that don't parse only need to differ."""
    old, new = numbers(installed), numbers(target)
    if old and new:
        width = max(len(old), len(new))
        old, new = (part + (0,) * (width - len(part)) for part in (old, new))
        if old != new:
            return new > old
    return target != installed


# ---------- npm ----------

def npm_listing() -> dict[str, Package]:
    report = tools.json_object(tools.raw(["npm", "outdated", "-g", "--json"], ok_codes=(0, 1)))
    return {name: Package("npm", name, entry["current"], entry["latest"]) for name, entry in report.items()
            if isinstance(entry, dict) and isinstance(entry.get("current"), str)
            and isinstance(entry.get("latest"), str)}


def npm_newest(state: UpdateState, package: Package, in_major: int | None) -> str:
    return package.latest if in_major is None else state.npm.newest(package.name, in_major)


def npm_released(state: UpdateState, package: Package, version: str) -> str | None:
    return state.npm.released(package.name).get(version)


# ---------- pipx and uv, with releases from PyPI ----------

PYPI_SPEC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\s*(?P<extras>\[[^\]]*\])?\s*(?:[=<>!~].*)?")


def pipx_listing() -> dict[str, Package]:
    """Tools pipx installed from PyPI (not from a URL or a folder)."""
    venvs = tools.json_object(tools.raw(["pipx", "list", "--json"])).get("venvs")
    found = {}
    for name, venv in venvs.items() if isinstance(venvs, dict) else ():
        try:
            main = venv["metadata"]["main_package"]
            package, version, spec = main["package"], main["package_version"], main["package_or_url"]
        except (KeyError, TypeError):
            continue
        if all(isinstance(value, str) for value in (package, version, spec)) and (source := PYPI_SPEC.fullmatch(spec)):
            found[name] = Package("pipx", package, version, extras=source["extras"] or "")
    return found


def uv_listing() -> dict[str, Package]:
    """`uv tool list` names each tool as `name vX.Y.Z`, followed by the commands it installed."""
    return {match[1]: Package("uv", match[1], match[2])
            for match in re.finditer(r"^([^\s-]\S*) v(\d\S*)", tools.raw(["uv", "tool", "list"]), re.MULTILINE)}


def pypi_releases(name: str) -> dict[str, str | None]:
    """Each final release of `name` on PyPI that is not yanked, and when its first file was uploaded."""
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"
    try:
        with urllib.request.urlopen(url, timeout=20) as reply:
            data = json.load(reply)
    except (OSError, ValueError):
        return {}
    releases = data.get("releases") if isinstance(data, dict) else None
    found = {}
    for version, files in releases.items() if isinstance(releases, dict) else ():
        files = [file for file in files if isinstance(file, dict) and not file.get("yanked")] \
            if isinstance(files, list) else []
        if FINAL.fullmatch(version) and files:
            times = sorted(stamp for file in files if isinstance(stamp := file.get("upload_time_iso_8601"), str))
            found[version] = times[0] if times else None
    return found


def pypi(state: UpdateState, package: Package) -> dict[str, str | None]:
    return state.once(("pypi", package.name), lambda: pypi_releases(package.name))


def pypi_newest(state: UpdateState, package: Package, in_major: int | None) -> str:
    versions = [version for version in pypi(state, package) if in_major is None or major(version) == in_major]
    return max(versions, key=numbers, default="")


def pypi_released(state: UpdateState, package: Package, version: str) -> str | None:
    return pypi(state, package).get(version)


# ---------- Homebrew ----------

def brew_listing() -> dict[str, Package]:
    """Outdated formulae and casks that are not pinned, by short name."""
    report = tools.json_object(tools.raw(["brew", "outdated", "--json=v2"], timeout=300))
    found = {}
    for kind, section in (("formula", "formulae"), ("cask", "casks")):
        for entry in report.get(section) or ():
            if not isinstance(entry, dict) or entry.get("pinned") is True:
                continue
            name, versions, current = entry.get("name"), entry.get("installed_versions"), entry.get("current_version")
            if isinstance(name, str) and isinstance(current, str) and isinstance(versions, list) and versions \
                    and isinstance(versions[-1], str):
                found[name.rsplit("/", 1)[-1]] = Package("homebrew", name, versions[-1], current, kind=kind)
    return found


def brew_newest(state: UpdateState, package: Package, in_major: int | None) -> str:
    """Homebrew installs only its current version."""
    return package.latest if in_major is None or major(package.latest) in (in_major, None) else ""


@dataclass(frozen=True)
class Manager:
    label: str
    listing: Callable[[], dict[str, Package]]                       # installed packages it may upgrade, by name
    newest: Callable[[UpdateState, Package, int | None], str]       # newest release (in a major version), "" unknown
    released: Callable[[UpdateState, Package, str], str | None] | None   # when a release came out; None: no wait
    install: Callable[[Package, str], list[str]]


MANAGERS = {
    "npm": Manager("npm", npm_listing, npm_newest, npm_released,
                   lambda package, version: ["npm", "install", "-g", f"{package.name}@{version}"]),
    "pipx": Manager("pipx", pipx_listing, pypi_newest, pypi_released,
                    lambda package, version: ["pipx", "install", "--force",
                                              f"{package.name}{package.extras}=={version}"]),
    "uv": Manager("uv", uv_listing, pypi_newest, pypi_released,
                  lambda package, version: ["uv", "tool", "install", "--force", f"{package.name}=={version}"]),
    "homebrew": Manager("Homebrew", brew_listing, brew_newest, None,
                        lambda package, version: ["brew", "upgrade", f"--{package.kind}", package.name]),
}


def installed(state: UpdateState, manager: str) -> dict[str, Package]:
    return state.once(("listing", manager), MANAGERS[manager].listing)


def upgrade(log, dry: bool, state: UpdateState, manager: str, name: str, *, majors: bool, why: str = "") -> None:
    """Upgrade one installed package within the guard; `majors` installs a settled new major version without
    asking. Each package is looked at once per run."""
    if (manager, name) in state.handled:
        return
    state.handled.add((manager, name))
    resolver = MANAGERS[manager]
    package = installed(state, manager).get(name)
    if package is None:
        return
    latest = resolver.newest(state, package, None)
    if not latest:
        log("info", f"{name} ({resolver.label}) is skipped this run: its releases could not be looked up")
        return
    now = time.time()

    def waits(version: str) -> bool:
        if resolver.released is None:
            return False
        stamp = resolver.released(state, package, version)
        if not mcp.settled(stamp, now):
            reason = (f"it is under {mcp.SETTLE_DAYS} days old" if stamp else
                      "its release time could not be looked up")
            log("info", f"{name} {version} ({resolver.label}) waits: {reason}")
            return True
        return False

    def install(version: str, key: str | None = None) -> None:
        label = f"{name} {package.installed} -> {version} ({resolver.label}{', ' + why if why else ''})"
        if tools.act(log, dry, label, resolver.install(package, version)) and key:
            pending.removed(key)

    old, new = major(package.installed), major(latest)
    if old is not None and new is not None and new > old:
        if not waits(latest):
            key = f"upgrade:{manager}:{name}@{new}"
            what = (f"upgrade {name} {package.installed} -> {latest} "
                    "(a new major version, which can include breaking changes)")
            if majors or pending.due(key, what, by_key=True):
                install(latest, key)
                return
        same = resolver.newest(state, package, old)
    else:
        same = (resolver.newest(state, package, old) if old is not None else "") or latest
    if same and forward(package.installed, same) and not waits(same):
        install(same)
