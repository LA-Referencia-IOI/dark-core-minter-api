"""
Database ORM models.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
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

Base = declarative_base()


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
    
    # Publish tracking fields for async worker
    publish_retry_count = Column(Integer, default=0, nullable=False, server_default="0")
    publish_last_error = Column(Text, nullable=True)
    publish_last_attempt_at = Column(DateTime, nullable=True)
    publish_permanently_failed = Column(Integer, default=0, nullable=False, server_default="0")
    
    # Composite indexes for common queries
    __table_args__ = (
        UniqueConstraint("naan", "name", name="uq_naan_name"),
        Index("ix_naan_name", "naan", "name"),
        Index("ix_state_authority", "state", "authority_id"),
        Index("ix_state_permanently_failed_created", "state", "publish_permanently_failed", "created_at"),
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
    Runtime status for standalone workers using DB heartbeat.

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
    last_error = Column(Text, nullable=True)

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
    level1_json = Column(JSON, nullable=False)
    level1_cid = Column(String(100), nullable=True)   # Set by worker after IPFS store
    
    # Level 2 — original metadata (opaque content from client)
    original_content = Column(Text, nullable=False)
    original_schema = Column(String(50), nullable=False)  # "dublin_core", etc.
    original_cid = Column(String(100), nullable=True)     # Set by worker after IPFS store
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<ARKMetadata(id={self.id}, ark_record_id={self.ark_record_id}, schema={self.original_schema})>"
