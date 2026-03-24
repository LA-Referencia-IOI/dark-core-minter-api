# Current Implementation - dARK Core Minter API

## Status

Active implementation with separated runtime architecture:

- API HTTP process (`app.main`)
- Publisher worker process (`app.main_worker`)
- Full local ARK lifecycle persistence in `ark_records`
- Deterministic sequential minting per namespace in `noid_counters`
- Two-level metadata pipeline (L1 validated JSON + L2 original content) with shared metadata/storage abstractions from `dark-core-lib`

## 1. Process Architecture

### API (HTTP only)

- Entry point: `app/main.py`
- CLI: `dark-core-api`
- Direct Uvicorn: `uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 4`
- Exposes ARK and Authority endpoints
- Does not run internal scheduler/publisher

### Worker (publish/update only)

- Entry point: `app/main_worker.py`
- CLI: `dark-core-worker`
- APScheduler-based interval loop
- Process status command: `dark-core-worker-status`
- Also available as module command: `python -m app.main_worker status`

### Operational model

- API and worker run decoupled
- Deployment and scaling are independent
- Worker runtime status is exposed through DB heartbeat

## 2. Persistence and Data Model

### Database

- SQLAlchemy 2.0+
- Alembic migrations
- PostgreSQL as standard engine (dev/staging/prod)

### Main table: `ark_records`

Relevant fields:

- `id` (PK autoincrement)
- `naan`, `name`
- `state` (`R`, `D`, `U`, `P`, `T`)
- `authority_id`
- `target`
- `created_at`, `updated_at`, `tombstoned_at`
- `client_item_id`
- publish tracking:
  - `publish_retry_count`
  - `publish_last_error`
  - `publish_last_attempt_at`
  - `publish_permanently_failed`

### Metadata table: `ark_metadata`

Relevant fields:

- `ark_record_id` (1:1 FK to `ark_records`)
- `level1_json` (validated Level-1 JSON payload)
- `level1_cid` (assigned by worker after storage)
- `original_content` (raw Level-2 content)
- `original_schema` (client-provided schema label, e.g. `dublin_core`, `oai_dc`)
- `original_media_type` (client-provided or inferred MIME type for Level-2)
- `original_cid` (assigned by worker after storage)

### Modeling decisions

- No physical `ark` column
- Full ARK is computed as `ark:{naan}/{name}`
- Real uniqueness boundary: `uq_naan_name` on `(naan, name)`

### Counter table: `noid_counters`

Relevant fields:

- `namespace_key` (PK, format `NAAN:SHOULDER`)
- `next_value` (next integer to allocate)
- `updated_at`

### Worker runtime table: `worker_runtime_status`

Relevant fields:

- `worker_name` (PK)
- `instance_id`, `host`, `pid`, `status`
- `last_heartbeat_at`, `started_at`, `last_cycle_at`
- runtime counters (`total_processed`, `total_succeeded`, `total_failed`, `total_permanent_failures`)

## 3. Minting and Concurrency Strategy

- ARK names are deterministic (not random).
- `POST /arks` and `POST /arks/batch` allocate a sequential counter per namespace.
- Counter increment is atomic and encoded in base29.
- Current policy: fixed generated-part length `7` (`MINTER_NOID_LENGTH=7`).
- Capacity per namespace: `29^7 = 17,249,876,309`.
- NOID checkdigit is appended by default.
- `GET/PUT/DELETE` validate checkdigit when `MINTER_NOID_CHECKDIGIT=true`.
- ARK insert uses savepoint so collisions do not lose consumed counter progress.

PostgreSQL concurrency hardening:

- Draft claim uses `FOR UPDATE SKIP LOCKED`.
- Lifecycle transitions use SQL compare-and-set (CAS):
  - `RESERVED -> DRAFT`
  - `PUBLISHED/UPDATE -> UPDATE`
  - `DRAFT/UPDATE -> PUBLISHED`
  - `* -> TOMBSTONE` (idempotent)
- `publish_retry_count` increments atomically in SQL.
- Worker singleton is enforced with PostgreSQL advisory lock (`pg_try_advisory_lock`) keyed by `WORKER_RUNTIME_NAME`.

## 4. Lifecycle Flows

```text
RESERVED (POST /arks)
    -> DRAFT (PUT /arks/{ark})
    -> PUBLISHED (worker: create_ark)

PUBLISHED (local/chain)
    -> UPDATE (PUT /arks/{ark})
    -> PUBLISHED (worker: update_ark)

RESERVED|DRAFT|UPDATE|PUBLISHED
    -> TOMBSTONE (DELETE /arks/{ark})
```

Behavior details for `PUT /api/v1/arks/{ark}`:

- If local state is `RESERVED`: move to `DRAFT`.
- If local state is `DRAFT`: overwrite pending payload, remain `DRAFT`.
- If local state is `PUBLISHED` or `UPDATE`: move to/keep `UPDATE`.
- If local record is missing but ARK exists on-chain: import local `PUBLISHED`, then move to `UPDATE`.
- If local state is `TOMBSTONE`: reject update.

## 5. API Endpoints

### ARKs

- `POST /api/v1/arks`
- `POST /api/v1/arks/batch`
- `GET /api/v1/arks/{ark}`
- `PUT /api/v1/arks/{ark}`
- `DELETE /api/v1/arks/{ark}`

### Authority

- `GET /api/v1/authority/{uuid}`
- `GET /api/v1/authority/{uuid}/naans`
- `GET /api/v1/authority/{uuid}/authorized/{naan}`

