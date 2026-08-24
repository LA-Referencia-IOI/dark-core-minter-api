# dARK Core Minter API

REST API and background workers for ARK reservation, metadata staging, metadata persistence, and on-chain publication using `dark-core-lib`.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)

## Overview

`dark-core-minter-api` is the service responsible for the minting lifecycle of ARKs in the dARK stack. It does not publish directly on every write request. Instead, it splits the lifecycle into two phases:

1. synchronous API work
   - reserve deterministic ARKs locally
   - validate and store metadata in PostgreSQL
   - move ARKs into `DRAFT` or `UPDATE`
2. asynchronous worker work
   - persist Level-2 and Level-1 metadata to the configured storage backend
   - purge local staged metadata payloads after storage persistence
   - publish `create_ark` or `update_ark` on-chain through `dark-core-lib`
   - finalize the ARK as `PUBLISHED`

This separation gives us better resilience, simpler retries, and a cleaner operational model.

## Documentation Map

Use the docs in this order, depending on what you need:

- [README.md](./README.md)
  - operational overview, lifecycle, deployment, config, notebooks
- [minter-architecture.md](./minter-architecture.md)
  - technical implementation details, module map, concurrency model, worker internals
- [notebooks/README.md](./notebooks/README.md)
  - notebook index, expected environment variables, execution notes
- [noid.md](./noid.md)
  - detailed NOID generation rules and rationale

## Direct Import and NAAN Audit Commands

`dark-import-arks` imports ARK identifiers and target URLs directly to the
blockchain without metadata, IPFS storage, or a Minter API request. It creates
records with an empty CID and uses the same `dark-core-lib` configuration as
the Minter.

The input is a UTF-8 CSV with, at minimum, these columns:

```csv
darkidentifier,itemurl
ark:/41046/001300001kq89,https://repositorio.example/handle/123/1
```

Run its non-writing preflight first, then pass `--execute` to submit
transactions:

```bash
dark-import-arks records.csv \
  --authority-id authority-uuid \
  --env-file /opt/dark/minter/.env

dark-import-arks records.csv \
  --authority-id authority-uuid \
  --env-file /opt/dark/minter/.env \
  --execute
```

Before writing, it verifies that the authority is active and is authorized for
every NAAN in the valid input. Existing ARKs with the same owner and URL are
skipped; different owner or URL values are reported as conflicts. The result
CSV is flushed after each row and can be reused as a checkpoint.

`dark-check-naans` is read-only: without an authority it lists the unique NAANs
in a CSV, and with one it verifies assignments. To list only missing NAANs:

```bash
dark-check-naans records.csv \
  --authority-id authority-uuid \
  --env-file /opt/dark/minter/.env \
  --only-missing \
  --format csv
```

It exits with status `1` when there is an invalid ARK, inactive authority, or a
missing NAAN assignment.

## Runtime Architecture

If you want the implementation-oriented view behind this diagram, continue with [minter-architecture.md](./minter-architecture.md).

```mermaid
flowchart LR
    C["Client or Notebook"] --> API["Minter API (FastAPI)"]
    API --> DB["PostgreSQL"]
    API --> CORE["dark-core-lib"]
    CORE --> CHAIN["Blockchain RPC + Contracts"]

    MW["Metadata Worker"] --> DB
    MW --> STORE["Metadata Storage via dark-core-lib\nfilesystem or dark-store-api"]

    CW["Chain Worker"] --> DB
    CW --> CORE

    DB --> HB["worker_runtime_status"]
    API --> WS["GET /api/v1/worker/status"]
    WS --> HB
```

## Main Responsibilities

- API process
  - reserves ARKs deterministically
  - validates authority and NAAN authorization
  - validates Level-1 metadata
  - stores Level-1 and Level-2 metadata in PostgreSQL
  - exposes health and worker status
- Metadata worker process
  - claims `DRAFT` and `UPDATE` records whose metadata CIDs are incomplete
  - persists metadata through the shared `dark_core_lib.metadata` layer
  - stores `level1_cid` and `original_cid`
  - purges local `level1_json` and `original_content`
  - keeps DB heartbeat fresh during long metadata cycles
- Chain worker process
  - claims `DRAFT` and `UPDATE` records whose CIDs are complete
  - publishes create/update transactions through `dark-core-lib`
  - handles retries, backoff, and permanent failures for blockchain publication
  - keeps DB heartbeat fresh during long blockchain batches
