"""
Automated tests for the reconciliation service.

Run with:  pytest -v

Each test spins up a fresh in-memory-equivalent SQLite DB file so tests
don't interfere with each other or with any local dev DB.
"""
import os
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./test_reconciliation.db"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    """Recreate all tables before every test for isolation."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def make_event(**overrides):
    base = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_1",
        "merchant_name": "TestCo",
        "amount": 1000.0,
        "currency": "INR",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ingest_creates_transaction():
    event = make_event()
    r = client.post("/events", json=event)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "accepted"
    assert body["transaction_payment_status"] == "initiated"

    detail = client.get(f"/transactions/{event['transaction_id']}")
    assert detail.status_code == 200
    assert detail.json()["payment_status"] == "initiated"
    assert len(detail.json()["events"]) == 1


def test_duplicate_event_is_idempotent():
    event = make_event(event_id="dupe-1")
    r1 = client.post("/events", json=event)
    r2 = client.post("/events", json=event)  # exact resubmission
    assert r1.json()["status"] == "accepted"
    assert r2.json()["status"] == "duplicate_ignored"

    detail = client.get(f"/transactions/{event['transaction_id']}")
    # only ONE event should be recorded, not two
    assert len(detail.json()["events"]) == 1


def test_duplicate_does_not_corrupt_state():
    txn_id = str(uuid.uuid4())
    e1 = make_event(transaction_id=txn_id, event_type="payment_initiated", timestamp="2026-01-01T00:00:00Z")
    e2 = make_event(transaction_id=txn_id, event_type="payment_processed", timestamp="2026-01-01T00:01:00Z")
    client.post("/events", json=e1)
    client.post("/events", json=e2)

    # Resubmit e1 (the stale/earlier event) again as a duplicate.
    r = client.post("/events", json=e1)
    assert r.json()["status"] == "duplicate_ignored"

    # Transaction should still show "processed", not regress to "initiated".
    detail = client.get(f"/transactions/{txn_id}")
    assert detail.json()["payment_status"] == "processed"


def test_full_lifecycle_settles_correctly():
    txn_id = str(uuid.uuid4())
    for etype, ts in [
        ("payment_initiated", "2026-01-01T00:00:00Z"),
        ("payment_processed", "2026-01-01T00:01:00Z"),
        ("settled", "2026-01-01T05:00:00Z"),
    ]:
        client.post("/events", json=make_event(transaction_id=txn_id, event_type=etype, timestamp=ts))

    detail = client.get(f"/transactions/{txn_id}").json()
    assert detail["payment_status"] == "processed"
    assert detail["settlement_status"] == "settled"
    assert len(detail["events"]) == 3


def test_unknown_event_type_rejected():
    event = make_event(event_type="something_bogus")
    r = client.post("/events", json=event)
    assert r.status_code == 422


def test_nonexistent_transaction_404():
    r = client.get("/transactions/does-not-exist")
    assert r.status_code == 404


def test_list_transactions_filters_by_merchant():
    client.post("/events", json=make_event(merchant_id="merchant_a"))
    client.post("/events", json=make_event(merchant_id="merchant_b"))

    r = client.get("/transactions?merchant_id=merchant_a")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["merchant_id"] == "merchant_a"


def test_list_transactions_pagination():
    for _ in range(5):
        client.post("/events", json=make_event())

    r = client.get("/transactions?page=1&page_size=2")
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["page"] == 1


def test_discrepancy_settled_despite_failed():
    txn_id = str(uuid.uuid4())
    client.post("/events", json=make_event(transaction_id=txn_id, event_type="payment_failed", timestamp="2026-01-01T00:00:00Z"))
    client.post("/events", json=make_event(transaction_id=txn_id, event_type="settled", timestamp="2026-01-01T01:00:00Z"))

    r = client.get("/reconciliation/discrepancies?type=settled_failed")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["transaction_id"] == txn_id
    assert "settled_despite_failed_payment" in body["items"][0]["reasons"]


def test_discrepancy_conflicting_terminal_events():
    txn_id = str(uuid.uuid4())
    client.post("/events", json=make_event(transaction_id=txn_id, event_type="payment_processed", timestamp="2026-01-01T00:00:00Z"))
    client.post("/events", json=make_event(transaction_id=txn_id, event_type="payment_failed", timestamp="2026-01-01T00:01:00Z"))

    r = client.get("/reconciliation/discrepancies?type=conflicting_events")
    body = r.json()
    assert body["total"] == 1
    assert "conflicting_terminal_events" in body["items"][0]["reasons"]


def test_discrepancy_unsettled_processed_respects_stale_hours():
    txn_id = str(uuid.uuid4())
    # processed "now" (recent) - should NOT show as stale with default 24h window
    client.post(
        "/events",
        json=make_event(transaction_id=txn_id, event_type="payment_processed", timestamp="2026-09-27T00:00:00Z"),
    )
    r = client.get("/reconciliation/discrepancies?type=unsettled_processed")
    assert r.json()["total"] == 0

    # with stale_hours=0, everything unsettled+processed counts immediately
    r2 = client.get("/reconciliation/discrepancies?type=unsettled_processed&stale_hours=0")
    assert r2.json()["total"] == 1


def test_reconciliation_summary_by_merchant():
    client.post("/events", json=make_event(merchant_id="merchant_x", amount=500))
    client.post("/events", json=make_event(merchant_id="merchant_x", amount=1500))

    r = client.get("/reconciliation/summary?group_by=merchant")
    results = {row["merchant_id"]: row for row in r.json()["results"]}
    assert results["merchant_x"]["transaction_count"] == 2
    assert results["merchant_x"]["total_amount"] == 2000
