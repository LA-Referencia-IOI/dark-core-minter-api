"""Compact internal workflow codes for asynchronous ARK processing.

The public ARK lifecycle remains ``R/D/U/P/T``.  These numeric codes are an
internal implementation detail and are translated to readable labels by the
API and operational tooling.
"""

from enum import IntEnum


class ProcessingStage(IntEnum):
    NONE = 0
    METADATA = 1
    AVAILABILITY = 2
    CHAIN = 3
    REPLICATION = 4
    COMPLETE = 5


class ProcessingStatus(IntEnum):
    IDLE = 0
    PENDING = 1
    RECOVERABLE = 2
    FAILED = 3
    DONE = 4
    CANCELLED = 5


class ProcessingErrorCode(IntEnum):
    STORAGE_UNAVAILABLE = 100
    STORAGE_INVALID_RESPONSE = 101
    REPLICATION_UNAVAILABLE = 200
    CID_MISMATCH = 201
    CHAIN_RPC_UNAVAILABLE = 300
    CHAIN_REVERTED = 301
    AUTHORITY_NOT_FOUND = 302
    AUTHORIZATION_FAILED = 303
    CHAIN_STATE_CONFLICT = 304
    RECOVERY_NOT_POSSIBLE = 901
    UNEXPECTED = 900


def code_label(value: int | None, enum_type: type[IntEnum]) -> str | None:
    """Return the stable API label for a persisted numeric code."""
    if value is None:
        return None
    try:
        return enum_type(int(value)).name.lower()
    except ValueError:
        return "unknown"