- PostgreSQL
  - source of truth for local lifecycle state
  - stores reserved IDs, staged metadata, CIDs, retry status, and worker heartbeats
- Metadata storage backend
  - stores raw Level-2 content and the publishable Level-1 JSON
  - is configured in the minter, but implemented in `dark-core-lib` so resolver and minter use the same contract
- Blockchain
  - final public state for ARK registration and updates

## End-to-End Flow

```mermaid
sequenceDiagram
    participant Client as "Client"
    participant API as "Minter API"
    participant DB as "PostgreSQL"
    participant MetadataWorker as "Metadata Worker"
    participant ChainWorker as "Chain Worker"
    participant Store as "Metadata Storage"
    participant Core as "dark-core-lib"
    participant Chain as "Blockchain"

    Client->>API: POST /api/v1/arks
    API->>DB: reserve ARK locally (state=RESERVED)
    API-->>Client: 201 ark:NAAN/name

    Client->>API: PUT /api/v1/arks/{ark}
    API->>DB: store L1 + L2, move to DRAFT/UPDATE
    API-->>Client: 200 pending publication

    MetadataWorker->>DB: claim ARKs pending metadata persistence
    MetadataWorker->>Store: store Level-2 metadata
    MetadataWorker->>Store: store Level-1 metadata with embedded L2 reference
    MetadataWorker->>DB: persist CIDs and purge local payloads

    ChainWorker->>DB: claim ARKs with complete CIDs
    ChainWorker->>Core: create_ark or update_ark
    Core->>Chain: signed transaction
    ChainWorker->>DB: mark ARK as PUBLISHED
```

## ARK Lifecycle

### State Machine

```mermaid
stateDiagram-v2
    [*] --> RESERVED: POST /api/v1/arks
    RESERVED --> DRAFT: PUT /api/v1/arks/{ark}
    DRAFT --> PUBLISHED: worker create_ark
    PUBLISHED --> UPDATE: PUT /api/v1/arks/{ark}
    UPDATE --> PUBLISHED: worker update_ark
    RESERVED --> TOMBSTONE: DELETE /api/v1/arks/{ark}
    DRAFT --> TOMBSTONE: DELETE /api/v1/arks/{ark}
    UPDATE --> TOMBSTONE: DELETE /api/v1/arks/{ark}
    PUBLISHED --> TOMBSTONE: DELETE /api/v1/arks/{ark}
    TOMBSTONE --> TOMBSTONE: idempotent delete
```

### What Each State Means

- `RESERVED`
  - ARK exists only in the local database.
  - No blockchain transaction has happened yet.
- `DRAFT`
  - metadata is staged locally and waiting for `create_ark` publication.
- `UPDATE`
  - ARK already exists on-chain and has a pending metadata update.
- `PUBLISHED`
  - the latest Level-1 CID has been published on-chain.
- `TOMBSTONE`
  - the ARK is locally deactivated in the minter.
  - today this is a local lifecycle state, not an on-chain tombstone transaction.

## Detailed Flows

### 1. Reserve

A reserve call generates a deterministic name using a namespace counter and NOID encoding.

```mermaid
sequenceDiagram
    participant Client as "Client"
    participant API as "POST /api/v1/arks"
    participant Auth as "Authorization Cache + Core"
    participant Counter as "NoidCounterRepository"
    participant DB as "PostgreSQL"

    Client->>API: reserve(authority_id, naan)
    API->>Auth: is authority authorized for NAAN?
    Auth-->>API: yes/no
    API->>Counter: allocate next counter for namespace
    Counter->>DB: UPDATE ... RETURNING next_value
    DB-->>Counter: counter
    API->>API: mint ark:NAAN/name
    API->>DB: INSERT ark_records(state=RESERVED)
    API-->>Client: 201 Created
```

Important points:
- ARKs are reserved locally first.
- The namespace key is `NAAN + shoulder`.
- The name format is `{shoulder}{generated_part}{checkdigit}`.
- A uniqueness collision is retried with a new counter value.

### 2. Update Metadata

`PUT /api/v1/arks/{ark}` does not publish immediately. It stores metadata locally and prepares the record for the asynchronous workers.

```mermaid
sequenceDiagram
    participant Client as "Client"
    participant API as "PUT /api/v1/arks/{ark}"
    participant DB as "PostgreSQL"
    participant Chain as "Blockchain"

    Client->>API: target + minimal_metadata + original_metadata
    API->>API: validate ARK + checkdigit + authority ownership
    alt ARK exists locally
        API->>DB: update metadata and state
    else ARK missing locally
        API->>Chain: check if ARK exists on-chain
        API->>DB: import local record if owner matches authority
        API->>DB: move to UPDATE
    end
    API-->>Client: 200 pending asynchronous processing
```

