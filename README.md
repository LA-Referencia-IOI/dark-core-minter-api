# dARK Core Minter API

REST API and background workers for ARK reservation, metadata staging,
metadata persistence, IPFS replication reconciliation, and on-chain
publication using `dark-core-lib`.

[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)

## Overview

`dark-core-minter-api` is the service responsible for the minting lifecycle of ARKs in the dARK stack. It does not publish directly on every write request. Instead, it splits the lifecycle into two phases:

1. synchronous API work
   - reserve deterministic ARKs locally
   - validate and store metadata in PostgreSQL
   - move ARKs into `DRAFT` or `UPDATE`
2. asynchronous worker work
   - persist Level-2 and Level-1 metadata to the configured storage backend
   - confirm one real L1/L2 pin before publication; request final durability only after the critical queue is clear
   - publish `create_ark` or `update_ark` on-chain through `dark-core-lib`
   - finalize the ARK as `PUBLISHED`

Normal Cluster states (`queued`, `pinning`, initial visibility) are scheduled
with explicit bounded backoff through `next_action_at`; they are not errors.
The deployer-level operational policy is archived in the dark-deployer repo:
`docs/old/publication-latency-control.md` (archived).

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

## Current storage and worker sequence

Metadata persistence stores L1/L2 payloads and obtains their CIDs through Store
API. A successful `store` means that Cluster accepted the content; it does not
mean that a peer has completed the pin. The replication worker observes both
CIDs in batches and keeps the ARK in `AVAILABILITY` while either first pin is
still `queued`, `pinning`, or not yet visible.

When both CIDs have at least `REPLICATION_PUBLISH_AFTER_REPLICAS` confirmed
`pinned` replicas, the worker moves the ARK to `CHAIN`. After Chain publishes,
the replication worker continues observing until both CIDs reach
`REPLICATION_TARGET_REPLICAS`; only then are local payloads purged.

The current selector uses pages of 100 by default and batches up to 200 unique
CIDs. First-pin work has priority. A persisted per-ARK schedule avoids polling
Cluster continuously: first pins use 15 s, 1 min and then 5 min; final
durability uses 5 min, 15 min and then hourly.

The current two-phase policy requests one initial Cluster allocation. After
publication, the replication worker promotes existing pins to the configured
target only when SQL shows no metadata persistence backlog and no pending
first-pin availability work. This promotion is asynchronous; the worker then
observes the pins and purges only after both CIDs reach the target.

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

    RW["Replication Worker"] --> DB
    RW --> STORE

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
  - records one initial replica for each CID and keeps `level1_json` and `original_content`
- Replication reconciliation worker process
  - selects retained payloads only after both CIDs exist
  - checks each CID periodically through the metadata storage backend
  - repairs zero-copy CIDs and purges only when both L1 and L2 meet Store API's target
  - retries recoverable replication failures indefinitely
  - keeps an independent DB heartbeat and singleton lock
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
    participant ReplicationWorker as "Replication Worker"
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
    MetadataWorker->>DB: persist CIDs and retain local payloads

    ChainWorker->>DB: claim ARKs with complete CIDs
    ChainWorker->>Core: create_ark or update_ark
    Core->>Chain: signed transaction
    ChainWorker->>DB: mark ARK as PUBLISHED

    ReplicationWorker->>Store: query live L1/L2 replication
    ReplicationWorker->>DB: update counts; purge only when both targets are met
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

The asynchronous path is split into three independent workers. The metadata
worker persists staged metadata, the replication worker verifies and repairs
the resulting CIDs, and the chain worker is the only component that publishes
blockchain transactions. A Store API `pin_queued` or `pinning` result is a
normal wait, not an error and not a repair trigger.

