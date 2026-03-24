# dARK Core Minter API Architecture

This document complements the main [README](./README.md). The README explains how to use and operate the service; this document focuses on how the minter is implemented internally, why it is structured this way, and which runtime guarantees it relies on.

Language note: this document is intentionally maintained in English.

## 1. Design Goals

The minter is built around a few explicit design choices:

- reservation is local and deterministic
- publication is asynchronous
- PostgreSQL is the local source of truth
- blockchain logic is delegated to `dark-core-lib`
- metadata persistence is pluggable
- worker execution is singleton-oriented

Those choices let the service accept requests quickly, survive transient blockchain or storage failures, and keep retry state outside process memory.

## 2. Module Map

The service is intentionally split into a few layers.

- HTTP API
  - [`app/main.py`](./app/main.py)
  - [`app/api/router.py`](./app/api/router.py)
  - [`app/api/arks.py`](./app/api/arks.py)
  - [`app/api/authority.py`](./app/api/authority.py)
  - [`app/api/worker.py`](./app/api/worker.py)
- Runtime dependencies
  - [`app/config.py`](./app/config.py)
  - [`app/dependencies.py`](./app/dependencies.py)
  - [`app/middleware/auth.py`](./app/middleware/auth.py)
- Persistence
  - [`app/database/models.py`](./app/database/models.py)
  - [`app/repositories/ark_repository.py`](./app/repositories/ark_repository.py)
  - [`app/repositories/noid_counter_repository.py`](./app/repositories/noid_counter_repository.py)
  - [`app/repositories/worker_runtime_repository.py`](./app/repositories/worker_runtime_repository.py)
- Metadata and validation
  - [`dark_core_lib/metadata/schemas.py`](../../core/dark-core-lib/dark_core_lib/metadata/schemas.py)
  - [`dark_core_lib/metadata/service.py`](../../core/dark-core-lib/dark_core_lib/metadata/service.py)
  - [`dark_core_lib/metadata/storage/filesystem.py`](../../core/dark-core-lib/dark_core_lib/metadata/storage/filesystem.py)
  - [`dark_core_lib/metadata/storage/store_api.py`](../../core/dark-core-lib/dark_core_lib/metadata/storage/store_api.py)
- Worker
  - [`app/main_worker.py`](./app/main_worker.py)
  - [`app/workers/publisher.py`](./app/workers/publisher.py)
- Utilities
  - [`app/utils/noid.py`](./app/utils/noid.py)
  - [`app/utils/auth_cache.py`](./app/utils/auth_cache.py)

## 3. Runtime Topology

```mermaid
flowchart LR
    Client["Client or Notebook"] --> API["FastAPI process"]
    API --> DB["PostgreSQL"]
    API --> Core["dark-core-lib client"]
    Core --> Chain["Authority + dARK contracts"]

    Worker["Standalone worker process"] --> DB
    Worker --> Store["Metadata storage backend"]
    Worker --> Core

    DB --> Runtime["worker_runtime_status"]
    API --> Status["GET /api/v1/worker/status"]
    Status --> Runtime
```

The API and worker are separate processes on purpose:

- API stays responsive and transactional
- worker owns the slow path
- retry and failure tracking stay in PostgreSQL
- deployment can scale API and keep worker singleton

## 4. Startup and Dependency Wiring

### API Startup

The API startup path in [`app/main.py`](./app/main.py) does three important things:

1. validates runtime configuration
2. initializes the database
3. initializes long-lived singletons

Those singletons are created in [`app/dependencies.py`](./app/dependencies.py):

- `DARKCoreClient`
- metadata storage backend created through `dark_core_lib.metadata.get_metadata_storage(...)`

```mermaid
sequenceDiagram
    participant Main as "app/main.py"
    participant Config as "Settings"
    participant DB as "init_db()"
    participant Deps as "dependencies.py"
    participant Core as "DARKCoreClient"
    participant Store as "MetadataStorage"

    Main->>Config: load .env.integration or .env
    Main->>Config: validate blockchain and mTLS settings
    Main->>DB: init_db()
    Main->>Deps: init_corelib_client()
    Deps->>Core: build configured client
    Main->>Deps: init_metadata_storage()
    Deps->>Store: build filesystem/store_api backend
```

