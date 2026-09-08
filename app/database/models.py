"""
Database ORM models.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    and_,
    Column,
    Float,
    Integer,
    SmallInteger,
    BigInteger,
    Boolean,
    String,
    Text,
    DateTime,
    Index,
    JSON,
    UniqueConstraint,
    ForeignKey,

)
from sqlalchemy.sql import func
from sqlalchemy.orm import declarative_base

from app.models.states import ARKState
from app.models.processing import ProcessingStage, ProcessingStatus, ProcessingWaitReason

Base = declarative_base()


class ProcessingStageCode(Base):
    """Seeded catalogue for compact internal workflow stage IDs."""

    __tablename__ = "processing_stages"

    id = Column(SmallInteger, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)
    description = Column(Text, nullable=False)


class ProcessingStatusCode(Base):
    """Seeded catalogue for compact internal workflow status IDs."""

    __tablename__ = "processing_statuses"

    id = Column(SmallInteger, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)
    description = Column(Text, nullable=False)


class ProcessingWaitReasonCode(Base):
    """Seeded catalogue for why a normal workflow action is deferred."""

    __tablename__ = "processing_wait_reasons"

    id = Column(SmallInteger, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)
    description = Column(Text, nullable=False)


class ProcessingErrorCodeModel(Base):
    """Seeded catalogue for error IDs and retry policy."""

    __tablename__ = "processing_error_codes"

    id = Column(SmallInteger, primary_key=True)
    code = Column(String(64), nullable=False, unique=True)
    retryable = Column(Boolean, nullable=False, default=False, server_default="false")
    description = Column(Text, nullable=False)


class ARKRecord(Base):
    """
    ARK persistence model.
    
    Tracks the full lifecycle of ARK identifiers from reservation
    through publication to tombstone.
    
    The full ARK identifier is computed from naan and name: ark:{naan}/{name}
    """
    
    __tablename__ = "ark_records"
    
    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # ARK components (ark is computed as ark:{naan}/{name})
    naan = Column(String(50), nullable=False)
    name = Column(String(100), nullable=False)
    
    # State tracking (R=reserved, D=draft, U=update, P=published, T=tombstone)
    state = Column(String(1), nullable=False, index=True)
    
    # Authority/ownership
    authority_id = Column(String(255), nullable=False, index=True)
    
    # ARK data
    target = Column(Text, nullable=True)
    # metadata_format removed in favor of ARKMetadata.original_schema
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    tombstoned_at = Column(DateTime, nullable=True)
    
    # Batch tracking
    client_item_id = Column(String(100), nullable=True)
    
    # Internal worker workflow. Public lifecycle state above remains R/D/U/P/T.
    processing_stage = Column(
        SmallInteger,
        ForeignKey("processing_stages.id"),
        default=int(ProcessingStage.NONE),
        nullable=False,
        server_default="0",
    )
    processing_status = Column(
        SmallInteger,
        ForeignKey("processing_statuses.id"),
        default=int(ProcessingStatus.DONE),
        nullable=False,
        server_default=str(int(ProcessingStatus.DONE)),
    )
    processing_attempt_count = Column(SmallInteger, default=0, nullable=False, server_default="0")
    next_action_at = Column(DateTime, nullable=True, index=True)
    processing_wait_reason = Column(
        SmallInteger,
        ForeignKey("processing_wait_reasons.id"),
        default=int(ProcessingWaitReason.NONE),
        nullable=False,
        server_default=str(int(ProcessingWaitReason.NONE)),
    )
    processing_error_code = Column(SmallInteger, ForeignKey("processing_error_codes.id"), nullable=True)
    processing_error_detail = Column(Text, nullable=True)
    
    # Composite indexes for common queries
    __table_args__ = (
        UniqueConstraint("naan", "name", name="uq_naan_name"),
        Index("ix_naan_name", "naan", "name"),
        Index("ix_state_authority", "state", "authority_id"),
        Index(
            "ix_ark_processing_queue",
            "processing_stage",
            "processing_status",
            "next_action_at",
        ),
        Index(
            "ix_ark_processing_active_due",
            "processing_stage",
            "next_action_at",
            "id",
            postgresql_where=processing_status.in_([int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)]),
        ),
        Index(
            "ix_ark_detail_active_rollup",
            "processing_stage",
            "processing_status",
            "processing_wait_reason",
            "next_action_at",
            postgresql_where=state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value, ARKState.PUBLISHED.value]),
            sqlite_where=state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value, ARKState.PUBLISHED.value]),
        ),
        Index(
            "ix_ark_failed_summary",
            "processing_stage",
            "processing_error_code",
            postgresql_where=processing_status == int(ProcessingStatus.FAILED),
            sqlite_where=processing_status == int(ProcessingStatus.FAILED),
        ),
        Index(
            "ix_ark_failed_recent",
            "updated_at",
            "id",
            postgresql_where=processing_status == int(ProcessingStatus.FAILED),
            sqlite_where=processing_status == int(ProcessingStatus.FAILED),
        ),
        Index(
            "uq_ark_records_active_client_item",
            "authority_id",
            "naan",
            "client_item_id",
            unique=True,
            postgresql_where=and_(client_item_id.isnot(None), state != ARKState.TOMBSTONE.value),
            sqlite_where=and_(client_item_id.isnot(None), state != ARKState.TOMBSTONE.value),
        ),
    )
    
    @property
    def ark(self) -> str:
        """Compute full ARK identifier from naan and name."""
        return f"ark:{self.naan}/{self.name}"
    
    def __repr__(self):
        return f"<ARKRecord(ark={self.ark}, state={self.state}, authority={self.authority_id})>"


class NoidCounter(Base):
    """
    Per-namespace sequential counter used for deterministic NOID minting.

    `next_value` stores the next integer to allocate for this namespace.
    """

    __tablename__ = "noid_counters"

    namespace_key = Column(String(160), primary_key=True)
    next_value = Column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<NoidCounter(namespace={self.namespace_key}, next_value={self.next_value})>"


class WorkerRuntimeStatus(Base):
    """
    Runtime status for worker processes using DB heartbeat.

    The API reads this table to expose worker status without
    direct process coupling.
    """

    __tablename__ = "worker_runtime_status"

    worker_name = Column(String(100), primary_key=True)
    instance_id = Column(String(100), nullable=False)
    host = Column(String(255), nullable=False)
    pid = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    last_heartbeat_at = Column(DateTime, nullable=False, index=True)
    started_at = Column(DateTime, nullable=False)
    last_cycle_at = Column(DateTime, nullable=True)
    next_wake_at = Column(DateTime, nullable=True)
    last_cycle_duration_seconds = Column(Float, nullable=True)
    last_cycle_processed = Column(Integer, nullable=True)
    last_cycle_succeeded = Column(Integer, nullable=True)
    last_cycle_failed = Column(Integer, nullable=True)
    last_reconciliation_at = Column(DateTime, nullable=True)
    last_reconciliation_checked = Column(Integer, nullable=False, default=0, server_default="0")
    last_reconciliation_advanced = Column(Integer, nullable=False, default=0, server_default="0")
    last_reconciliation_waiting = Column(Integer, nullable=False, default=0, server_default="0")
    last_reconciliation_repaired = Column(Integer, nullable=False, default=0, server_default="0")
    last_reconciliation_purged = Column(Integer, nullable=False, default=0, server_default="0")
    last_reconciliation_failed = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    consecutive_no_progress_cycles = Column(SmallInteger, nullable=False, default=0, server_default="0")

    total_processed = Column(Integer, nullable=False, default=0, server_default="0")
    total_succeeded = Column(Integer, nullable=False, default=0, server_default="0")
    total_failed = Column(Integer, nullable=False, default=0, server_default="0")
    total_permanent_failures = Column(Integer, nullable=False, default=0, server_default="0")

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return (
            f"<WorkerRuntimeStatus(worker={self.worker_name}, status={self.status}, "
            f"heartbeat={self.last_heartbeat_at})>"
        )


class ARKMetadata(Base):
    """
    Two-level metadata storage for ARKs.
    
    Level 1: Minimal extracted metadata (JSON)
    Level 2: Original metadata content (XML/JSON/Text)
    """
    
    __tablename__ = "ark_metadata"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    ark_record_id = Column(Integer, ForeignKey("ark_records.id"), nullable=False, unique=True, index=True)
    
    # Level 1 — minimal metadata (validated JSON from client)
    level1_json = Column(JSON, nullable=True)
    level1_cid = Column(String(100), nullable=True)   # Set by worker after IPFS store
    
    # Level 2 — original metadata (opaque content from client)
    original_content = Column(Text, nullable=True)
    original_schema = Column(String(50), nullable=False)  # "dublin_core", etc.
    original_media_type = Column(String(255), nullable=True)
    original_cid = Column(String(100), nullable=True)     # Set by worker after IPFS store

    # Last replication counts observed by the metadata reconciler.
    # NULL means not observed yet; zero means Cluster was queried and has no pin.
    level1_replica_count = Column(SmallInteger, nullable=True)
    level2_replica_count = Column(SmallInteger, nullable=True)
    replication_checked_at = Column(DateTime, nullable=True, index=True)
    replication_observation_count = Column(SmallInteger, nullable=False, default=0, server_default="0")
    replication_error_code = Column(SmallInteger, ForeignKey("processing_error_codes.id"), nullable=True)
    replication_last_error = Column(Text, nullable=True)
    last_repair_at = Column(DateTime, nullable=True)
    payload_purged_at = Column(DateTime, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return (
            f"<ARKMetadata(id={self.id}, ark_record_id={self.ark_record_id}, "
            f"schema={self.original_schema}, media_type={self.original_media_type})>"
        )
