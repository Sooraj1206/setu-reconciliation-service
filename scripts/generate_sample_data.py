"""
Generates a realistic sample_events.json for local testing / demoing.

Produces >= 10,000 events across 5 merchants, covering:
  - clean successful flows        (initiated -> processed -> settled)
  - clean failures                (initiated -> failed)
  - pending settlement            (initiated -> processed, no settlement yet)
  - stale unsettled (discrepancy) (processed long ago, still never settled)
  - settled-despite-failed        (initiated -> failed -> settled)   [discrepancy]
  - conflicting terminal events   (initiated -> processed -> failed) [discrepancy]
  - duplicate event resubmission  (~7% of all events are exact repeats)

Usage:
    python scripts/generate_sample_data.py [--out sample_events.json] [--transactions 3000] [--seed 42]
"""
import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone

MERCHANTS = [
    ("merchant_1", "UrbanCart"),
    ("merchant_2", "FreshBasket"),
    ("merchant_3", "QuickRide"),
    ("merchant_4", "BillPayHub"),
    ("merchant_5", "StreamFlix"),
]

NOW = datetime(2026, 1, 20, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def make_event(event_type, transaction_id, merchant_id, merchant_name, amount, timestamp):
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "transaction_id": transaction_id,
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "amount": round(amount, 2),
        "currency": "INR",
        "timestamp": iso(timestamp),
    }


def build_transaction_events(rng: random.Random):
    """Return a list of events for a single transaction, based on a randomly
    chosen realistic scenario."""
    merchant_id, merchant_name = rng.choice(MERCHANTS)
    transaction_id = str(uuid.uuid4())
    amount = round(rng.uniform(50, 50000), 2)

    # start time: spread over the last 45 days
    start = NOW - timedelta(days=rng.uniform(0, 45), hours=rng.uniform(0, 24))

    scenario = rng.choices(
        [
            "clean_success",
            "clean_failure",
            "pending_settlement_recent",
            "stale_unsettled",  # discrepancy: processed, never settled, old
            "settled_despite_failed",  # discrepancy
            "conflicting_terminal",  # discrepancy
        ],
        weights=[45, 20, 12, 10, 6, 7],
    )[0]

    events = []
    t = start
    events.append(make_event("payment_initiated", transaction_id, merchant_id, merchant_name, amount, t))

    if scenario == "clean_success":
        t += timedelta(seconds=rng.uniform(1, 30))
        events.append(make_event("payment_processed", transaction_id, merchant_id, merchant_name, amount, t))
        t += timedelta(hours=rng.uniform(1, 20))
        events.append(make_event("settled", transaction_id, merchant_id, merchant_name, amount, t))

    elif scenario == "clean_failure":
        t += timedelta(seconds=rng.uniform(1, 30))
        events.append(make_event("payment_failed", transaction_id, merchant_id, merchant_name, amount, t))

    elif scenario == "pending_settlement_recent":
        # processed very recently -> not stale yet, should NOT show as a discrepancy
        t = NOW - timedelta(hours=rng.uniform(0.5, 6))
        events.append(make_event("payment_processed", transaction_id, merchant_id, merchant_name, amount, t))

    elif scenario == "stale_unsettled":
        t += timedelta(seconds=rng.uniform(1, 30))
        events.append(make_event("payment_processed", transaction_id, merchant_id, merchant_name, amount, t))
        # no settlement event ever -> and it's old, so it IS a discrepancy

    elif scenario == "settled_despite_failed":
        t += timedelta(seconds=rng.uniform(1, 30))
        events.append(make_event("payment_failed", transaction_id, merchant_id, merchant_name, amount, t))
        t += timedelta(hours=rng.uniform(1, 10))
        events.append(make_event("settled", transaction_id, merchant_id, merchant_name, amount, t))

    elif scenario == "conflicting_terminal":
        t += timedelta(seconds=rng.uniform(1, 30))
        events.append(make_event("payment_processed", transaction_id, merchant_id, merchant_name, amount, t))
        t += timedelta(seconds=rng.uniform(1, 60))
        # a second, conflicting terminal event for the SAME transaction
        events.append(make_event("payment_failed", transaction_id, merchant_id, merchant_name, amount, t))

    return events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="sample_events.json")
    parser.add_argument("--transactions", type=int, default=3200, help="number of distinct transactions to generate")
    parser.add_argument("--duplicate-rate", type=float, default=0.07, help="fraction of events resubmitted as duplicates")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)

    all_events = []
    for _ in range(args.transactions):
        all_events.extend(build_transaction_events(rng))

    # Inject exact-duplicate resubmissions (same event_id) scattered randomly
    # to simulate at-least-once delivery from an upstream event pipeline.
    n_dupes = int(len(all_events) * args.duplicate_rate)
    dupes = [dict(e) for e in rng.choices(all_events, k=n_dupes)]
    all_events.extend(dupes)

    # Shuffle overall submission order (independent of true event timestamp)
    # so the ingestion API also has to cope with realistic out-of-order and
    # interleaved delivery across transactions, not just in-order replay.
    rng.shuffle(all_events)

    with open(args.out, "w") as f:
        json.dump(all_events, f, indent=2)

    print(f"Wrote {len(all_events)} events ({n_dupes} duplicates) for {args.transactions} transactions to {args.out}")


if __name__ == "__main__":
    main()
