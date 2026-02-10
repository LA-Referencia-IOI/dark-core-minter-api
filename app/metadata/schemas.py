"""
Two-level metadata schema definitions.

Level 1 (L1): Fixed JSON schema with minimal fields, provided by client.
Level 2 (L2): Original metadata record (opaque to API).

The client provides both levels. The API validates L1 structure,
the worker stores both in IPFS and injects the L2 CID into L1.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Metadata schema type enum
# ---------------------------------------------------------------------------

class MetadataSchemaType(str, Enum):
    """Supported original-metadata schema types."""

    DUBLIN_CORE = "dublin_core"
    DATACITE = "datacite"
    OPENAIRE4 = "openaire4"
    JATS = "jats"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class AlternateIdentifierL1(BaseModel):
    """Alternate identifier inside Level-1 metadata."""

    schema_: str = Field(..., alias="schema", description="Identifier schema (doi, oai, etc.)")
    value: str = Field(..., description="Identifier value")

    model_config = {"populate_by_name": True}


class OriginalMetadataRef(BaseModel):
    """Reference to the original (Level-2) metadata stored in IPFS."""

    schema_: str = Field(
        ...,
        alias="schema",
        description="Original metadata schema (dublin_core, datacite, etc.)",
    )
    cid: Optional[str] = Field(
        None,
        description="CID of the original metadata in IPFS (set by worker)",
    )

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Level-1 Metadata (fixed JSON schema)
# ---------------------------------------------------------------------------

LEVEL1_SCHEMA_URI = "https://dark.la-referencia.info/schemas/ark-metadata/v1"
LEVEL1_SCHEMA_VERSION = "1.0"


class Level1Metadata(BaseModel):
    """
    Level-1 minimal metadata record.

    Provided by the client with extracted fields from the original record.
    The ``original_metadata.cid`` is injected by the worker after storing L2.
    """

    # Schema identification
    schema_uri: str = Field(
        default=LEVEL1_SCHEMA_URI,
        alias="$schema",
        description="JSON schema URI",
    )
    schema_version: str = Field(
        default=LEVEL1_SCHEMA_VERSION,
        description="Schema version",
    )

    # ARK identifier (auto-set by the API)
    ark: Optional[str] = Field(None, description="Full ARK identifier (set by API)")

    # ---- Required minimal fields ----
    title: str = Field(..., description="Resource title")
    authors: list[str] = Field(..., min_length=1, description="List of author names")
    year: int = Field(..., description="Publication year")

    # ---- Optional fields ----
    publisher: Optional[str] = Field(None, description="Publisher name")
    resource_type: Optional[str] = Field(None, description="Resource type (article, dataset, etc.)")
    language: Optional[str] = Field(None, description="Language code (ISO 639-1)")
    abstract: Optional[str] = Field(None, description="Resource abstract or description")
    subjects: Optional[list[str]] = Field(None, description="Subject keywords")
    rights: Optional[str] = Field(None, description="Rights or license identifier")

    # ---- Identifiers & URLs ----
    alternate_identifiers: Optional[list[AlternateIdentifierL1]] = Field(
        None, description="Alternate identifiers (DOI, OAI, etc.)"
    )
    alternate_urls: Optional[list[str]] = Field(
        None, description="Alternate access URLs"
    )

    # ---- Reference to Level-2 metadata ----
    original_metadata: OriginalMetadataRef = Field(
        ..., description="Reference to original metadata record"
    )

    # ---- Timestamps (auto-set) ----
    created_at: Optional[datetime] = Field(None, description="Creation timestamp (set by API)")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp (set by API)")

    model_config = {"populate_by_name": True}
