# Current Implementation - dARK Core Minter API

## Status

Active implementation with separated runtime architecture:

- API HTTP process (`app.main`)
- Metadata persistence worker process (`app.main_worker metadata`)
- Chain publisher worker process (`app.main_worker chain`)
- Full local ARK lifecycle persistence in `ark_records`
- Deterministic sequential minting per namespace in `noid_counters`
- Two-level metadata pipeline (L1 validated JSON + L2 original content) with shared metadata/storage abstractions from `dark-core-lib`

## 1. Process Architecture

### API (HTTP only)

- Entry point: `app/main.py`
- CLI: `dark-core-api`
- Direct Uvicorn: `uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers ${MINTER_API_WORKERS:-2}`
- Exposes ARK and Authority endpoints
- Does not run internal publisher loop
- Does not require RPC during startup; blockchain endpoints initialize `dark-core-lib` lazily

### Workers

- Entry point: `app/main_worker.py`
- CLI: `dark-core-worker`
- Runtime modes:
  - `metadata`: persists staged metadata to storage, stores CIDs, and purges local payloads
  - `chain`: publishes ARKs with complete metadata CIDs to blockchain
- Post-cycle sleep loop with separate normal and full-batch sleeps
- Process status command: `dark-core-worker-status`
- Also available as module command: `python -m app.main_worker status`

### Operational model

- API, metadata worker, and chain worker run decoupled
- Deployment and scaling are independent
- Worker runtime status is exposed through DB heartbeat for both workers
- The chain worker pauses with `PAUSED_RPC_UNAVAILABLE` while RPC is unavailable and resumes after RPC health recovers
- Schema migrations are an explicit operation, not part of API/worker startup

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
- `level1_json` (validated Level-1 JSON payload; nullable after storage purge)
- `level1_cid` (assigned by worker after storage)
- `original_content` (raw Level-2 content; nullable after storage purge)
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
- last-cycle cadence metrics (`last_cycle_duration_seconds`, processed/succeeded/failed)
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
  - `PUBLISHED -> UPDATE`
  - `DRAFT/UPDATE -> PUBLISHED`
  - `* -> TOMBSTONE` (idempotent)
- `publish_retry_count` increments atomically in SQL.
- Worker singleton is enforced with PostgreSQL advisory lock (`pg_try_advisory_lock`) keyed by each worker runtime name.

## 4. Lifecycle Flows

```text
RESERVED (POST /arks)
    -> DRAFT (PUT /arks/{ark})
    -> PUBLISHED (metadata worker stores CIDs, then chain worker create_ark)

PUBLISHED (local/chain)
    -> UPDATE (PUT /arks/{ark})
    -> PUBLISHED (metadata worker stores CIDs, then chain worker update_ark)

RESERVED|DRAFT|UPDATE|PUBLISHED
    -> TOMBSTONE (DELETE /arks/{ark})
```

Behavior details for `PUT /api/v1/arks/{ark}`:

- If local state is `RESERVED`: move to `DRAFT`.
- If local state is `DRAFT`: reject with `409` because creation is already pending.
- If local state is `PUBLISHED`: move to `UPDATE`.
- If local state is `UPDATE`: reject with `409` because an update is already pending.
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

- `GET /api/v1/worker/status` (compact operational summary)
- `GET /api/v1/worker/status?detail=full` (full DB heartbeat, queue, cadence, config, and error details)

### Health

- `GET /health`
- Verifies blockchain, DB, and metadata storage

## 6. Workers

### Metadata worker behavior

- Claims pending ARKs in `DRAFT`/`UPDATE` whose metadata CIDs are incomplete
- Applies exponential backoff for retries
- Stores Level-2 and Level-1 through the configured metadata backend
- Persists `original_cid` and `level1_cid`
- Purges local `level1_json` and `original_content`
- Resets `publish_*` tracking after successful metadata persistence

### Chain worker behavior

- Claims pending ARKs in `DRAFT`/`UPDATE` whose metadata CIDs are complete
- Applies exponential backoff for retries
- Groups claimed ARKs by `authority_id`
- Publishes to blockchain via `dark-core-lib` `publish_ark_operations(...)`
- Sends individual create/update transactions with sequential pending nonces in windows controlled by `CHAIN_WORKER_PAGE_SIZE`
- Does not do routine pre-write `exists()` or post-write `get()` reads in the success path
- Updates state to `PUBLISHED` only for confirmed semantic results
- On reverted, ambiguous, send-failed, or not-sent results, reads on-chain state once to reconcile:
  - matching `target` + `level1_cid` is treated as success
  - create mismatch is a permanent conflict
  - update mismatch or missing state follows retry/permanent classification based on the original error
- Marks retryable or permanent publish failures when reconcile does not confirm success

### Worker loop

