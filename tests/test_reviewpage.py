import json
import socket
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
    assert request_page("plugins", {"ids": ["example"]}, path="/plugins")[0] == 404
    assert request_page("plugins", {"ids": ["example"]}, host="example.com:8765")[0] == 403
    assert request_page("plugins", {"ids": ["example"]}, content_type="text/plain")[0] == 415
    assert not load(home=tmp_path).plugin_skills


def test_preview_is_read_only_and_apply_returns_backups_and_the_fresh_scan(request_page, tmp_path):
    paths = instructions.files(tmp_path)
    choice = make_choice(request_page, paths)
    assert request_page("plan", choice) == (200, {
        "steps": [
            {"key": "instructions:Global instructions", "tools": ["codex"], "path": "~/.codex/AGENTS.md",
             "action": "replace"},
            {"key": "instructions:Global instructions", "tools": ["cursor"],
             "path": "~/.cursor/rules/agent-sync-instructions.mdc", "action": "create"},
        ], "backup": "~/.local/state/agent-sync/backups",
    })
    assert paths["codex"].read_text() == "Write fresh fixtures.\n"
    assert not paths["cursor"].exists() and not Config(home=tmp_path).state_dir.exists()

    status, result = request_page("apply", choice)
    assert status == 200 and "error" not in result
    assert paths["codex"].read_text() == "Use the existing tests.\n"
    assert "Use the existing tests." in paths["cursor"].read_text()
    assert len(result["changes"]) == 2
    backup = tmp_path / result["backup"].removeprefix("~/")
    assert backup.name.startswith("review-") and S_IMODE(backup.stat().st_mode) == 0o700
    assert (backup / ".codex/AGENTS.md").read_text() == "Write fresh fixtures.\n"
    assert result["scan"]["groups"][0]["status"] == "same"


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


def test_plugin_switch_saves_sharing_settings(request_page, tmp_path):
    sample = tmp_path / "plugin-example"
    write(sample / "skills/example/SKILL.md", "# Example\n")
    write(tmp_path / ".claude/plugins/installed_plugins.json", json.dumps({
        "plugins": {"example@market": [{"scope": "user", "installPath": str(sample)}]},
    }))
    scan, _ = scan_on_page(request_page)
    assert scan["plugins"]["example"]["claude"][0]["shared"] is False
    status, result = request_page("plugins", {"ids": ["example@market"]})
    assert status == 200
    assert result["scan"]["plugins"]["example"]["claude"][0]["shared"] is True
    assert load(home=tmp_path).plugin_skills == ["example@market"]
