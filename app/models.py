"""
SQL schema.

Design notes (see README for the full rationale):

- `payment_events` is an append-only log of everything ingested. The unique
  constraint on `event_id` is the actual idempotency guarantee (enforced by
  the DB, not just application logic), and it doubles as full event history
  for the transaction-detail endpoint.

- `transactions` is a derived, denormalized "current state" table that is
  updated incrementally as events arrive (see app/crud.py). This means
  GET /transactions and the reconciliation endpoints are simple indexed
  reads/aggregations instead of replaying the whole event log on every
  request.

- `last_payment_event_at` / `last_settlement_event_at` let us apply events
  out of order safely (a status only moves forward in time), which matters
  because event pipelines don't always deliver in order.

- `has_conflicting_events` is a cheap boolean flag set whenever a
  transaction receives two different terminal payment states
  (`processed` then `failed`, or vice-versa) so that reconciliation queries
  don't need to re-scan the event log.
"""
import enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class PaymentStatus(str, enum.Enum):
    initiated = "initiated"
    processed = "processed"
    failed = "failed"


class SettlementStatus(str, enum.Enum):
    unsettled = "unsettled"
    settled = "settled"


class EventType(str, enum.Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


class Merchant(Base):
    __tablename__ = "merchants"

    merchant_id = Column(String, primary_key=True)
    merchant_name = Column(String, nullable=False)

    transactions = relationship("Transaction", back_populates="merchant")


class Transaction(Base):
    __tablename__ = "transactions"

    transaction_id = Column(String, primary_key=True)
    merchant_id = Column(String, ForeignKey("merchants.merchant_id"), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False, default="INR")

    payment_status = Column(
        Enum(PaymentStatus), nullable=False, default=PaymentStatus.initiated
    )
    settlement_status = Column(
        Enum(SettlementStatus), nullable=False, default=SettlementStatus.unsettled
    )

    last_payment_event_at = Column(DateTime(timezone=True), nullable=True)
    last_settlement_event_at = Column(DateTime(timezone=True), nullable=True)

    # 1 if this transaction ever received two conflicting terminal payment
    # events (e.g. both `payment_processed` and `payment_failed`).
    has_conflicting_events = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    merchant = relationship("Merchant", back_populates="transactions")
    events = relationship(
        "PaymentEvent", back_populates="transaction", order_by="PaymentEvent.timestamp"
    )

    __table_args__ = (
        Index("ix_transactions_merchant_id", "merchant_id"),
        Index("ix_transactions_payment_status", "payment_status"),
        Index("ix_transactions_settlement_status", "settlement_status"),
        Index("ix_transactions_merchant_status", "merchant_id", "payment_status"),
        Index("ix_transactions_created_at", "created_at"),
    )


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, nullable=False, unique=True)  # idempotency key
    event_type = Column(Enum(EventType), nullable=False)
    transaction_id = Column(
        String, ForeignKey("transactions.transaction_id"), nullable=False
    )
    merchant_id = Column(String, ForeignKey("merchants.merchant_id"), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False, default="INR")
    timestamp = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now())

    transaction = relationship("Transaction", back_populates="events")

    __table_args__ = (
        Index("ix_events_event_id", "event_id", unique=True),
        Index("ix_events_transaction_id", "transaction_id"),
        Index("ix_events_txn_timestamp", "transaction_id", "timestamp"),
    )
