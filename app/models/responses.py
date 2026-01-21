"""
Response models for dARK Core API.

Pydantic models for API responses.
"""

from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field


# =============================================================================
# Lookup Responses
# =============================================================================

class URLLookupResponse(BaseModel):
    """Response from URL lookup."""
    
    exists: bool = Field(
        ...,
        description="Whether an ARK exists for this URL"
    )
    dark_id: Optional[str] = Field(
        None,
        description="Full ARK identifier if exists",
        examples=["ark:/12345/xk9a2b7"]
    )
    naan: Optional[str] = Field(
        None,
        description="NAAN component if exists"
    )
    name: Optional[str] = Field(
        None,
        description="Name component if exists"
    )


# =============================================================================
# Mint Responses
# =============================================================================

class MintItemResult(BaseModel):
    """Result for a single item in batch mint."""
    
    dark_id: Optional[str] = Field(
        None,
        description="Full ARK identifier"
    )
    name: Optional[str] = Field(
        None,
        description="Name (for correlation on errors)"
    )
    status: Literal["success", "authorization_error", "already_exists", "transient_error", "error"] = Field(
        ...,
        description="Result status"
    )
    transaction_ref: Optional[str] = Field(
        None,
        description="Blockchain transaction hash on success"
    )
    error: Optional[str] = Field(
        None,
        description="Error message if failed"
    )


class BatchMintResponse(BaseModel):
    """Response from batch mint operation."""
    
    status: Literal["ok", "partial_success", "error"] = Field(
        ...,
        description="Overall batch status"
    )
    results: list[MintItemResult] = Field(
        ...,
        description="Per-item results"
    )


# =============================================================================
# ARK Responses
# =============================================================================

class ARKResolveResponse(BaseModel):
    """Response from ARK resolution."""
    
    ark_id: str = Field(
        ...,
        description="Full ARK identifier"
    )
    url: str = Field(
        ...,
        description="URL the ARK resolves to"
    )
    cid: str = Field(
        ...,
        description="Content identifier"
    )


class ARKExistsResponse(BaseModel):
    """Response for ARK existence check."""
    
    exists: bool
    ark_id: str


class ARKInfoResponse(BaseModel):
    """Full ARK information response."""
    
    ark_id: str
    naan: str
    name: str
    url: str
    cid: str
    owner: str = Field(
        ...,
        description="Owner wallet address"
    )
    created_at: datetime
    updated_at: datetime


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