Important points:
- reserve only assigns identifiers; it does not accept or persist a `target`
- `minimal_metadata` is the validated Level-1 JSON payload.
- `original_metadata` is the raw Level-2 payload.
- For `RESERVED -> DRAFT`, the `target` URL is required.
- If an ARK exists on-chain but not locally, the minter can import it into the local DB before staging an update.
- The import path now validates that the blockchain owner matches the authority making the request.

### 3. Async Metadata Persistence and Publish

The asynchronous path is split into two independent workers. The metadata worker is the only component that writes staged metadata to the backend. The chain worker is the only component that publishes blockchain transactions.

```mermaid
sequenceDiagram
    participant MetadataWorker as "MetadataPersistenceWorker"
    participant ChainWorker as "ChainPublisherWorker"
    participant Repo as "ARKRepository"
    participant DB as "PostgreSQL"
    participant Store as "Metadata Storage"
    participant Core as "dark-core-lib"
    participant Chain as "Blockchain"

    MetadataWorker->>Repo: get_metadata_pending_persist(limit)
    Repo->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    Repo->>DB: mark publish_last_attempt_at
    MetadataWorker->>DB: commit claim

    loop each claimed ARK
        MetadataWorker->>Store: store Level-2 content
        Store-->>MetadataWorker: original_cid
        MetadataWorker->>Store: store Level-1 JSON with schema + media_type + original_cid
        Store-->>MetadataWorker: level1_cid
        MetadataWorker->>Repo: persist CIDs, purge local payloads, reset publish tracking
    end

    ChainWorker->>Repo: get_chain_pending_publish(limit)
    Repo->>DB: SELECT rows with complete CIDs
    Repo->>DB: mark publish_last_attempt_at
    ChainWorker->>DB: commit claim

    ChainWorker->>ChainWorker: group claimed ARKs by authority_id
    loop each authority group
        ChainWorker->>Core: publish_ark_operations(..., pipeline_size)
        Core->>Chain: send signed txs with sequential pending nonces
        Core->>Chain: wait receipts and return one semantic result per ARK
        alt result confirmed
            ChainWorker->>Repo: update_to_published
        else reverted, ambiguous, send_failed, or not_sent
            ChainWorker->>Core: get_ark(naan, name)
            alt on-chain cid/url match expected values
                ChainWorker->>Repo: update_to_published
            else missing or divergent on-chain state
                ChainWorker->>Repo: mark retryable or permanent failure
            end
        end
        ChainWorker->>DB: commit
    end
```

Important points:
- storage and blockchain publication happen in workers, not in the request thread.
- Level-2 is stored first.
- Level-1 is then re-serialized with the embedded Level-2 reference: `schema`, `media_type`, and `cid`.
- after both CIDs are stored, local `level1_json` and `original_content` are purged from PostgreSQL.
- the blockchain stores the Level-1 CID as the canonical pointer.
- `publish_*` retry fields are reused for the currently pending stage; they are reset after metadata persistence succeeds so blockchain retries start cleanly.
- the shared implementation of this pipeline now lives in `dark_core_lib.metadata.MetadataService`.
- the metadata worker checks metadata storage readiness before claiming ARKs. If Store API/IPFS is unavailable, or IPFS Cluster reports too few peers, it pauses as `PAUSED_STORAGE_UNAVAILABLE` and does not consume ARK retries.
- the chain worker calls `dark-core-lib` through `publish_ark_operations(...)`, grouping ARKs by `authority_id` and sending individual create/update transactions with sequential pending nonces.
- `CHAIN_WORKER_PAGE_SIZE` is the nominal maximum chain page size; the default is `20`. Before each cycle, the worker asks `dark-core-lib` for `get_chain_capacity(...)` and may temporarily use a smaller effective page size for both DB claim and the core-lib pipeline window.
- the chain worker pauses before claiming ARKs if core-lib reports RPC unavailable, stalled block production, or critical chain congestion. It also pauses after a full effective page of infrastructure-deferred receipt failures, which protects QBFT/Besu from repeated saturated pages.
- the chain worker only reads blockchain during error handling. Reconcile never creates a different operation; it only confirms that the failed/ambiguous write already produced the expected `target` + `level1_cid`, or classifies the failure for retry/permanent handling.

