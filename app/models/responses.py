"""
Response models for dARK Core API.

Pydantic models for API responses.
"""

from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field
from app.models.states import ARKState






# =============================================================================
# ARK Lifecycle Responses
# =============================================================================

class ARKResponse(BaseModel):
    """Full ARK record response."""
    
    ark: str = Field(..., description="Full ARK identifier")
    state: ARKState = Field(..., description="Current lifecycle state")
    target: Optional[str] = Field(None, description="Target URL")
    metadata_cid: Optional[str] = Field(None, description="IPFS CID of metadata (if published)")
    alternate_identifiers: Optional[list] = Field(None, description="External identifiers")
    client_item_id: Optional[str] = Field(None, description="Client correlation ID")
    
    # Timestamps could be added here if we track them in DB/Orchestrator
    
    class Config:
        use_enum_values = True


class ARKBatchResponse(BaseModel):
    """Response for batch reservation."""
    
    results: list[ARKResponse] = Field(..., description="List of created/reserved ARKs")








# =============================================================================
# Authority Responses
# =============================================================================

class AuthorityResponse(BaseModel):
    """Authority information response."""
    
    uuid: str
    wallet_address: str
    naans: list[str] = Field(
        ...,
        description="List of authorized NAANs"
    )
    active: bool


class AuthorityNAANsResponse(BaseModel):
    """List of authorized NAANs for an authority."""
    
    uuid: str
    naans: list[str]


class AuthorityAuthorizedResponse(BaseModel):
    """Authorization check response."""
    
    uuid: str
    naan: str
    authorized: bool


# =============================================================================
# Error Responses
# =============================================================================

class ErrorResponse(BaseModel):
    """Standard error response."""
    
    error: str = Field(
        ...,
        description="Error code"
    )
    message: str = Field(
        ...,
        description="Human-readable error message"
    )
    retryable: bool = Field(
        False,
        description="Whether the operation can be retried"
    )
    details: Optional[dict] = Field(
        None,
        description="Additional error details"
    )
