"""The bridge itself: two polling loops plus the consumption logic."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from . import __version__
from .config import Config, Rule
from .inventree import (
    InventreeClient,
    InventreeError,
    InsufficientStock,
    PartNotFound,
    PartialConsumption,
)
from .journal import DONE, FAILED, INSUFFICIENT, PENDING, UNKNOWN, Journal, iso, parse_iso, utcnow
from .notifier import (
    Notifier,
    consumption_message,
    failure_message,
    insufficient_message,
    low_stock_message,
    partial_message,
)
from .rules import analysis_keyword, analysis_uid, describe_analysis, match_rule, sample_id
from .senaite import SenaiteClient, SenaiteError

LOGGER = logging.getLogger(__name__)

# Consumption outcome -> report counter. ``dry_run`` is intentionally not a
# report key of its own, so the boolean flag cannot be overwritten by a count.
COUNTERS = {
    "consumed": "consumed",
    "insufficient": "insufficient",
    "failed": "failed",
    "already_processed": "already_processed",
    "dry_run": "would_consume",
}


def _created(analysis: Mapping[str, Any]) -> datetime | None:
    for key in ("created", "getResultCaptureDate", "getDateVerified", "modified"):
        value = parse_iso(analysis.get(key) if isinstance(analysis.get(key), str) else None)
        if value is not None:
            return value
    return None


class Bridge:
    def __init__(
        self,
        config: Config,
        senaite: SenaiteClient | None = None,
        inventree: InventreeClient | None = None,
        journal: Journal | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.dry_run = config.run.dry_run
        self.senaite = senaite or SenaiteClient(
            config.senaite.base_url,
            config.senaite.username,
            config.senaite.password,
            verify_ssl=config.senaite.verify_ssl,
            timeout=config.senaite.timeout,
        )
        self.inventree = inventree or InventreeClient(
            config.inventree.base_url,
            config.inventree.token,
            verify_ssl=config.inventree.verify_ssl,
            timeout=config.inventree.timeout,
        )
        self.journal = journal or Journal(config.run.journal_path)
        self.notifier = notifier or Notifier(config.notify, dry_run=self.dry_run)
        self._lock = threading.Lock()
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def startup(self) -> dict[str, Any]:
        """Record the install time and settle records interrupted by a crash."""

        started = self.journal.get_state("started_at")
        if not started:
            started = iso(utcnow())
            self.journal.set_state("started_at", started)
        recovered = self.journal.recover_pending()
        for record in recovered:
            self.journal.record_event("recovered_unknown", record)
            LOGGER.error(
                "consumption %s was interrupted while pending; marked unknown, check InvenTree manually",
                record.get("key"),
            )
            self.notifier.send(
                "SENAITE bridge: کسری نامشخص",
                "\n".join(
                    [
                        f"قاعده: {record.get('rule_id')}",
                        f"UID آزمون: {record.get('analysis_uid')}",
                        "سرویس پیش از ثبت پاسخ InvenTree متوقف شده است؛ لطفاً موجودی را دستی بررسی کنید.",
                    ]
                ),
                record,
            )
        self._started = True
        return {"started_at": started, "recovered_unknown": len(recovered)}

    def close(self) -> None:
        self.journal.close()

    # -- checks ------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        report: dict[str, Any] = {"version": __version__, "ok": True, "problems": [], "errors": []}
        try:
            report["senaite"] = {
                "version": self.senaite.version().get("version"),
                "user": self.senaite.check_login(),
                "base_url": self.config.senaite.base_url,
            }
        except SenaiteError as exc:
            report["ok"] = False
            report["errors"].append(f"SENAITE: {exc}")
        try:
            report["inventree"] = self.inventree.check_connection()
        except InventreeError as exc:
            report["ok"] = False
            report["errors"].append(f"InvenTree: {exc}")
        report["rules"] = [rule.describe() for rule in self.config.rules]
        report["notify_channels"] = list(self.config.notify.channels)
        report["dry_run"] = self.dry_run
        # Rule targets must exist, otherwise a verified analysis cannot be consumed.
        for rule in self.config.rules:
            for line in rule.consume:
                try:
                    part = self.inventree.find_part(ipn=line.part_ipn, pk=line.part_pk)
                except InventreeError as exc:
                    report["problems"].append(f"rule {rule.id}: {exc}")
                    continue
                quantity = part.get("in_stock")
                if quantity is not None:
                    try:
                        available = float(quantity)
                    except (TypeError, ValueError):
                        available = None
                    if available is not None and available < float(line.quantity):
                        report["problems"].append(
                            f"rule {rule.id}: {part.get('IPN')} has {available} in stock, "
                            f"needs {line.quantity} per use"
                        )
        return report

    # -- scenario 1: verified analysis -> stock deduction ------------------

    def poll_analyses(self) -> dict[str, Any]:
        with self._lock:
            return self._poll_analyses_locked()

    def _poll_analyses_locked(self) -> dict[str, Any]:
        config = self.config
        started = parse_iso(self.journal.get_state("started_at")) or utcnow()
        cutoff = started - timedelta(days=config.senaite.initial_lookback_days)
        window_start = utcnow() - timedelta(days=config.senaite.rescan_days)
        since = max(cutoff, window_start)

        result: dict[str, Any] = {
            "checked_at": iso(utcnow()),
            "dry_run": self.dry_run,
            "since": iso(since),
            "fetched": 0,
            "skipped_old": 0,
            "unmatched": 0,
            "already_processed": 0,
            "consumed": 0,
            "insufficient": 0,
            "failed": 0,
            "would_consume": 0,
            "details": [],
            "truncated": False,
        }

        analyses = self.senaite.analyses(config.senaite.trigger_states, limit=config.senaite.batch_size)
        result["fetched"] = len(analyses)
        if analyses:
            result["truncated"] = len(analyses) >= config.senaite.batch_size

        samples: dict[str, Mapping[str, Any] | None] = {}
        ordered = sorted(analyses, key=lambda item: _created(item) or datetime.min.replace(tzinfo=timezone.utc))
        for analysis in ordered:
            created = _created(analysis)
            if created is not None and created < cutoff:
                result["skipped_old"] += 1
                continue
            if created is not None and created < since:
                # Older than the rescan window: nothing new can hide there.
                continue

            parent_uid = str(analysis.get("parent_uid") or "")
            if parent_uid and parent_uid not in samples:
                samples[parent_uid] = self.senaite.analysis_request(parent_uid)
            sample = samples.get(parent_uid)

            rule = match_rule(config.rules, analysis, sample)
            if rule is None:
                result["unmatched"] += 1
                continue

            outcome, detail = self._consume(analysis, sample, rule)
            counter = COUNTERS.get(outcome, "consumed")
            result[counter] = int(result.get(counter, 0)) + 1
            result["details"].append(detail)

        self.journal.record_event("poll_analyses", {k: v for k, v in result.items() if k != "details"})
        LOGGER.info(
            "poll: fetched=%s consumed=%s insufficient=%s failed=%s unmatched=%s skipped_old=%s",
            result["fetched"],
            result["consumed"],
            result["insufficient"],
            result["failed"],
            result["unmatched"],
            result["skipped_old"],
        )
        return result

    def _consume(
        self,
        analysis: Mapping[str, Any],
        sample: Mapping[str, Any] | None,
        rule: Rule,
    ) -> tuple[str, dict[str, Any]]:
        uid = analysis_uid(analysis)
        key = f"{uid}:{rule.id}"
        sid = sample_id(sample, analysis) or "?"
        keyword = analysis_keyword(analysis)
        detail: dict[str, Any] = {
            "analysis_uid": uid,
            "sample": sid,
            "keyword": keyword,
            "rule": rule.id,
        }

        existing = self.journal.consumption(key)
        if existing and existing.get("status") in (DONE, PENDING, UNKNOWN):
            detail["status"] = "already_processed"
            detail["previous_status"] = existing.get("status")
            return "already_processed", detail

        try:
            plans = self.inventree.plan_consumption(rule.consume)
        except InsufficientStock as exc:
            shortages = [f"{item.describe()}: نیاز {item.required}، موجود {item.available}" for item in exc.shortages]
            detail.update({"status": "insufficient", "shortages": shortages})
            if self.dry_run:
                LOGGER.info("[dry-run] insufficient stock for %s: %s", sid, "; ".join(shortages))
                return "insufficient", detail
            previous = existing.get("status") if existing else None
            self.journal.set_consumption(key, uid, rule.id, INSUFFICIENT, detail)
            if previous != INSUFFICIENT:
                subject, text = insufficient_message(
                    sample_id=sid, keyword=keyword, rule_id=rule.id, shortages=shortages
                )
                self.notifier.send(subject, text, detail)
                self.journal.record_event("insufficient", detail)
            return "insufficient", detail
        except (PartNotFound, InventreeError) as exc:
            detail.update({"status": "failed", "error": str(exc)})
            if self.dry_run:
                LOGGER.error("[dry-run] planning stock for %s failed: %s", sid, exc)
                return "failed", detail
            previous = existing.get("status") if existing else None
            self.journal.set_consumption(key, uid, rule.id, FAILED, detail)
            LOGGER.error("planning stock for %s failed: %s", key, exc)
            if previous != FAILED:
                subject, text = failure_message(
                    sample_id=sid, keyword=keyword, rule_id=rule.id, error=str(exc)
                )
                self.notifier.send(subject, text, detail)
            return "failed", detail

        allocations = [
            {"part": plan.part_ipn or plan.part_pk, "stock_item": item.stock_item, "quantity": str(item.quantity)}
            for plan in plans
            for item in plan.allocations
        ]
        describe = [f"{plan.part_ipn or plan.part_pk}: {plan.total}" for plan in plans]
        detail.update({"allocations": allocations, "parts": describe})

        if self.dry_run:
            detail["status"] = "dry_run"
            LOGGER.info("[dry-run] would consume for %s: %s", sid, ", ".join(describe))
            self.journal.record_event("dry_run", detail)
            return "dry_run", detail

        notes = (
            f"SENAITE {sid} | {keyword} = {analysis.get('getResult', analysis.get('Result', ''))} "
            f"{analysis.get('getUnit', analysis.get('Unit', ''))} | analysis {uid} | rule {rule.id}"
        ).replace("  ", " ")

        # Write the intent first: if the process dies during the HTTP call the
        # record is recovered as ``unknown`` instead of being retried.
        self.journal.set_consumption(key, uid, rule.id, PENDING, detail)
        try:
            response = self.inventree.consume(plans, notes)
        except PartialConsumption as exc:
            # Part of the stock already moved: retrying would deduct it twice.
            partial_detail = dict(detail, status=UNKNOWN, error=str(exc), removed=exc.removed)
            self.journal.set_consumption(key, uid, rule.id, UNKNOWN, partial_detail)
            self.journal.record_event("partial_consumption", partial_detail)
            LOGGER.error("partial stock removal for %s: %s", key, exc)
            subject, text = partial_message(
                sample_id=sid,
                keyword=keyword,
                rule_id=rule.id,
                error=str(exc),
                removed=exc.removed,
            )
            self.notifier.send(subject, text, partial_detail)
            return "failed", partial_detail
        except InventreeError as exc:
            failure_detail = dict(detail, status="failed", error=str(exc))
            self.journal.set_consumption(key, uid, rule.id, FAILED, failure_detail)
            LOGGER.error("stock removal for %s failed: %s", key, exc)
            subject, text = failure_message(sample_id=sid, keyword=keyword, rule_id=rule.id, error=str(exc))
            self.notifier.send(subject, text, failure_detail)
            return "failed", failure_detail

        done_detail = dict(detail, status="done", response=response)
        self.journal.set_consumption(key, uid, rule.id, DONE, done_detail)
        self.journal.record_event("consumed", done_detail)
        LOGGER.info("consumed stock for %s (%s): %s", sid, rule.id, ", ".join(describe))
        subject, text = consumption_message(
            sample_id=sid,
            analysis_uid=uid,
            keyword=keyword,
            result=str(analysis.get("getResult", analysis.get("Result", ""))),
            unit=str(analysis.get("getUnit", analysis.get("Unit", ""))),
            rule_id=rule.id,
            parts=describe,
        )
        self.notifier.send(subject, text, done_detail)
        return "consumed", done_detail

    # -- scenario 2: low stock -> alert ------------------------------------

    def poll_low_stock(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            return self._poll_low_stock_locked(force)

    def _poll_low_stock_locked(self, force: bool) -> dict[str, Any]:
        config = self.config
        result: dict[str, Any] = {
            "checked_at": iso(utcnow()),
            "enabled": config.low_stock.enabled,
            "low": 0,
            "alerted": 0,
            "cleared": 0,
            "dry_run": self.dry_run,
        }
        if not config.low_stock.enabled:
            return result

        parts = self.inventree.low_stock_parts()
        result["low"] = len(parts)
        now = utcnow()
        repeat = timedelta(hours=config.low_stock.repeat_after_hours)
        due: list[Mapping[str, Any]] = []
        low_keys: set[str] = set()

        for part in parts:
            pk = part.get("pk")
            if pk is None:
                continue
            key = f"low_stock:{pk}"
            low_keys.add(key)
            state = self.journal.get_state(key) or {}
            last = parse_iso(state.get("alerted_at"))
            if force or last is None or (now - last) >= repeat:
                due.append(part)

        # Clear remembered state once a part is replenished, so the next drop
        # alerts again. A dry run must not touch the journal.
        if not self.dry_run:
            for key in self.journal.keys_with_prefix("low_stock:"):
                if key not in low_keys:
                    self.journal.delete_state(key)
                    result["cleared"] += 1

        result["alerted"] = len(due)
        if due:
            subject, text = low_stock_message(due)
            deliveries = self.notifier.send(subject, text, {"parts": [dict(part) for part in due]})
            delivered = any(item.get("status") in ("sent", "dry_run") for item in deliveries)
            # Remember the alert only once it actually went out, otherwise a
            # broken channel would silence the part for the whole repeat window.
            if delivered and not self.dry_run:
                for part in due:
                    self.journal.set_state(
                        f"low_stock:{part.get('pk')}",
                        {
                            "alerted_at": iso(now),
                            "ipn": part.get("IPN"),
                            "name": part.get("name"),
                            "in_stock": part.get("in_stock"),
                            "minimum_stock": part.get("minimum_stock"),
                        },
                    )
            self.journal.record_event("low_stock", {"parts": [dict(part) for part in due]})
            LOGGER.warning("low stock alert for %s part(s)", len(due))
        return result

    # -- combined ----------------------------------------------------------

    def run_once(self, include_low_stock: bool = True, force_low_stock: bool = False) -> dict[str, Any]:
        if not self._started:
            self.startup()
        report: dict[str, Any] = {}
        try:
            report["analyses"] = self.poll_analyses()
        except SenaiteError as exc:
            LOGGER.error("SENAITE poll failed: %s", exc)
            report["analyses"] = {"error": str(exc), "consumed": 0, "failed": 0, "insufficient": 0}
        if include_low_stock:
            try:
                report["low_stock"] = self.poll_low_stock(force=force_low_stock)
            except InventreeError as exc:
                LOGGER.error("low stock poll failed: %s", exc)
                report["low_stock"] = {"error": str(exc)}
        return report

    def notify_test(self) -> list[dict[str, Any]]:
        return self.notifier.send(
            "SENAITE bridge: پیام آزمایشی",
            f"اتصال اطلاع‌رسانی پل برقرار است (نسخهٔ {__version__}).",
            {"test": True},
        )