```mermaid
sequenceDiagram
    participant MetadataWorker as "MetadataPersistenceWorker"
    participant ReplicationWorker as "ReplicationReconciliationWorker"
    participant ChainWorker as "ChainPublisherWorker"
    participant Repo as "ARKRepository"
    participant DB as "PostgreSQL"
    participant Store as "Metadata Storage"
    participant Core as "dark-core-lib"
    participant Chain as "Blockchain"

    MetadataWorker->>Repo: get_metadata_pending_persist(limit)
    Worker->>DB: acquire advisory lock for the ARK
    Repo->>DB: select METADATA/READY work (or due waits) and acquire one advisory lock per ARK
    MetadataWorker->>DB: commit claim

    loop each claimed ARK
        MetadataWorker->>Store: store Level-2 content
        Store-->>MetadataWorker: original_cid
        MetadataWorker->>Store: store Level-1 JSON with schema + media_type + original_cid
        Store-->>MetadataWorker: level1_cid
        MetadataWorker->>Repo: persist CIDs, retain payloads, advance to AVAILABILITY/READY
    end

    ReplicationWorker->>Repo: get_metadata_pending_reconciliation(limit, recheck)
    loop each AVAILABILITY or REPLICATION item with both CIDs
        ReplicationWorker->>Store: POST /v1/status/batch for L1 and L2
        opt a CID has zero copies
            ReplicationWorker->>Store: restore retained payload
            ReplicationWorker->>ReplicationWorker: require regenerated CID to match
        end
        ReplicationWorker->>Repo: lock row and compare current CIDs
        alt both counts reach publish_after_replicas
            ReplicationWorker->>Repo: advance to CHAIN/READY
        else published and both counts reach target_replicas
            ReplicationWorker->>Repo: purge payload and mark COMPLETE/DONE
        end
    end

    ChainWorker->>Repo: get_chain_pending_publish(limit)
    Repo->>DB: SELECT only CHAIN/READY rows whose availability was verified
    Repo->>DB: select CHAIN work and acquire one advisory lock per ARK
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
- after both CIDs are stored, local `level1_json` and `original_content` remain
  in PostgreSQL until the replication worker verifies both replication targets.
- the blockchain stores the Level-1 CID as the canonical pointer.
- Internal worker state is stored as compact numeric `processing_stage`,
  `processing_status` and error codes. The public ARK state remains `R/D/U/P/T`.
- `publish_after_replicas` controls when a verified L1/L2 pair may reach the
  chain worker; `target_replicas` controls only payload retention.
- the shared implementation of this pipeline now lives in `dark_core_lib.metadata.MetadataService`.
- the metadata and replication workers check metadata storage readiness before
  each cycle. If Store API/IPFS is unavailable, they pause as
  `PAUSED_STORAGE_UNAVAILABLE` without claiming or modifying ARKs.
- the chain worker calls `dark-core-lib` through `publish_ark_operations(...)`, grouping ARKs by `authority_id` and sending individual create/update transactions with sequential pending nonces.
- `CHAIN_WORKER_PAGE_SIZE` is the nominal maximum chain page size; the default is `100`. Before each cycle, the worker asks `dark-core-lib` for `get_chain_capacity(...)` and may temporarily use a smaller effective page size for both DB claim and the core-lib pipeline window.
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
- `POST /api/v1/arks/status/batch` is the versioned (`version: "v1"`) mTLS-protected reconciliation read API. It accepts `{"arks": ["ark:..."]}` with 1--100 values and returns ordered `results`, each with the requested `ark` and either `status` (the GET representation) or `{ "error": { "code", "message", "retryable" } }`. Invalid, missing, and blockchain-failed values do not abort sibling results. It is distinct from `POST /api/v1/arks/batch`, which reserves identifiers.
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
    C["Pending publish work"] --> D["PostgreSQL advisory lock per ARK"]
    E["Finalize publish"] --> F["CAS transition to PUBLISHED"]
    G["Retry tracking"] --> H["DB-backed retry count + backoff"]
```

This gives us:
- deterministic reservation without duplicate names in normal PostgreSQL deployment
- worker-safe claim of pending ARKs
- compare-and-set style final state transitions
- retry tracking that survives restarts

### Worker Status

`GET /api/v1/worker/status` returns a low-cost liveness summary by default. It
reads the three worker rows in `worker_runtime_status` with one query; it does not call RPC,
Store API, count ARKs, scan queues, or aggregate metadata:

```json
{
  "overall": "ok",
  "message": "Metadata worker is working; Replication worker is working; Chain worker is working.",
  "source": "db_heartbeat",
  "workers": {
    "metadata": {
      "state": "working",
      "alive": true,
      "last_heartbeat_seconds": 9,
      "last_cycle": {
        "observed": 100,
        "advanced": 100,
        "waiting": 0,
        "repaired": 0,
        "failed": 0,
        "duration_seconds": 10.573
      }
    }
  }
}
```

In the compact payload, `last_cycle.observed` means records inspected in the
latest cycle. `advanced` means records moved to the next stage, `waiting` means
records deliberately scheduled for a later observation, and `repaired` means
payloads submitted again after a confirmed absence/error. These are cycle
counters, not queue sizes or lifetime totals. The heartbeat stores the actual
`wake_at`; the API does not infer sleeping from the previous cycle.

Use `GET /api/v1/worker/status?detail=full` for RPC and storage probes, bounded
workload aggregates, permanent-error aggregates and the last cycle. Full mode is
intended for diagnosis, not frequent monitoring. It reports process state and
workload separately: `READY` is work that can be attempted now, while `WAITING`
is a normal scheduled observation with a concrete `next_action_at` and reason.
The full response includes `no_progress_cycles` and `stalled_suspected` when a
worker has attempted work repeatedly without advancing any record.

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

