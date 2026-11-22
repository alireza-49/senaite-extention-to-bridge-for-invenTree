import json
import urllib.error
import urllib.request

import pytest

from bridge.http import serve
from bridge.inventree import InventreeClient
from bridge.journal import Journal
from bridge.senaite import SenaiteClient
from bridge.service import Bridge
from tests.fakes import (
    FakeServer,
    InventreeHandler,
    RecordingNotifier,
    SenaiteHandler,
    inventree_state,
    make_config,
    senaite_state,
)


def call(url: str, method: str = "GET", token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def http_bridge():
    senaite = FakeServer(SenaiteHandler, senaite_state())
    inventree = FakeServer(InventreeHandler, inventree_state())
    config = make_config(senaite.url, inventree.url, rules=())
    bridge = Bridge(
        config,
        senaite=SenaiteClient(config.senaite.base_url, "api", "secret"),
        inventree=InventreeClient(config.inventree.base_url, "tok"),
        journal=Journal(":memory:"),
        notifier=RecordingNotifier(),
    )
    bridge.startup()
    server, _ = serve(bridge, "127.0.0.1", 0, "bridge-token")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield bridge, base
    server.shutdown()
    server.server_close()
    senaite.close()
    inventree.close()


def test_health_is_public(http_bridge):
    _, base = http_bridge
    status, payload = call(f"{base}/healthz")
    assert status == 200
    assert payload["status"] == "ok"


def test_jobs_require_the_bearer_token(http_bridge):
    _, base = http_bridge
    status, payload = call(f"{base}/jobs/poll", method="POST")
    assert status == 401
    assert payload["error"] == "unauthorized"


def test_jobs_run_with_the_bearer_token(http_bridge):
    _, base = http_bridge
    status, payload = call(f"{base}/jobs/poll", method="POST", token="bridge-token")
    assert status == 200
    assert payload["ok"] is True
    assert payload["result"]["fetched"] == 0


def test_unknown_route_is_404(http_bridge):
    _, base = http_bridge
    status, _ = call(f"{base}/nope")
    assert status == 404
