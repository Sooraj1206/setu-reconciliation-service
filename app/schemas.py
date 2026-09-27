from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventIn(BaseModel):
    event_id: str = Field(..., description="Globally unique ID for this event; used for idempotency.")
    event_type: str = Field(
        ..., description="One of payment_initiated | payment_processed | payment_failed | settled"
    )
    transaction_id: str
    merchant_id: str
    merchant_name: Optional[str] = None
    amount: float
    currency: str = "INR"
    timestamp: datetime

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("amount must be >= 0")
        return v

    @field_validator("timestamp")
    @classmethod
    def normalize_to_naive_utc(cls, v: datetime) -> datetime:
        # SQLite has no native timezone-aware column type - it silently
        # drops tzinfo on round-trip. To keep every datetime comparison in
        # the app (idempotency ordering, staleness cutoffs) apples-to-apples
        # regardless of backend, we normalize all incoming timestamps to
        # naive UTC at the boundary, once, here. Postgres deployments are
        # unaffected since we're consistent either way.
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    event_type: str
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    timestamp: datetime
    received_at: Optional[datetime] = None

    @field_validator("event_type", mode="before")
    @classmethod
    def enum_to_str(cls, v):
        return v.value if hasattr(v, "value") else v


class IngestResponse(BaseModel):
    status: str  # "accepted" | "duplicate_ignored"
    event_id: str
    transaction_id: str
    transaction_payment_status: str
    transaction_settlement_status: str


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    payment_status: str
    settlement_status: str
    created_at: datetime
    updated_at: datetime

    @field_validator("payment_status", "settlement_status", mode="before")
    @classmethod
    def enum_to_str(cls, v):
        return v.value if hasattr(v, "value") else v


class TransactionDetail(TransactionOut):
    merchant_name: Optional[str] = None
    events: List[EventOut] = []


class PaginatedTransactions(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[TransactionOut]


class DiscrepancyItem(BaseModel):
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    payment_status: str
    settlement_status: str
    reasons: List[str]
    last_payment_event_at: Optional[datetime] = None
    last_settlement_event_at: Optional[datetime] = None


class PaginatedDiscrepancies(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[DiscrepancyItem]