### Worker Startup

The worker startup path in [`app/main_worker.py`](./app/main_worker.py) adds lifecycle controls on top of normal dependency init:

- pidfile guard
- PostgreSQL advisory lock
- scheduler loop
- heartbeat persistence

```mermaid
flowchart TD
    A["worker process start"] --> B["create pidfile guard"]
    B --> C["init_db()"]
    C --> D["acquire PostgreSQL advisory lock"]
    D -->|success| E["init dark-core-lib"]
    E --> F["init metadata storage"]
    F --> G["start APScheduler"]
    G --> H["write RUNNING heartbeat"]
    D -->|failure| X["abort startup"]
```

## 5. Request Path: Reserve

The reserve flow is implemented in [`app/api/arks.py`](./app/api/arks.py) and backed by [`app/repositories/noid_counter_repository.py`](./app/repositories/noid_counter_repository.py).

### Why reservation is local

Reservation is deliberately not a blockchain operation. The goal is:

- fast request latency
- deterministic ARK generation
- no on-chain contention for every reserve
- ability to stage metadata before paying transaction cost

### Flow

```mermaid
sequenceDiagram
    participant Client as "Client"
    participant API as "reserve_ark"
    participant Cache as "auth_cache"
    participant Core as "dark-core-lib"
    participant Counter as "NoidCounterRepository"
    participant Repo as "ARKRepository"
    participant DB as "PostgreSQL"

    Client->>API: POST /api/v1/arks
    API->>API: enforce authority identity
    API->>Cache: authorization lookup
    Cache->>Core: is_authorized_for_naan()
    Core-->>Cache: boolean
    API->>Counter: allocate_next(namespace_key)
    Counter->>DB: update namespace counter
    DB-->>Counter: next counter
    API->>API: mint_ark_id()
    API->>Repo: create_reserved()
    Repo->>DB: INSERT state=RESERVED
    API->>DB: COMMIT
    API-->>Client: ARKResponse
```

### Reservation Guarantees

- the namespace is `NAAN + shoulder`
- generated names use base29 NOID encoding
- a checkdigit can be appended and enforced
- uniqueness is protected by DB constraints plus retry on collision
- the ARK exists locally before any publish attempt is made

## 6. Request Path: Metadata Staging

The update flow is also implemented in [`app/api/arks.py`](./app/api/arks.py), but the actual state transitions live in [`app/repositories/ark_repository.py`](./app/repositories/ark_repository.py).

### Why staging exists

Staging separates client write success from blockchain publication success.

That gives us:

- predictable HTTP latency
- explicit `DRAFT` and `UPDATE` states
- retries outside the request thread
- room for metadata storage failures and blockchain failures without losing intent

### Flow

```mermaid
sequenceDiagram
    participant Client as "Client"
    participant API as "update_ark_metadata"
    participant Repo as "ARKRepository"
    participant DB as "PostgreSQL"
    participant Core as "dark-core-lib"
    participant Chain as "Blockchain"

    Client->>API: PUT /api/v1/arks/{ark}
    API->>API: validate checkdigit and authority identity
    alt ARK exists locally
        API->>Repo: create_or_update_metadata()
        API->>Repo: update_to_draft/update_draft_content/update_to_update
        Repo->>DB: CAS state transition
    else ARK missing locally
        API->>Core: get_ark / get_authority
        Core->>Chain: read on-chain state
        API->>API: validate on-chain owner matches authority wallet
        API->>Repo: create_published_import()
        API->>Repo: create_or_update_metadata()
        API->>Repo: update_to_update()
    end
    API->>DB: COMMIT
    API-->>Client: staged response
```

### Important Rules

- `RESERVED -> DRAFT` requires a non-empty `target`
- `DRAFT -> DRAFT` overwrites pending payload before creation
- `PUBLISHED -> UPDATE` stages a blockchain update
- `UPDATE -> UPDATE` overwrites the pending update payload
- `TOMBSTONE` is terminal for write flows

## 7. Publish Path: Worker-Owned Slow Path

The publish logic lives in [`app/workers/publisher.py`](./app/workers/publisher.py). The worker does not keep long-lived in-memory queues. It queries PostgreSQL every cycle and claims work there.