Replication heartbeats also expose four bounded last-cycle aggregates:
`promotions_accepted`, `confirmed_cids`, `pending_cids`, and
`batch_latency_ms`. Technical per-cycle deferrals are named
`transient_deferred`; they are not permanent ARK errors. Promotion pressure is
derived from the already-fetched Cluster batch and can reduce the durability
budget from 100 to 50 or 20 without affecting first-pin priority.

API mutations and worker attempts acquire the same PostgreSQL advisory lock per ARK before reading the record. A physical connection remains checked out until unlock, across commits and rollbacks; a lost connection cannot reconnect and write an unlocked result. Workers recheck stage, state and retry eligibility after acquiring the lock. Metadata updates, tombstones and manual rescue return `409 Conflict` when an ARK is busy. Updates require `PUBLISHED`; initial metadata submission still allows `RESERVED → DRAFT`. No revision counter or lease is stored: content cannot change during a worker attempt. PostgreSQL releases the lock when its connection terminates. Clients should retry a busy response later; pending `DRAFT`/`UPDATE` records must finish their workflow before accepting another metadata update.

The detailed `config.chain.worker_page_size` field shows the active chain transaction pipeline window because chain page size and core-lib pipeline size are intentionally the same value. Cycle counters describe only the latest observation round, not lifetime totals or queue size.

### Worker errors and explicit retry

`GET /api/v1/worker/errors` returns only permanent failures: records which
require an operator decision. Normal storage/RPC backoff and IPFS propagation
are `WAITING` workflow actions and never appear as errors. Results are paginated
and can be filtered by `stage`, `authority_id`, and `error_code`.

`POST /api/v1/worker/retry` is an mTLS-protected administrative retry. It returns an explicitly approved `FAILED` ARK to `READY` in its current internal stage; the owning worker performs the retry.

## API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/arks` | `POST` | Reserve one ARK |
| `/api/v1/arks/batch` | `POST` | Reserve multiple ARKs |
| `/api/v1/arks/status/batch` | `POST` | mTLS-protected batch status lookup for up to 100 ARKs; per-item results/errors |
| `/api/v1/arks/{ark}` | `GET` | Read local ARK state plus metadata/CIDs, with best-effort storage fallback |
| `/api/v1/arks/{ark}` | `PUT` | Stage metadata and move to `DRAFT` or `UPDATE` |
| `/api/v1/arks/{ark}` | `DELETE` | Tombstone an ARK locally |
| `/api/v1/authority/{uuid}` | `GET` | Fetch authority details |
| `/api/v1/authority/{uuid}/naans` | `GET` | List authorized NAANs |
| `/api/v1/authority/{uuid}/authorized/{naan}` | `GET` | Check NAAN authorization |
| `/api/v1/worker/status` | `GET` | Read low-cost worker liveness from DB heartbeat; use `?detail=full` for diagnostics |
| `/api/v1/worker/errors` | `GET` | List permanent errors, with bounded summary and filters |
| `/api/v1/worker/retry` | `POST` | Approve an explicit failed ARK retry by its owning worker |
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
        smallint processing_stage
        smallint processing_status
        smallint processing_attempt_count
        smallint processing_error_code
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

### Inside `dark-deployer`

```bash
cd /Users/lmatas/source/dark-deployer
source venv/bin/activate
pip install -r components/dark-core-minter-api/requirements.txt
pip install -e components/dark-core-lib
pip install -e components/dark-core-minter-api
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

# Replication reconciliation worker
python -m app.main_worker replication

# Chain worker
python -m app.main_worker chain

# Legacy chain-worker status helper
dark-core-worker-status
```

## Deployment

