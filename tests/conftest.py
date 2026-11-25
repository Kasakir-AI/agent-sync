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

    def offline_run(args, *rest, **kwargs):
        if isinstance(args, list | tuple) and args and Path(str(args[0])).name in blocked:
            return subprocess.CompletedProcess(args, 1, "", "offline in tests")
        return run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", offline_run)
