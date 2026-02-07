# NOID Implementation (dARK Core Minter API)

This document describes the current NOID implementation in `dark-core-minter-api`.

## 1. Executive Summary

ARK minting uses a deterministic, sequential, persistent scheme:

- Per-namespace counter in DB (`noid_counters`)
- Base29 encoding without vowels
- Generated-part length operated as fixed (`7`)
- NOID checkdigit suffix (enabled by default)

Result: stable IDs, no RNG dependency, and predictable per-namespace capacity.

## 2. Identifier Format

Full ARK:

```text
ark:{naan}/{name}
```

Current `name`:

```text
{shoulder}{counter_part}{checkdigit}
```

Where:

- `shoulder`: optional minter prefix
- `counter_part`: base29-encoded counter with fixed length `7`
- `checkdigit`: character computed over `"{naan}/{shoulder}{counter_part}"`

Examples:

- `ark:12345/x0000000d`
- `ark:12345/x0000001w`

## 3. Alphabet and Base

Alphabet:

```text
0123456789bcdfghjkmnpqrstvwxz
```

- Base = 29
- Vowels are excluded to reduce accidental words

Implemented in:

- `app/utils/noid.py` (`ALPHABET`, `BASE`)

## 4. Relevant Configuration

Variables:

- `MINTER_SHOULDER` (default `""`)
- `MINTER_NOID_LENGTH` (default `7`)
- `MINTER_NOID_CHECKDIGIT` (default `true`)

Important note:

- Current policy operates it as fixed length (recommended `7`).

## 5. Namespace Capacity

`counter_part` capacity:

```text
base^length = 29^7 = 17,249,876,309
```

Per namespace:

```text
namespace = "{NAAN}:{SHOULDER}"
```

Checkdigit does not reduce this capacity because it is appended.

## 6. Data Model

Table:

- `noid_counters`
  - `namespace_key` (PK, `NAAN:SHOULDER`)
  - `next_value` (next value to allocate)
  - `updated_at`

Migration:

- `alembic/versions/0002_add_noid_counters.py`

## 7. Minting Flow

### 7.1 Single reserve (`POST /api/v1/arks`)

1. Validate authority/NAAN authorization
2. Build `namespace_key`
3. Allocate counter atomically (`allocate_next`)
4. Encode `counter_part` in base29
5. Build `name` with shoulder
6. Append checkdigit when `MINTER_NOID_CHECKDIGIT=true`
7. Insert `ark_records` within savepoint
8. Commit transaction

### 7.2 Batch reserve (`POST /api/v1/arks/batch`)

- Same flow per item
- Per-item collision retries
- Collect results and errors
- Commit once at end of batch

## 8. Concurrency and Atomicity

Repository:

- `app/repositories/noid_counter_repository.py`

Strategy:

- Lazy namespace-row creation (`INSERT ... ON CONFLICT DO NOTHING` on PostgreSQL)
- Atomic increment with `UPDATE ... RETURNING` when available
- Fallback transactional lock (`with_for_update`) for dialects without `RETURNING`

Goal:

- Prevent two concurrent processes from allocating the same `counter_value` in the same namespace

## 9. Checkdigit

Functions:

- `compute_checkdigit(payload: str) -> str`
- `validate_name_checkdigit(naan: str, name: str) -> bool`

Rule:

- Map each character to its ordinal inside `ALPHABET`
- Characters outside alphabet contribute `0`
- Weighted positional sum (1-based)
- `sum % 29` selects the checkdigit character

Mint payload:

```text
{naan}/{name_without_checkdigit}
```

## 10. API Validation

When `MINTER_NOID_CHECKDIGIT=true`:

- `GET /api/v1/arks/{ark}`
- `PUT /api/v1/arks/{ark}`
- `DELETE /api/v1/arks/{ark}`

validate `name` checkdigit.

On mismatch:

- HTTP `400` with invalid-checkdigit detail

Expected side effect:

- Legacy ARKs without valid checkdigit are rejected by those routes while validation is enabled

## 11. Overflow Policy

With fixed length `7`, if `counter_value` exceeds namespace capacity, encoded length can grow naturally.

Current operational policy:

- Keep `MINTER_NOID_LENGTH=7` as development standard
- Monitor per-namespace consumption
- If needed, move to length `8` in a controlled migration

## 12. Errors and Retries

- Unique collisions in `ark_records` (`uq_naan_name`):
  - Retry with next counter
- Non-unique integrity errors:
  - Return `500`
- Collision retries exhausted:
  - Return `409`

## 13. Key Files

- `app/utils/noid.py`
- `app/api/arks.py`
- `app/repositories/noid_counter_repository.py`
- `app/database/models.py`
- `alembic/versions/0002_add_noid_counters.py`
- `tests/test_persistence.py`

## 14. Design Decisions

1. DB counter instead of random generation:
   - avoids probabilistic collisions and improves traceability
2. Namespace by `NAAN + shoulder`:
   - issuer/context isolation
3. Default checkdigit:
   - early typo/corruption detection
4. Fixed operational length:
   - stable and predictable client-facing format
