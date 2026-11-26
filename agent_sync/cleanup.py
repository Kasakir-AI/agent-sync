from __future__ import annotations

import re
import shutil
from configparser import ConfigParser, Error
from pathlib import Path

from .textmerge import load_baselines, save_baselines

CHECKOUT_NAME = re.compile(r"[A-Za-z0-9-]+__[A-Za-z0-9_.-]+")


def checkout_name(package: str) -> str:
    return package.replace("/", "__")


def is_package_checkout(path: Path) -> bool:
    git = path / ".git"
    config = git / "config"
    if git.is_symlink() or config.is_symlink() or not config.is_file():
        return False
    owner, _, repo = path.name.partition("__")
    settings = ConfigParser(interpolation=None, strict=False)
    try:
        settings.read_string(config.read_text())
    except (Error, OSError, UnicodeError):
        return False
    return settings.get('remote "origin"', "url", fallback="") == f"https://github.com/{owner}/{repo}.git"


def prune_packages(root: Path, packages: list[str], dry: bool, log) -> None:
    if root.is_symlink():
        log("WARN", f"skipped symlink {root}")
        return
    if not root.is_dir():
        return
    keep = {checkout_name(package) for package in packages}
    for path in sorted(root.iterdir()):
        if not CHECKOUT_NAME.fullmatch(path.name):
            continue
        if path.is_symlink():
            log("WARN", f"skipped symlink {path}")
        elif path.name not in keep and path.is_dir() and is_package_checkout(path):
            if not dry:
                shutil.rmtree(path)
            log("info", f"{'Would delete' if dry else 'Deleted'} package checkout {path}")


def prune_records(path: Path, cutoff: float, dry: bool, log, *, legacy: bool = False) -> None:
    if path.is_symlink():
        log("WARN", f"skipped symlink {path}")
        return
    if not path.is_file():
        return
    records = load_baselines(path)
    migrated = path.stat().st_mtime if legacy else None
    kept = {}
    for key, stamp in records.items():
        if legacy and stamp is True:
            stamp = migrated
        if type(stamp) in (int, float) and stamp > cutoff:
            kept[key] = stamp
    if records != kept and not dry:
        save_baselines(path, kept)
    if expired := len(records) - len(kept):
        log("info", f"{'Would delete' if dry else 'Deleted'} {expired} expired records from {path.name}")


OUTPUT_MAX = 1024 * 1024    # the scheduler's output file is trimmed once it passes 1 MiB
OUTPUT_KEEP = 256 * 1024    # to its newest 256 KiB, from the first whole line


def trim_output(path: Path, dry: bool, log) -> None:
    """Keep the scheduler's output file small. It is rewritten in place, because launchd keeps the running job's
    stdout open on it in append mode."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= OUTPUT_MAX:
        return
    with path.open("r+b") as file:
        file.seek(-OUTPUT_KEEP, 2)
        tail = file.read()
        tail = tail[tail.find(b"\n") + 1:]
        if not dry:
            file.seek(0)
            file.write(tail)
            file.truncate()
    log("info", f"{'Would trim' if dry else 'Trimmed'} {path.name} to its newest {len(tail) // 1024} KiB")
