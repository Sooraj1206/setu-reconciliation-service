from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
                "event_type": "payment_processed",
                "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
                "merchant_id": "merchant_2",
                "merchant_name": "FreshBasket",
                "amount": 15248.29,
                "currency": "INR",
                "timestamp": "2026-01-08T12:11:58.085567+00:00",
            }
        }
    )

    event_id: str = Field(..., description="Globally unique ID for this event; used for idempotency.")
    event_type: str = Field(
        ..., description="One of payment_initiated | payment_processed | payment_failed | settled"
    )
    transaction_id: str = Field(
        ..., json_schema_extra={"example": "2f86e94c-239c-4302-9874-75f28e3474ee"}
    )
    merchant_id: str = Field(..., json_schema_extra={"example": "merchant_2"})
    merchant_name: Optional[str] = Field(None, json_schema_extra={"example": "FreshBasket"})
    amount: float = Field(..., json_schema_extra={"example": 15248.29})
    currency: str = Field("INR", json_schema_extra={"example": "INR"})
    timestamp: datetime = Field(
        ..., json_schema_extra={"example": "2026-01-08T12:11:58.085567+00:00"}
    )

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
