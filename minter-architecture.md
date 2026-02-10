# dARK Core Minter API - Architecture and Flows

This document explains the full minter architecture: runtime components, data flows, ARK lifecycle transitions, and PostgreSQL concurrency controls.

Language note: this document is intentionally maintained in English.

## 1. System Topology

```mermaid
flowchart LR
    C["API Client"] --> API["API Process (FastAPI)"]
    API --> DB["PostgreSQL"]
    API --> ST["Metadata Storage (filesystem/store_api)"]
    API --> ORCH["dark-core-orchestrator"]
    ORCH --> CHAIN["Blockchain (Authority + dARK)"]

    W["Worker Process (APScheduler)"] --> DB
    W --> ORCH

    DB --> HB["worker_runtime_status (heartbeat)"]
    API --> WS["GET /api/v1/worker/status"]
    WS --> HB
```

## 2. Main Components

- `app/main.py`: API-only HTTP process.
- `app/main_worker.py`: standalone publisher worker process.
- `app/api/arks.py`: ARK lifecycle endpoints.
- `app/repositories/ark_repository.py`: `ark_records` access with CAS state transitions.
- `app/repositories/noid_counter_repository.py`: deterministic NOID namespace counter.
- `app/workers/publisher.py`: publish loop and retry policy.
- `app/api/worker.py`: worker status endpoint backed by DB heartbeat.
- `app/storage/store_api.py`: metadata backend that delegates persistence to `dark-store-api`.

## 3. ARK Reservation (POST /api/v1/arks)

```mermaid
sequenceDiagram
    participant Client as "Client"
    participant API as "arks.py"
    participant CRepo as "NoidCounterRepository"
    participant ARepo as "ARKRepository"
    participant DB as "PostgreSQL"

    Client->>API: "POST /api/v1/arks"
    API->>CRepo: "allocate_next(namespace)"
    CRepo->>DB: "INSERT ... ON CONFLICT DO NOTHING"
    CRepo->>DB: "UPDATE noid_counters SET next_value = next_value + 1 RETURNING"
    DB-->>CRepo: "counter_value"
    CRepo-->>API: "counter_value"
    API->>API: "mint_ark_id(counter, shoulder, checkdigit)"
    API->>ARepo: "create_reserved(naan, name, authority)"
    ARepo->>DB: "INSERT ark_records (state=R)"
    API->>DB: "COMMIT"
    API-->>Client: "201 Created (ark:NAAN/name)"
```

## 4. Metadata Update (PUT /api/v1/arks/{ark})

Important rule: `target` URL is mandatory for `RESERVED -> DRAFT`.
If `target` is missing or empty, the request is rejected and the ARK stays in `RESERVED`.

```mermaid
sequenceDiagram
    participant Client as "Client"
    participant API as "arks.py"
    participant ST as "Metadata Storage"
    participant ARepo as "ARKRepository"
    participant DB as "PostgreSQL"

    Client->>API: "PUT /api/v1/arks/{ark}"
    API->>API: "validate target, metadata, format"
    API->>ST: "store_metadata(content, format)"
    ST-->>API: "metadata_cid"
    API->>ARepo: "update_to_draft(ark, target, cid, format)"
    ARepo->>DB: "UPDATE ... WHERE state='R' (CAS)"
    DB-->>ARepo: "rowcount"
    API->>DB: "COMMIT"
    API-->>Client: "200 OK (state=D)"
```

## 5. Asynchronous Worker Publish Flow (`DRAFT`/`UPDATE` -> `PUBLISHED`)

```mermaid
sequenceDiagram
    participant W as "Worker Scheduler"
    participant Pub as "ARKPublisher"
    participant ARepo as "ARKRepository"
    participant DB as "PostgreSQL"
    participant ORCH as "dark-core-orchestrator"
    participant CHAIN as "Blockchain"

    W->>Pub: "run_publish_cycle()"
    Pub->>ARepo: "get_drafts_pending_publish(limit)"
    ARepo->>DB: "SELECT ... FOR UPDATE SKIP LOCKED"
    ARepo->>DB: "UPDATE publish_last_attempt_at (claim)"
    Pub->>DB: "COMMIT (release row locks)"

    loop "for each claimed ARK"
        Pub->>Pub: "if state=DRAFT -> create_ark"
        Pub->>Pub: "if state=UPDATE -> update_ark"
        Pub->>ORCH: "create_ark / update_ark"
        ORCH->>CHAIN: "tx create_ark / update_ark"
        Pub->>ARepo: "update_to_published(ark, cid)"
        ARepo->>DB: "UPDATE ... WHERE state in ('D','U') (CAS)"
        Pub->>DB: "COMMIT"
    end
```

