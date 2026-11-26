import json
import shutil
import socket
import subprocess
from http.client import HTTPConnection
from pathlib import Path
from stat import S_IMODE
from threading import Thread

import pytest

from agent_sync import instructions, reviewpage
from agent_sync.config import Config, load


@pytest.fixture
def request_page(tmp_path):
    class UnboundServer(reviewpage._ReviewServer):
        def server_bind(self):
            self.server_port = 8765

        def server_activate(self):
            pass

    cfg = Config(home=tmp_path)
    try:
        server = reviewpage._ReviewServer(tmp_path, cfg)
        bound = True
    except PermissionError:
        # The request handler still gets a real socket when a test sandbox forbids listening.
        server = UnboundServer(tmp_path, cfg)
        bound = False

    def request(route="", body=None, *, path=None, content_type="application/json", host=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        if bound:
            worker = Thread(target=server.handle_request)
        else:
            connection.sock, peer = socket.socketpair()

            def respond():
                with peer:
                    server.finish_request(peer, ("127.0.0.1", 0))

            worker = Thread(target=respond)
        worker.start()
        headers = {"Content-Type": content_type}
        if host is not None:
            headers["Host"] = host
        try:
            connection.request("GET" if body is None else "POST", path or server.prefix + route,
                               json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            data = response.read().decode()
            if response.headers.get_content_type() == "application/json":
                data = json.loads(data)
            return response.status, data
        finally:
            connection.close()
            worker.join(timeout=3)

    with server:
        yield request


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def scan_on_page(request_page):
    status, page = request_page()
    assert status == 200
    payload = page.split('<script type="application/json" id="scan">', 1)[1].split("</script>", 1)[0]
    return json.loads(payload), page


def make_choice(request_page, files):
    write(files["claude"], "Use the existing tests.\n")
    write(files["codex"], "Write fresh fixtures.\n")
    scan, _ = scan_on_page(request_page)
    group = scan["groups"][0]
    return {"choices": {group["key"]: group["winner"]}, "digests": {group["key"]: group["snapshot"]}}


def test_only_the_local_token_url_can_get_or_change_settings(request_page, tmp_path):
    assert request_page(path="/")[0] == 404
    assert request_page(host="example.com:8765")[0] == 403
    assert request_page("plan", {"choices": {}}, path="/plan")[0] == 404
    assert request_page("plan", {"choices": {}}, host="example.com:8765")[0] == 403
    assert request_page("plan", {"choices": {}}, content_type="text/plain")[0] == 415
    assert not load(home=tmp_path).plugin_skills


def test_preview_is_read_only_and_apply_returns_backups_and_the_fresh_scan(request_page, tmp_path):
    paths = instructions.files(tmp_path)
    choice = make_choice(request_page, paths)
    assert request_page("plan", choice) == (200, {
        "files": [
            {"key": "instructions:Global instructions", "tools": ["codex"], "path": "~/.codex/AGENTS.md",
             "action": "update", "diff": [" Write fresh fixtures.", "+Use the existing tests."], "own": 1,
             "lines": 2, "binary": False, "removed": []},
            {"key": "instructions:Global instructions", "tools": ["cursor"],
             "path": "~/.cursor/rules/agent-sync-instructions.mdc", "action": "create", "diff": [], "own": 0,
             "lines": 5, "binary": False, "removed": []},
        ], "backup": "~/.local/state/agent-sync/backups",
    })
    assert paths["codex"].read_text() == "Write fresh fixtures.\n"
    assert not paths["cursor"].exists() and not Config(home=tmp_path).state_dir.exists()

    status, result = request_page("apply", choice)
    assert status == 200 and "error" not in result
    assert paths["codex"].read_text() == "Write fresh fixtures.\nUse the existing tests.\n"
    assert "Use the existing tests." in paths["cursor"].read_text()
    assert len(result["changes"]) == 2
    backup = tmp_path / result["backup"].removeprefix("~/")
    assert backup.name.startswith("review-") and S_IMODE(backup.stat().st_mode) == 0o700
    assert (backup / ".codex/AGENTS.md").read_text() == "Write fresh fixtures.\n"
    assert result["scan"]["groups"][0]["status"] == "same"


@pytest.mark.parametrize("check_ui", [False, pytest.param(
    True, marks=pytest.mark.skipif(not shutil.which("node"), reason="review JavaScript needs Node.js"),
)])
def test_check_once_choice_is_remembered_without_changing_files(request_page, tmp_path, check_ui):
    paths = instructions.files(tmp_path)
    shared = "Use the existing tests.\n"
    expected = {"claude": shared, "codex": shared + "Codex-only note.\n", "cursor": shared}
    for tool, content in expected.items():
        write(paths[tool], content)
    scan, page = scan_on_page(request_page)
    group, = scan["groups"]
    assert group["status"] == "check"
    choice = {"choices": {group["key"]: group["winner"]}, "digests": {group["key"]: group["snapshot"]}}

    status, plan = request_page("plan", choice)
    assert status == 200 and plan["files"] == []
    status, result = request_page("apply", choice)
    assert status == 200 and "error" not in result
    assert result["changes"] == [] and result["backup"] is None
    assert result["scan"]["groups"][0]["status"] == "same"
    assert {tool: path.read_text() for tool, path in paths.items()} == expected
    if check_ui:
        subprocess.run(
            ["node", str(Path(__file__).with_name("review_confirmation.cjs"))], check=True, timeout=5, text=True,
            input=json.dumps({"page": page, "plan": plan, "applied": result}),
        )


@pytest.mark.parametrize("changed", ["claude", "codex", "cursor"])
def test_apply_refuses_edited_or_added_versions(request_page, tmp_path, changed):
    paths = instructions.files(tmp_path)
    choice = make_choice(request_page, paths)
    write(paths[changed], "Changed after the page opened.\n")
    before = {tool: path.read_bytes() if path.exists() else None for tool, path in paths.items()}

    status, result = request_page("apply", choice)
    assert status == 409 and "reload" in result["error"].lower()
    assert {tool: path.read_bytes() if path.exists() else None for tool, path in paths.items()} == before
    assert not (Config(home=tmp_path).state_dir / "backups").exists()


def test_apply_conflicts_with_upkeep(request_page, tmp_path):
    paths = instructions.files(tmp_path)
    choice = make_choice(request_page, paths)
    (Config(home=tmp_path).state_dir / "lock").mkdir(parents=True)
    status, result = request_page("apply", choice)
    assert status == 409 and "upkeep run" in result["error"]
    assert paths["codex"].read_text() == "Write fresh fixtures.\n"
    assert not paths["cursor"].exists()


def test_scan_text_cannot_close_the_embedded_json_script(request_page, tmp_path):
    paths = instructions.files(tmp_path)
    text = "</script><script>window.unsafe = true</script>"
    write(paths["claude"], "Use a local fixture.\n")
    write(paths["codex"], text + "\n")
    scan, page = scan_on_page(request_page)
    assert text not in page
    assert any(line.endswith(text) for version in scan["groups"][0]["versions"] for line in version["diff"])


def test_page_offers_each_target_only_line_and_refuses_other_removals(request_page, tmp_path):
    paths = instructions.files(tmp_path)
    write(paths["claude"], "Keep replies short.\nRun the tests.\n")
    write(paths["codex"], "Codex sandbox notes.\nKeep replies short.\nAsk before pushing.\nRun the tests.\n")
    write(paths["cursor"], "Keep replies short.\nAsk before pushing.\nRun the tests.\n")
    scan, _ = scan_on_page(request_page)
    group = scan["groups"][0]
    codex = next(version for version in group["versions"] if version["tools"] == ["codex"])
    assert group["status"] == "differ"
    assert codex["only"][group["winner"]] == [{"path": "~/.codex/AGENTS.md", "lines": [
        ["Codex sandbox notes.", False], ["Ask before pushing.", True]]}]

    def choose(remove):
        return {"choices": {group["key"]: {"version": group["winner"], "remove": remove}},
                "digests": {group["key"]: group["snapshot"]}}

    status, plan = request_page("plan", choose({"~/.codex/AGENTS.md": ["Ask before pushing."]}))
    assert status == 200
    edit = next(file for file in plan["files"] if file["path"] == "~/.codex/AGENTS.md")
    assert edit["removed"] == ["Ask before pushing."] and "-Ask before pushing." in edit["diff"]
    assert edit["own"] == 1
    status, result = request_page("apply", choose({"~/.codex/AGENTS.md": ["Run the tests."]}))
    assert status == 400 and "Run the tests." in result["error"]
    assert "Ask before pushing." in paths["codex"].read_text()
    assert request_page("plan", choose({"~/.codex/AGENTS.md": "Ask before pushing."}))[0] == 400