### 4. Tombstone

`DELETE /api/v1/arks/{ark}` performs a local tombstone transition.

```mermaid
flowchart LR
    A["DELETE /api/v1/arks/{ark}"] --> B["validate authority identity"]
    B --> C["validate local ownership"]
    C --> D["set local state to TOMBSTONE"]
    D --> E["return tombstoned record"]
```

Important points:
- the endpoint now validates local ownership before allowing the tombstone.
- the current behavior is local to the minter database.
- this is not yet a blockchain-level revocation or deactivation flow.

## Metadata Model

The service manages two levels of metadata.

```mermaid
flowchart TD
    L2["Level-2 original_metadata\nXML, JSON, text, raw payload"] --> CID2["stored in backend -> original_cid"]
    L1["Level-1 minimal_metadata\nvalidated JSON"] --> EMBED["inject original_metadata.{schema, media_type, cid}"]
    CID2 --> EMBED
    EMBED --> CID1["stored in backend -> level1_cid"]
    CID1 --> CHAIN["published on-chain"]
```

- Level-1
  - validated JSON
  - canonical publish payload
  - includes the pointer to Level-2
- Level-2
  - original raw payload as submitted by the client
  - can be XML, JSON, or other supported text content
- PostgreSQL stores both payloads only while they are staged for metadata persistence.
- After metadata persistence succeeds, PostgreSQL keeps CIDs and schema/media-type fields but purges `level1_json` and `original_content`.
- `GET /api/v1/arks/{ark}` returns `minimal_metadata` from local DB when available; after purge it best-effort loads Level-1 from the configured storage backend using `level1_cid`.
- If that storage lookup fails, `GET /api/v1/arks/{ark}` still returns state, target, CIDs, and schema without `minimal_metadata`.
- The storage backend stores the immutable content addressed versions.
- The storage backend interface and Level-1 schema are shared with the resolver through `dark-core-lib`.

## Worker Model and Concurrency

### Singleton Strategy

```mermaid
flowchart TD
    S["worker start"] --> P["pidfile guard"]
    P --> L["PostgreSQL advisory lock"]
    L -->|lock acquired| R["post-cycle sleep loop"]
    L -->|lock denied| X["abort startup"]
    R --> H["persist heartbeat in worker_runtime_status"]
    H --> API["/api/v1/worker/status"]
```

The worker uses two protections:
- local pidfile guard
- PostgreSQL advisory lock derived from each worker runtime name

### Publish Concurrency Strategy

```mermaid
flowchart LR
    A["Reserve counter"] --> B["atomic counter allocation"]
    C["Pending publish claim"] --> D["FOR UPDATE SKIP LOCKED"]
    E["Finalize publish"] --> F["CAS transition to PUBLISHED"]
    G["Retry tracking"] --> H["DB-backed retry count + backoff"]
```

This gives us:
- deterministic reservation without duplicate names in normal PostgreSQL deployment
- worker-safe claim of pending ARKs
- compare-and-set style final state transitions
- retry tracking that survives restarts

### Worker Status

`GET /api/v1/worker/status` returns a compact operational summary by default:

```json
{
  "overall": "degraded",
  "message": "Metadata worker is backlogged with 6862 pending; Chain worker is backlogged with 1573 pending.",
  "workers": {
    "metadata": {
      "state": "backlogged",
      "alive": true,
      "last_heartbeat_seconds": 9,
      "last_cycle": {
        "processed": 100,
        "succeeded": 100,
        "failed": 0,
        "duration_seconds": 10.573
      },
      "queue": {
        "pending": 6862,
        "ready": 6830,
        "delayed": 32
      }
    }
  },
  "errors": {
    "retrying": 0,
    "permanent": 0
  },
  "arks": {
    "reserved": 70,
    "draft": 8435,
    "update": 0,
    "published": 96,
    "tombstone": 0
  }
}
```

In the compact payload, `last_cycle.processed` means "attempted in the latest worker cycle". It is not a lifetime counter and it is not the remaining queue size. For the chain worker, `last_cycle.succeeded` means ARKs published or reconciled as already published in the latest cycle. For the metadata worker, it means ARKs whose metadata was persisted in the latest cycle.

Use `GET /api/v1/worker/status?detail=full` for detailed heartbeat, queue, cadence, config, and host/process fields. The full payload includes a `cadence` block for each worker:

