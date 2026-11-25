import json
import shlex
import subprocess
from queue import Queue
from threading import Event

import pytest

from agent_sync import notify
from agent_sync.config import Config
from agent_sync.log import Log


@pytest.fixture(autouse=True)
def fake_notifications(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("unexpected notification process")

    monkeypatch.setattr(notify.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(notify.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(notify.subprocess, "run", unexpected)
    monkeypatch.setattr(notify.subprocess, "Popen", unexpected)


def available(monkeypatch, *commands):
    monkeypatch.setattr(notify, "find_command", lambda name: f"/fake/bin/{name}" if name in commands else None)


def tty(monkeypatch, replies):
    replies = iter(replies)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        answer = next(replies)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(notify.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(notify.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", read)
    return prompts


def dialogs(monkeypatch, *outcomes):
    replies = iter(outcomes)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        answer = next(replies)
        if isinstance(answer, Exception):
            raise answer
        code, output = answer
        return subprocess.CompletedProcess(args, code, output)

    monkeypatch.setattr(notify.subprocess, "run", run)
    return calls


def test_terminal_warns_first_and_prompts_even_when_desktop_delivery_is_disabled(tmp_path, monkeypatch, capsys):
    available(monkeypatch, "osascript", "plannotator")
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    prompts = tty(monkeypatch, ["y", "", ""])
    cfg = Config(home=tmp_path, notify=False)
    log = Log(cfg.log_file)
    assert notify.deliver(cfg, log, ["example warning"], {"a": "old skill", "b": "old server"}) == ({"a"}, True)
    assert "Warning: example warning" in capsys.readouterr().out
    assert prompts == ["Remove old skill? [y/N] ", "Remove old server? [y/N] ",
                       "Press Enter once you have read the warnings. "]


@pytest.mark.parametrize("exception", [EOFError(), KeyboardInterrupt()])
def test_terminal_aborts_all_choices_if_warning_prompt_is_interrupted(tmp_path, monkeypatch, exception):
    tty(monkeypatch, ["y", exception])
    cfg = Config(home=tmp_path)
    assert notify.deliver(cfg, Log(cfg.log_file), ["warning"], {"a": "old item"}) == (set(), False)


def test_terminal_requires_both_tty_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    available(monkeypatch, "osascript")
    calls = dialogs(monkeypatch, (0, "button returned:OK, gave up:false\n"))
    cfg = Config(home=tmp_path)
    assert notify.deliver(cfg, Log(cfg.log_file), ["warning"], {}) == (set(), True)
    assert len(calls) == 1


def test_macos_uses_argv_and_tracks_duplicate_labels_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    available(monkeypatch, "osascript")
    calls = dialogs(monkeypatch, (0, "2\n\n"), (0, "button returned:OK, gave up:false\n"))
    cfg = Config(home=tmp_path)
    log = Log(cfg.log_file)
    label = 'a "quoted" \\path'
    warnings = [f"warning-{i:02}" for i in range(17)]
    assert notify.deliver(cfg, log, warnings, {"first": label, "second": label}) == ({"second"}, True)
    (choose, choose_kwargs), (alert, alert_kwargs) = calls
    assert choose[0] == alert[0] == "/fake/bin/osascript"
    assert choose[-2:] == [f"1\t{label}", f"2\t{label}"]
    assert label not in choose[2]
    assert 'OK button name "Approve selected"' in choose[2]
    assert 'cancel button name "Keep all for now"' in choose[2]
    assert alert[-2] == "agent-sync: 17 new warnings"
    assert alert[-1].splitlines()[:15] == warnings[:15]
    assert f"and 2 more in {log.path}" in alert[-1]
    assert warnings[15] not in alert[-1]
    assert "giving up after 43200" in alert[2]
    assert choose_kwargs["timeout"] == alert_kwargs["timeout"] == 43200


@pytest.mark.parametrize(
    "choose,alert",
    [
        ((0, "false\n"), (0, "button returned:OK, gave up:true\n")),
        ((0, "\n"), (0, "gave up:false\n")),
        ((1, "1\n"), (0, "button returned:Cancel, gave up:false\n")),
        (subprocess.TimeoutExpired("osascript", 43200), subprocess.TimeoutExpired("osascript", 43200)),
    ],
)
def test_macos_only_accepts_affirmative_dialog_results(tmp_path, monkeypatch, choose, alert):
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    available(monkeypatch, "osascript")
    dialogs(monkeypatch, choose, alert)
    cfg = Config(home=tmp_path)
    assert notify.deliver(cfg, Log(cfg.log_file), ["warning"], {"a": "old item"}) == (set(), False)


@pytest.mark.parametrize("display", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_linux_checklist_returns_only_the_selected_key_then_acknowledges_on_ok(tmp_path, monkeypatch, display):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setenv(display, "test-display")
    available(monkeypatch, "zenity")
    key = "second|with\nseparator"
    calls = dialogs(monkeypatch, (0, json.dumps(key) + "\n"), (0, ""))
    cfg = Config(home=tmp_path)
    warnings = ["<warning>", *(f"warning-{i}" for i in range(16))]
    assert notify.deliver(cfg, Log(cfg.log_file), warnings, {"a": "same", key: "same"}) == ({key}, True)
    (choose, _), (alert, _) = calls
    assert "--checklist" in choose
    assert "--print-column=2" in choose
    assert choose[-6:] == ["FALSE", '"a"', "same", "FALSE", json.dumps(key), "same"]
    assert "--info" in alert
    assert "--text=" + "\n".join(warnings) in alert
    assert "--no-markup" in alert


def test_linux_dismissal_does_not_acknowledge_or_approve(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", "test-display")
    available(monkeypatch, "zenity")
    dialogs(monkeypatch, (1, '"a"\n'), (5, ""))
    cfg = Config(home=tmp_path)
    assert notify.deliver(cfg, Log(cfg.log_file), ["warning"], {"a": "item"}) == (set(), False)


def test_linux_without_display_and_disabled_nonterminal_channels_are_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    available(monkeypatch, "zenity")
    cfg = Config(home=tmp_path)
    log = Log(cfg.log_file)
    assert notify.deliver(cfg, log, ["warning"], {"a": "item"}) == (set(), False)
    monkeypatch.setenv("DISPLAY", "test-display")
    available(monkeypatch, "zenity", "plannotator")
    cfg.notify = False
    assert notify.deliver(cfg, log, ["warning"], {"a": "item"}) == (set(), False)


class InboxProcess:
    def __init__(self, tool_reply):
        self.tool_reply = tool_reply
        self.messages = []
        self.replies = Queue()
        self.stopped = Event()
        self.stdin = self
        self.stdout = iter(self.replies.get, None)
        self.returncode = None

    def write(self, line):
        message = json.loads(line)
        self.messages.append(message)
        if message["method"] == "initialize":
            self.replies.put(b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}\n')
        elif message["method"] == "tools/call" and self.tool_reply is not None:
            self.replies.put(json.dumps({"jsonrpc": "2.0", "id": 2, **self.tool_reply}).encode() + b"\n")

    def flush(self):
        pass

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.replies.put(None)
        self.stopped.set()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        assert self.stopped.wait(2), "MCP server left running"


def inbox(monkeypatch, *replies):
    answers = iter(replies)
    processes = []

    def popen(args, **kwargs):
        assert args == ["/fake/bin/plannotator", "inbox", "mcp"]
        process = InboxProcess(next(answers))
        process.kwargs = kwargs
        processes.append(process)
        return process

    monkeypatch.setattr(notify.subprocess, "Popen", popen)
    return processes


def test_inbox_history_write_failure_keeps_delivery_nonfatal(tmp_path, monkeypatch):
    available(monkeypatch, "plannotator")
    processes = inbox(monkeypatch, {"result": {}})
    cfg = Config(home=tmp_path)
    log = Log(cfg.log_file)

    def read_only(*_args):
        raise OSError("read-only notification history")

    monkeypatch.setattr(notify.textmerge, "save_baselines", read_only)
    assert notify.deliver(cfg, log, ["warning"], {}) == (set(), False)
    assert len(processes) == 1
    assert "Could not save Inbox notification history" in log.path.read_text()


def test_inbox_sends_sorted_contents_once_without_acknowledging(tmp_path, monkeypatch):
    available(monkeypatch, "plannotator")
    processes = inbox(monkeypatch, {"result": {}}, {"result": {}})
    cfg = Config(home=tmp_path)
    log = Log(cfg.log_file)
    key = "skill:old 'name' \\path"
    warning = "Warning A"
    assert notify.deliver(cfg, log, ["Warning B", warning], {key: "old skill", "mcp:a": "old server"}) == (set(), False)
    assert notify.deliver(cfg, log, ["Warning C"], {}) == (set(), False)
    assert notify.deliver(cfg, log, [warning, "Warning B"], {"mcp:a": "old server", key: "old skill"}) == (set(), False)
    assert len(processes) == 2
    posted, new = processes
    assert posted.kwargs["cwd"] == cfg.home
    assert posted.stopped.is_set() and new.stopped.is_set()
    init, initialized, request = posted.messages
    assert init["method"] == "initialize"
    assert init["params"]["protocolVersion"] == "2025-06-18"
    assert initialized == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert request["method"] == "tools/call"
    assert request["params"]["name"] == "send_message"
    args = request["params"]["arguments"]
    assert args["agent_host"] == args["agent_name"] == "agent-sync"
    assert args["thread"] == "agent-sync upkeep"
    assert args["project_path"] == str(tmp_path)
    assert args["body"].startswith("- Warning A\n- Warning B\n\n")
    assert ":::run\nApprove: remove old skill\n" in args["body"]
    assert f"```\nagent-sync pending --approve {shlex.quote(key)}\n```" in args["body"]
    assert f"```\nagent-sync pending --keep {shlex.quote(key)}\n```" in args["body"]
    assert len(args["idempotency_key"]) == 64
    assert args["idempotency_key"] != new.messages[-1]["params"]["arguments"]["idempotency_key"]


@pytest.mark.parametrize("failure", [{"error": {"code": -32603}}, {"result": {"isError": True}}, None])
def test_inbox_failure_logs_once_and_retries_the_same_digest(tmp_path, monkeypatch, failure):
    available(monkeypatch, "plannotator")
    monkeypatch.setattr(notify, "INBOX_TIMEOUT", 0.1)
    processes = inbox(monkeypatch, failure, {"result": {}})
    cfg = Config(home=tmp_path)
    log = Log(cfg.log_file)
    assert notify.deliver(cfg, log, ["warning"], {}) == (set(), False)
    assert log.kinds["info"] == 1
    assert notify.deliver(cfg, log, ["warning"], {}) == (set(), False)
    assert log.kinds["info"] == 1
    assert all(process.stopped.is_set() for process in processes)
    first, second = (process.messages[-1]["params"]["arguments"] for process in processes)
    assert first["idempotency_key"] == second["idempotency_key"]


def test_inbox_runs_after_terminal_and_receives_original_payload(tmp_path, monkeypatch):
    available(monkeypatch, "plannotator", "osascript")
    prompts = tty(monkeypatch, ["y", ""])
    processes = inbox(monkeypatch, {"result": {}})
    popen = notify.subprocess.Popen

    def after_prompts(*args, **kwargs):
        assert len(prompts) == 2
        return popen(*args, **kwargs)

    monkeypatch.setattr(notify.subprocess, "Popen", after_prompts)
    cfg = Config(home=tmp_path)
    assert notify.deliver(cfg, Log(cfg.log_file), ["warning"], {"a": "item"}) == ({"a"}, True)
    assert "Approve: remove item" in processes[0].messages[-1]["params"]["arguments"]["body"]


def test_inbox_dynamic_text_cannot_start_new_command_blocks(tmp_path, monkeypatch):
    available(monkeypatch, "plannotator")
    processes = inbox(monkeypatch, {"result": {}})
    cfg = Config(home=tmp_path)
    injected = "message\n```\n:::run\nOther action\n![image](/path)"
    notify.deliver(cfg, Log(cfg.log_file), [injected], {f"skill:{injected}": injected})
    body = processes[0].messages[-1]["params"]["arguments"]["body"]
    assert body.splitlines().count(":::run") == 2
    assert body.splitlines().count("```") == 4
    assert "![image]" not in body
