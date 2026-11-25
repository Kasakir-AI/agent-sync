import subprocess

import pytest


@pytest.mark.parametrize("launcher, command", [
    ("run", "/usr/bin/osascript"),
    ("Popen", "zenity"),
    ("Popen", "/opt/homebrew/bin/plannotator"),
])
def test_notification_processes_are_blocked(launcher, command):
    with pytest.raises(pytest.fail.Exception, match="test tried to launch"):
        getattr(subprocess, launcher)([command])
