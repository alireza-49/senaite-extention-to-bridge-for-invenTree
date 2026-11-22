"""Test fakes: minimal SENAITE / InvenTree HTTP servers.

They implement only the routes the bridge uses, with the payload shapes and
quirks observed on SENAITE 2.6.0 and the documented InvenTree REST API.
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from bridge.config import (
    Config,
    ConsumeLine,
    InventreeConfig,
    LowStockConfig,
    NotifyConfig,
    Rule,
    RunConfig,
    SenaiteConfig,
)


def iso_now(offset_days: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat(timespec="seconds")


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _first(self, query: dict[str, list[str]], key: str, default: Any = None) -> Any:
        values = query.get(key)
        return values[0] if values else default


class SenaiteHandler(_Base):
    PREFIX = "/SENAITE/@@API/senaite/v1/"

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        state = self.server.state  # type: ignore[attr-defined]
        return decoded == f"{state['username']}:{state['password']}"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith(self.PREFIX):
            return self._json({"error": "not found"}, 404)
        route = path[len(self.PREFIX) :]
        state = self.server.state  # type: ignore[attr-defined]
        state["requests"].append(route)

        if route == "version":
            return self._json({"version": "2.6.0"})
        if route == "users/current":
            if self._authorized():
                return self._json({"items": [{"username": state["username"], "authenticated": True}]})
            return self._json({"items": [{"username": "Anonymous User", "authenticated": False}]})
        if route == "analysis":
            if not self._authorized():
                return self._json({"count": 0, "items": [], "page": 1, "pages": 1})
            query = self._query()
            wanted = self._first(query, "review_state")
            items = [
                item for item in state["analyses"] if wanted is None or item.get("review_state") == wanted
            ]
            limit = int(self._first(query, "limit", 200))
            return self._json({"count": len(items), "items": items[:limit], "page": 1, "pages": 1})
        if route.startswith("analysisrequest/"):
            if not self._authorized():
                return self._json({"error": "not found"}, 404)
            uid = route.split("/", 1)[1]
            sample = state["samples"].get(uid)
            if sample is None:
                return self._json({"error": "not found"}, 404)
            return self._json(sample)
        return self._json({"error": "not found"}, 404)


class InventreeHandler(_Base):
    def _authorized(self) -> bool:
        state = self.server.state  # type: ignore[attr-defined]
        return self.headers.get("Authorization") == f"Token {state['token']}"

    def _part_view(self, part: dict[str, Any]) -> dict[str, Any]:
        state = self.server.state  # type: ignore[attr-defined]
        total = sum(item["quantity"] for item in state["stock"] if item["part"] == part["pk"])
        return {**part, "in_stock": total}

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._json({"detail": "Invalid token"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        query = self._query()
        state = self.server.state  # type: ignore[attr-defined]
        state["requests"].append(path)

        if path == "/api/part":
            parts = [self._part_view(part) for part in state["parts"]]
            if str(self._first(query, "low_stock", "")).lower() in ("true", "1"):
                low = [p for p in parts if p["minimum_stock"] and p["in_stock"] < p["minimum_stock"]]
                return self._json({"count": len(low), "results": low})
            ipn = self._first(query, "IPN")
            if ipn is not None:
                parts = [p for p in parts if str(p["IPN"]).casefold() == str(ipn).casefold()]
            return self._json({"count": len(parts), "results": parts[: int(self._first(query, "limit", 50))]})

        if path.startswith("/api/part/"):
            pk = int(path.rsplit("/", 1)[1])
            for part in state["parts"]:
                if part["pk"] == pk:
                    return self._json(self._part_view(part))
            return self._json({"detail": "not found"}, 404)

        if path == "/api/stock":
            part_pk = int(self._first(query, "part", 0))
            location = self._first(query, "location")
            items = [item for item in state["stock"] if item["part"] == part_pk and item["quantity"] > 0]
            if location is not None:
                items = [item for item in items if str(item.get("location")) == str(location)]
            items.sort(key=lambda item: str(item.get("expiry_date") or "9999-12-31"))
            return self._json({"count": len(items), "results": items})

        if path == "/api/stock/location":
            search = str(self._first(query, "search", "")).casefold()
            found = [loc for loc in state["locations"] if loc["name"].casefold() == search]
            return self._json({"count": len(found), "results": found})

        return self._json({"detail": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._json({"detail": "Invalid token"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        state = self.server.state  # type: ignore[attr-defined]

        if path == "/api/stock/remove":
            if state.get("remove_missing"):
                # Simulate an old InvenTree without the bulk endpoint.
                return self._json({"detail": "not found"}, 404)
            state["remove_calls"].append(body)
            for item in body.get("items", []):
                for stock in state["stock"]:
                    if stock["pk"] == item["pk"]:
                        stock["quantity"] -= float(item["quantity"])
            return self._json({"ok": True})

        if path.startswith("/api/stock/item/") and path.endswith("/remove"):
            pk = int(path.split("/")[4])
            if state.get("fail_pk") == pk:
                return self._json({"detail": "stock item is locked"}, 500)
            state["remove_calls"].append({"per_item": True, "body": body})
            for stock in state["stock"]:
                if stock["pk"] == pk:
                    stock["quantity"] -= float(body["quantity"])
            return self._json({"ok": True})

        return self._json({"detail": "not found"}, 404)


class FakeServer:
    def __init__(self, handler: type[BaseHTTPRequestHandler], state: dict[str, Any]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.state = state  # type: ignore[attr-defined]
        self.state = state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def senaite_state(username: str = "api", password: str = "secret") -> dict[str, Any]:
    return {"username": username, "password": password, "analyses": [], "samples": {}, "requests": []}


def inventree_state(token: str = "tok") -> dict[str, Any]:
    return {
        "token": token,
        "parts": [],
        "stock": [],
        "locations": [],
        "remove_calls": [],
        "requests": [],
        "remove_missing": False,
        "fail_pk": None,
    }


def add_analysis(
    state: dict[str, Any],
    *,
    uid: str = "analysis-uid",
    sample_uid: str = "sample-uid",
    keyword: str = "Total Plate Count",
    result: str = "12",
    unit: str = "CFU/ml",
    review_state: str = "verified",
    sample_type: str = "water",
    sample_id: str = "S-0001",
    created: str | None = None,
) -> dict[str, Any]:
    analysis = {
        "uid": uid,
        "getKeyword": keyword,
        "getResult": result,
        "getUnit": unit,
        "review_state": review_state,
        "parent_uid": sample_uid,
        "getRequestID": sample_id,
        "created": created or iso_now(),
        "modified": created or iso_now(),
    }
    state["analyses"].append(analysis)
    state["samples"][sample_uid] = {
        "uid": sample_uid,
        "getRequestID": sample_id,
        "getSampleTypeTitle": sample_type,
        "review_state": "verified",
        "getDateVerified": iso_now(),
        "getBatchID": "",
    }
    return analysis


def add_part(
    state: dict[str, Any],
    *,
    pk: int,
    ipn: str,
    name: str | None = None,
    minimum_stock: float = 0,
    stock: float = 0,
    location: int | None = None,
    expiry_date: str | None = None,
) -> dict[str, Any]:
    part = {"pk": pk, "IPN": ipn, "name": name or ipn, "minimum_stock": minimum_stock}
    state["parts"].append(part)
    if stock:
        add_stock_item(state, part_pk=pk, quantity=stock, location=location, expiry_date=expiry_date)
    return part


def add_stock_item(
    state: dict[str, Any],
    *,
    part_pk: int,
    quantity: float,
    location: int | None = None,
    expiry_date: str | None = None,
    pk: int | None = None,
) -> dict[str, Any]:
    item = {
        "pk": pk if pk is not None else len(state["stock"]) + 1,
        "part": part_pk,
        "quantity": float(quantity),
        "location": location,
        "expiry_date": expiry_date,
    }
    state["stock"].append(item)
    return item


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def send(self, subject: str, text: str, payload: Any = None) -> list[dict[str, Any]]:
        self.sent.append((subject, text, payload))
        return [{"channel": "recording", "status": "sent"}]


def make_config(
    senaite_url: str,
    inventree_url: str,
    *,
    rules: tuple[Rule, ...] = (),
    dry_run: bool = False,
    repeat_after_hours: float = 24.0,
    low_stock_enabled: bool = True,
    http_token: str | None = "bridge-token",
) -> Config:
    return Config(
        senaite=SenaiteConfig(
            base_url=f"{senaite_url}/SENAITE",
            username="api",
            password="secret",
            poll_seconds=60,
            batch_size=200,
            rescan_days=7,
            initial_lookback_days=0,
        ),
        inventree=InventreeConfig(base_url=inventree_url, token="tok"),
        rules=rules,
        low_stock=LowStockConfig(enabled=low_stock_enabled, poll_seconds=60, repeat_after_hours=repeat_after_hours),
        notify=NotifyConfig(),
        run=RunConfig(dry_run=dry_run, journal_path=":memory:", http_token=http_token),
    )


def tpc_rule(rule_id: str = "microbiology-tpc") -> Rule:
    return Rule(
        id=rule_id,
        keywords=("Total Plate Count", "TPC"),
        sample_types=("water",),
        consume=(
            ConsumeLine(part_ipn="PETRI-90", quantity=Decimal("2")),
            ConsumeLine(part_ipn="PCA-MEDIUM", quantity=Decimal("50"), location="Micro Lab"),
        ),
    )
