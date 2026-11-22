"""SQLite journal: idempotency keys, audit trail and low-stock alert state.

The journal is the reason the bridge can be restarted, can run twice, or can
run behind an HTTP retry without ever deducting stock twice for the same
analysis.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

# Consumption record statuses.
PENDING = "pending"      # intent written, InvenTree call not confirmed yet
DONE = "done"            # stock deducted and confirmed
FAILED = "failed"        # InvenTree rejected the call; retried on the next poll
INSUFFICIENT = "insufficient"  # not enough stock; retried on the next poll
UNKNOWN = "unknown"      # crash between the call and its record: needs a human

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consumption (
    key TEXT PRIMARY KEY,
    analysis_uid TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS consumption_status ON consumption (status);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Journal:
    """Thread-safe journal wrapper around a single SQLite file."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- key/value state ---------------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        if row is None or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return row["value"]

    def set_state(self, key: str, value: Any) -> None:
        payload = None if value is None else json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._connection.execute(
                "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, payload, iso(utcnow())),
            )
            self._connection.commit()

    def keys_with_prefix(self, prefix: str) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT key FROM state WHERE key LIKE ?", (prefix.replace("%", "") + "%",)
            ).fetchall()
        return [row["key"] for row in rows]

    def delete_state(self, key: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM state WHERE key = ?", (key,))
            self._connection.commit()

    # -- consumption records ----------------------------------------------

    def consumption(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM consumption WHERE key = ?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def set_consumption(
        self,
        key: str,
        analysis_uid: str,
        rule_id: str,
        status: str,
        detail: Any = None,
    ) -> dict[str, Any]:
        now = iso(utcnow())
        payload = None if detail is None else json.dumps(detail, ensure_ascii=False, default=str)
        with self._lock:
            self._connection.execute(
                "INSERT INTO consumption (key, analysis_uid, rule_id, status, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET status = excluded.status, detail = excluded.detail, "
                "updated_at = excluded.updated_at",
                (key, analysis_uid, rule_id, status, payload, now, now),
            )
            self._connection.commit()
        return self.consumption(key) or {}

    def recover_pending(self) -> list[dict[str, Any]]:
        """Mark interrupted ``pending`` records as ``unknown``.

        A pending record means the process stopped between writing the intent
        and recording InvenTree's answer. Stock may or may not have moved, so
        the bridge must not retry automatically.
        """

        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM consumption WHERE status = ?", (PENDING,)
            ).fetchall()
        recovered: list[dict[str, Any]] = []
        for row in rows:
            record = self.set_consumption(
                row["key"],
                row["analysis_uid"],
                row["rule_id"],
                UNKNOWN,
                {"previous_detail": row["detail"], "reason": "interrupted while pending"},
            )
            recovered.append(record)
        return recovered

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS total FROM consumption GROUP BY status"
            ).fetchall()
        return {row["status"]: row["total"] for row in rows}

    # -- audit events ------------------------------------------------------

    def record_event(self, kind: str, payload: Any = None) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
                (iso(utcnow()), kind, None if payload is None else json.dumps(payload, ensure_ascii=False, default=str)),
            )
            self._connection.commit()

    def events(self, limit: int = 20, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            query += f" WHERE kind IN ({placeholders})"
            params.extend(kinds)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
