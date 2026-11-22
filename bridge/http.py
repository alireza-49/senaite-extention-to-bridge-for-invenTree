"""Small HTTP surface: ``/healthz`` and token-protected manual jobs.

Only ``POST /jobs/...`` mutates anything, and only with a bearer token. The
health endpoint carries no secrets so it can be used by Docker/compose
health checks.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from . import __version__
from .service import Bridge

LOGGER = logging.getLogger(__name__)


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], bridge: Bridge, token: str | None) -> None:
        super().__init__(address, _Handler)
        self.bridge = bridge
        self.token = token


class _Handler(BaseHTTPRequestHandler):
    server_version = f"senaite-bridge/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def bridge(self) -> Bridge:
        return self.server.bridge  # type: ignore[attr-defined]

    @property
    def token(self) -> str | None:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature from base class
        LOGGER.debug("http %s - %s", self.address_string(), format % args)

    # -- helpers -----------------------------------------------------------

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            self._send(503, {"error": "job endpoint disabled: BRIDGE_TOKEN is not set"})
            return False
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(header.strip(), expected):
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def _drain_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            self.rfile.read(length)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by base class
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/healthz", "/health", "/"):
            self._send(200, {"status": "ok", "version": __version__, "dry_run": self.bridge.dry_run})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - required by base class
        self._drain_body()
        path = self.path.split("?", 1)[0].rstrip("/")
        jobs: dict[str, Callable[[], Any]] = {
            "/jobs/poll": self.bridge.poll_analyses,
            "/jobs/low-stock": lambda: self.bridge.poll_low_stock(force=True),
            "/jobs/once": self.bridge.run_once,
        }
        job = jobs.get(path)
        if job is None:
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        try:
            result = job()
        except Exception as exc:  # noqa: BLE001 - report, do not crash the server
            LOGGER.exception("job %s failed", path)
            self._send(500, {"ok": False, "error": str(exc)})
            return
        self._send(200, {"ok": True, "result": result})


def serve(bridge: Bridge, host: str, port: int, token: str | None) -> tuple[BridgeHTTPServer, threading.Thread]:
    server = BridgeHTTPServer((host, port), bridge, token)
    thread = threading.Thread(target=server.serve_forever, name="bridge-http", daemon=True)
    thread.start()
    LOGGER.info("HTTP listening on %s:%s (jobs %s)", host, port, "enabled" if token else "disabled")
    return server, thread
