"""
Request models for dARK Core API.

Pydantic models for validating incoming API requests.
"""

from typing import Optional
from pydantic import BaseModel, Field, HttpUrl


class URLLookupRequest(BaseModel):
    """Request to look up an ARK by URL."""
    
    url: str = Field(
        ...,
        description="URL to look up",
        examples=["https://repository.example.edu/handle/1234/5678"]
    )


class MintItem(BaseModel):
    """Single item in a batch mint request."""
    
    authority_id: str = Field(
        ...,
        description="Authority UUID (must be registered and authorized for NAAN)",
        examples=["6b7f1d3a-9f9b-4d1f-bc4c-1a2b3c4d5e6f"]
    )
    naan: str = Field(
        ...,
        description="NAAN for the ARK",
        examples=["12345"]
    )
    name: str = Field(
        ...,
        description="Name/identifier within NAAN",
        examples=["xk9a2b7"]
    )
    url: str = Field(
        ...,
        description="URL the ARK resolves to",
        examples=["https://repository.example.edu/doc/001"]
    )
    cid: str = Field(
        ...,
        description="Content identifier (e.g., IPFS CID of stable record)",
        examples=["bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper"]
    )


class BatchMintRequest(BaseModel):
    """Request to mint multiple ARKs in a batch."""
    
    items: list[MintItem] = Field(
        ...,
        description="List of ARKs to mint",
        min_length=1,
    )


class UpdateARKRequest(BaseModel):
    """Request to update an existing ARK."""
    
    authority_id: str = Field(
        ...,
        description="Authority UUID (must be the original owner)"
    )
    url: str = Field(
        ...,
        description="New URL the ARK resolves to"
    )
    cid: str = Field(
        ...,
        description="New content identifier"
    )