- Configured by:
  - `METADATA_WORKER_PAGE_SIZE`
  - `METADATA_WORKER_SLEEP_SECONDS`
  - `METADATA_WORKER_MAX_RETRIES`
  - `METADATA_WORKER_RETRY_BACKOFF_BASE`
  - `CHAIN_WORKER_PAGE_SIZE`
  - `CHAIN_WORKER_SLEEP_SECONDS`
  - `CHAIN_WORKER_RPC_RETRY_SECONDS`
  - `CHAIN_WORKER_MAX_RETRIES`
  - `CHAIN_WORKER_RETRY_BACKOFF_BASE`
- `*_WORKER_PAGE_SIZE` is the number of ARKs claimed per cycle.
- `CHAIN_WORKER_PAGE_SIZE` is also the core-lib transaction pipeline size.
- `*_WORKER_SLEEP_SECONDS` is used only after an empty or partial page; full pages continue immediately.
- `CHAIN_WORKER_RPC_RETRY_SECONDS` is used while RPC is unavailable.
- A lightweight heartbeat supervisor thread keeps DB heartbeat fresh during long-running cycles, for example when a full chain batch takes longer than `WORKER_HEARTBEAT_STALE_AFTER_SECONDS`.

### Distributed singleton operation

- PostgreSQL advisory lock per worker runtime name
- If a lock is already held for that worker, startup aborts
- Local pidfile remains for host-level process status and is scoped by worker name
- `dark-core-worker-status` returns:
  - `RUNNING pid=<pid>` (exit code 0)
  - `NOT_RUNNING` (exit code 1)
- Periodic heartbeat persisted in DB with status and counters, including during long worker cycles

## 7. Metadata Storage (Two-Level)

- API receives:
  - `minimal_metadata` (minimal extracted metadata JSON),
  - `original_metadata` (raw record),
  - `metadata_schema` (schema label for L2).
- `alternate_identifiers` / `alternate_urls` are part of `minimal_metadata` and are persisted only in `ark_metadata.level1_json`.
- API validates L1 against `Level1Metadata` and stores both levels in `ark_metadata`.
- Metadata worker performs storage pipeline:
  1. Store L2 (`original_content`) -> `original_cid`.
  2. Inject `original_cid` into L1 JSON (`original_metadata.cid`).
  3. Store L1 JSON -> `level1_cid`.
  4. Persist both CIDs.
  5. Purge local `level1_json` and `original_content`.
- Chain worker publishes on-chain using `level1_cid`.
- `metadata_cid` is sourced from on-chain state (`cid`) and/or `ark_metadata.level1_cid`; it is not duplicated in `ark_records`.
- `filesystem` and `store_api` backends are used by the metadata worker during the storage phase.
- the implementation of those backends now lives in `dark_core_lib.metadata.storage`, not in the minter package.

## 8. Relevant Configuration

```bash
# DB
DATABASE_URL=postgresql://dark:dark_password@localhost:5433/minter
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10

# Metadata worker
METADATA_WORKER_ENABLED=true
METADATA_WORKER_PAGE_SIZE=100
METADATA_WORKER_SLEEP_SECONDS=2
METADATA_WORKER_MAX_RETRIES=5
METADATA_WORKER_RETRY_BACKOFF_BASE=2.0
METADATA_WORKER_RUNTIME_NAME=metadata-publisher

# Chain worker
CHAIN_WORKER_ENABLED=true
CHAIN_WORKER_PAGE_SIZE=20
CHAIN_WORKER_SLEEP_SECONDS=5
CHAIN_WORKER_RPC_RETRY_SECONDS=10
CHAIN_WORKER_MAX_RETRIES=5
CHAIN_WORKER_RETRY_BACKOFF_BASE=2.0
CHAIN_WORKER_RUNTIME_NAME=chain-publisher

# Worker heartbeat
WORKER_HEARTBEAT_INTERVAL_SECONDS=10
WORKER_HEARTBEAT_STALE_AFTER_SECONDS=180

# Metadata storage backend
METADATA_STORAGE_TYPE=store_api
METADATA_STORAGE_PATH=./metadata_storage
METADATA_STORE_API_URL=http://localhost:8003
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
- `minter-metadata-worker`
- `minter-chain-worker`

API and both workers share PostgreSQL via `DATABASE_URL`.
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

Recent full regression result: `155 passed`.

## 11. Migrations

- Migration files in `alembic/versions/`
- Run explicitly with `python -m app.database migrate` or `docker compose run --rm minter-api migrate`
- API and worker startup only verify database connectivity

## 12. Key Updated Files

- `app/main.py` (API without internal publisher loop)
- `app/main_worker.py` (metadata/chain workers + DB heartbeat + advisory lock)
- `app/api/arks.py` (`PUT` state-aware transitions including import path)
- `app/api/worker.py` (simple worker status plus full detail mode with queue metrics, cadence metrics, and DB heartbeat)
- `app/repositories/ark_repository.py` (CAS transitions and atomic updates)
- `app/workers/publisher.py` (metadata persistence worker, chain publisher worker, and legacy facade)
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

**Overall status:** Operational implementation with API/worker separation, two independent worker stages, two-level metadata pipeline, local payload purge after storage persistence, shared metadata/storage contract in `dark-core-lib`, and robust PostgreSQL concurrency controls (row locks + CAS + advisory lock).
