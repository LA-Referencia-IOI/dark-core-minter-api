# dARK Core Minter API

New platform developer: follow [developer onboarding](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/developer-onboarding.md)
for the complete local stack, signed requests and component iteration.

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
   - confirm the configured publication threshold for both L1 and L2 before publication; request final durability only after the critical queue is clear
   - publish `create_ark` or `update_ark` on-chain through `dark-core-lib`
   - finalize the ARK as `PUBLISHED`

Normal Cluster states (`queued`, `pinning`, initial visibility) are scheduled
with explicit bounded backoff through `next_action_at`; they are not errors.
See the [platform architecture](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/architecture.md) for the cross-service flow.

This separation gives us better resilience, simpler retries, and a cleaner operational model.

## Documentation Map

Use the docs in this order, depending on what you need:

- [README.md](README.md) (this file): operational overview, lifecycle, API,
  configuration, deployment and troubleshooting.
- [docs/architecture.md](docs/architecture.md): technical implementation details,
  module map, concurrency model, worker internals, data model.
- [docs/noid.md](docs/noid.md): NOID generation rules, shoulders and check
  character.
- [docs/security.md](docs/security.md): request authentication summary (signed
  Ed25519 mutations); the client integration guide lives in the
  [dark-deployer documentation](https://github.com/LA-Referencia-IOI/dark-deployer/tree/main/docs/minter-auth).
- [notebooks/README.md](notebooks/README.md): notebook index and environment.
- Sphinx sources: [docs/index.rst](docs/index.rst), [docs/api.rst](docs/api.rst).
- Platform context: the dark-deployer
  [architecture](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/architecture.md)
  and [documentation index](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/README.md).

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

Store initially requests one allocation. A publication threshold above one can
therefore leave a new record waiting if no separate mechanism supplies additional
pins: promotion to the durability target runs only after publication. Use a
threshold of one for this workflow until pre-publication promotion is supported
and validated.

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

If you want the implementation-oriented view behind this diagram, continue with [minter-architecture.md](docs/architecture.md).

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
  - claims `DRAFT` and `UPDATE` records in the chain-ready stage after the publication threshold is observed
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

    ReplicationWorker->>Store: observe both CIDs until publication threshold
    ReplicationWorker->>DB: advance to CHAIN when confirmed
    ChainWorker->>DB: claim CHAIN-ready ARKs
    ChainWorker->>Core: create_ark or update_ark
    Core->>Chain: signed transaction
    ChainWorker->>DB: mark ARK as PUBLISHED

    ReplicationWorker->>Store: query live L1/L2 replication
    ReplicationWorker->>DB: update counts and purge when both targets are met
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
    Repo->>DB: select eligible METADATA work

    loop each selected ARK
        MetadataWorker->>DB: acquire advisory lock and recheck eligibility
        MetadataWorker->>Store: store Level-2 content
        Store-->>MetadataWorker: original_cid
        MetadataWorker->>Store: store Level-1 JSON with schema + media_type + original_cid
        Store-->>MetadataWorker: level1_cid
        MetadataWorker->>Repo: persist CIDs, retain payloads, advance to AVAILABILITY/READY
    end

    ReplicationWorker->>Repo: get_reconciliation_candidates(limit, stage)
    loop each AVAILABILITY or REPLICATION item with both CIDs
        ReplicationWorker->>Store: POST /v1/status/batch for L1 and L2
        opt a CID has zero copies
            ReplicationWorker->>Store: restore retained payload
            ReplicationWorker->>ReplicationWorker: require regenerated CID to match
        end
        ReplicationWorker->>DB: acquire advisory lock and recheck eligibility
        ReplicationWorker->>Repo: lock row and compare current CIDs
        alt both counts reach publish_after_replicas
            ReplicationWorker->>Repo: advance to CHAIN/READY
        else published and both counts reach target_replicas
            ReplicationWorker->>Repo: purge payload and mark COMPLETE/DONE
        end
    end

    ChainWorker->>Repo: get_chain_pending_publish(limit)
    Repo->>DB: SELECT only CHAIN/READY rows whose availability was verified
    ChainWorker->>ChainWorker: group selected ARKs by authority_id
    loop each authority group
        ChainWorker->>Core: publish_ark_operations(..., pipeline_size)
        Core->>Chain: send signed txs with sequential pending nonces
        Core->>Chain: wait receipts and return one semantic result per ARK
        ChainWorker->>DB: acquire per-ARK advisory lock to apply each result
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
    D --> E["return null response"]
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
- After metadata persistence succeeds, PostgreSQL retains `level1_json` and `original_content` while replication is pending. The replication worker purges these payload fields only after publication and confirmation of the target replica count for both CIDs.
- `GET /api/v1/arks/{ark}` returns `minimal_metadata` from local DB when available; after purge it best-effort loads Level-1 from the configured storage backend using `level1_cid`.
- If that storage lookup fails, `GET /api/v1/arks/{ark}` still returns state, target, CIDs, and schema without `minimal_metadata`.
- `POST /api/v1/arks/status/batch` is the versioned (`version: "v1"`) reconciliation read API. The Minter router does not require authentication for this read, although a gateway may restrict it. It accepts `{"arks": ["ark:..."]}` with 1--100 values and returns ordered `results`, each with the requested `ark` and either `status` (the GET representation) or `{ "error": { "code", "message", "retryable" } }`. Invalid, missing, and blockchain-failed values do not abort sibling results. It is distinct from `POST /api/v1/arks/batch`, which reserves identifiers.
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

A running worker entry includes `enabled`, `alive`, `process_state`,
`activity`, `heartbeat_age_seconds`, `last_cycle`, `totals`, and, when
applicable, sleep or pause details. The keys under `workers` are `metadata`,
`replication` and `chain`; disabled or missing workers have a smaller entry.
`overall` is `ok`, `degraded` or `down` based on heartbeat state and suspected
stalls. The compact response does not establish application or infrastructure
readiness.

The `last_cycle` fields `observed`, `advanced`, `waiting`, `repaired`, `failed`
and `duration_seconds` are cycle observations, not queue sizes or lifetime
totals. Replication can also report `transient_deferred` and bounded promotion
metrics. A fresh heartbeat records `wake_at`; the API also has a fallback for
runtime rows whose cycle and heartbeat timestamps coincide.

Use `?detail=workload` for bounded grouped SQL, permanent-error aggregates,
backlog age and replication configuration; `?detail=infrastructure` adds RPC
and storage probes; `?detail=full` combines both. The workload and process
views are separate. `no_progress_cycles` and `stalled_suspected` are reported
from the worker heartbeat for a live worker.

Worker heartbeats are emitted by a lightweight supervisor thread, so a full chain batch can take longer than `WORKER_HEARTBEAT_STALE_AFTER_SECONDS` without incorrectly marking the worker stale.

Replication heartbeats also expose four bounded last-cycle aggregates:
`promotions_accepted`, `confirmed_cids`, `pending_cids`, and
`batch_latency_ms`. Technical per-cycle deferrals are named
`transient_deferred`; they are not permanent ARK errors. Promotion pressure is
derived from the already-fetched Cluster batch and can reduce the durability
budget from 100 to 50 or 20 without affecting first-pin priority.

API mutations and metadata and replication attempts use the same PostgreSQL advisory lock per ARK. A physical connection remains checked out until unlock, across commits and rollbacks; PostgreSQL releases the lock when its connection terminates. The metadata and replication workers recheck eligibility under the lock and hold it through their per-ARK work. The chain pipeline selects and submits operations without holding these per-ARK locks, then acquires them to reconcile and apply individual results; the locks therefore do not exclude API activity for the duration of chain submission. Metadata updates, tombstones and manual rescue return `409 Conflict` when an ARK is busy. Updates require `PUBLISHED`; initial metadata submission still allows `RESERVED → DRAFT`. Clients should retry a busy response later; pending `DRAFT`/`UPDATE` records must finish their workflow before accepting another metadata update.

The detailed `config.chain.worker_page_size` field shows the active chain transaction pipeline window because chain page size and core-lib pipeline size are intentionally the same value. Cycle counters describe only the latest observation round, not lifetime totals or queue size.

### Worker errors and explicit retry

`GET /api/v1/worker/errors` returns only permanent failures: records which
require an operator decision. Normal storage/RPC backoff and IPFS propagation
are `WAITING` workflow actions and never appear as errors. Results are paginated
and can be filtered by `stage`, `authority_id`, and `error_code`.

`POST /api/v1/worker/retry` requires the requesting authority's identity and,
when signed authentication is enabled, a DARK2 signature. It returns an
explicitly approved `FAILED` ARK owned by that authority to `READY` in its
current internal stage; the owning worker performs the retry.

## API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/arks` | `POST` | Reserve one ARK |
| `/api/v1/arks/batch` | `POST` | Reserve multiple ARKs |
| `/api/v1/arks/status/batch` | `POST` | Public in the Minter router; batch status lookup for up to 100 ARKs with per-item results/errors |
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

Set `MINTER_SIGNED_AUTH_ENABLED=true` to require Ed25519 request signatures on
ARK mutations and administrative retries. Set it to `false` (default) to use
`X-DARK-Authority-Id` without cryptographic verification. In unsigned mode
the header is a caller-provided identity, not proof of identity; do not expose
this mode to untrusted clients without an independently enforced trusted
access boundary. Both modes retain the applicable authority matching, ARK
ownership, assignment and NAAN checks. Restart the API after changing the
setting. Legacy mTLS settings do not select or enforce either mode.

Queries are public in the Minter router, including `POST /api/v1/arks/status/batch`;
the gateway may impose additional restrictions. Use HTTPS at the gateway and
protect direct access to the upstream API. The signed-auth feature does not
configure server TLS or trust arbitrary forwarded-protocol headers.

Enabling requires the updated Authority contract/ABI, a registered public key,
and migration
`0002_authority_request_nonces`. Missing contract support fails closed. Multiple
replicas of one logical endpoint must share PostgreSQL.

Key cache idle TTL defaults to 60 seconds and is configurable through
`MINTER_CLIENT_KEY_CACHE_IDLE_TTL_SECONDS`. Valid accepted signatures renew it;
invalid requests/replays do not. Cached key and active status remain trusted
while in use, even after an on-chain change. An authenticated new fingerprint
refreshes the entry. Existing business authorization checks remain independent.

Timestamps are Unix seconds, with a configurable 300-second symmetric window
(`MINTER_SIGNED_AUTH_MAX_SKEW_SECONDS`). Nonces are UUIDs consumed in a separate
PostgreSQL transaction before business logic. Duplicates return 409; failures
never return a previous result. Cleanup runs at most once per minute per process
when accepting signed requests. After a lost response, query state before
submitting a new modification.

Use `dark_core_lib.request_signing.sign_request` with an Ed25519 private key,
method, exact path/query, body bytes and authority UUID. Send the exact
signed bytes, not a separately serialized `json=` body. The canonical UTF-8
string is newline-separated, without a final newline:

```text
DARK2
<METHOD>
<RAW_PATH_WITH_QUERY>
<AUTHORITY_ID>
<SHA256_PUBLIC_KEY_HEX>
<UNIX_TIMESTAMP>
<UUID_NONCE>
<SHA256_BODY_HEX>
```

Register `client_public_key` as base64 of the raw 32-byte Ed25519 public key in
Admin authority creation (Admin also accepts an OpenSSH Ed25519 public-key
line), or replace it using
`PUT /api/v1/admin/authority/{uuid}/client-key`. The private client key stays on
the client and is independent of blockchain signing credentials. Authorities
created without a client key can use only unsigned mode until a key is set.
For the complete wire format, key registration, error handling and client
examples, see [docs/security.md](docs/security.md) and the
[client integration guide](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/minter-auth/minter-client-authentication-guide.md).

## NOID Scheme

Current policy:
- alphabet: `0-9bcdfghjkmnpqrstvwxz` (base29)
- generated-part minimum length: `7` by default
- checkdigit: enabled by default
- namespace key: `"{NAAN}:{SHOULDER}"`

Seven base-29 digits represent `29^7 = 17,249,876,309` values. The
encoder grows beyond the configured minimum length for larger counters; this
is not an enforced namespace limit.

Detailed algorithm notes: [noid.md](docs/noid.md)

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

### Local development (including a checkout inside `dark-deployer`)

From this component's root, use a separate Python 3.14 environment:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip install -e ../dark-core-lib
source .venv/bin/activate
```

The sibling `dark-core-lib` checkout is required. Do not install several APIs
into the controller environment: they each contain a package named `app`.
Run the commands below from **this component directory**. Set the documented
runtime configuration first; installing Python packages does not start the
chain, Store or databases. The [platform onboarding guide](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/developer-onboarding.md)
provides the complete local stack, internal Docker endpoints and a working-tree
Resolver example. Keep component tests in this isolated environment.

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

## Tests

From this component directory, after installing the isolated dev environment:

```bash
.venv/bin/python -m pytest
```

The default test database is a temporary SQLite file. `TEST_DATABASE_URL`
overrides it, and fixtures drop/recreate the test schema: use only a disposable
database for that override. PostgreSQL-specific concurrency/locking behavior
requires a separate PostgreSQL integration run. The notebooks test the complete
published lifecycle against the deployed stack; unit tests are not that check.

## Deployment

dark-core-minter-api is deployed by the [dark-deployer](https://github.com/LA-Referencia-IOI/dark-deployer) from an operator inventory (format `dark-operator-inventory` v3; see the [inventory reference](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/examples/operator-inventory/README.md)). The deployer resolves the inventory, renders one Docker Compose project per machine and applies it through guided operations (`./deploy.sh operation plan`, `apply`, `resume`; see the [deployer documentation](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/deployer.md)). The image is built from this repository at the branch declared in the inventory (`overrides.components`; default `main`). Runtime environment variables are generated from the inventory, not edited by hand. There is no standalone docker-compose file in this component.

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
- the resolver must use a Store reader that can retrieve the metadata CIDs; it may be a separate Store instance
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
| `METADATA_WORKER_MAX_RETRIES` | Legacy parameter passed to the selector; current selector does not enforce a retry limit | `5` |
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
| `CHAIN_WORKER_MAX_RETRIES` | Legacy parameter passed to the selector; current selector does not enforce a retry limit | `5` |
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
| `MINTER_NOID_LENGTH` | Minimum generated-part length | `7` |
| `MINTER_NOID_CHECKDIGIT` | Append and validate checkdigit | `true` |
| `AUTH_CACHE_TTL` | NAAN authorization cache TTL | `60` |
| `AUTH_CACHE_MAXSIZE` | Authorization cache max entries | `1000` |

### Request authentication

| Variable | Description | Default |
|----------|-------------|---------|
| `MINTER_SIGNED_AUTH_ENABLED` | Require DARK2 signatures for mutations and retry | `false` |
| `MINTER_CLIENT_KEY_CACHE_IDLE_TTL_SECONDS` | Per-process signing-key cache inactivity TTL | `60` |
| `MINTER_SIGNED_AUTH_MAX_SKEW_SECONDS` | Symmetric timestamp tolerance for signed requests | `300` |
| `MINTER_ALLOWED_AUTHORITY_IDS` | Comma-separated authorities assigned to this logical Minter; empty means unrestricted | empty |

`MTLS_ENABLED`, `TLS_CERT_FILE`, `TLS_KEY_FILE` and `TLS_CA_FILE` remain in
the settings for legacy helpers. They do not turn on a certificate gate for
the current API routes or configure TLS in the standard server entrypoint.
An independently configured gateway may enforce additional transport rules.

## Useful URLs

Once the service is running:
- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`
- Health: `http://localhost:8001/health`
- Worker status: `http://localhost:8001/api/v1/worker/status`

## Notebooks

The notebooks directory contains both focused notebooks and a simpler consolidated integration notebook.

- [notebooks/minter_api_test.ipynb](notebooks/minter_api_test.ipynb)
  - recommended single entry notebook for end-to-end testing
- [notebooks/minter_01_smoke_authority.ipynb](notebooks/minter_01_smoke_authority.ipynb)
- [notebooks/minter_02_reserve_validation.ipynb](notebooks/minter_02_reserve_validation.ipynb)
- [notebooks/minter_03_update_metadata_formats.ipynb](notebooks/minter_03_update_metadata_formats.ipynb)
- [notebooks/minter_04_tombstone_missing.ipynb](notebooks/minter_04_tombstone_missing.ipynb)
- [notebooks/minter_05_batch_concurrency.ipynb](notebooks/minter_05_batch_concurrency.ipynb)
- [notebooks/minter_06_worker_publish_update.ipynb](notebooks/minter_06_worker_publish_update.ipynb)
- [notebooks/minter_07_chain_import_optional.ipynb](notebooks/minter_07_chain_import_optional.ipynb)
- [notebooks/minter_08_resolver_end_to_end.ipynb](notebooks/minter_08_resolver_end_to_end.ipynb)
  - simple direct-HTTP flow from authority creation to resolver lookup
- `notebooks/dark_e2e_authority_to_resolver.ipynb` at the dark-deployer repo root
  - repo-level end-to-end notebook from authority creation to resolver lookup

See [notebooks/README.md](notebooks/README.md) for execution notes and environment variables.

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
- `X-DARK-Authority-Id` is present in both modes and the authority is assigned
  to this Minter, if an assignment list is configured
- when signed authentication is enabled, all six DARK2 headers, the registered
  key, exact path/query and body bytes, timestamp and nonce are correct
- the header identity matches the request body `authority_id`
- the authority is active and authorized for the requested NAAN where required
- the authority owns the ARK where ownership is required

For signed requests, inspect `X-DARK-Error-Code` and consult the
[client troubleshooting guide](https://github.com/LA-Referencia-IOI/dark-deployer/blob/main/docs/minter-auth/minter-client-authentication-guide.md#18-authentication-errors-codes-and-actions).

### Metadata is not visible where expected

Remember the split responsibility:
- API stores payloads in PostgreSQL first
- worker stores immutable payloads in the configured backend later
- on filesystem backend, API and worker must share the same storage volume or directory

## Related Docs

- [docs/noid.md](docs/noid.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/security.md](docs/security.md)
- [notebooks/README.md](notebooks/README.md)

## Ownership transfer and retirement

Each authority operates in one logical Minter; different authorities can use different nodes. Admin transfers only on blockchain, without reading or changing Minter PostgreSQL. Local queues and pending replication do not restrict transfer. Admin inventories current chain owners and blocks retirement while on-chain PIDs remain; stop the authority’s activity and settle in-flight transactions before retirement. The previous request-ID resume API, database retirement gates and transfer coordination dependency were removed.

Minter trusts local identity for normal queued publication and verifies owner/active status/NAAN permissions after a failure. Ownership loss marks the original job obsolete without signing it as the successor. Local-only tombstone/retry verify ownership first; reservations verify active status. A successor request can import the chain PID, archiving superseded local payload under its original authority. Migration `0004_remove_admin_coordination` preserves historical audit rows and creates local `superseded_ark_jobs`. `MINTER_ALLOWED_AUTHORITY_IDS` supplies disjoint assignments for independent Minters (empty means single-Minter unrestricted deployment). See deployer `docs/pid-ownership-transfer-design.md` for API, recovery and operational limitations.
