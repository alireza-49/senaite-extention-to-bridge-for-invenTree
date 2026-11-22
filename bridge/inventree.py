"""InvenTree REST client: part lookup, stock allocation and stock removal.

InvenTree has no outbound webhooks, so the bridge also polls
``GET /api/part/?low_stock=true`` to detect parts below their minimum stock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

import requests

from .config import ConsumeLine


class InventreeError(RuntimeError):
    """Raised for transport, HTTP or payload problems."""


class PartNotFound(InventreeError):
    def __init__(self, line: ConsumeLine) -> None:
        self.line = line
        super().__init__(
            "part not found in InvenTree for " + (f"IPN {line.part_ipn}" if line.part_ipn else f"pk {line.part_pk}")
        )


class PartialConsumption(InventreeError):
    """Some stock items were already changed before the request failed.

    Retrying would deduct those items twice, so the caller must record the
    consumption as ``unknown`` and leave it to a human.
    """

    def __init__(self, removed: Sequence[Mapping[str, Any]], error: BaseException) -> None:
        self.removed = [dict(item) for item in removed]
        self.error = error
        pks = ", ".join(str(item.get("pk")) for item in self.removed)
        super().__init__(f"{error} (already removed from stock item(s) {pks})")


class InsufficientStock(InventreeError):
    def __init__(self, shortages: Sequence["Shortage"]) -> None:
        self.shortages = list(shortages)
        text = "; ".join(
            f"{item.describe()}: need {item.required}, available {item.available}"
            for item in self.shortages
        )
        super().__init__("insufficient stock: " + text)


@dataclass
class Allocation:
    stock_item: int
    quantity: Decimal


@dataclass
class LinePlan:
    line: ConsumeLine
    part_pk: int
    part_ipn: str
    part_name: str
    allocations: list[Allocation] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((item.quantity for item in self.allocations), Decimal("0"))


@dataclass
class Shortage:
    part: str
    required: Decimal
    available: Decimal

    def describe(self) -> str:
        return self.part


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - InvenTree may return null/odd values
        return Decimal("0")


def _quantity(value: Decimal) -> str:
    """Render a quantity the way InvenTree expects (no exponent, no padding)."""

    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(int(normalized))
    return format(normalized, "f")


class InventreeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http = session or requests.Session()
        self._http.verify = verify_ssl
        self._http.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            }
        )

    # -- plumbing ----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/{path.lstrip('/')}"

    def _request(self, method: str, path: str, params: Mapping[str, Any] | None = None, json: Any = None) -> Any:
        url = self._url(path)
        try:
            response = self._http.request(method, url, params=dict(params or {}), json=json, timeout=self.timeout)
        except requests.RequestException as exc:
            raise InventreeError(f"{method} {url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise InventreeError(
                f"{method} {url} returned HTTP {response.status_code}: {response.text[:300].strip()}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise InventreeError(f"{method} {url} did not return JSON") from exc

    @staticmethod
    def _results(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, Mapping):
            for key in ("results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    # -- parts and stock ---------------------------------------------------

    def find_part(self, ipn: str | None = None, pk: int | None = None) -> dict[str, Any]:
        if pk is not None:
            part = self._request("GET", f"part/{pk}/")
            if not isinstance(part, Mapping) or not part.get("pk"):
                raise PartNotFound(ConsumeLine(part_ipn=None, quantity=Decimal(1), part_pk=pk))
            return dict(part)
        if not ipn:
            raise InventreeError("part lookup needs an IPN or a pk")
        needle = ipn.strip()
        exact = self._match_ipn(self._results(self._request("GET", "part/", {"IPN": needle, "limit": 50})), needle)
        if not exact:
            # Some InvenTree versions ignore the ``IPN`` filter and answer with
            # the whole (paginated) part list, so retry with the generic search
            # and always match the IPN locally.
            exact = self._match_ipn(self._results(self._request("GET", "part/", {"search": needle, "limit": 50})), needle)
        if not exact:
            raise PartNotFound(ConsumeLine(part_ipn=ipn, quantity=Decimal(1)))
        return exact[0]

    @staticmethod
    def _match_ipn(candidates: Sequence[Mapping[str, Any]], ipn: str) -> list[dict[str, Any]]:
        folded = ipn.strip().casefold()
        return [
            dict(item)
            for item in candidates
            if str(item.get("IPN") or "").strip().casefold() == folded
        ]

    def location_pk(self, name: str) -> int | None:
        for endpoint in ("stock/location/", "stock/location"):
            try:
                payload = self._request("GET", endpoint, {"search": name, "limit": 50})
            except InventreeError:
                continue
            for item in self._results(payload):
                if str(item.get("name") or "").strip().casefold() == name.strip().casefold():
                    return int(item["pk"])
        return None

    def stock_items(self, part_pk: int, location_pk: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"part": part_pk, "in_stock": "true", "limit": 250}
        if location_pk is not None:
            params["location"] = location_pk
        payload = self._request("GET", "stock/", params)
        items = [item for item in self._results(payload) if _decimal(item.get("quantity")) > 0]
        # Prefer the batch that expires first, then the oldest stock item.
        items.sort(key=lambda item: (str(item.get("expiry_date") or "9999-12-31"), int(item.get("pk") or 0)))
        return items

    def plan_consumption(self, lines: Iterable[ConsumeLine]) -> list[LinePlan]:
        """Resolve parts and split each quantity over available stock items.

        Raises :class:`InsufficientStock` when any line cannot be satisfied; in
        that case the caller must not change any stock.
        """

        plans: list[LinePlan] = []
        shortages: list[Shortage] = []
        location_cache: dict[str, int | None] = {}

        for line in lines:
            part = self.find_part(ipn=line.part_ipn, pk=line.part_pk)
            part_pk = int(part["pk"])
            label = str(part.get("IPN") or part.get("name") or part_pk)

            location_pk = None
            if line.location:
                if line.location not in location_cache:
                    location_cache[line.location] = self.location_pk(line.location)
                location_pk = location_cache[line.location]

            stock = self.stock_items(part_pk, location_pk)
            available = sum((_decimal(item.get("quantity")) for item in stock), Decimal("0"))
            plan = LinePlan(
                line=line,
                part_pk=part_pk,
                part_ipn=str(part.get("IPN") or ""),
                part_name=str(part.get("name") or ""),
            )
            remaining = line.quantity
            for item in stock:
                if remaining <= 0:
                    break
                take = min(remaining, _decimal(item.get("quantity")))
                if take <= 0:
                    continue
                plan.allocations.append(Allocation(stock_item=int(item["pk"]), quantity=take))
                remaining -= take
            if remaining > 0:
                shortages.append(Shortage(part=label, required=line.quantity, available=available))
            plans.append(plan)

        if shortages:
            raise InsufficientStock(shortages)
        return plans

    def consume(self, plans: Sequence[LinePlan], notes: str) -> dict[str, Any]:
        """Remove the planned quantities from InvenTree stock."""

        items = [
            {"pk": allocation.stock_item, "quantity": _quantity(allocation.quantity)}
            for plan in plans
            for allocation in plan.allocations
        ]
        if not items:
            return {"removed": []}
        try:
            self._request("POST", "stock/remove/", json={"items": items, "notes": notes})
        except InventreeError as exc:
            if "HTTP 404" not in str(exc) and "HTTP 405" not in str(exc):
                raise
            # Older InvenTree releases only expose the per-item endpoint.
            removed: list[dict[str, Any]] = []
            try:
                for item in items:
                    self._request(
                        "POST",
                        f"stock/item/{item['pk']}/remove/",
                        json={"quantity": item["quantity"], "notes": notes},
                    )
                    removed.append(item)
            except InventreeError as exc:
                if removed:
                    raise PartialConsumption(removed, exc) from exc
                raise
            return {"removed": removed, "fallback": "per-item"}
        return {"removed": items}

    def low_stock_parts(self, limit: int = 200) -> list[dict[str, Any]]:
        payload = self._request("GET", "part/", {"low_stock": "true", "limit": limit})
        return self._results(payload)

    def check_connection(self) -> dict[str, Any]:
        """Cheap read-only call used by ``bridge check``."""

        payload = self._request("GET", "part/", {"limit": 1})
        results = self._results(payload)
        count = payload.get("count") if isinstance(payload, Mapping) else len(results)
        return {"count": count, "sample": results[0].get("IPN") if results else None}
