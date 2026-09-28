# Setu Payment Reconciliation Service

A backend service that ingests payment lifecycle events, maintains transaction/settlement
state, and surfaces reconciliation discrepancies for operations teams.

This project was designed, implemented, and validated by me as the primary owner and
builder of the solution. I used AI coding and quality-assistance tools as practical
accelerators to help draft code, improve structure, generate tests, validate edge cases,
and refine documentation — but the core design decisions, implementation direction,
review, and final acceptance remain mine.

Built with **FastAPI** + **SQLAlchemy**, backed by **SQLite** locally (zero setup) with a
one-line switch to **Postgres** for production.

## Submission Links

- **GitHub Repository:** https://github.com/Sooraj1206/setu-reconciliation-service
- **Live API Documentation:** https://setu-reconciliation-service-7ycf.onrender.com/docs
- **Video Walkthrough:** https://www.loom.com/share/1a3a4557bc8a41c4a7f2164032e16544

---

## Table of contents

- [Architecture overview](#architecture-overview)
- [Data model](#data-model)
- [Local reproduction](#local-reproduction)
- [Sample data](#sample-data)
- [API documentation](#api-documentation)
- [Testing](#testing)
- [Deployment](#deployment)
- [Assumptions & tradeoffs](#assumptions--tradeoffs)
- [AI tool disclosure](#ai-tool-disclosure)

---

## Architecture overview

```
                POST /events
                     │
                     ▼
          ┌─────────────────────┐
          │   FastAPI app        │
          │  (app/main.py)       │
          └──────────┬───────────┘
                      │
        ┌─────────────┼──────────────┐
        ▼             ▼              ▼
   events router  transactions   reconciliation
   (ingest)        router         router
        │           (read)         (read)
        ▼
  app/crud.py  ──►  idempotent ingest + state machine
        │
        ▼
┌─────────────────────────────────────────┐
│               SQL database                │
│  merchants ── transactions ── payment_events │
└─────────────────────────────────────────┘
```

**Key design decision: derived state, not event replay.**
`payment_events` is an append-only log — every event ever received, kept forever, used as
the idempotency ledger and as transaction history. `transactions` is a separate,
denormalized "current state" table that's updated incrementally as each event arrives.

This means:
- `GET /transactions` and `GET /reconciliation/*` are simple indexed reads/aggregations
  against a small, current-state table — not a replay of the entire event log on every
  request. This matters at 10k+ events and would matter more at production scale.
- Idempotency is enforced by a **database-level unique constraint** on `event_id`, not
  just application logic — so it holds even under concurrent duplicate submissions.
- Applying an event to the derived state is **monotonic on event timestamp**
  (`last_payment_event_at` / `last_settlement_event_at`), so out-of-order or
  duplicate-but-stale events can never regress a transaction's state.

## Data model

Three tables (see `app/models.py` for the full SQLAlchemy definitions):

**`merchants`**
| column | type | notes |
|---|---|---|
| merchant_id | string, PK | |
| merchant_name | string | |

**`transactions`** — the derived "current state" of each transaction
| column | type | notes |
|---|---|---|
| transaction_id | string, PK | |
| merchant_id | string, FK, indexed | |
| amount, currency | float, string | |
| payment_status | enum: initiated / processed / failed, indexed | |
| settlement_status | enum: unsettled / settled, indexed | |
| last_payment_event_at | datetime, nullable | used for out-of-order-safe updates |
| last_settlement_event_at | datetime, nullable | " |
| has_conflicting_events | bool (int) | set when both `processed` and `failed` are seen |
| created_at, updated_at | datetime, indexed | |

**`payment_events`** — append-only raw event log / idempotency ledger
| column | type | notes |
|---|---|---|
| id | int, PK autoincrement | |
| event_id | string, **unique**, indexed | idempotency key |
| event_type | enum | payment_initiated / payment_processed / payment_failed / settled |
| transaction_id | string, FK, indexed | |
| merchant_id | string, FK, indexed | |
| amount, currency, timestamp | | as submitted |
| received_at | datetime | server-side ingestion time |

**Indexes** are placed on every column used in a `WHERE`, `GROUP BY`, `ORDER BY`, or join
in the API's queries: `merchant_id`, `payment_status`, `settlement_status`, `created_at`,
a composite `(merchant_id, payment_status)` for the common "merchant + status" filter
combo, `event_id` (unique), and a composite `(transaction_id, timestamp)` on events for
fetching ordered history per transaction.

## Local reproduction

Requires Python 3.11+.

```bash
git clone <this-repo-url>
cd setu-reconciliation-service

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

# Generate sample data (10,000+ events, 5 merchants) - optional, one is
# already included in the repo as sample_events.json
python scripts/generate_sample_data.py

# Load the sample data into a local SQLite DB (reconciliation.db)
python scripts/seed_db.py --file sample_events.json

# Run the API
uvicorn app.main:app --reload
```

The API is now at **http://localhost:8000**, with interactive docs at
**http://localhost:8000/docs** (Swagger UI) and **http://localhost:8000/redoc**.

By default the service uses a local SQLite file (`reconciliation.db`) — nothing else to
install or configure. To use Postgres instead, set `DATABASE_URL` (see `.env.example`) or
run `docker compose up --build`, which starts Postgres + the API together.

### Running with Docker

```bash
docker build -t setu-reconciliation .
docker run -p 8000:8000 setu-reconciliation
```

## Sample data

`sample_events.json` (included, and regeneratable via `scripts/generate_sample_data.py`)
contains **10,704 events** across **5 merchants**, covering:

| scenario | outcome |
|---|---|
| clean success | initiated → processed → settled |
| clean failure | initiated → failed |
| pending settlement (recent) | initiated → processed, no settlement yet — **not** flagged (too recent) |
| stale unsettled | initiated → processed, never settled, old enough to be **flagged** |
| settled despite failed | initiated → failed → settled — **flagged** discrepancy |
| conflicting terminal events | initiated → processed → failed for the same transaction — **flagged** discrepancy |
| duplicate resubmission | ~7% of all events are exact repeats (same `event_id`) of another event, scattered randomly through the file |

Events are shuffled so submission order doesn't match true event chronology, exercising
out-of-order delivery as well.

`scripts/seed_db.py` loads this file directly through the same `app.crud.ingest_event`
function the live API uses, so seeding exercises the real idempotency/state-machine
code path, not a bulk-insert shortcut.

## API documentation

Full interactive docs (with request/response schemas) are auto-generated at `/docs`.
Summary below; see `postman_collection.json` for ready-to-run example requests.

### `POST /events`
Ingest one payment lifecycle event.

```json
{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type": "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": 15248.29,
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}
```

Returns `202` with:
```json
{
  "status": "accepted",              // or "duplicate_ignored"
  "event_id": "...",
  "transaction_id": "...",
  "transaction_payment_status": "initiated",
  "transaction_settlement_status": "unsettled"
}
```
Resubmitting the exact same `event_id` returns `status: "duplicate_ignored"` and leaves
transaction state untouched — the event log is not double-written and no other field is
recomputed.

### `GET /transactions`
List transactions. Query params: `merchant_id`, `payment_status`
(`initiated`/`processed`/`failed`), `settlement_status` (`unsettled`/`settled`),
`start_date` / `end_date` (filters on `created_at`, ISO 8601), `sort_by`
(`created_at`/`updated_at`/`amount`), `sort_order` (`asc`/`desc`), `page`, `page_size`.
All filtering, sorting and pagination happen in SQL.

### `GET /transactions/{transaction_id}`
Transaction details, current status, merchant name, and full event history
(chronological).

### `GET /reconciliation/summary`
Aggregated counts/amounts. Query params: `group_by`
(`merchant`/`date`/`payment_status`/`settlement_status`), plus optional `merchant_id`,
`start_date`, `end_date`. Returns per-group `transaction_count`, `total_amount`,
`settled_count`, `failed_count`. Aggregation is done via SQL `GROUP BY` + conditional
`SUM`, not in Python.

### `GET /reconciliation/discrepancies`
Transactions where payment state and settlement state disagree. Query params:
`merchant_id`, `type` (`unsettled_processed` / `settled_failed` / `conflicting_events`,
omit for all three), `stale_hours` (default 24 — how long a `processed` payment can go
unsettled before it's flagged), `page`, `page_size`. Each result includes a `reasons`
array explaining why it was flagged:

- `processed_but_not_settled` — payment processed, no settlement recorded after
  `stale_hours`.
- `settled_despite_failed_payment` — a settlement exists for a payment marked `failed`.
- `conflicting_terminal_events` — the transaction received two different terminal
  payment events (e.g. both `processed` and `failed`), almost always meaning
  out-of-order or duplicate events with **different payloads** landed for the same
  `transaction_id`.

## Testing

```bash
pytest -v
```

13 tests cover: idempotent ingestion (exact duplicate resubmission does not double-write
or corrupt state), out-of-order event handling, the full success lifecycle, input
validation (`422` on unknown event types), `404` on unknown transactions, filtering and
pagination on `/transactions`, and all three discrepancy types on `/reconciliation`.

`postman_collection.json` also includes inline test assertions (status codes, response
shape) runnable via Postman's Collection Runner or `newman`.

## Deployment

The application has been deployed as a live Render Web Service from this GitHub
repository. The deployed service runs the FastAPI application and exposes the same
reconciliation endpoints described in this README.

- **Live API documentation:** https://setu-reconciliation-service-7ycf.onrender.com/docs
- **Health check:** https://setu-reconciliation-service-7ycf.onrender.com/health
- **Deployment source:** [GitHub Repository](https://github.com/Sooraj1206/setu-reconciliation-service)
- **Walkthrough:** [Loom Video](https://www.loom.com/share/1a3a4557bc8a41c4a7f2164032e16544)

The live Swagger UI can be used to inspect the OpenAPI schemas and execute the primary
workflows directly, including event ingestion, reconciliation summaries, and discrepancy
queries. The deployed application was validated locally with the automated test suite:
**13 tests passed**.

## Assumptions & tradeoffs

- **SQLite by default, Postgres via env var.** SQLite keeps local setup to zero
  dependencies, which the assignment weights heavily ("if the reviewer cannot run your
  service within a few minutes, nothing else matters"). `DATABASE_URL` swaps the backend
  with no code changes — `docker-compose.yml` demonstrates this against Postgres.
- **No migration tool (Alembic).** Tables are created via
  `Base.metadata.create_all()` on startup. For a take-home with a stable, small schema
  this is simpler and faster to review; a real production service handling schema
  evolution over time would use Alembic migrations instead.
- **Idempotency window is unbounded** — `event_id` uniqueness is enforced forever, not
  just within a recent window. This is the simplest correct behavior and matches "event
  history should be preserved." A very high-volume system might instead expire old
  `event_id`s after some retention period to bound the idempotency table's size.
- **Timestamps are normalized to naive UTC at the API boundary** (in
  `schemas.EventIn`). SQLite has no native timezone-aware column type and silently drops
  tzinfo on round-trip; normalizing once at ingestion keeps every downstream comparison
  (idempotency ordering, staleness cutoffs) consistent regardless of backend. Postgres
  deployments are unaffected since the app is consistent internally either way.
- **"Stale" is a query-time parameter, not a stored flag.** Whether an unsettled
  processed payment counts as a discrepancy depends on `stale_hours` (default 24),
  passed at query time rather than baked into the stored state. This keeps the derived
  `transactions` table simple and lets ops teams adjust the staleness threshold without
  a backfill.
- **Conflicting-event detection is a boolean flag, not a full audit trail.** When a
  transaction receives two different terminal payment events (`processed` then
  `failed`, or vice versa), we set `has_conflicting_events = 1` rather than trying to
  reconstruct "which one was right." The full event history is still available via
  `GET /transactions/{id}` for a human to inspect and decide.
- **What I'd do with more time:** Alembic migrations; a background job to
  auto-refresh/cache the reconciliation summary for very large datasets; structured
  request logging and basic auth/rate-limiting on the ingestion endpoint; webhook-style
  retries/backoff simulation in the sample data generator; splitting `reconciliation
  discrepancies` reasons into a proper enum surfaced in the OpenAPI schema instead of
  free-text strings.

## AI tool disclosure

This project was built by me, with AI assistance used as a force multiplier rather than a
substitute for ownership or engineering judgment.

I used tools such as **Claude Code**, **GitHub Copilot**, **Postman Visualize**, and other
AI-assisted validation and review workflows to help with:
- scaffolding the FastAPI + SQLAlchemy structure and application layout,
- drafting and refining domain logic, endpoints, and data-model design,
- generating and improving the sample data and seed scripts,
- writing and iterating on tests, validation checks, and edge-case coverage,
- improving the clarity and completeness of the documentation and API examples,
- reviewing logic, spotting gaps, and checking for quality issues before finalizing the work.

These tools were used to speed up implementation and increase correctness, but I am the
responsible author of this solution. I reviewed, ran, and verified the code locally against
sample data and the automated test suite before finalizing it for this repository. See the
[Testing](#testing) section for the validation performed.
