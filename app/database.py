"""
Database engine / session configuration.

Defaults to a local SQLite file so the service runs with zero external
dependencies. Set DATABASE_URL to point at Postgres (or any SQLAlchemy
supported DB) for a production deployment, e.g.:

    postgresql+psycopg2://user:password@host:5432/reconciliation
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./reconciliation.db")

# check_same_thread is only needed/valid for SQLite.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