### Worker

- `GET /api/v1/worker/status` (DB heartbeat based)

### Health

- `GET /health`
- Verifies blockchain, DB, and metadata storage

## 6. Worker Publisher

### Behavior

- Claims pending ARKs in `DRAFT`/`UPDATE`
- Applies exponential backoff for retries
- Publishes to blockchain via corelib
- Updates state to `PUBLISHED` or marks publish failures

### Scheduler

- `APScheduler`
- Configured by:
  - `WORKER_INTERVAL_SECONDS`
  - `WORKER_BATCH_SIZE`
  - `WORKER_MAX_RETRIES`
  - `WORKER_RETRY_BACKOFF_BASE`

### Distributed singleton operation

- PostgreSQL advisory lock per `WORKER_RUNTIME_NAME`
- If lock is already held, worker startup aborts
- Local pidfile (`/tmp/dark-core-worker.pid`) remains for host-level process status
- `dark-core-worker-status` returns:
  - `RUNNING pid=<pid>` (exit code 0)
  - `NOT_RUNNING` (exit code 1)
- Periodic heartbeat persisted in DB with status and counters

## 7. Metadata Storage (Two-Level)

- API receives:
  - `minimal_metadata` (minimal extracted metadata JSON),
  - `original_metadata` (raw record),
  - `metadata_schema` (schema label for L2).
- `alternate_identifiers` / `alternate_urls` are part of `minimal_metadata` and are persisted only in `ark_metadata.level1_json`.
- API validates L1 against `Level1Metadata` and stores both levels in `ark_metadata`.
- Worker performs storage/publish pipeline:
  1. Store L2 (`original_content`) -> `original_cid`.
  2. Inject `original_cid` into L1 JSON (`original_metadata.cid`).
  3. Store L1 JSON -> `level1_cid`.
  4. Persist both CIDs and publish on-chain using `level1_cid`.
- `metadata_cid` is sourced from on-chain state (`cid`) and/or `ark_metadata.level1_cid`; it is not duplicated in `ark_records`.
- `filesystem` and `store_api` backends are used by worker during the publish phase.
- the implementation of those backends now lives in `dark_core_lib.metadata.storage`, not in the minter package.

## 8. Relevant Configuration

```bash
# DB
DATABASE_URL=postgresql://dark:dark_password@localhost:5432/minter
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10

# Worker standalone
WORKER_ENABLED=true
WORKER_INTERVAL_SECONDS=60
WORKER_BATCH_SIZE=10
WORKER_MAX_RETRIES=5
WORKER_RETRY_BACKOFF_BASE=2.0
WORKER_RUNTIME_NAME=ark-publisher
WORKER_HEARTBEAT_INTERVAL_SECONDS=10
WORKER_HEARTBEAT_STALE_AFTER_SECONDS=180

# Metadata storage backend
METADATA_STORAGE_TYPE=store_api
METADATA_STORAGE_PATH=./metadata_storage
METADATA_STORE_API_URL=http://localhost:8002
METADATA_STORE_API_TIMEOUT_SECONDS=10.0

# Authorization cache
AUTH_CACHE_TTL=60
AUTH_CACHE_MAXSIZE=1000

# Deterministic minting
MINTER_SHOULDER=
MINTER_NOID_LENGTH=7
MINTER_NOID_CHECKDIGIT=true
```

## 9. Docker and Compose

`docker-compose.yml` defines separated services:

- `postgres`
- `minter-api`
- `minter-worker`

API and worker share PostgreSQL via `DATABASE_URL`.
`dark-store-api` is an external companion service and should be reachable via `METADATA_STORE_API_URL` when using `METADATA_STORAGE_TYPE=store_api`.

## 10. Testing

Main suites:

- `tests/test_persistence.py`
- `tests/test_worker.py`
- `tests/test_worker_unit.py`
- `tests/test_main_worker_lock.py`
- `tests/test_storage.py`
- `tests/test_storage_store_api.py`
- `tests/test_auth_cache.py`
- `tests/test_middleware.py`

Recent full regression result: `107 passed`.

## 11. Migrations

- Migration files in `alembic/versions/`
- `alembic upgrade head` auto-runs on API and worker startup
- Fail-fast if migration fails

## 12. Key Updated Files

- `app/main.py` (API without internal scheduler)
- `app/main_worker.py` (standalone worker + DB heartbeat + advisory lock)
- `app/api/arks.py` (`PUT` state-aware transitions including import path)
- `app/api/worker.py` (worker status from DB heartbeat)
- `app/repositories/ark_repository.py` (CAS transitions and atomic updates)
- `app/workers/publisher.py` (`create_ark` vs `update_ark` by state)
- `app/models/states.py` (new `UPDATE` state)
- `dark_core_lib/metadata/service.py` (shared L1/L2 orchestration)
- `dark_core_lib/metadata/storage/*` (shared storage backends used by minter and resolver)
- `app/dependencies.py` (runtime backend selection by `METADATA_STORAGE_TYPE`)
- `README.md`
- `minter-architecture.md`
- `noid.md`

## 13. Recommended Next Improvements

- Strict ownership validation for `DELETE` using mTLS identity mapping
- Operational alerting for stale worker heartbeat (Prometheus/Grafana or equivalent)

---

**Overall status:** Operational implementation with full API/worker separation, two-level metadata pipeline, shared metadata/storage contract in `dark-core-lib`, and robust PostgreSQL concurrency controls (row locks + CAS + advisory lock).
