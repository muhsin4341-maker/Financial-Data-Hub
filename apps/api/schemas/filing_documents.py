"""
Pydantic schemas for StoredDocument — M3.6 S3 Storage Pipeline.

Follows the same pattern as schemas/filings.py:
  StoredDocumentCreate — write schema for repository.create().
  StoredDocumentRead   — response schema returned by service / repository.

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StoredDocumentCreate(BaseModel):
    """Input schema for creating a new StoredDocument record."""

    model_config = ConfigDict(from_attributes=True)

    accession_number: str = Field(
        ...,
        min_length=1,
        max_length=25,
        description="SEC EDGAR accession number ('XXXXXXXXXX-YY-ZZZZZZ').",
    )
    storage_type: str = Field(
        ...,
        description="Backend type: 'local' or 's3'.",
    )
    bucket_name: str | None = Field(
        default=None,
        description="S3 bucket name; None for local storage.",
    )
    object_key: str = Field(
        ...,
        description="Storage key within the backend.",
    )
    content_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="SHA-256 hex digest of the stored content.",
    )
    content_length: int = Field(
        ...,
        ge=0,
        description="Byte length of the stored content.",
    )
    mime_type: str = Field(
        ...,
        description="MIME type, e.g. 'text/html'.",
    )
    stored_at: datetime = Field(
        ...,
        description="UTC timestamp of first storage.",
    )
    filing_id: uuid.UUID | None = Field(
        default=None,
        description="Optional FK to the filings table.",
    )


class StoredDocumentRead(BaseModel):
    """Response schema for a stored document metadata record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    accession_number: str
    storage_type: str
    bucket_name: str | None
    object_key: str
    content_hash: str
    content_length: int
    mime_type: str
    stored_at: datetime
    filing_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
