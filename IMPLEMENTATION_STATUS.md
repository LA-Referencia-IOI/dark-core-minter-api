# IMPLEMENTATION STATUS (Two-Level Metadata)

This document summarizes the changes made to implement the two-level ARK metadata system (L1 JSON + L2 Original), the split worker pipeline, and the remaining integration validation steps.

## ✅ Completed Tasks

### 1. Schema & Models
- **Level 1 Schema:** Consolidated in `dark_core_lib.metadata.schemas.Level1Metadata`. The minter and resolver now share the same L1/L2 metadata contract.
- **Database Models:** 
  - Added `ARKMetadata` table (`app/database/models.py`) to store both L1 JSON and L2 raw content.
  - Removed `metadata_cid`/metadata payload duplication from `ARKRecord`; metadata CIDs and payload now live in `ARKMetadata`.
  - `ark_metadata.level1_json` and `ark_metadata.original_content` are nullable so local staged payloads can be purged after storage persistence.
- **API Models:**
  - Updated `UpdateARKMetadataRequest` (`app/models/requests.py`) to accept `minimal_metadata` (dict) and `original_metadata` (string).
  - Removed top-level `alternate_identifiers`/`alternate_urls`; these now live only inside `minimal_metadata`.
  - Updated `ARKResponse` (`app/models/responses.py`) to include `metadata_schema`, full `minimal_metadata` (Level-1), `level1_cid`, and `level2_cid` (without deprecated `metadata_format`).

### 2. Logic Implementation
- **API Endpoint:** Updated `update_ark_metadata` in `app/api/arks.py`.
  - Validates L1 JSON against schema.
  - Stores metadata in the DB (`ark_metadata` table) *instead* of writing to the external metadata backend directly.
  - Triggers the worker (via state transition).
- **Repository Layer:** Updated `app/repositories/ark_repository.py`.
  - Added `create_or_update_metadata`.
  - Updated state transition methods (`update_to_draft`, etc.) to align with the new flow.
- **Worker Logic:** Split the worker pipeline in `app/workers/publisher.py`.
  - `MetadataPersistenceWorker` claims `DRAFT`/`UPDATE` ARKs whose metadata CIDs are incomplete.
  - The metadata worker stores L2 through the shared metadata backend -> gets `level2_cid`.
  - It injects `level2_cid` into L1 JSON.
  - It stores modified L1 JSON through the same shared backend -> gets `level1_cid`.
  - It updates DB with both CIDs, purges local `level1_json` / `original_content`, and resets `publish_*` tracking.
  - `ChainPublisherWorker` claims `DRAFT`/`UPDATE` ARKs whose metadata CIDs are complete and publishes to blockchain using `level1_cid`.
  - The chain worker groups claimed ARKs by `authority_id` and calls `dark-core-lib` `publish_ark_operations(...)`.
  - The core-lib pipeline sends individual create/update transactions with sequential pending nonces and returns one semantic result per ARK.
  - The chain worker uses the fast path: no routine `exists()` before write and no routine `get()` after successful receipts.
  - If a result is reverted, ambiguous, send-failed, or not-sent, the worker reconciles once with `get_ark`; matching on-chain `target` + `level1_cid` is treated as success, while missing/divergent state is classified as retryable or permanent.
  - `ARKPublisher` remains as a backward-compatible facade for existing tests and callers.

### 3. Testing
- Updated endpoint tests to match the new API contract (`minimal_metadata` + `original_metadata`).
- Updated persistence tests for two-level metadata behavior (DB storage, state transitions, metadata CID reset/rebuild semantics).
- Reworked worker tests (`tests/test_worker.py` and `tests/test_worker_unit.py`) to validate:
  - required `ARKMetadata` presence,
  - L2 -> L1 storage order,
  - L2 CID injection into L1 before publish,
  - DB updates for `level1_cid` / `level2_cid`,
  - local payload purge after successful metadata persistence,
  - chain publishing when CIDs are complete and local payloads are gone,
  - fast-path chain publishing without routine success-path reads,
  - authority grouping and transaction-pipeline result handling,
  - reconcile-on-error behavior for matched, missing, and divergent on-chain state,
  - publish and retry/permanent-failure flows for both DRAFT and UPDATE states.
- Updated `GET /api/v1/arks/{ark}` to return `minimal_metadata` and `metadata_schema` without deprecated `metadata_format`; after local payload purge it best-effort loads Level-1 from storage by `level1_cid`, and still returns CIDs/schema if storage lookup fails.
- Updated `GET /api/v1/worker/status` to return a compact operational summary by default, with `?detail=full` for detailed metadata/chain heartbeat, queue, cadence, config, and error summaries.
- Worker heartbeat now uses a lightweight supervisor thread so long metadata or blockchain cycles keep liveness fresh while the main worker loop is busy.
- API/worker startup no longer runs Alembic automatically; migrations are explicit via `python -m app.database migrate` or `docker compose run --rm minter-api migrate`.
- The API no longer requires RPC during startup. The chain worker checks RPC before claiming ARKs, pauses as `PAUSED_RPC_UNAVAILABLE` when unavailable, and resumes when RPC health recovers.
- Test runner now defaults to local SQLite in `tests/conftest.py` (override with `TEST_DATABASE_URL` for PostgreSQL).
- Documentation and operational assets synced to new contract:
  - `README.md`, `IMPLEMENTATION_SUMMARY.md`, `minter-architecture.md`, `docs/api.rst`,
  - all notebooks in `notebooks/`,
  - agent skill/workflow files (`.agent/skills/dark-core-minter-api/SKILL.md`, `.agent/workflows/test-minter-api.md`, `.agent/workflows/setup-minter-postgres.md`).
- Validation result (2026-05-20): `.venv/bin/python -m pytest` -> **155 passed**.

## ⚠️ Pending Validation / Next Steps

### 1. Fresh Schema Validation
- The current preference is to recreate the local/integration database from the base schema instead of adding a new incremental migration for the metadata purge nullable change.
- `0001_initial_schema` now represents the expected clean schema for `ark_metadata.level1_json` and `ark_metadata.original_content`.
- **Action:** Recreate DB and run `python -m app.database migrate`, then verify:
  - `ark_records` no longer has `alternate_identifiers`,
  - `ark_metadata.level1_json` keeps `alternate_identifiers`/`alternate_urls` when provided.
  - `ark_metadata.original_media_type` is present for raw L2 responses in the resolver.
  - `ark_metadata.level1_json` and `ark_metadata.original_content` are nullable.

### 2. Integration Testing
- **Shared metadata backend:** The worker logic assumes a functional configured backend (`filesystem` or `store_api`).
- **Blockchain:** The worker publishes to the blockchain.
- **Action:**
  1. Start PostgreSQL/blockchain and run migrations: `docker compose run --rm minter-api migrate`.
  2. Start the stack: `docker compose up -d` (including postgres, blockchain, minter API, metadata worker, chain worker, and optional resolver/store-api).
  3. Reserve an ARK via API: `POST /api/v1/arks`
  4. Update metadata via API: `PUT /api/v1/arks/{ark}` with L1+L2 payload.
  5. Verify in DB: `ark_metadata` should have content, `ark_records` should be `DRAFT`.
  6. Run or wait for metadata worker: verify `ark_metadata` has CIDs and local payloads are purged.
  7. Run or wait for chain worker: verify `ark_records` is `PUBLISHED`.
  8. Verify Result: shared backend should contain the data and `GET /api/v1/worker/status` should show both worker heartbeats and RPC state.
  9. Verify through resolver: redirect, `?info`, and `?metadata`.
