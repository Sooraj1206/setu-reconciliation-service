from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db

router = APIRouter(prefix="/events", tags=["events"])

VALID_EVENT_TYPES = {e.value for e in models.EventType}


@router.post("", response_model=schemas.IngestResponse, status_code=202)
def ingest_event(event: schemas.EventIn, db: Session = Depends(get_db)):
    """
    Ingest a single payment lifecycle event.

    Idempotent: submitting the same `event_id` again returns
    status="duplicate_ignored" and leaves transaction state untouched.
    """
    if event.event_type not in VALID_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported event_type '{event.event_type}'. Must be one of {sorted(VALID_EVENT_TYPES)}",
        )

    status, txn = crud.ingest_event(db, event)
    return schemas.IngestResponse(
        status=status,
        event_id=event.event_id,
        transaction_id=txn.transaction_id,
        transaction_payment_status=txn.payment_status.value,
        transaction_settlement_status=txn.settlement_status.value,
    )
