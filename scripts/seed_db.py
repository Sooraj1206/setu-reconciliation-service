"""
Loads events from a JSON file straight into the database via the same
ingestion logic the API uses (app.crud.ingest_event), so seeding exercises
the exact idempotency/state-machine code path used at runtime.

Usage:
    python scripts/seed_db.py [--file sample_events.json]

Run this against a fresh DB, or re-run it any time - it's idempotent, so
re-seeding the same file will just report duplicates for events already
ingested.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crud, schemas  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="sample_events.json")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)

    with open(args.file) as f:
        events = json.load(f)

    db = SessionLocal()
    accepted = 0
    duplicates = 0
    errors = 0
    start = time.time()

    for i, raw in enumerate(events, start=1):
        try:
            event_in = schemas.EventIn(**raw)
            status, _ = crud.ingest_event(db, event_in)
            if status == "accepted":
                accepted += 1
            else:
                duplicates += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  [error] event {i} ({raw.get('event_id')}): {e}")

        if i % 2000 == 0:
            print(f"  ...{i}/{len(events)} processed")

    db.close()
    elapsed = time.time() - start
    print(
        f"\nDone in {elapsed:.2f}s. accepted={accepted} duplicate_ignored={duplicates} "
        f"errors={errors} total={len(events)}"
    )


if __name__ == "__main__":
    main()
