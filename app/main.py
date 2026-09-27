from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine
from .routers import events, reconciliation, transactions

# For this take-home, tables are created on startup via SQLAlchemy metadata
# instead of a migration tool (see README "Assumptions & Tradeoffs" for why
# Alembic was skipped).
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Setu Payment Reconciliation Service",
    description=(
        "Ingests payment lifecycle events, maintains transaction/settlement "
        "state, and surfaces reconciliation discrepancies for ops teams."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events.router)
app.include_router(transactions.router)
app.include_router(reconciliation.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["health"])
def root():
    return {
        "service": "Setu Payment Reconciliation Service",
        "docs": "/docs",
        "health": "/health",
    }
