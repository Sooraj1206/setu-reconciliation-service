"""
Business logic for turning raw events into derived transaction state.

Idempotency strategy
---------------------
1. `payment_events.event_id` has a DB-level UNIQUE constraint. That's the
   actual source of truth for "have we seen this event before" - not an
   in-memory cache or a pre-check that could race.
2. We still do a cheap SELECT first (fast path, uses the unique index) to
   avoid an unnecessary INSERT + rollback on the common case of a client
   retrying a request it already saw succeed.
3. If two requests for the same event_id land concurrently and both pass
   the pre-check, the DB's UNIQUE constraint rejects the second INSERT;
   we catch that IntegrityError and treat it as a duplicate rather than
   letting it 500.
4. Applying an event to the derived `transactions` row is monotonic on
   event timestamp (`last_payment_event_at` / `last_settlement_event_at`),
   so replays and out-of-order delivery can't move state backwards.
"""
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas

PAYMENT_EVENT_TO_STATUS = {
    models.EventType.payment_initiated: models.PaymentStatus.initiated,
    models.EventType.payment_processed: models.PaymentStatus.processed,
    models.EventType.payment_failed: models.PaymentStatus.failed,
}

TERMINAL_PAYMENT_STATUSES = {models.PaymentStatus.processed, models.PaymentStatus.failed}


def get_or_create_merchant(db: Session, merchant_id: str, merchant_name: str | None) -> models.Merchant:
    merchant = db.get(models.Merchant, merchant_id)
    if merchant is None:
        merchant = models.Merchant(merchant_id=merchant_id, merchant_name=merchant_name or merchant_id)
        db.add(merchant)
        db.flush()
    elif merchant_name and merchant.merchant_name != merchant_name:
        merchant.merchant_name = merchant_name
    return merchant


def _get_or_create_transaction(db: Session, event_in: schemas.EventIn) -> models.Transaction:
    txn = db.get(models.Transaction, event_in.transaction_id)
    if txn is None:
        txn = models.Transaction(
            transaction_id=event_in.transaction_id,
            merchant_id=event_in.merchant_id,
            amount=event_in.amount,
            currency=event_in.currency,
            payment_status=models.PaymentStatus.initiated,
            settlement_status=models.SettlementStatus.unsettled,
        )
        db.add(txn)
        db.flush()
    return txn


def _apply_event_to_transaction(txn: models.Transaction, event_type: models.EventType, event_in: schemas.EventIn) -> None:
    if event_type in PAYMENT_EVENT_TO_STATUS:
        if txn.last_payment_event_at is None or event_in.timestamp >= txn.last_payment_event_at:
            new_status = PAYMENT_EVENT_TO_STATUS[event_type]
            if (
                txn.payment_status in TERMINAL_PAYMENT_STATUSES
                and new_status in TERMINAL_PAYMENT_STATUSES
                and new_status != txn.payment_status
            ):
                txn.has_conflicting_events = 1
            txn.payment_status = new_status
            txn.last_payment_event_at = event_in.timestamp
    elif event_type == models.EventType.settled:
        if txn.last_settlement_event_at is None or event_in.timestamp >= txn.last_settlement_event_at:
            txn.settlement_status = models.SettlementStatus.settled
            txn.last_settlement_event_at = event_in.timestamp


def ingest_event(db: Session, event_in: schemas.EventIn) -> Tuple[str, models.Transaction]:
    """Ingest one event idempotently. Returns (status, transaction)."""
    event_type = models.EventType(event_in.event_type)

    existing = db.scalar(select(models.PaymentEvent).where(models.PaymentEvent.event_id == event_in.event_id))
    if existing is not None:
        return "duplicate_ignored", db.get(models.Transaction, existing.transaction_id)

    get_or_create_merchant(db, event_in.merchant_id, event_in.merchant_name)
    txn = _get_or_create_transaction(db, event_in)

    event_row = models.PaymentEvent(
        event_id=event_in.event_id,
        event_type=event_type,
        transaction_id=event_in.transaction_id,
        merchant_id=event_in.merchant_id,
        amount=event_in.amount,
        currency=event_in.currency,
        timestamp=event_in.timestamp,
    )
    db.add(event_row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(models.PaymentEvent).where(models.PaymentEvent.event_id == event_in.event_id))
        return "duplicate_ignored", db.get(models.Transaction, existing.transaction_id)

    _apply_event_to_transaction(txn, event_type, event_in)

    db.commit()
    db.refresh(txn)
    return "accepted", txn
