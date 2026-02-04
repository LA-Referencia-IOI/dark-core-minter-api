"""
Request models for dARK Core API.

Pydantic models for validating incoming API requests.
"""

from typing import Optional
from typing import Optional, Any
from pydantic import BaseModel, Field, HttpUrl
from app.models.states import ARKState








# =============================================================================
# New ARK Lifecycle Requests
# =============================================================================


class AlternateIdentifier(BaseModel):
    """Alternate identifier (e.g. DOI, OAI)."""
    schema_: str = Field(..., alias="schema", description="Identifier schema (doi, oai, etc.)")
    value: str = Field(..., description="Identifier value")


class ReserveARKRequest(BaseModel):
    """Request to reserve a new ARK."""
    
    authority_id: str = Field(
        ...,
        description="Authority UUID requesting the reservation"
    )
    naan: str = Field(
        ...,
        description="NAAN to use for the ARK"
    )

    alternate_identifiers: Optional[list[AlternateIdentifier]] = Field(
        None,
        description="External identifiers (stored in IPFS metadata)"
    )


class ReserveBatchItem(BaseModel):
    """Item for batch reservation."""
    target: Optional[str] = Field(
        None,
        description="Initial target URL (optional)"
    )
    alternate_identifiers: Optional[list[AlternateIdentifier]] = Field(
        None,
        description="External identifiers (stored in IPFS metadata)"
    )
    client_item_id: str = Field(
        ...,
        description="Client-side ID for correlation (required in batch)"
    )



class ReserveBatchRequest(BaseModel):
    """Request to reserve multiple ARKs."""
    
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
    """Request to update ARK metadata and promote to DRAFT."""
    
    authority_id: str = Field(
        ...,
        description="Authority UUID"
    )
    target: str = Field(
        ...,
        description="Target URL for resolution"
    )
    metadata: Any = Field(
        ...,
        description="Metadata payload (JSON/Dict). Will be stored opaquely."
    )
    alternate_identifiers: Optional[list[AlternateIdentifier]] = Field(
        None,
        description="External identifiers to update/add"
    )

