"""
Source Registry request/response Pydantic schemas.

Engineering Specification references:
  M3 Execution Plan, Section 6.1   — source_configs table design
  M3 Execution Plan, M3.1          — Source Registry milestone

Schemas:
  SourceConfigCreate       — POST /api/v1/sources request body
  SourceConfigUpdate       — PATCH /api/v1/sources/{id} request body (all optional)
  SourceConfigResponse     — read model returned for single-source responses
  SourceConfigListResponse — paginated list envelope for GET /api/v1/sources

Validation highlights:
  - code is uppercased and stripped (normalised at input); immutable after creation.
  - provider_type must be one of the ProviderType enum values.
  - rate_limit_per_minute must be >= 1.
  - config is an optional freeform dict — validated only for JSON well-formedness.

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.api.models import ProviderType

# ---------------------------------------------------------------------------
# Write models (create / update)
# ---------------------------------------------------------------------------


class SourceConfigCreate(BaseModel):
    """
    Request body for POST /api/v1/sources.

    All required fields must be provided.  Optional fields default to None
    and are written to the database as NULL when omitted.
    """

    code: str = Field(
        min_length=1,
        max_length=50,
        description=(
            "Machine-readable identifier. Will be uppercased and stripped. "
            "Must be globally unique. Examples: SEC_EDGAR, NSE, BSE, MANUAL_UPLOAD."
        ),
        examples=["SEC_EDGAR", "NSE", "BSE"],
    )
    name: str = Field(
        min_length=1,
        max_length=100,
        description="Human-readable display name.",
        examples=["SEC EDGAR", "NSE India", "BSE India"],
    )
    description: str | None = Field(
        default=None,
        description="Optional free-text description of the data source.",
    )
    provider_type: str = Field(
        description=(
            "Category of provider. "
            "Allowed values: regulatory | exchange | manual | broker."
        ),
        examples=["regulatory", "exchange"],
    )
    country_code: str | None = Field(
        default=None,
        max_length=5,
        description="ISO 3166-1 alpha-2 country code. Omit for multi-country sources.",
        examples=["US", "IN"],
    )
    base_url: str | None = Field(
        default=None,
        max_length=500,
        description="Root URL used by the acquisition service for HTTP requests.",
        examples=["https://efts.sec.gov", "https://www.nseindia.com"],
    )
    rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        description="Maximum HTTP requests per minute. Default: 60.",
        examples=[60, 600, 10],
    )
    is_active: bool = Field(
        default=True,
        description="Whether the source is enabled for acquisition. Default: True.",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Source-specific configuration blob (freeform JSON). "
            "Use for URLs, flags, and metadata that do not fit standard columns."
        ),
    )

    @field_validator("code")
    @classmethod
    def _normalise_code(cls, v: str) -> str:
        """Strip whitespace, uppercase, and replace spaces with underscores."""
        normalised = v.strip().upper().replace(" ", "_")
        if not normalised:
            raise ValueError("Source code must not be blank.")
        return normalised

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        """Strip surrounding whitespace from the name."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Source name must not be blank.")
        return stripped

    @field_validator("provider_type")
    @classmethod
    def _validate_provider_type(cls, v: str) -> str:
        """Validate that provider_type is one of the known ProviderType values."""
        allowed = {pt.value for pt in ProviderType}
        normalised = v.strip().lower()
        if normalised not in allowed:
            raise ValueError(
                f"provider_type must be one of: {sorted(allowed)}. "
                f"Received: {v!r}"
            )
        return normalised

    @field_validator("country_code")
    @classmethod
    def _normalise_country_code(cls, v: str | None) -> str | None:
        """Uppercase and strip the country code."""
        if v is None:
            return None
        stripped = v.strip().upper()
        return stripped if stripped else None


