"""
Request models for dARK Core API.

Pydantic models for validating incoming API requests.
"""

from typing import Optional
from pydantic import BaseModel, ConfigDict, Field








# =============================================================================
# New ARK Lifecycle Requests
# =============================================================================


class ReserveARKRequest(BaseModel):
    """Request to reserve a new ARK."""

    model_config = ConfigDict(extra="forbid")
    
    authority_id: str = Field(
        ...,
        description="Authority UUID requesting the reservation"
    )
    naan: str = Field(
        ...,
        description="NAAN to use for the ARK"
    )

class ReserveBatchItem(BaseModel):
    """Item for batch reservation."""

    model_config = ConfigDict(extra="forbid")
    target: Optional[str] = Field(
        None,
        description="Initial target URL (optional)"
    )
    client_item_id: str = Field(
        ...,
        description="Client-side ID for correlation (required in batch)"
    )



class ReserveBatchRequest(BaseModel):
    """Request to reserve multiple ARKs."""

    model_config = ConfigDict(extra="forbid")
    
    authority_id: str = Field(
        ...,
        description="Authority UUID requesting the reservation"
    )
    naan: str = Field(
        ...,
        description="NAAN to use for the ARKs"
    )
    items: list[ReserveBatchItem] = Field(
        ...,
        description="List of items to reserve (can be empty objects to just get IDs)",
        min_length=1
    )


class UpdateARKMetadataRequest(BaseModel):
    """
    Request to update ARK metadata and transition to DRAFT state.
    
    The client provides both:
    1. Level 1 (minimal extracted fields) as a JSON object
    2. Level 2 (original record) as a raw string
    """

    model_config = ConfigDict(extra="forbid")
    
    authority_id: str = Field(..., description="Authority UUID")
    target: str = Field(..., description="Target URL for resolution")
    
    # Level 1 — client-provided minimal JSON
    level1_metadata: dict = Field(
        ...,
        description="Minimal metadata extracted from original record (validated against Level1Metadata schema)"
    )
    
    # Level 2 — client-provided original metadata
    original_metadata: str = Field(
        ...,
        description="Raw content of the original metadata record (XML/JSON/Text)"
    )
    metadata_schema: str = Field(
        ...,
        description="Schema type of the original metadata (dublin_core, datacite, etc.)"
    )
