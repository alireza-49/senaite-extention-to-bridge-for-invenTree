from bridge.journal import DONE, PENDING, UNKNOWN, Journal


def test_state_roundtrip_and_prefix():
    journal = Journal(":memory:")
    journal.set_state("low_stock:7", {"alerted_at": "2026-01-01T00:00:00+00:00"})
    journal.set_state("started_at", "2026-01-01T00:00:00+00:00")

    assert journal.get_state("low_stock:7")["alerted_at"].startswith("2026-01-01")
    assert journal.keys_with_prefix("low_stock:") == ["low_stock:7"]

    journal.delete_state("low_stock:7")
    assert journal.get_state("low_stock:7") is None
    assert journal.keys_with_prefix("low_stock:") == []


def test_consumption_upsert_keeps_a_single_row():
    journal = Journal(":memory:")
    journal.set_consumption("uid:rule", "uid", "rule", PENDING, {"step": 1})
    journal.set_consumption("uid:rule", "uid", "rule", DONE, {"step": 2})

    record = journal.consumption("uid:rule")
    assert record["status"] == DONE
    assert '"step": 2' in record["detail"]
    assert journal.count_by_status() == {DONE: 1}


def test_pending_records_are_recovered_as_unknown():
    journal = Journal(":memory:")
    journal.set_consumption("uid:rule", "uid", "rule", PENDING, {})

    recovered = journal.recover_pending()

    assert len(recovered) == 1
    assert journal.consumption("uid:rule")["status"] == UNKNOWN
    assert journal.recover_pending() == []
