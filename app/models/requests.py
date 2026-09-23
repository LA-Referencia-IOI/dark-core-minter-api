"""
Request models for dARK Core API.

Pydantic models for validating incoming API requests.
"""

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


class ARKStatusBatchRequest(BaseModel):
    """Version 1 request for a bounded batch ARK-status lookup."""

    model_config = ConfigDict(extra="forbid")

    arks: list[str] = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Full ARK identifiers to resolve. Each value is independently "
            "validated, so an invalid ARK is reported in its result without "
            "aborting the batch."
        ),
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
    
    # Minimal metadata — client-provided minimal JSON (Level 1)
    minimal_metadata: dict = Field(
        ...,
        description="Minimal metadata extracted from original record (validated against Level1Metadata schema)",
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
    metadata_media_type: str = Field(
        ...,
        description="MIME type for the original metadata (application/xml, application/json, etc.)",
    )