### Why the worker owns storage and chain publication

This keeps side effects serialized and easier to reason about:

- metadata CIDs are produced in one place
- retries can reuse previously stored CIDs
- blockchain calls happen outside the request path
- publication metrics can be derived from DB and worker heartbeat
- minter and resolver can share the same Level-1 schema and storage contract without duplicating code

### Publish Cycle

```mermaid
sequenceDiagram
    participant Scheduler as "APScheduler"
    participant Publisher as "ARKPublisher"
    participant Repo as "ARKRepository"
    participant DB as "PostgreSQL"
    participant Store as "Metadata backend"
    participant Core as "dark-core-lib"
    participant Chain as "Blockchain"

    Scheduler->>Publisher: run_publish_cycle()
    Publisher->>Repo: get_drafts_pending_publish()
    Repo->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    Repo->>DB: set publish_last_attempt_at
    Publisher->>DB: commit claim

    loop each claimed ARK
        Publisher->>Repo: load record + metadata
        Publisher->>Store: store Level-2
        Store-->>Publisher: original_cid
        Publisher->>Store: store Level-1 with embedded original_cid
        Store-->>Publisher: level1_cid
        Publisher->>Repo: update_metadata_cids()
        alt state == DRAFT
            Publisher->>Core: create_ark()
        else state == UPDATE
            Publisher->>Core: update_ark()
        end
        Core->>Chain: transaction
        Publisher->>Repo: update_to_published()
        Publisher->>DB: commit
    end
```

### Storage Ordering

The ordering is intentional:

1. store Level-2 raw payload
2. inject `original_metadata.cid` into Level-1 JSON
3. store finalized Level-1 JSON
4. publish the Level-1 CID on-chain

This makes Level-1 the canonical chain-facing entry point while still preserving the original payload.

The concrete implementation of these steps is now shared through `dark_core_lib.metadata.MetadataService`.

## 8. State Transition Model

The lifecycle enum is defined in [`app/models/states.py`](./app/models/states.py), but the business meaning comes from how API and worker cooperate.

```mermaid
stateDiagram-v2
    [*] --> RESERVED: reserve
    RESERVED --> DRAFT: stage create
    DRAFT --> DRAFT: overwrite pending create
    DRAFT --> PUBLISHED: worker create_ark
    PUBLISHED --> UPDATE: stage update
    UPDATE --> UPDATE: overwrite pending update
    UPDATE --> PUBLISHED: worker update_ark
    RESERVED --> TOMBSTONE: local delete
    DRAFT --> TOMBSTONE: local delete
    UPDATE --> TOMBSTONE: local delete
    PUBLISHED --> TOMBSTONE: local delete
```

### Compare-and-Set Transitions

The repository uses compare-and-set style updates for critical transitions:

- expected current state is part of the SQL predicate
- if `rowcount == 0`, caller reloads and treats it as conflict

This is one of the main protections against races between API and worker or between multiple update attempts.

## 9. Concurrency Model

The minter relies on PostgreSQL semantics for most of its concurrency guarantees.

### Summary

```mermaid
flowchart LR
    A["NOID counter"] --> B["atomic counter allocation"]
    C["Pending publish claims"] --> D["FOR UPDATE SKIP LOCKED"]
    E["Lifecycle transitions"] --> F["CAS SQL updates"]
    G["Worker singleton"] --> H["advisory lock + pidfile"]
    I["Retry schedule"] --> J["publish_last_attempt_at + retry_count"]
```

### Counter Allocation

The counter repository uses:

- a namespace row in `noid_counters`
- atomic increment on PostgreSQL
- fallback logic for non-PostgreSQL dialects

Operationally, the minter is designed around PostgreSQL deployment, and that is where the strongest guarantees exist.

### Publish Claims

`get_drafts_pending_publish()` does not keep long row locks for the whole publish transaction. Instead it:

1. selects pending records with `FOR UPDATE SKIP LOCKED`
2. marks them as attempted
3. commits the claim
4. processes each ARK independently

That design avoids long locks during slow external operations such as:

- storage writes
- blockchain RPC calls
- gas estimation
- transaction propagation

### Retry Policy

