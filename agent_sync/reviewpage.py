"""Serve the local review chooser and apply confirmed choices."""
from __future__ import annotations

import json
import secrets
import time
import webbrowser
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from difflib import unified_diff
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import Event, Thread

from . import review
from .config import Config
from .locking import run_lock

IDLE_SECONDS = 30 * 60
MAX_REQUEST_BYTES = 4 * 1024 * 1024
STALE = "Something changed since this page opened. Reload and review your choices again."
BUSY = "An upkeep run is in progress. Try again in a minute."


def _short_path(home: Path, path: Path) -> str:
    return str(Path("~") / path.relative_to(home)) if path.is_relative_to(home) else str(path)


def _snapshot(group: review.Group) -> dict:
    return {"missing": sorted(group.missing), "versions": {
        version.id: {"digest": version.digest, "tools": sorted(version.tools)} for version in group.versions
    }}


def _group_data(home: Path, group: review.Group, source: str) -> dict:
    winner, reasons = review.recommend(group, source)
    versions = []
    for version in group.versions:
        path = _short_path(home, version.path)
        lines = list(unified_diff(winner.body.splitlines(), version.body.splitlines(),
                                  fromfile=_short_path(home, winner.path), tofile=path, lineterm=""))
        versions.append({
            "id": version.id, "tools": version.tools, "path": path, "lines": version.lines,
            "modified": version.modified, "origin": version.origin, "label": review.ORIGIN_LABELS[version.origin],
            "note": version.note.replace(str(home) + "/", "~/"),
            "cautions": review.cautions(version, winner) if version.id != winner.id else [],
            "diff": lines,
        })
    return {"key": group.key, "kind": group.kind, "name": group.name, "status": group.status,
            "missing": group.missing, "matched_by": group.matched_by, "winner": winner.id, "reasons": reasons,
            "snapshot": _snapshot(group), "versions": versions}


def _scan_data(home: Path, cfg: Config, scan: review.Scan) -> dict:
    return {
        "groups": [_group_data(home, group, cfg.source) for group in scan.groups],
        "plugins": {name: {tool: [asdict(plugin) for plugin in entries] for tool, entries in tools.items()}
                    for name, tools in scan.plugins.items()},
        "managed": scan.managed, "tools": review.TOOL_LABELS, "plugin_skills": cfg.plugin_skills,
    }


def _backup_folder(root: Path) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now()
    while True:
        backup = root / f"review-{stamp:%Y%m%d-%H%M%S}"
        try:
            backup.mkdir(mode=0o700)
        except FileExistsError:
            stamp += timedelta(seconds=1)
        else:
            return backup


class _RequestError(ValueError):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def _choices(data: dict, scan: review.Scan, *, applying: bool) -> dict[str, str]:
    raw, digests = data.get("choices"), data.get("digests")
    if not isinstance(raw, dict) or any(not isinstance(value, str) for value in raw.values()):
        raise _RequestError(HTTPStatus.BAD_REQUEST, "Choose a version for each setting you want to change.")
    if (applying or digests is not None) and not isinstance(digests, dict):
        raise _RequestError(HTTPStatus.BAD_REQUEST, "Reload the page to get the latest settings.")
    groups = {group.key: group for group in scan.groups}
    choices = {key: choice for key, choice in raw.items() if choice != "leave"}
    for key, choice in choices.items():
        group = groups.get(key)
        if digests is not None and (group is None or digests.get(key) != _snapshot(group)):
            raise _RequestError(HTTPStatus.CONFLICT, STALE)
        if group is None or all(version.id != choice for version in group.versions):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "A selected version is unavailable. Reload the page.")
    return choices