- `last_cycle_duration_seconds`: how long the last cycle took.
- `last_cycle_processed`: how many ARKs the last cycle attempted.
- `page_size`: the nominal maximum ARKs this worker can claim in one cycle.
- `effective_page_size_recommended`: the current core-lib recommendation for chain pacing.
- `sleep_seconds`: how long the worker sleeps after an empty or partial page.
- `last_cycle_full_page`: whether the last cycle processed a full page.
- `next_action`: `continue`, `sleep`, or `pause_rpc`.
- `sleep_seconds_next`: the next planned wait, if any.
- `cycle_utilization_ratio`: `last_cycle_duration_seconds / sleep_seconds`.
- `ready_pages`: how many configured pages are needed for the currently ready queue.
- `estimated_seconds_to_drain_ready`: rough drain time for the ready queue at the current sleep and page size.
- `pressure`: compact state such as `idle`, `working`, `backlogged`, `rpc_unavailable`, `stale`, or `unknown`.

The payload also includes `chain_capacity`, a semantic snapshot returned by `dark-core-lib` with the current capacity state, reason, block number, txpool pending count when available, and recommended effective page size.

For the chain worker, `last_cycle_full_page=true` means the worker will continue immediately without sleeping. If core-lib reports RPC or chain capacity unavailable, it does not claim ARKs and reports `next_action=pause_rpc` or `next_action=pause_chain`.

Worker heartbeats are emitted by a lightweight supervisor thread, so a full chain batch can take longer than `WORKER_HEARTBEAT_STALE_AFTER_SECONDS` without incorrectly marking the worker stale.

The detailed `config.chain.worker_page_size` field shows the active chain transaction pipeline window because chain page size and core-lib pipeline size are intentionally the same value. `last_cycle.processed` still means ARKs attempted in the latest cycle, not transactions confirmed forever and not queue size.

### Worker Errors and Rescue

`GET /api/v1/worker/errors` returns a compact error report by default. Add `list=permanent`, `list=retrying`, or `list=all` to receive paginated rows. Both summary and list modes accept `authority_id`, `stage=all|metadata|chain`, and `error_type=all|transaction_reverted|gas_limit|authority|metadata_storage|infrastructure|reconcile_conflict|max_retries|unknown`.

`POST /api/v1/worker/errors/rescue` applies a rescue action to the same filtered universe, limited to permanent chain-stage errors for the authenticated authority. It retries one ARK at a time with `DARK_GAS_LIMIT * 2`; if the gas estimate is still above that limit, the ARK remains permanent and is classified as `gas_limit`.

## API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/arks` | `POST` | Reserve one ARK |
| `/api/v1/arks/batch` | `POST` | Reserve multiple ARKs |
| `/api/v1/arks/{ark}` | `GET` | Read local ARK state plus metadata/CIDs, with best-effort storage fallback |
| `/api/v1/arks/{ark}` | `PUT` | Stage metadata and move to `DRAFT` or `UPDATE` |
| `/api/v1/arks/{ark}` | `DELETE` | Tombstone an ARK locally |
| `/api/v1/authority/{uuid}` | `GET` | Fetch authority details |
| `/api/v1/authority/{uuid}/naans` | `GET` | List authorized NAANs |
| `/api/v1/authority/{uuid}/authorized/{naan}` | `GET` | Check NAAN authorization |
| `/api/v1/worker/status` | `GET` | Read compact worker, queue, error, and ARK state summary |
| `/api/v1/worker/errors` | `GET` | Read compact worker error report or filtered error list |
| `/api/v1/worker/errors/rescue` | `POST` | Retry filtered permanent chain errors for the authenticated authority |
| `/health` | `GET` | Check database, blockchain, and storage wiring |

## Authentication and Authorization

### Current Behavior

- Mutating endpoints
  - `POST /api/v1/arks`
  - `POST /api/v1/arks/batch`
  - `PUT /api/v1/arks/{ark}`
  - `DELETE /api/v1/arks/{ark}`
- When `MTLS_ENABLED=false`
  - the caller must send `X-Authority-Id` or `X-Authority-UUID`
  - that identity must match the `authority_id` in the request body when applicable
- When `MTLS_ENABLED=true`
  - the request must pass the mTLS gate
  - the service also resolves authority identity from trusted request data
- Authorization checks are performed against `dark-core-lib` and cached locally for NAAN authorization decisions.

### Important Operational Note

The current codebase supports local developer mode with header-based authority identity when mTLS is disabled. That is useful for notebooks and local stacks, but production deployments should put the service behind a trusted TLS terminator or enforce stronger identity guarantees.

