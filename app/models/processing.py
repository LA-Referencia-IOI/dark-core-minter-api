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
    READY = 1
    WAITING = 2
    FAILED = 3
    DONE = 4
    CANCELLED = 5


class ProcessingWaitReason(IntEnum):
    """Why a normal worker scheduled a later action for an ARK."""

    NONE = 0
    CLUSTER_PINNING = 1
    INITIAL_VISIBILITY = 2
    REPLICA_TARGET = 3
    STORAGE_BACKOFF = 4
    RPC_BACKOFF = 5
    CHAIN_CONFIRMATION = 6
    CLUSTER_QUEUED = 7


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
    UNEXPECTED = 900


def code_label(value: int | None, enum_type: type[IntEnum]) -> str | None:
    """Return the stable API label for a persisted numeric code."""
    if value is None:
        return None
    try:
        return enum_type(int(value)).name.lower()
    except ValueError:
        return "unknown"
