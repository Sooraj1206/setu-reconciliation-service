from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.get("/summary")
def reconciliation_summary(
    group_by: str = Query(
        "merchant",
        pattern="^(merchant|date|payment_status|settlement_status)$",
        examples={
            "merchant": {"summary": "Group by merchant", "value": "merchant"},
            "date": {"summary": "Group by date", "value": "date"},
        },
    ),
    merchant_id: Optional[str] = Query(
        None,
        description="Optional merchant filter.",
        examples={"example": {"summary": "Sample merchant", "value": "merchant_2"}},
    ),
    start_date: Optional[datetime] = Query(
        None,
        description="Inclusive start date for the aggregation window (ISO 8601).",
        examples={"example": {"summary": "Sample start", "value": "2026-01-01T00:00:00Z"}},
    ),
    end_date: Optional[datetime] = Query(
        None,
        description="Inclusive end date for the aggregation window (ISO 8601).",
        examples={"example": {"summary": "Sample end", "value": "2026-01-31T23:59:59Z"}},
    ),
    db: Session = Depends(get_db),
):
    """
    Aggregate transaction counts/amounts grouped by merchant, date,
    payment_status, or settlement_status. All aggregation happens in SQL
    (GROUP BY + conditional SUM), not in Python.
    """
    if group_by == "merchant":
        group_col = models.Transaction.merchant_id
        label = "merchant_id"
    elif group_by == "date":
        group_col = func.date(models.Transaction.created_at)
        label = "date"
    elif group_by == "payment_status":
        group_col = models.Transaction.payment_status
        label = "payment_status"
    else:
        group_col = models.Transaction.settlement_status
        label = "settlement_status"

    stmt = select(
        group_col.label(label),
        func.count().label("transaction_count"),
        func.coalesce(func.sum(models.Transaction.amount), 0).label("total_amount"),
        func.sum(
            case((models.Transaction.settlement_status == models.SettlementStatus.settled, 1), else_=0)
        ).label("settled_count"),
        func.sum(
            case((models.Transaction.payment_status == models.PaymentStatus.failed, 1), else_=0)
        ).label("failed_count"),
    ).group_by(group_col)

    conditions = []
    if merchant_id:
        conditions.append(models.Transaction.merchant_id == merchant_id)
    if start_date:
        conditions.append(models.Transaction.created_at >= start_date)
    if end_date:
        conditions.append(models.Transaction.created_at <= end_date)
    if conditions:
        stmt = stmt.where(*conditions)

    stmt = stmt.order_by(func.count().desc())

    rows = db.execute(stmt).all()
    results = [dict(r._mapping) for r in rows]
    # normalize enum objects / label to plain string for JSON + pretty date
    for r in results:
        val = r[label]
        r[label] = val.value if hasattr(val, "value") else str(val)

    return {"group_by": group_by, "results": results}


@router.get("/discrepancies", response_model=schemas.PaginatedDiscrepancies)
def reconciliation_discrepancies(
    merchant_id: Optional[str] = Query(
        None,
        description="Optional merchant filter.",
        examples={"example": {"summary": "Sample merchant", "value": "merchant_2"}},
    ),
    type: Optional[str] = Query(
        None,
        pattern="^(unsettled_processed|settled_failed|conflicting_events)$",
        description="Filter to a single discrepancy type. Omit to return all types.",
        examples={
            "unsettled": {"summary": "Unsettled processed", "value": "unsettled_processed"},
            "settled_failed": {"summary": "Settled failed", "value": "settled_failed"},
            "conflict": {"summary": "Conflicting terminal events", "value": "conflicting_events"},
        },
    ),
    stale_hours: int = Query(
        24,
        ge=0,
        description="A 'processed' payment with no settlement is only flagged once this many hours have passed since it was processed.",
        examples={"example": {"summary": "Default stale window", "value": 24}},
    ),
    page: int = Query(1, ge=1, examples={"example": {"summary": "Page number", "value": 1}}),
    page_size: int = Query(50, ge=1, le=200, examples={"example": {"summary": "Page size", "value": 50}}),
    db: Session = Depends(get_db),
):
    """
    Return transactions where payment state and settlement state disagree:

    - unsettled_processed: payment processed, but no settlement recorded
      after `stale_hours`.
    - settled_failed: a settlement was recorded for a payment marked failed.
    - conflicting_events: the transaction received two different terminal
      payment events (e.g. both processed and failed), which usually means
      out-of-order/duplicate events with different payloads landed for the
      same transaction_id.
    """
    # naive UTC to match how timestamps are normalized/stored (see schemas.EventIn)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=stale_hours)

    unsettled_processed = and_(
        models.Transaction.payment_status == models.PaymentStatus.processed,
        models.Transaction.settlement_status == models.SettlementStatus.unsettled,
        models.Transaction.last_payment_event_at.isnot(None),
        models.Transaction.last_payment_event_at <= cutoff,
    )
    settled_failed = and_(
        models.Transaction.settlement_status == models.SettlementStatus.settled,
        models.Transaction.payment_status == models.PaymentStatus.failed,
    )
    conflicting = models.Transaction.has_conflicting_events == 1

    type_map = {
        "unsettled_processed": unsettled_processed,
        "settled_failed": settled_failed,
        "conflicting_events": conflicting,
    }
    condition = type_map.get(type, or_(unsettled_processed, settled_failed, conflicting))

    stmt = select(models.Transaction).where(condition)
    if merchant_id:
        stmt = stmt.where(models.Transaction.merchant_id == merchant_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))

    stmt = (
        stmt.order_by(models.Transaction.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = db.scalars(stmt).all()

    items = []
    for txn in rows:
        reasons = []
        if (
            txn.payment_status == models.PaymentStatus.processed
            and txn.settlement_status == models.SettlementStatus.unsettled
            and txn.last_payment_event_at
            and txn.last_payment_event_at <= cutoff
        ):
            reasons.append("processed_but_not_settled")
        if (
            txn.settlement_status == models.SettlementStatus.settled
            and txn.payment_status == models.PaymentStatus.failed
        ):
            reasons.append("settled_despite_failed_payment")
        if txn.has_conflicting_events:
            reasons.append("conflicting_terminal_events")

        items.append(
            schemas.DiscrepancyItem(
                transaction_id=txn.transaction_id,
                merchant_id=txn.merchant_id,
                amount=txn.amount,
                currency=txn.currency,
                payment_status=txn.payment_status.value,
                settlement_status=txn.settlement_status.value,
                reasons=reasons,
                last_payment_event_at=txn.last_payment_event_at,
                last_settlement_event_at=txn.last_settlement_event_at,
            )
        )

    return schemas.PaginatedDiscrepancies(total=total, page=page, page_size=page_size, items=items)
