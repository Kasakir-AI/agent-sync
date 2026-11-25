import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def disable_live_notifications(monkeypatch):
    from agent_sync import notify

    monkeypatch.setattr(notify, "find_command", lambda _name: None)
