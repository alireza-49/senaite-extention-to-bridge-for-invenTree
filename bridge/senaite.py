"""Minimal client for the SENAITE JSON API (``senaite.jsonapi``).

Only the read-only routes the bridge needs are implemented. SENAITE answers
200 with an empty result set for wrong credentials, so authentication must be
verified through ``users/current`` (which reports ``authenticated``) rather
than by checking whether a collection is empty.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping
from urllib.parse import urljoin

import requests

API_SUFFIX = "/@@API/senaite/v1/"


class SenaiteError(RuntimeError):
    """Raised for transport, HTTP or payload problems."""


class SenaiteClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http = session or requests.Session()
        self._http.auth = (username, password)
        self._http.verify = verify_ssl
        self._http.headers.update({"Accept": "application/json"})

    # -- plumbing ----------------------------------------------------------

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/")) if path.startswith("@@") else (
            self.base_url + API_SUFFIX + path.lstrip("/")
        )

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = self._url(path)
        try:
            response = self._http.get(url, params=dict(params or {}), timeout=self.timeout)
        except requests.RequestException as exc:
            raise SenaiteError(f"GET {url} failed: {exc}") from exc
        if response.status_code != 200:
            raise SenaiteError(
                f"GET {url} returned HTTP {response.status_code}: {response.text[:200].strip()}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SenaiteError(
                f"GET {url} did not return JSON (is base_url pointing at the SENAITE site?)"
            ) from exc

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, Mapping):
            for key in ("items", "objects", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    # -- endpoints ---------------------------------------------------------

    def version(self) -> dict[str, Any]:
        payload = self._get("version")
        return payload if isinstance(payload, Mapping) else {}

    def current_user(self) -> dict[str, Any]:
        items = self._items(self._get("users/current"))
        return items[0] if items else {}

    def check_login(self) -> str:
        """Return the authenticated username or raise :class:`SenaiteError`."""

        user = self.current_user()
        if not user.get("authenticated"):
            raise SenaiteError(
                "SENAITE did not accept the credentials (anonymous session); "
                "check senaite.username/password"
            )
        return str(user.get("username") or "unknown")

    def analyses(self, states: Iterable[str], limit: int = 200) -> list[dict[str, Any]]:
        """Fetch analyses in the given review states, newest first.

        Results are de-duplicated by ``uid``; a later state (e.g. ``published``)
        wins over an earlier one (``verified``).
        """

        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for state in states:
            payload = self._get(
                "analysis",
                {"review_state": state, "limit": limit, "sort_on": "created", "sort_order": "descending"},
            )
            for item in self._items(payload):
                uid = str(item.get("uid") or "")
                if not uid:
                    continue
                if uid not in merged:
                    order.append(uid)
                merged[uid] = item
        return [merged[uid] for uid in order]

    def analysis_request(self, uid: str) -> dict[str, Any] | None:
        """Return one AnalysisRequest (sample) or ``None`` when not found."""

        try:
            payload = self._get(f"analysisrequest/{uid}")
        except SenaiteError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        if isinstance(payload, Mapping) and "uid" in payload:
            return dict(payload)
        items = self._items(payload)
        return items[0] if items else None