## NOID Scheme

Current policy:
- alphabet: `0-9bcdfghjkmnpqrstvwxz` (base29)
- generated-part length: `7`
- checkdigit: enabled by default
- namespace key: `"{NAAN}:{SHOULDER}"`

Capacity per namespace:
- `29^7 = 17,249,876,309` generated identifiers

Detailed algorithm notes: [noid.md](./noid.md)

## Data Model Summary

```mermaid
erDiagram
    ARK_RECORDS {
        int id PK
        string naan
        string name
        string state "R,D,U,P,T"
        string authority_id
        string target
        int publish_retry_count
        datetime publish_last_attempt_at
        boolean publish_permanently_failed
    }

    ARK_METADATA {
        int id PK
        int ark_record_id FK
        json level1_json
        string level1_cid
        text original_content
        string original_schema
        string original_media_type
        string original_cid
    }

    NOID_COUNTERS {
        string namespace_key PK
        bigint next_value
    }

    WORKER_RUNTIME_STATUS {
        string worker_name PK
        string status
        datetime last_heartbeat_at
        int total_processed
        int total_succeeded
        int total_failed
    }
```

## Quick Start

### Inside `dark-developer`

```bash
cd /Users/lmatas/source/dark-developer
source venv/bin/activate
pip install -r components/services/dark-core-minter-api/requirements.txt
pip install -e components/core/dark-core-lib
pip install -e components/services/dark-core-minter-api
```

### Standalone Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ../dark-core-lib
```

### Run API and Worker Manually

```bash
# Run schema migrations explicitly before starting API/workers
python -m app.database migrate

# API
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

# Metadata worker
python -m app.main_worker metadata

# Chain worker
python -m app.main_worker chain

# Legacy chain-worker status helper
dark-core-worker-status
```

## Docker Deployment in the Monorepo

The recommended local stack uses separate compose projects:
- blockchain in `components/blockchain/dark-env`
- minter in `components/services/dark-core-minter-api`

```mermaid
flowchart LR
    ENV["dark-env compose"] --> NET["dark-net"]
    MINTER["minter compose"] --> NET
    MINTER --> PG["postgres"]
    MINTER --> API["minter-api"]
    MINTER --> MW["minter-metadata-worker"]
    MINTER --> CW["minter-chain-worker"]
```

### Start Order

```bash
cd /Users/lmatas/source/dark-developer/components/blockchain/dark-env
docker compose up -d

cd /Users/lmatas/source/dark-developer/components/services/dark-core-minter-api
docker compose run --rm minter-api migrate
docker compose up -d --build
```

### Docker Notes

- `minter-api`, `minter-metadata-worker`, and `minter-chain-worker` join the external `dark-net` network.
- `DARK_RPC_URL` comes from `.env.integration` as generated by dark-deployer: `http://rpc01:8545` when blockchain runs on this same `dark-net` (co-located install), or the real external RPC address for a decoupled/remote blockchain tier.
- PostgreSQL runs as a sibling service in the same compose project.
- `minter-api`, `minter-metadata-worker`, and `minter-chain-worker` share PostgreSQL.
- `minter-api` and `minter-metadata-worker` share the `metadata-storage` Docker volume mounted at `/app/metadata_storage` when `METADATA_STORAGE_TYPE=filesystem`.
- `.env.integration` is preferred automatically when present.
- Alembic migrations are explicit: run `docker compose run --rm minter-api migrate` after schema changes or a fresh database.
- `minter-api` defaults to two Uvicorn workers. `MINTER_API_WORKERS` only affects the API process; it does not create extra metadata or chain worker processes.
- The API starts without requiring RPC. The chain worker pauses as `PAUSED_RPC_UNAVAILABLE` while RPC is unavailable and resumes after recovery.

## Metadata Backends

### Filesystem

Use this only for local development and simple fallback scenarios.

```env
METADATA_STORAGE_TYPE=filesystem
METADATA_STORAGE_PATH=./metadata_storage
```

### `dark-store-api`

Use this when you want metadata persistence delegated to a dedicated external service.

```env
METADATA_STORAGE_TYPE=store_api
METADATA_STORE_API_URL=http://localhost:8003
METADATA_STORE_API_TIMEOUT_SECONDS=10.0
```

