from decimal import Decimal

import pytest

from bridge.config import ConsumeLine, Rule
from bridge.inventree import InventreeClient
from bridge.journal import DONE, INSUFFICIENT, PENDING, UNKNOWN, Journal
from bridge.senaite import SenaiteClient, SenaiteError
from bridge.service import Bridge
from tests.fakes import (
    FakeServer,
    InventreeHandler,
    RecordingNotifier,
    SenaiteHandler,
    add_analysis,
    add_part,
    add_stock_item,
    inventree_state,
    iso_now,
    make_config,
    senaite_state,
    tpc_rule,
)


@pytest.fixture
def world():
    senaite = FakeServer(SenaiteHandler, senaite_state())
    inventree = FakeServer(InventreeHandler, inventree_state())
    state = inventree.state
    state["locations"] = [{"pk": 5, "name": "Micro Lab"}]
    add_part(state, pk=1, ipn="PETRI-90", minimum_stock=5, stock=0)
    add_stock_item(state, part_pk=1, quantity=1, pk=11, expiry_date="2027-01-01")
    add_stock_item(state, part_pk=1, quantity=10, pk=12, expiry_date="2028-01-01")
    add_part(state, pk=3, ipn="PCA-MEDIUM", minimum_stock=20, stock=100, location=5)
    yield {"senaite": senaite, "inventree": inventree, "notifier": RecordingNotifier()}
    senaite.close()
    inventree.close()


def build(world, **kwargs) -> Bridge:
    config = make_config(world["senaite"].url, world["inventree"].url, rules=kwargs.pop("rules", (tpc_rule(),)), **kwargs)
    bridge = Bridge(
        config,
        senaite=SenaiteClient(config.senaite.base_url, "api", "secret"),
        inventree=InventreeClient(config.inventree.base_url, "tok"),
        journal=Journal(":memory:"),
        notifier=world["notifier"],
    )
    bridge.startup()
    return bridge


def quantities(state) -> dict[int, float]:
    return {item["pk"]: item["quantity"] for item in state["stock"]}


def test_verified_analysis_consumes_configured_parts(world):
    add_analysis(world["senaite"].state)
    bridge = build(world)

    report = bridge.run_once(include_low_stock=False)

    assert report["analyses"]["consumed"] == 1
    calls = world["inventree"].state["remove_calls"]
    assert len(calls) == 1
    assert calls[0]["items"] == [
        {"pk": 11, "quantity": "1"},
        {"pk": 12, "quantity": "1"},
        {"pk": 3, "quantity": "50"},
    ]
    assert "S-0001" in calls[0]["notes"]
    assert quantities(world["inventree"].state) == {11: 0.0, 12: 9.0, 3: 50.0}
    assert len(world["notifier"].sent) == 1
    assert "کسر شد" in world["notifier"].sent[0][0]
    assert bridge.journal.consumption("analysis-uid:microbiology-tpc")["status"] == DONE


def test_polling_again_never_consumes_twice(world):
    add_analysis(world["senaite"].state)
    bridge = build(world)

    bridge.run_once(include_low_stock=False)
    report = bridge.run_once(include_low_stock=False)

    assert report["analyses"]["already_processed"] == 1
    assert report["analyses"]["consumed"] == 0
    assert len(world["inventree"].state["remove_calls"]) == 1


def test_insufficient_stock_removes_nothing_and_alerts_once(world):
    state = world["inventree"].state
    state["stock"] = [item for item in state["stock"] if item["pk"] != 12]
    add_analysis(world["senaite"].state)
    bridge = build(world)

    first = bridge.run_once(include_low_stock=False)
    second = bridge.run_once(include_low_stock=False)

    assert first["analyses"]["insufficient"] == 1
    assert second["analyses"]["insufficient"] == 1
    assert world["inventree"].state["remove_calls"] == []
    assert quantities(state) == {11: 1.0, 3: 100.0}
    assert len(world["notifier"].sent) == 1
    assert "موجودی ناکافی" in world["notifier"].sent[0][0]
    assert bridge.journal.consumption("analysis-uid:microbiology-tpc")["status"] == INSUFFICIENT


def test_consumption_resumes_once_stock_is_replenished(world):
    state = world["inventree"].state
    state["stock"] = [item for item in state["stock"] if item["pk"] != 12]
    add_analysis(world["senaite"].state)
    bridge = build(world)
    bridge.run_once(include_low_stock=False)

    add_stock_item(state, part_pk=1, quantity=5, pk=13, expiry_date="2029-01-01")
    report = bridge.run_once(include_low_stock=False)

    assert report["analyses"]["consumed"] == 1
    assert quantities(state)[3] == 50.0


def test_dry_run_changes_nothing_and_keeps_the_window_open(world):
    add_analysis(world["senaite"].state)
    bridge = build(world, dry_run=True)

    report = bridge.run_once(include_low_stock=False)

    assert report["analyses"]["dry_run"] is True
    assert report["analyses"]["would_consume"] == 1
    assert world["inventree"].state["remove_calls"] == []
    assert quantities(world["inventree"].state) == {11: 1.0, 12: 10.0, 3: 100.0}
    assert bridge.journal.consumption("analysis-uid:microbiology-tpc") is None
    assert world["notifier"].sent == []


def test_analysis_created_before_the_service_started_is_skipped(world):
    add_analysis(world["senaite"].state, created=iso_now(-30))
    bridge = build(world)

    report = bridge.run_once(include_low_stock=False)

    assert report["analyses"]["skipped_old"] == 1
    assert world["inventree"].state["remove_calls"] == []


def test_published_analysis_is_picked_up(world):
    add_analysis(world["senaite"].state, review_state="published")
    bridge = build(world)

    report = bridge.run_once(include_low_stock=False)

    assert report["analyses"]["consumed"] == 1


