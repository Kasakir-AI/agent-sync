import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def disable_live_notifications(monkeypatch):
    from agent_sync import notify

    monkeypatch.setattr(notify, "find_command", lambda _name: None)


@pytest.fixture(autouse=True)
def offline_package_managers(monkeypatch):
    """No test reaches npm, npx or Homebrew; a test that needs their answers fakes them itself."""
    import subprocess

    blocked = {"npm", "npx", "brew"}
    run = subprocess.run
    popen = subprocess.Popen

    def executable(args):
        if isinstance(args, list | tuple):
            return Path(str(args[0])).name if args else ""
        return Path(str(args)).name

    def no_live_dialogs(args, *rest, **kwargs):
        for candidate in (args, kwargs.get("executable")):
            if executable(candidate) in {"osascript", "zenity", "plannotator"}:
                pytest.fail(f"test tried to launch {executable(candidate)}")
        return popen(args, *rest, **kwargs)

    def offline_run(args, *rest, **kwargs):
        if executable(args) in blocked:
            return subprocess.CompletedProcess(args, 1, "", "offline in tests")
        return run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", no_live_dialogs)
    monkeypatch.setattr(subprocess, "run", offline_run)