In that mode:
- the API still stores Level-1 and Level-2 payloads in PostgreSQL first
- the metadata worker sends raw Level-2 bytes and Level-1 JSON to `dark-store-api`
- the returned CIDs are persisted locally, local payloads are purged, and the chain worker publishes `level1_cid` on-chain
- the resolver must point at the same `dark-store-api` instance to resolve `?info` and `?metadata`
- `L1.original_metadata` carries the Level-2 `schema`, `media_type`, and internal `cid`

## Configuration

The app prefers `.env.integration` over `.env`.

### Core Runtime

| Variable | Description | Default |
|----------|-------------|---------|
| `MINTER_API_HOST` | API bind host | `0.0.0.0` |
| `MINTER_API_PORT` | API bind port | `8001` |
| `MINTER_API_WORKERS` | Uvicorn worker processes for `minter-api` only | `2` |
| `BATCH_SIZE_LIMIT` | Max items per batch reserve request | `100` |
| `METADATA_WORKER_ENABLED` | Enable metadata persistence worker | `true` |
| `METADATA_WORKER_PAGE_SIZE` | ARKs per metadata cycle | `100` |
| `METADATA_WORKER_SLEEP_SECONDS` | Metadata sleep after an empty or partial page | `2` |
| `METADATA_WORKER_STORAGE_RETRY_SECONDS` | Sleep while metadata worker is paused for unavailable storage | `10` |
| `METADATA_WORKER_MAX_RETRIES` | Metadata retry attempts before permanent failure | `5` |
| `METADATA_WORKER_RUNTIME_NAME` | Metadata worker identity | `metadata-publisher` |
| `CHAIN_WORKER_ENABLED` | Enable blockchain publication worker | `true` |
| `CHAIN_WORKER_PAGE_SIZE` | ARKs per chain cycle and max in-flight core-lib transaction window | `20` |
| `CHAIN_WORKER_SLEEP_SECONDS` | Chain sleep after an empty or partial page | `5` |
| `CHAIN_WORKER_RPC_RETRY_SECONDS` | Sleep while chain worker is paused for unavailable RPC | `10` |
| `CHAIN_WORKER_CONGESTION_RETRY_SECONDS` | Sleep while chain worker is paused for stalled/congested chain conditions | `30` |
| `CHAIN_WORKER_BLOCK_STALL_SECONDS` | Seconds without block progress before pausing chain worker | `120` |
| `CHAIN_WORKER_ADAPTIVE_PAGE_ENABLED` | Enable core-lib chain-capacity based effective page size | `true` |
| `CHAIN_WORKER_MIN_PAGE_SIZE` | Minimum effective chain page size during adaptive throttling | `1` |
| `CHAIN_WORKER_RECOVERY_SUCCESS_CYCLES` | Healthy capacity cycles before increasing effective page size | `3` |
| `CHAIN_WORKER_MAX_RETRIES` | Chain retry attempts before permanent failure | `5` |
| `CHAIN_WORKER_RUNTIME_NAME` | Chain worker identity | `chain-publisher` |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | DB heartbeat interval for both workers | `10` |
| `WORKER_HEARTBEAT_STALE_AFTER_SECONDS` | Stale heartbeat threshold | `180` |
| `PERMANENT_RESCUE_MAX_ITEMS` | Max ARKs rescued in one filtered rescue request | `50` |

Legacy `WORKER_*` variables are still accepted as aliases for `CHAIN_WORKER_*`.

Backward compatibility aliases still accepted:
- `CORE_API_HOST`
- `CORE_API_PORT`

### Blockchain

| Variable | Description |
|----------|-------------|
| `DARK_RPC_URL` | RPC endpoint |
| `DARK_RPC_HEALTH_TIMEOUT_SECONDS` | Timeout for lightweight RPC health checks |
| `DARK_RPC_CONNECT_RETRY_SECONDS` | Max retry window when initializing dark-core-lib |
| `DARK_RPC_CONNECT_RETRY_INTERVAL_SECONDS` | Delay between dark-core-lib connection retries |
| `DARK_CHAIN_ID` | Chain id |
| `DARK_GAS_LIMIT` | Normal chain worker gas limit; rescue uses double this value |
| `DARK_AUTHORITY_ADDRESS` | Authority contract address |
| `DARK_CONTRACT_ADDRESS` | dARK contract address |
| `DARK_ADMIN_PRIVATE_KEY` | Admin signer private key |

These values are required for blockchain operations. DB-only API endpoints and worker status can remain available when RPC is temporarily down.