def test_unmatched_analysis_is_ignored(world):
    add_analysis(world["senaite"].state, keyword="Chlorine", sample_type="water")
    bridge = build(world)

    report = bridge.run_once(include_low_stock=False)

    assert report["analyses"]["unmatched"] == 1
    assert world["inventree"].state["remove_calls"] == []


def test_interrupted_consumption_is_marked_unknown_and_not_retried(world):
    add_analysis(world["senaite"].state)
    config = make_config(world["senaite"].url, world["inventree"].url, rules=(tpc_rule(),))
    journal = Journal(":memory:")
    journal.set_consumption("analysis-uid:microbiology-tpc", "analysis-uid", "microbiology-tpc", PENDING, {})
    bridge = Bridge(
        config,
        senaite=SenaiteClient(config.senaite.base_url, "api", "secret"),
        inventree=InventreeClient(config.inventree.base_url, "tok"),
        journal=journal,
        notifier=world["notifier"],
    )

    bridge.startup()
    report = bridge.run_once(include_low_stock=False)

    assert journal.consumption("analysis-uid:microbiology-tpc")["status"] == UNKNOWN
    assert report["analyses"]["already_processed"] == 1
    assert world["inventree"].state["remove_calls"] == []
    assert any("نامشخص" in subject for subject, _, _ in world["notifier"].sent)


def test_partial_per_item_removal_is_never_retried(world):
    state = world["inventree"].state
    state["remove_missing"] = True  # forces the per-item fallback
    state["fail_pk"] = 12  # second item fails after the first one was removed
    add_analysis(world["senaite"].state)
    bridge = build(world)

    first = bridge.run_once(include_low_stock=False)
    second = bridge.run_once(include_low_stock=False)

    assert first["analyses"]["failed"] == 1
    assert second["analyses"]["already_processed"] == 1
    assert bridge.journal.consumption("analysis-uid:microbiology-tpc")["status"] == UNKNOWN
    assert quantities(state) == {11: 0.0, 12: 10.0, 3: 100.0}
    assert any("نیمه‌کاره" in subject for subject, _, _ in world["notifier"].sent)


def test_failed_low_stock_delivery_does_not_silence_the_part(world):
    state = world["inventree"].state
    state["stock"][2]["quantity"] = 1.0

    class BrokenNotifier:
        def send(self, subject, text, payload=None):
            return [{"channel": "telegram", "status": "error", "error": "network down"}]

    config = make_config(world["senaite"].url, world["inventree"].url, rules=())
    bridge = Bridge(
        config,
        senaite=SenaiteClient(config.senaite.base_url, "api", "secret"),
        inventree=InventreeClient(config.inventree.base_url, "tok"),
        journal=Journal(":memory:"),
        notifier=BrokenNotifier(),
    )
    bridge.startup()

    first = bridge.poll_low_stock()
    second = bridge.poll_low_stock()

    assert first["alerted"] == 1
    assert second["alerted"] == 1  # tried again, not suppressed for 24h
    assert bridge.journal.get_state("low_stock:3") is None


def test_dry_run_low_stock_keeps_the_journal_untouched(world):
    state = world["inventree"].state
    state["stock"][2]["quantity"] = 1.0
    bridge = build(world, rules=(), dry_run=True)

    first = bridge.poll_low_stock()
    second = bridge.poll_low_stock()

    assert first["alerted"] == 1
    assert second["alerted"] == 1
    assert first["dry_run"] is True
    assert bridge.journal.get_state("low_stock:3") is None


def test_low_stock_alerts_once_and_resets_when_refilled(world):
    state = world["inventree"].state
    bridge = build(world, rules=())
    state["stock"][2]["quantity"] = 10.0  # PCA-MEDIUM (min 20) is now low

    first = bridge.poll_low_stock()
    second = bridge.poll_low_stock()

    assert first["alerted"] == 1
    assert second["alerted"] == 0
    assert len(world["notifier"].sent) == 1
    assert "زیر حد موجودی" in world["notifier"].sent[0][0]

    state["stock"][2]["quantity"] = 100.0
    refilled = bridge.poll_low_stock()
    assert refilled["low"] == 0
    assert refilled["cleared"] == 1

    state["stock"][2]["quantity"] = 1.0
    again = bridge.poll_low_stock()
    assert again["alerted"] == 1


def test_low_stock_repeats_after_the_configured_window(world):
    state = world["inventree"].state
    state["stock"][2]["quantity"] = 1.0
    bridge = build(world, rules=(), repeat_after_hours=0)

    bridge.poll_low_stock()
    second = bridge.poll_low_stock()

    assert second["alerted"] == 1
    assert len(world["notifier"].sent) == 2


def test_check_reports_connections_and_rule_targets(world):
    bridge = build(world)

    report = bridge.check()

    assert report["ok"] is True
    assert report["senaite"]["user"] == "api"
    assert report["inventree"]["count"] == 2
    assert report["problems"] == []


def test_check_reports_a_missing_rule_target(world):
    rule = Rule(
        id="missing",
        keywords=("TPC",),
        sample_types=(),
        consume=(ConsumeLine(part_ipn="DOES-NOT-EXIST", quantity=Decimal("1")),),
    )
    bridge = build(world, rules=(rule,))

    report = bridge.check()

    assert report["ok"] is True
    assert any("DOES-NOT-EXIST" in problem for problem in report["problems"])


def test_wrong_senaite_credentials_are_detected(world):
    client = SenaiteClient(f"{world['senaite'].url}/SENAITE", "api", "wrong")

    with pytest.raises(SenaiteError, match="credentials"):
        client.check_login()
