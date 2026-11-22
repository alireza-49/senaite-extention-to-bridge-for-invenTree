"""Command line entry point: ``python -m bridge <command>``."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from . import __version__
from .config import Config, ConfigError, load_config, validate_for_run
from .http import serve
from .journal import iso, parse_iso, utcnow
from .service import Bridge

LOGGER = logging.getLogger("bridge")

DEFAULT_CONFIG = os.environ.get("BRIDGE_CONFIG", "config.yml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="senaite-inventree-bridge",
        description="SENAITE -> InvenTree bridge (stock consumption + low stock alerts)",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.yml (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="never change InvenTree stock")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify configuration, credentials and rule targets (read-only)")

    once = sub.add_parser("once", help="run one polling cycle and exit")
    once.add_argument("--since", help="ISO timestamp: treat analyses created after it as new (backfill)")
    once.add_argument("--no-low-stock", action="store_true", help="skip the low stock cycle")
    once.add_argument("--force-low-stock", action="store_true", help="alert even inside the repeat window")

    sub.add_parser("run", help="run the polling loops and the HTTP health/job server")

    sub.add_parser("notify-test", help="send a test message through the configured channels")

    events = sub.add_parser("events", help="show the most recent journal entries")
    events.add_argument("--limit", type=int, default=20)
    return parser


def configure_logging(level: str | None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or os.environ.get("LOG_LEVEL", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.dry_run and not config.run.dry_run:
        LOGGER.warning("dry-run forced on the command line: InvenTree stock will not change")
        config = dataclasses.replace(config, run=dataclasses.replace(config.run, dry_run=True))
    return config


def _print(payload: Any, as_json: bool) -> None:
    # Output is always JSON (stable for scripting); --json is kept as an alias.
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _bridge(config: Config, startup: bool = True) -> Bridge:
    bridge = Bridge(config)
    if startup:
        bridge.startup()
    return bridge


def _run_loop(bridge: Bridge, config: Config) -> int:
    stop = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.warning("received signal %s, stopping", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except ValueError:  # pragma: no cover - non main thread
            pass

    try:
        # Always serve: /healthz is what the container healthcheck uses. Only
        # the /jobs/* routes need the token and they answer 503 without it.
        server, _ = serve(bridge, config.run.http_host, config.run.http_port, config.run.http_token)
    except OSError as exc:
        LOGGER.error(
            "cannot listen on %s:%s: %s", config.run.http_host, config.run.http_port, exc
        )
        bridge.close()
        return 1
    if not config.run.http_token:
        LOGGER.warning("BRIDGE_TOKEN is empty: /jobs/* stays disabled, /healthz is still served")

    next_analysis = datetime.now(timezone.utc)
    next_low = datetime.now(timezone.utc) if config.low_stock.enabled else None
    try:
        while not stop.is_set():
            now = datetime.now(timezone.utc)
            if now >= next_analysis:
                try:
                    bridge.poll_analyses()
                except Exception:  # noqa: BLE001 - keep the loop alive
                    LOGGER.exception("analysis poll failed")
                next_analysis = datetime.now(timezone.utc) + timedelta(seconds=config.senaite.poll_seconds)
            if next_low is not None and now >= next_low:
                try:
                    bridge.poll_low_stock()
                except Exception:  # noqa: BLE001 - keep the loop alive
                    LOGGER.exception("low stock poll failed")
                next_low = datetime.now(timezone.utc) + timedelta(seconds=config.low_stock.poll_seconds)
            stop.wait(1.0)
    finally:
        if server is not None:
            server.shutdown()
        bridge.close()
        LOGGER.info("bridge stopped")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    try:
        config = _apply_overrides(load_config(args.config), args)
    except ConfigError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2

    for problem in validate_for_run(config):
        LOGGER.warning("%s", problem)

    if args.command == "check":
        # check must stay read-only: no startup(), so no pending record is
        # rewritten to unknown and no alert is sent behind the live service.
        bridge = _bridge(config, startup=False)
        try:
            report = bridge.check()
        finally:
            bridge.close()
        _print(report, args.json)
        return 0 if report.get("ok") else 1

    if args.command == "notify-test":
        bridge = _bridge(config)
        try:
            results = bridge.notify_test()
        finally:
            bridge.close()
        _print({"channels": results}, args.json)
        return 0 if results and all(item.get("status") in ("sent", "dry_run") for item in results) else 1

    if args.command == "events":
        bridge = _bridge(config, startup=False)
        try:
            _print(bridge.journal.events(args.limit), args.json)
        finally:
            bridge.close()
        return 0

    if args.command == "once":
        if args.since:
            moment = parse_iso(args.since)
            if moment is None:
                LOGGER.error("invalid --since value: %s", args.since)
                return 2
            # Widen the rescan window so the backfill range is actually read.
            days = max(config.senaite.rescan_days, (utcnow() - moment).total_seconds() / 86400)
            config = dataclasses.replace(
                config, senaite=dataclasses.replace(config.senaite, rescan_days=days)
            )
            LOGGER.warning("treating analyses created after %s as new", iso(moment))
        bridge = _bridge(config)
        try:
            if args.since:
                bridge.journal.set_state("started_at", iso(parse_iso(args.since) or utcnow()))
            report = bridge.run_once(
                include_low_stock=not args.no_low_stock, force_low_stock=args.force_low_stock
            )
        finally:
            bridge.close()
        _print(report, args.json)
        failure = report.get("analyses", {}).get("error") is not None
        return 1 if failure else 0

    if args.command == "run":
        bridge = _bridge(config)
        return _run_loop(bridge, config)

    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