### Database

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://dark:dark_password@localhost:5433/minter` |
| `DATABASE_ECHO` | SQL echo logging | `false` |
| `DATABASE_POOL_SIZE` | Pool size | `5` |
| `DATABASE_MAX_OVERFLOW` | Overflow connections | `10` |

### Metadata Storage

| Variable | Description | Default |
|----------|-------------|---------|
| `METADATA_STORAGE_TYPE` | `filesystem` or `store_api` | `store_api` |
| `METADATA_STORAGE_PATH` | Local storage path | `./metadata_storage` |
| `METADATA_STORE_API_URL` | External store base URL | `http://localhost:8003` |
| `METADATA_STORE_API_TIMEOUT_SECONDS` | Store API timeout | `10.0` |

### NOID and Identity

| Variable | Description | Default |
|----------|-------------|---------|
| `MINTER_SHOULDER` | Prefix for generated names | `""` |
| `MINTER_NOID_LENGTH` | Generated-part length | `7` |
| `MINTER_NOID_CHECKDIGIT` | Append and validate checkdigit | `true` |
| `AUTH_CACHE_TTL` | NAAN authorization cache TTL | `60` |
| `AUTH_CACHE_MAXSIZE` | Authorization cache max entries | `1000` |

### TLS and mTLS

| Variable | Description | Default |
|----------|-------------|---------|
| `MTLS_ENABLED` | Enable mTLS gate | `false` |
| `TLS_CERT_FILE` | Server certificate path | - |
| `TLS_KEY_FILE` | Server private key path | - |
| `TLS_CA_FILE` | CA certificate path | - |

## Useful URLs

Once the service is running:
- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`
- Health: `http://localhost:8001/health`
- Worker status: `http://localhost:8001/api/v1/worker/status`

## Notebooks

The notebooks directory contains both focused notebooks and a simpler consolidated integration notebook.

- [notebooks/minter_api_test.ipynb](./notebooks/minter_api_test.ipynb)
  - recommended single entry notebook for end-to-end testing
- [notebooks/minter_01_smoke_authority.ipynb](./notebooks/minter_01_smoke_authority.ipynb)
- [notebooks/minter_02_reserve_validation.ipynb](./notebooks/minter_02_reserve_validation.ipynb)
- [notebooks/minter_03_update_metadata_formats.ipynb](./notebooks/minter_03_update_metadata_formats.ipynb)
- [notebooks/minter_04_tombstone_missing.ipynb](./notebooks/minter_04_tombstone_missing.ipynb)
- [notebooks/minter_05_batch_concurrency.ipynb](./notebooks/minter_05_batch_concurrency.ipynb)
- [notebooks/minter_06_worker_publish_update.ipynb](./notebooks/minter_06_worker_publish_update.ipynb)
- [notebooks/minter_07_chain_import_optional.ipynb](./notebooks/minter_07_chain_import_optional.ipynb)
- [notebooks/minter_08_resolver_end_to_end.ipynb](./notebooks/minter_08_resolver_end_to_end.ipynb)
  - simple direct-HTTP flow from authority creation to resolver lookup
- `/Users/lmatas/source/dark-developer/notebooks/dark_e2e_authority_to_resolver.ipynb`
  - root-level copy of the same simple end-to-end notebook for monorepo use

See [notebooks/README.md](./notebooks/README.md) for execution notes and environment variables.

## Troubleshooting

### ARKs remain in `DRAFT` or `UPDATE`

Check, in this order:
1. both worker containers are running
2. `GET /api/v1/worker/status` shows `workers.metadata.alive=true` and `workers.chain.alive=true`
3. blockchain connectivity is healthy
4. storage backend is reachable and writable
5. the ARK has not reached permanent failure state

### Worker refuses to start

Likely causes:
- another worker still holds the PostgreSQL advisory lock
- a stale pidfile exists
- the database is unreachable
- blockchain configuration is incomplete

### Requests are rejected with `401` or `403`

Check:
- `X-Authority-Id` or `X-Authority-UUID` is present in local dev mode
- the header identity matches the request body `authority_id`
- the authority is authorized for the requested NAAN
- for updates imported from chain, the authority wallet matches the on-chain owner

### Metadata is not visible where expected

Remember the split responsibility:
- API stores payloads in PostgreSQL first
- worker stores immutable payloads in the configured backend later
- on filesystem backend, API and worker must share the same storage volume or directory

## Related Docs

- [noid.md](./noid.md)
- [minter-architecture.md](./minter-architecture.md)
- [notebooks/README.md](./notebooks/README.md)