class _ReviewServer(ThreadingHTTPServer):
    daemon_threads = False
    timeout = 0.5

    def __init__(self, home: Path, cfg: Config):
        self.home = home
        self.cfg = cfg
        self.done = Event()
        self.last_request = time.monotonic()
        self.prefix = f"/{secrets.token_urlsafe(24)}/"
        self.nonce = secrets.token_urlsafe(24)
        self.template = files("agent_sync").joinpath("review.html").read_text(encoding="utf-8")
        super().__init__(("127.0.0.1", 0), _Handler)
        self.host = f"127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    server: _ReviewServer

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        pass

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if self.headers.get_all("Host") != [self.server.host]:
            self._json({"error": "Invalid host."}, HTTPStatus.FORBIDDEN)
            return False
        if not self.path.startswith(self.server.prefix):
            self._json({"error": "Page not found."}, HTTPStatus.NOT_FOUND)
            return False
        self.route = self.path[len(self.server.prefix):]
        self.server.last_request = time.monotonic()
        return True

    def _send(self, content: bytes, media_type: str, status: HTTPStatus):
        self.send_response(status)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        nonce = self.server.nonce
        self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; "
                         f"style-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, data: dict, status: HTTPStatus = HTTPStatus.OK):
        self._send(json.dumps(data).encode(), "application/json", status)

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise _RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Send the choices as JSON.")
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length > MAX_REQUEST_BYTES:
                raise _RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Too many choices to send at once.")
            if length < 1:
                raise ValueError
            data = json.loads(self.rfile.read(length))
        except _RequestError:
            raise
        except (ValueError, RecursionError, TimeoutError) as exc:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid JSON request.") from exc
        if not isinstance(data, dict):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid JSON request.")
        return data

    def do_GET(self):
        if self.route:
            self._json({"error": "Page not found."}, HTTPStatus.NOT_FOUND)
            return
        server = self.server
        home, cfg = server.home, server.cfg
        try:
            data = json.dumps(_scan_data(home, cfg, review.scan(home, cfg))).replace("<", r"\u003c")
        except (OSError, ValueError) as exc:
            self._json({"error": f"Could not read settings. {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        page = server.template.replace("__NONCE__", server.nonce).replace("__SCAN__", data, 1)
        self._send(page.encode(), "text/html", HTTPStatus.OK)

    def do_POST(self):
        handlers = {"plan": self._plan, "apply": self._apply, "plugins": self._plugins, "quit": self._quit}
        handler = handlers.get(self.route)
        if handler is None:
            self._json({"error": "Page not found."}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._json(handler(self._body()))
        except _RequestError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (OSError, TypeError, ValueError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _plan(self, data: dict) -> dict:
        home, cfg = self.server.home, self.server.cfg
        scan = review.scan(home, cfg)
        steps = review.plan(home, scan, _choices(data, scan, applying=False), cfg.source)
        return {"steps": [{"key": step.key, "tools": step.tools, "path": _short_path(home, step.path),
                           "action": step.action} for step in steps],
                "backup": _short_path(home, cfg.state_dir / "backups")}

    def _apply(self, data: dict) -> dict:
        home, cfg = self.server.home, self.server.cfg
        with run_lock(cfg.state_dir / "lock") as locked:
            if not locked:
                raise _RequestError(HTTPStatus.CONFLICT, BUSY)
            scan = review.scan(home, cfg)
            steps = review.plan(home, scan, _choices(data, scan, applying=True), cfg.source)
            backup = _backup_folder(cfg.state_dir / "backups") if steps else None
            result = {"backup": _short_path(home, backup) if backup else None}
            try:
                result["changes"] = review.apply(home, steps, backup) if backup else []
            except (OSError, TypeError, ValueError) as exc:
                result["error"] = str(exc)
            try:
                result["scan"] = _scan_data(home, cfg, review.scan(home, cfg))
            except (OSError, ValueError) as exc:
                result["error"] = " ".join(filter(None, (result.get("error"), f"Could not refresh settings. {exc}")))
        return result

    def _plugins(self, data: dict) -> dict:
        ids = data.get("ids")
        if not isinstance(ids, list) or any(not isinstance(pid, str) or not pid for pid in ids):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid plugin choices.")
        home, cfg = self.server.home, self.server.cfg
        with run_lock(cfg.state_dir / "lock") as locked:
            if not locked:
                raise _RequestError(HTTPStatus.CONFLICT, BUSY)
            ids = list(dict.fromkeys(ids))
            review.set_plugin_skills(home, ids)
            self.server.cfg = cfg = replace(cfg, plugin_skills=ids)
            return {"scan": _scan_data(home, cfg, review.scan(home, cfg))}

    def _quit(self, data: dict) -> dict:
        self.server.done.set()
        return {"done": True}


def serve(home: Path, cfg: Config, open_browser: bool = True) -> int:
    """Run a loopback-only review session until quit, Ctrl-C, or 30 minutes idle."""
    with _ReviewServer(home, cfg) as server:
        url = f"http://{server.host}{server.prefix}"
        print(url, flush=True)
        if open_browser:
            Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            while not server.done.is_set() and time.monotonic() - server.last_request < IDLE_SECONDS:
                server.handle_request()
        except KeyboardInterrupt:
            pass
    return 0
