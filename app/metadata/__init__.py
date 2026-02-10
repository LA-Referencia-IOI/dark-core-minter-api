"""
Metadata schema definitions for two-level ARK metadata.

Level 1: Fixed-schema JSON with minimal extracted fields.
Level 2: Original metadata record (Dublin Core, DataCite, etc.).
"""

from app.metadata.schemas import Level1Metadata, OriginalMetadataRef, MetadataSchemaType

__all__ = ["Level1Metadata", "OriginalMetadataRef", "MetadataSchemaType"]
