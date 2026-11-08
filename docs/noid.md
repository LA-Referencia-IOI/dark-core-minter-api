# NOID Implementation (dARK Core Minter API)

This document describes the current NOID implementation in `dark-core-minter-api`.

## 1. Executive Summary

ARK minting uses a deterministic, sequential, persistent scheme:

- Per-namespace counter in DB (`noid_counters`)
- Base29 encoding without vowels
- Generated-part minimum length configurable (default `7`)
- NOID checkdigit suffix (enabled by default)

Result: persistent counter allocation without an RNG; encoded names grow when the counter needs more digits.

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

- `shoulder`: DARK 2 minter prefix (`2xx`)
- `counter_part`: base29-encoded counter padded to a configurable minimum length (default `7`)
- `checkdigit`: optional character computed over `"{naan}/{shoulder}{counter_part}"`

Examples:

- `ark:12345/2000000000c`
- `ark:12345/2000000001x`

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

- `MINTER_SHOULDER` (default `"200"`)
- `MINTER_NOID_LENGTH` (default `7`)
- `MINTER_NOID_CHECKDIGIT` (default `true`)

Important note:

- The encoder pads to the configured minimum length; it does not truncate longer values.
- The shoulder is exactly three lowercase ASCII alphanumeric characters in the
  form `2xx`: `2` denotes DARK 2 and each `x` is a minter-code character. The
  historical `00*` prefix space is reserved for DARK 1 and is rejected by the
  minter configuration.
- Assign each `xx` code uniquely among minters that can issue under the same
  NAAN. `200` is the default allocation.

## 5. Namespace Capacity

The number of values represented within seven characters is:

```text
base^length = 29^7 = 17,249,876,309
```

The encoder can emit more than seven characters for larger values; the number
above is not an enforced namespace limit. The database counter type and other
operational constraints still apply.

Per namespace:

```text
namespace = "{NAAN}:{SHOULDER}"
```

An enabled check digit is appended and does not consume a counter digit.

## 6. Data Model

Table:

- `noid_counters`
  - `namespace_key` (PK, `NAAN:SHOULDER`)
  - `next_value` (next value to allocate)
  - `updated_at`

Migration:

- `alembic/versions/0001_initial_schema.py`

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

With a minimum length of `7`, encoded length grows naturally once the counter no longer fits in seven base-29 digits.

Current operational policy:

- Keep `MINTER_NOID_LENGTH=7` as development standard
- Monitor per-namespace consumption
- Assess client and storage constraints before changing the minimum length; padding changes generated names

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
- `alembic/versions/0001_initial_schema.py`
- `tests/test_persistence.py`

## 14. Design Decisions

1. DB counter instead of random generation:
   - avoids probabilistic collisions and improves traceability
2. Namespace by `NAAN + shoulder`:
   - issuer/context isolation
3. Default checkdigit:
   - early typo/corruption detection
4. Configurable minimum length:
   - zero padding provides a consistent minimum without truncating larger values