dark-core-minter-api is deployed by the [dark-deployer](../../README.md) using the deployment v3 pipeline: an operator inventory (see [examples/operator-inventory/](../../examples/operator-inventory/)) is resolved, planned, and rendered into per-machine Docker Compose bundles, then applied with `./deploy.sh install` (locally) or `prepare`/`push`/`apply` (remote hosts). The runtime image version and the component checkout branch are pinned in `deployment_v3/catalog_data/dark-platform-baseline-v1.0.json`. There is no standalone docker-compose file in this component.

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
- the returned CIDs are persisted locally; the replication worker purges local
  payloads only after both CIDs meet the storage replication target, while the
  chain worker can publish `level1_cid` independently
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
| `METADATA_WORKER_CONCURRENCY` | Concurrent ARKs; each keeps L2 before L1 | `4` |
| `METADATA_WORKER_SLEEP_SECONDS` | Metadata sleep after an empty or partial page | `2` |
| `METADATA_WORKER_STORAGE_RETRY_SECONDS` | Sleep while metadata worker is paused for unavailable storage | `10` |
| `METADATA_WORKER_MAX_RETRIES` | Metadata retry attempts before permanent failure | `5` |
| `METADATA_WORKER_RUNTIME_NAME` | Metadata worker identity | `metadata-publisher` |
| `REPLICATION_WORKER_ENABLED` | Enable IPFS replication reconciliation worker | `true` |
| `REPLICATION_WORKER_PAGE_SIZE` | Retained metadata records checked per cycle | `100` |
| `REPLICATION_WORKER_CONCURRENCY` | Concurrent replication checks/repairs | `2` |
| `REPLICATION_WORKER_SLEEP_SECONDS` | Legacy worker sleep setting; normal queue observations use the global idle interval | `30` |
| `REPLICATION_STATUS_BATCH_SIZE` | Maximum unique CIDs per Store API status batch | `200` |
| `REPLICATION_IDLE_SLEEP_SECONDS` | Global sleep when no due replication work exists | `2` |
| `REPLICATION_WORKER_STORAGE_RETRY_SECONDS` | Pause while metadata storage is unavailable | `10` |
| `REPLICATION_WORKER_RUNTIME_NAME` | Reconciler heartbeat, PID and advisory-lock identity | `replication-reconciler` |
| `REPLICATION_PUBLISH_AFTER_REPLICAS` | Minimum real pins required before the record is publishable | `1` |
| `REPLICATION_TARGET_REPLICAS` | Desired global pin count before local payload purge | `2` |
| `CHAIN_WORKER_ENABLED` | Enable blockchain publication worker | `true` |
| `CHAIN_WORKER_PAGE_SIZE` | ARKs per chain cycle and max in-flight core-lib transaction window | `100` |
| `CHAIN_WORKER_SLEEP_SECONDS` | Chain sleep after an empty or partial page | `5` |
| `CHAIN_WORKER_RPC_RETRY_SECONDS` | Sleep while chain worker is paused for unavailable RPC | `10` |
| `CHAIN_WORKER_CONGESTION_RETRY_SECONDS` | Sleep while chain worker is paused for stalled/congested chain conditions | `30` |
| `CHAIN_WORKER_BLOCK_STALL_SECONDS` | Seconds without block progress before pausing chain worker | `120` |
| `CHAIN_WORKER_ADAPTIVE_PAGE_ENABLED` | Enable core-lib chain-capacity based effective page size | `true` |
| `CHAIN_WORKER_MIN_PAGE_SIZE` | Minimum effective chain page size during adaptive throttling | `1` |
| `CHAIN_WORKER_HEALTHY_CYCLES_BEFORE_GROWING` | Healthy capacity cycles before increasing effective page size | `3` |
| `CHAIN_WORKER_MAX_RETRIES` | Chain retry attempts before permanent failure | `5` |
| `CHAIN_WORKER_RUNTIME_NAME` | Chain worker identity | `chain-publisher` |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | DB heartbeat interval for all workers | `10` |
| `WORKER_HEARTBEAT_STALE_AFTER_SECONDS` | Stale heartbeat threshold | `180` |

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
| `DARK_GAS_LIMIT` | Normal chain worker gas limit |
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

### Logging

Production logs go to stdout by default. Successful requests, idle worker
cycles, per-ARK reconciliation, and detailed replication summaries are emitted
only at `DEBUG`; repeating pause warnings are rate-limited while the condition
persists. Keep `MINTER_UVICORN_ACCESS_LOG=false` when the edge proxy already
keeps access logs.

| Variable | Description | Default |
|----------|-------------|---------|
| `MINTER_LOG_LEVEL` | Application log threshold (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) | `INFO` |
| `MINTER_LOG_JSON` | Emit structured JSON records to stdout and the optional file | `false` |
| `MINTER_LOG_FILE` | Optional rotating log-file path; unset keeps logs on stdout only | unset |
| `MINTER_LOG_FILE_MAX_BYTES` | Maximum bytes per optional log file before rotation | `10485760` |
| `MINTER_LOG_FILE_BACKUP_COUNT` | Retained rotated log files | `5` |
| `MINTER_LOG_WARNING_REPEAT_SECONDS` | Minimum interval for repeating worker pause warnings | `300` |
| `MINTER_UVICORN_ACCESS_LOG` | Enable Uvicorn per-request access logs | `false` |

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
| `MINTER_SHOULDER` | DARK 2 prefix in format `2xx`; `xx` is lowercase alphanumeric and uniquely identifies the minter per NAAN | `200` |
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
- `notebooks/dark_e2e_authority_to_resolver.ipynb` at the dark-deployer repo root
  - repo-level end-to-end notebook from authority creation to resolver lookup

See [notebooks/README.md](./notebooks/README.md) for execution notes and environment variables.

## Troubleshooting

### ARKs remain in `DRAFT` or `UPDATE`

Check, in this order:
1. the three worker containers are running
2. `GET /api/v1/worker/status` shows `workers.metadata.alive=true`,
   `workers.replication.alive=true`, and `workers.chain.alive=true`
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