class SourceConfigUpdate(BaseModel):
    """
    Request body for PATCH /api/v1/sources/{id}.

    All fields are optional.  Only fields explicitly set in the request body are
    written to the database — Pydantic's model_fields_set is used by the
    repository to build the partial UPDATE.

    NOTE: ``code`` is intentionally absent — it is immutable after creation.
    Attempting to rename a source code would break existing acquisition worker
    references.  Use the SourceRegistryService to enforce this invariant.
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Updated human-readable display name.",
    )
    description: str | None = Field(
        default=None,
        description="Updated free-text description.",
    )
    provider_type: str | None = Field(
        default=None,
        description="Updated provider type. Allowed: regulatory | exchange | manual | broker.",
    )
    country_code: str | None = Field(
        default=None,
        max_length=5,
        description="Updated ISO 3166-1 alpha-2 country code.",
    )
    base_url: str | None = Field(
        default=None,
        max_length=500,
        description="Updated root URL.",
    )
    rate_limit_per_minute: int | None = Field(
        default=None,
        ge=1,
        description="Updated requests-per-minute limit.",
    )
    is_active: bool | None = Field(
        default=None,
        description="Set to false to disable. Prefer POST /{id}/disable endpoint.",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description="Replacement config blob. Entire object is replaced, not merged.",
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("Source name must not be blank.")
        return stripped

    @field_validator("provider_type")
    @classmethod
    def _validate_provider_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        allowed = {pt.value for pt in ProviderType}
        normalised = v.strip().lower()
        if normalised not in allowed:
            raise ValueError(
                f"provider_type must be one of: {sorted(allowed)}. "
                f"Received: {v!r}"
            )
        return normalised

    @field_validator("country_code")
    @classmethod
    def _normalise_country_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip().upper()
        return stripped if stripped else None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> SourceConfigUpdate:
        """Reject empty PATCH bodies — at least one field must be supplied."""
        if not self.model_fields_set:
            raise ValueError(
                "At least one field must be provided in a PATCH request."
            )
        return self


# ---------------------------------------------------------------------------
# Read models (responses)
# ---------------------------------------------------------------------------


class SourceConfigResponse(BaseModel):
    """
    Response schema for a single source config.

    Returned by:
      - POST   /api/v1/sources          (201)
      - GET    /api/v1/sources/{id}     (200)
      - PATCH  /api/v1/sources/{id}     (200)
      - POST   /api/v1/sources/{id}/enable   (200)
      - POST   /api/v1/sources/{id}/disable  (200)

    ``from_attributes=True`` allows instantiation directly from a SQLAlchemy
    ORM model instance (Pydantic v2 replacement for ``orm_mode=True``).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="UUID v7 primary key.")
    code: str = Field(description="Machine-readable identifier (uppercased, unique).")
    name: str = Field(description="Human-readable display name.")
    description: str | None = Field(description="Optional free-text description.")
    provider_type: str = Field(description="Provider category (regulatory|exchange|manual|broker).")
    country_code: str | None = Field(description="ISO 3166-1 alpha-2 country code.")
    base_url: str | None = Field(description="Root URL for acquisition HTTP requests.")
    rate_limit_per_minute: int = Field(description="Max requests per minute.")
    is_active: bool = Field(description="False = disabled; acquisition service skips this source.")
    config: dict[str, Any] | None = Field(description="Source-specific configuration blob.")
    created_at: datetime = Field(description="ISO 8601 creation timestamp (UTC).")
    updated_at: datetime = Field(description="ISO 8601 last-update timestamp (UTC).")


class SourceConfigListResponse(BaseModel):
    """
    Paginated response envelope for GET /api/v1/sources.

    Pagination contract matches M2 convention (Section 6.4):
      page       — 1-based current page number
      page_size  — items per page (default 20, max 100)
      total      — total item count across all pages
      pages      — total page count (ceil(total / page_size))
      items      — items on the current page
    """

    items: list[SourceConfigResponse] = Field(
        description="Source configs on the current page."
    )
    total: int = Field(
        ge=0,
        description="Total number of matching source configs across all pages.",
    )
    page: int = Field(
        ge=1,
        description="Current page number (1-based).",
    )
    page_size: int = Field(
        ge=1,
        le=100,
        description="Items per page.",
    )
    pages: int = Field(
        ge=0,
        description="Total number of pages (ceil(total / page_size)).",
    )

    @model_validator(mode="before")
    @classmethod
    def _compute_pages(cls, data: Any) -> Any:
        """
        Auto-compute ``pages`` from ``total`` and ``page_size`` when not provided.
        Callers may omit ``pages`` and let Pydantic compute it.
        """
        if isinstance(data, dict) and "pages" not in data:
            total = data.get("total", 0)
            page_size = data.get("page_size", 1)
            data["pages"] = math.ceil(total / page_size) if page_size else 0
        return data
