# dARK Core Minter API

REST API and background worker for ARK reservation, metadata staging, and on-chain publication using `dark-core-lib`.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)

## Overview

`dark-core-minter-api` is the service responsible for the minting lifecycle of ARKs in the dARK stack. It does not publish directly on every write request. Instead, it splits the lifecycle into two phases:

1. synchronous API work
   - reserve deterministic ARKs locally
   - validate and store metadata in PostgreSQL
   - move ARKs into `DRAFT` or `UPDATE`
2. asynchronous worker work
   - persist Level-2 and Level-1 metadata to the configured storage backend
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

## Runtime Architecture

If you want the implementation-oriented view behind this diagram, continue with [minter-architecture.md](./minter-architecture.md).

```mermaid
flowchart LR
    C["Client or Notebook"] --> API["Minter API (FastAPI)"]
    API --> DB["PostgreSQL"]
    API --> CORE["dark-core-lib"]
    CORE --> CHAIN["Blockchain RPC + Contracts"]

    W["Standalone Worker"] --> DB
    W --> STORE["Metadata Storage via dark-core-lib\nfilesystem or dark-store-api"]
    W --> CORE

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
- Worker process
  - claims `DRAFT` and `UPDATE` records in batches
  - persists metadata through the shared `dark_core_lib.metadata` layer
  - publishes create/update transactions through `dark-core-lib`
  - handles retries, backoff, and permanent failures
- PostgreSQL
  - source of truth for local lifecycle state
  - stores reserved IDs, metadata, retry status, and worker heartbeat
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
    participant Worker as "Worker"
    participant Store as "Metadata Storage"
    participant Core as "dark-core-lib"
    participant Chain as "Blockchain"

    Client->>API: POST /api/v1/arks
    API->>DB: reserve ARK locally (state=RESERVED)
    API-->>Client: 201 ark:NAAN/name

    Client->>API: PUT /api/v1/arks/{ark}
    API->>DB: store L1 + L2, move to DRAFT/UPDATE
    API-->>Client: 200 pending publication

    Worker->>DB: claim pending ARKs
    Worker->>Store: store Level-2 metadata
    Worker->>Store: store Level-1 metadata with embedded Level-2 CID
    Worker->>Core: create_ark or update_ark
    Core->>Chain: signed transaction
    Worker->>DB: mark ARK as PUBLISHED
```

## ARK Lifecycle

### State Machine

```mermaid
stateDiagram-v2
    [*] --> RESERVED: POST /api/v1/arks
    RESERVED --> DRAFT: PUT /api/v1/arks/{ark}
    DRAFT --> DRAFT: overwrite pending create
    DRAFT --> PUBLISHED: worker create_ark
    PUBLISHED --> UPDATE: PUT /api/v1/arks/{ark}
    UPDATE --> UPDATE: overwrite pending update
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

`PUT /api/v1/arks/{ark}` does not publish immediately. It stores metadata locally and prepares the record for the worker.

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
    API-->>Client: 200 pending worker publication
```

Important points:
- `minimal_metadata` is the validated Level-1 JSON payload.
- `original_metadata` is the raw Level-2 payload.
- For `RESERVED -> DRAFT`, the `target` URL is required.
- If an ARK exists on-chain but not locally, the minter can import it into the local DB before staging an update.
- The import path now validates that the blockchain owner matches the authority making the request.

### 3. Async Publish

The worker is the only component that writes metadata to the backend and publishes blockchain transactions.

```mermaid
sequenceDiagram
    participant Worker as "ARKPublisher"
    participant Repo as "ARKRepository"
    participant DB as "PostgreSQL"
    participant Store as "Metadata Storage"
    participant Core as "dark-core-lib"
    participant Chain as "Blockchain"

    Worker->>Repo: get_drafts_pending_publish(limit)
    Repo->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    Repo->>DB: mark publish_last_attempt_at
    Worker->>DB: commit claim

    loop each claimed ARK
        Worker->>Store: store Level-2 content
        Store-->>Worker: level2_cid
        Worker->>Store: store Level-1 JSON with level2_cid embedded
        Store-->>Worker: level1_cid
        Worker->>Repo: persist CIDs in DB
        alt state is DRAFT
            Worker->>Core: create_ark(..., cid=level1_cid)
        else state is UPDATE
            Worker->>Core: update_ark(..., cid=level1_cid)
        end
        Core->>Chain: signed tx
        Worker->>Repo: update_to_published
        Worker->>DB: commit
    end
```

Important points:
- storage and blockchain publication happen in the worker, not in the request thread.
- Level-2 is stored first.
- Level-1 is then re-serialized with the embedded Level-2 CID.
- the blockchain stores the Level-1 CID as the canonical pointer.
- the shared implementation of this pipeline now lives in `dark_core_lib.metadata.MetadataService`.

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
    L1["Level-1 minimal_metadata\nvalidated JSON"] --> EMBED["inject original_metadata.cid"]
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
- PostgreSQL stores both payloads before publication.
- The storage backend stores the immutable content addressed versions.
- The storage backend interface and Level-1 schema are shared with the resolver through `dark-core-lib`.

