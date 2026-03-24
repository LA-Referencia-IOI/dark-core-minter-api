# IMPLEMENTATION STATUS (Two-Level Metadata)

This document summarizes the changes made to implement the two-level ARK metadata system (L1 JSON + L2 Original) and outlines the pending validation steps.

## ✅ Completed Tasks

### 1. Schema & Models
- **Level 1 Schema:** Consolidated in `dark_core_lib.metadata.schemas.Level1Metadata`. The minter and resolver now share the same L1/L2 metadata contract.
- **Database Models:** 
  - Added `ARKMetadata` table (`app/database/models.py`) to store both L1 JSON and L2 raw content.
  - Removed `metadata_cid`/metadata payload duplication from `ARKRecord`; metadata CIDs and payload now live in `ARKMetadata`.
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
- **Worker Logic:** Updated `publish_single_ark` in `app/workers/publisher.py`.
  - Step 1: Checks for `ARKMetadata` in DB.
  - Step 2: Stores L2 content through the shared metadata backend -> gets `level2_cid`.
  - Step 3: Injects `level2_cid` into L1 JSON.
  - Step 4: Stores modified L1 JSON through the same shared backend -> gets `level1_cid`.
  - Step 5: Updates DB with both CIDs.
  - Step 6: Publishes to blockchain using `level1_cid`.

### 3. Testing
- Updated endpoint tests to match the new API contract (`minimal_metadata` + `original_metadata`).
- Updated persistence tests for two-level metadata behavior (DB storage, state transitions, metadata CID reset/rebuild semantics).
- Reworked worker tests (`tests/test_worker.py` and `tests/test_worker_unit.py`) to validate:
  - required `ARKMetadata` presence,
  - L2 -> L1 storage order,
  - L2 CID injection into L1 before publish,
  - DB updates for `level1_cid` / `level2_cid`,
  - publish and retry/permanent-failure flows for both DRAFT and UPDATE states.
- Updated `GET /api/v1/arks/{ark}` to return full `minimal_metadata` and `metadata_schema` (without deprecated `metadata_format`).
- Test runner now defaults to local SQLite in `tests/conftest.py` (override with `TEST_DATABASE_URL` for PostgreSQL).
- Documentation and operational assets synced to new contract:
  - `README.md`, `IMPLEMENTATION_SUMMARY.md`, `minter-architecture.md`, `docs/api.rst`,
  - all notebooks in `notebooks/`,
  - agent skill/workflow files (`.agent/skills/dark-core-minter-api/SKILL.md`, `.agent/workflows/test-minter-api.md`, `.agent/workflows/setup-minter-postgres.md`).
- Validation result (2026-02-10): `python3 -m pytest -q` -> **107 passed**.

## ⚠️ Pending Validation / Next Steps

### 1. Alembic Migrations
- Incremental migration `0002_add_original_media_type` is now part of the live schema.
- **Action:** Recreate DB and run `alembic upgrade head`, then verify:
  - `ark_records` no longer has `alternate_identifiers`,
  - `ark_metadata.level1_json` keeps `alternate_identifiers`/`alternate_urls` when provided.
  - `ark_metadata.original_media_type` is present for raw L2 responses in the resolver.

### 2. Integration Testing
- **Shared metadata backend:** The worker logic assumes a functional configured backend (`filesystem` or `store_api`).
- **Blockchain:** The worker publishes to the blockchain.
- **Action:**
  1. Start the stack: `docker compose up -d` (including postgres, blockchain, minter, worker, and optional resolver/store-api).
  2. Reserve an ARK via API: `POST /api/v1/arks`
  3. Update metadata via API: `PUT /api/v1/arks/{ark}` with L1+L2 payload.
  4. Verify in DB: `ark_metadata` should have content, `ark_records` should be `DRAFT`.
  5. Run Worker: Trigger the worker loop.
  6. Verify Result: `ark_records` should be `PUBLISHED`, `ark_metadata` should have CIDs, and the shared backend should contain the data.
  7. Verify through resolver: redirect, `?info`, and `?metadata`.