Retry state is persisted in the DB, not memory:

- `publish_retry_count`
- `publish_last_attempt_at`
- `publish_permanently_failed`
- `publish_error`

That means retries survive:

- process restart
- container recreation
- worker crashes

## 10. Worker Runtime Status

Worker liveness is represented in the database through `worker_runtime_status`, not by inspecting process internals over IPC.

```mermaid
sequenceDiagram
    participant Worker as "main_worker"
    participant Repo as "WorkerRuntimeRepository"
    participant DB as "PostgreSQL"
    participant API as "/api/v1/worker/status"

    Worker->>Repo: upsert_status(RUNNING, heartbeat, counters)
    Repo->>DB: UPSERT worker_runtime_status
    API->>Repo: get_by_name(worker_runtime_name)
    Repo->>DB: SELECT heartbeat row
    API->>API: derive running/stale flags
    API-->>Client: worker status payload
```

This makes worker status available even when:

- API and worker are different containers
- worker logs are not directly accessible
- only the shared database is visible

## 11. Data Model

```mermaid
erDiagram
    ARK_RECORDS {
        int id PK
        string naan
        string name
        string state "R,D,U,P,T"
        string authority_id
        string target
        string client_item_id
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
        string original_cid
    }

    NOID_COUNTERS {
        string namespace_key PK
        bigint next_value
        datetime updated_at
    }

    WORKER_RUNTIME_STATUS {
        string worker_name PK
        string instance_id
        string host
        int pid
        string status
        datetime last_heartbeat_at
        datetime started_at
        datetime last_cycle_at
        int total_processed
        int total_succeeded
        int total_failed
        int total_permanent_failures
    }
```

### Table Roles

- `ark_records`
  - lifecycle state and identity of each ARK
- `ark_metadata`
  - staged and published metadata payloads plus CIDs
- `noid_counters`
  - deterministic namespace counters
- `worker_runtime_status`
  - distributed worker heartbeat and counters

## 12. Security Model

The security model is intentionally split between local-dev ergonomics and production deployment assumptions.

### Local Developer Mode

When `MTLS_ENABLED=false`:

- mutating endpoints require `X-Authority-Id` or `X-Authority-UUID`
- body `authority_id` must match the resolved identity
- NAAN authorization is still checked against the blockchain layer via `dark-core-lib`

This makes notebooks and local integration easy, but it is not a complete production identity model.

### Production-Oriented Mode

When `MTLS_ENABLED=true`:

- the service expects a valid client certificate flow
- authority identity is resolved from trusted request information

The architecture assumes a trusted TLS termination path or equivalent deployment control. That boundary matters because the application can consume certificate-related request headers.

## 13. Deployment Notes

### Monorepo Docker Deployment

```mermaid
flowchart LR
    ENV["components/blockchain/dark-env"] --> NET["dark-net"]
    MINTER["components/services/dark-core-minter-api"] --> NET
    MINTER --> PG["postgres container"]
    MINTER --> API["minter-api"]
    MINTER --> W["minter-worker"]
    API --> VOL["metadata-storage volume"]
    W --> VOL
```

Important deployment assumptions:

- blockchain is already running on `dark-net`
- API and worker share PostgreSQL
- API and worker share metadata storage when filesystem backend is used
- `.env.integration` is the preferred deployed config file

## 14. Tradeoffs and Current Limits

Some tradeoffs are intentional and worth documenting explicitly.

- tombstone is currently local, not an on-chain revoke
- publication is eventually consistent, not immediate
- PostgreSQL is the real operational target; non-PostgreSQL fallback paths are weaker
- request success for staging does not mean on-chain success yet
- worker heartbeat is best-effort status reporting, not a hard availability proof

## 15. Reading Order

If you are onboarding to the codebase, this order works well:

1. [README.md](./README.md)
2. [`app/main.py`](./app/main.py)
3. [`app/api/arks.py`](./app/api/arks.py)
4. [`app/repositories/ark_repository.py`](./app/repositories/ark_repository.py)
5. [`app/workers/publisher.py`](./app/workers/publisher.py)
6. [`app/main_worker.py`](./app/main_worker.py)
7. [`app/middleware/auth.py`](./app/middleware/auth.py)
