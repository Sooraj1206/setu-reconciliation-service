from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/transactions", tags=["transactions"])

SORTABLE_FIELDS = {
    "created_at": models.Transaction.created_at,
    "updated_at": models.Transaction.updated_at,
    "amount": models.Transaction.amount,
}


@router.get("", response_model=schemas.PaginatedTransactions)
def list_transactions(
    merchant_id: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None, description="initiated | processed | failed"),
    settlement_status: Optional[str] = Query(None, description="unsettled | settled"),
    start_date: Optional[datetime] = Query(None, description="Filter created_at >= start_date (ISO 8601)"),
    end_date: Optional[datetime] = Query(None, description="Filter created_at <= end_date (ISO 8601)"),
    sort_by: str = Query("created_at", pattern="^(created_at|updated_at|amount)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    List transactions with filtering, pagination and sorting.
    All filtering/sorting/pagination happens in SQL against indexed columns.
    """
    conditions = []
    if merchant_id:
        conditions.append(models.Transaction.merchant_id == merchant_id)
    if payment_status:
        conditions.append(models.Transaction.payment_status == payment_status)
    if settlement_status:
        conditions.append(models.Transaction.settlement_status == settlement_status)
    if start_date:
        conditions.append(models.Transaction.created_at >= start_date)
    if end_date:
        conditions.append(models.Transaction.created_at <= end_date)

    count_stmt = select(func.count()).select_from(models.Transaction)
    stmt = select(models.Transaction)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)

    total = db.scalar(count_stmt)

    sort_col = SORTABLE_FIELDS[sort_by]
    sort_col = sort_col.desc() if sort_order == "desc" else sort_col.asc()
    stmt = stmt.order_by(sort_col).offset((page - 1) * page_size).limit(page_size)

    items = db.scalars(stmt).all()
    return schemas.PaginatedTransactions(total=total, page=page, page_size=page_size, items=items)


@router.get("/{transaction_id}", response_model=schemas.TransactionDetail)
def get_transaction(transaction_id: str, db: Session = Depends(get_db)):
    """Fetch a transaction, its merchant info, and its full event history."""
    txn = db.get(models.Transaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    merchant = db.get(models.Merchant, txn.merchant_id)
    events = db.scalars(
        select(models.PaymentEvent)
        .where(models.PaymentEvent.transaction_id == transaction_id)
        .order_by(models.PaymentEvent.timestamp)
    ).all()

    detail = schemas.TransactionDetail.model_validate(txn)
    detail.merchant_name = merchant.merchant_name if merchant else None
    detail.events = [schemas.EventOut.model_validate(e) for e in events]
    return detail