## Worker Model and Concurrency

### Singleton Strategy

```mermaid
flowchart TD
    S["worker start"] --> P["pidfile guard"]
    P --> L["PostgreSQL advisory lock"]
    L -->|lock acquired| R["scheduler loop"]
    L -->|lock denied| X["abort startup"]
    R --> H["persist heartbeat in worker_runtime_status"]
    H --> API["/api/v1/worker/status"]
```

The worker uses two protections:
- local pidfile guard
- PostgreSQL advisory lock derived from `WORKER_RUNTIME_NAME`

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

## API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/arks` | `POST` | Reserve one ARK |
| `/api/v1/arks/batch` | `POST` | Reserve multiple ARKs |
| `/api/v1/arks/{ark}` | `GET` | Resolve ARK state and metadata |
| `/api/v1/arks/{ark}` | `PUT` | Stage metadata and move to `DRAFT` or `UPDATE` |
| `/api/v1/arks/{ark}` | `DELETE` | Tombstone an ARK locally |
| `/api/v1/authority/{uuid}` | `GET` | Fetch authority details |
| `/api/v1/authority/{uuid}/naans` | `GET` | List authorized NAANs |
| `/api/v1/authority/{uuid}/authorized/{naan}` | `GET` | Check NAAN authorization |
| `/api/v1/worker/status` | `GET` | Read worker heartbeat and counters |
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
# API
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

# Worker
python -m app.main_worker run

# Worker status helper
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
    MINTER --> W["minter-worker"]
```

### Start Order

```bash
cd /Users/lmatas/source/dark-developer/components/blockchain/dark-env
docker compose up -d

cd /Users/lmatas/source/dark-developer/components/services/dark-core-minter-api
docker compose up -d --build
```

### Docker Notes

- `minter-api` and `minter-worker` join the external `dark-net` network.
- inside Docker, `DARK_RPC_URL` is overridden to `http://rpc01:8545`.
- PostgreSQL runs as a sibling service in the same compose project.
- `minter-api` and `minter-worker` share the `metadata-storage` Docker volume mounted at `/app/metadata_storage` when `METADATA_STORAGE_TYPE=filesystem`.
- `.env.integration` is preferred automatically when present.

## Metadata Backends

### Filesystem

Use this for local development and simple integration tests.

```env
METADATA_STORAGE_TYPE=filesystem
METADATA_STORAGE_PATH=./metadata_storage
```

### `dark-store-api`

Use this when you want metadata persistence delegated to a dedicated external service.

```env
METADATA_STORAGE_TYPE=store_api
METADATA_STORE_API_URL=http://localhost:8002
METADATA_STORE_API_TIMEOUT_SECONDS=10.0
```

In that mode:
- the API still stores Level-1 and Level-2 payloads in PostgreSQL first
- the worker sends the payloads to `dark-store-api`
- the returned CIDs are persisted locally and then published on-chain
- the resolver must point at the same `dark-store-api` instance to resolve `?info` and `?metadata`

## Configuration

The app prefers `.env.integration` over `.env`.

### Core Runtime

| Variable | Description | Default |
|----------|-------------|---------|
| `MINTER_API_HOST` | API bind host | `0.0.0.0` |
| `MINTER_API_PORT` | API bind port | `8001` |
| `BATCH_SIZE_LIMIT` | Max items per batch reserve request | `100` |
| `WORKER_ENABLED` | Enable standalone worker | `true` |
| `WORKER_INTERVAL_SECONDS` | Publish cycle interval | `60` |
| `WORKER_BATCH_SIZE` | ARKs per worker cycle | `10` |
| `WORKER_MAX_RETRIES` | Retry attempts before permanent failure | `5` |
| `WORKER_RUNTIME_NAME` | Logical worker identity | `ark-publisher` |

Backward compatibility aliases still accepted:
- `CORE_API_HOST`
- `CORE_API_PORT`

### Blockchain

| Variable | Description |
|----------|-------------|
| `DARK_RPC_URL` | RPC endpoint |
| `DARK_CHAIN_ID` | Chain id |
| `DARK_AUTHORITY_ADDRESS` | Authority contract address |
| `DARK_CONTRACT_ADDRESS` | dARK contract address |
| `DARK_ADMIN_PRIVATE_KEY` | Admin signer private key |

These values are required for a functional blockchain-connected deployment.

### Database

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://dark:dark_password@localhost:5432/minter` |
| `DATABASE_ECHO` | SQL echo logging | `false` |
| `DATABASE_POOL_SIZE` | Pool size | `5` |
| `DATABASE_MAX_OVERFLOW` | Overflow connections | `10` |

### Metadata Storage

| Variable | Description | Default |
|----------|-------------|---------|
| `METADATA_STORAGE_TYPE` | `filesystem` or `store_api` | `filesystem` |
| `METADATA_STORAGE_PATH` | Local storage path | `./metadata_storage` |
| `METADATA_STORE_API_URL` | External store base URL | `http://localhost:8002` |
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
1. worker process or container is running
2. `GET /api/v1/worker/status` shows a fresh heartbeat
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