## 6. Worker Singleton and Heartbeat

```mermaid
flowchart TD
    S["dark-core-worker start"] --> P["pidfile guard (/tmp/dark-core-worker.pid)"]
    P --> L["pg_try_advisory_lock(worker_runtime_name)"]
    L -->|"false"| X["Abort startup (another worker active)"]
    L -->|"true"| R["Start APScheduler loop"]
    R --> H["Persist heartbeat in worker_runtime_status"]
    H --> A["API GET /api/v1/worker/status reads heartbeat"]
    R --> U["Shutdown -> pg_advisory_unlock + remove pidfile"]
```

## 7. PostgreSQL Concurrency Strategy

```mermaid
flowchart LR
    N1["NOID counter"] --> N2["Atomic UPDATE ... RETURNING"]
    N3["Draft claim"] --> N4["FOR UPDATE SKIP LOCKED"]
    N5["State transitions"] --> N6["CAS: UPDATE ... WHERE state=expected"]
    N7["Retry tracking"] --> N8["Atomic SQL increment"]
    N9["Worker singleton"] --> N10["pg_try_advisory_lock"]
```

## 8. ARK State Machine

Canonical lifecycle:

```mermaid
stateDiagram-v2
    [*] --> RESERVED: POST /arks
    RESERVED --> DRAFT: PUT /arks/{ark} (target + L1 + L2 required)
    DRAFT --> DRAFT: PUT /arks/{ark} (overwrite pending create)
    DRAFT --> PUBLISHED: Worker create_ark
    PUBLISHED --> UPDATE: PUT /arks/{ark}
    UPDATE --> UPDATE: PUT /arks/{ark} (overwrite pending update)
    UPDATE --> PUBLISHED: Worker update_ark
    RESERVED --> TOMBSTONE: DELETE /arks/{ark}
    DRAFT --> TOMBSTONE: DELETE /arks/{ark}
    UPDATE --> TOMBSTONE: DELETE /arks/{ark}
    PUBLISHED --> TOMBSTONE: DELETE /arks/{ark}
    TOMBSTONE --> TOMBSTONE: DELETE /arks/{ark} (idempotent)
```

Transition rules (command-centric):

```mermaid
flowchart TD
    PUT["PUT /api/v1/arks/{ark}"] --> S{"Current DB state?"}
    S -->|RESERVED| TO_D["Set DRAFT (pending create_ark)"]
    S -->|DRAFT| KEEP_D["Keep DRAFT (overwrite pending payload)"]
    S -->|PUBLISHED| TO_U["Set UPDATE (pending update_ark)"]
    S -->|UPDATE| KEEP_U["Keep UPDATE (overwrite pending payload)"]
    S -->|TOMBSTONE| REJ["Reject (409)"]
    S -->|Missing in DB| CHAIN{"Exists on-chain?"}
    CHAIN -->|No| NOT_FOUND["404"]
    CHAIN -->|Yes| IMPORT["Import local PUBLISHED"]
    IMPORT --> TO_U
```

## 9. Data Model (Summary)

```mermaid
erDiagram
    ARK_RECORDS {
        int id PK
        string naan
        string name
        string state "R,D,U,P,T"
        string authority_id
        string target
        string metadata_cid
        int publish_retry_count
        datetime publish_last_attempt_at
        int publish_permanently_failed
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
        int total_processed
        int total_succeeded
        int total_failed
    }
```

## 10. Responsibility Map

- API:
  - validates authority.
  - reserves deterministic ARKs.
  - validates and stores L1/L2 metadata in DB.
  - applies `RESERVED -> DRAFT` / `PUBLISHED -> UPDATE` transitions.
  - exposes worker status via heartbeat.
- Worker:
  - safely claims DRAFT/UPDATE records.
  - stores L2 first, then L1 with injected L2 CID.
  - publishes ARKs on-chain using L1 CID.
  - applies `DRAFT -> PUBLISHED` with CAS.
  - reports heartbeat and runtime metrics.
- PostgreSQL:
  - source of truth for lifecycle state.
  - row-level locking for draft claims.
  - CAS-enforced state transitions.
  - advisory lock for distributed singleton worker.
