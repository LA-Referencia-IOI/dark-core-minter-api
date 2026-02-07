"""
Database ORM models.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Enum,
    Index,
    JSON,
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
    """
    
    __tablename__ = "ark_records"
    
    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # ARK identifiers
    ark = Column(String(255), unique=True, nullable=False, index=True)
    naan = Column(String(50), nullable=False)
    name = Column(String(100), nullable=False)
    
    # State tracking
    state = Column(String(1), nullable=False, index=True)
    
    # Authority/ownership
    authority_id = Column(String(255), nullable=False, index=True)
    
    # ARK data
    target = Column(Text, nullable=True)
    metadata_cid = Column(String(100), nullable=True)
    metadata_json = Column(JSON, nullable=True)
    alternate_identifiers = Column(JSON, nullable=True)
    
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
    publish_permanently_failed = Column(Integer, default=0, nullable=False, server_default="0")  # SQLite uses 0/1 for boolean
    
    # Composite indexes for common queries
    __table_args__ = (
        Index("ix_state_authority", "state", "authority_id"),
        Index("ix_state_permanently_failed_created", "state", "publish_permanently_failed", "created_at"),
    )
    
    def __repr__(self):
        return f"<ARKRecord(ark={self.ark}, state={self.state}, authority={self.authority_id})>"
