"""
Company request/response Pydantic schemas.

Engineering Specification references:
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables
  Part 1, Section 1.2, Decision 4  — soft delete via deleted_at
  M2 Execution Plan, Section 2.2.3 — CompanyCreate, CompanyUpdate, CompanyResponse
  M2 Execution Plan, Section 6.4   — pagination contract: items, total, page, page_size, pages

Schemas:
  CompanyCreate         — POST /api/v1/companies request body
  CompanyUpdate         — PATCH /api/v1/companies/{id} request body (all fields optional)
  CompanyResponse       — read model returned for single-company responses
  CompanyListResponse   — paginated list envelope for GET /api/v1/companies
  CompanyResolveResponse — GET /api/v1/companies/resolve response (M3.2)

Validation highlights:
  - Ticker is uppercased and stripped (normalised at input).
  - CIK is validated as 1–10 digits and zero-padded to exactly 10 characters.
  - Website URL is length-constrained to match the VARCHAR(500) column.
  - Ticker and name have minimum length of 1 (non-empty string).

Milestones:
  M2-Step 4  — CompanyCreate, CompanyUpdate, CompanyResponse, CompanyListResponse
  M3.2       — CompanyResolveResponse
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Write models (create / update)
# ---------------------------------------------------------------------------


class CompanyCreate(BaseModel):
    """
    Request body for POST /api/v1/companies.

    All required fields must be provided.  Optional fields default to None
    and are not written to the database if omitted.
    """

    name: str = Field(
        min_length=1,
        max_length=255,
        description="Full legal or trading name of the company.",
        examples=["Apple Inc.", "Microsoft Corporation"],
    )
    ticker: str = Field(
        min_length=1,
        max_length=20,
        description=(
            "Stock ticker symbol.  Will be uppercased and stripped. "
            "Must be unique within the tenant workspace."
        ),
        examples=["AAPL", "MSFT"],
    )
    cik: str | None = Field(
        default=None,
        description=(
            "SEC Central Index Key — 1 to 10 digits.  "
            "Will be zero-padded to exactly 10 characters on input.  "
            "Example: '320193' becomes '0000320193'."
        ),
        examples=["0000320193", "789019"],
    )
    exchange: str | None = Field(
        default=None,
        max_length=50,
        description="Primary listing exchange (e.g. 'NYSE', 'NASDAQ', 'OTC').",
        examples=["NASDAQ", "NYSE"],
    )
    sector: str | None = Field(
        default=None,
        max_length=100,
        description="GICS sector classification.",
        examples=["Information Technology"],
    )
    industry: str | None = Field(
        default=None,
        max_length=100,
        description="GICS industry classification.",
        examples=["Technology Hardware, Storage & Peripherals"],
    )
    description: str | None = Field(
        default=None,
        description="Free-text company description.",
    )
    website: str | None = Field(
        default=None,
        max_length=500,
        description="Corporate website URL.",
        examples=["https://www.apple.com"],
    )

    @field_validator("ticker")
    @classmethod
    def _normalise_ticker(cls, v: str) -> str:
        """Strip whitespace and uppercase the ticker symbol."""
        return v.strip().upper()

    @field_validator("cik")
    @classmethod
    def _validate_and_pad_cik(cls, v: str | None) -> str | None:
        """
        Validate that CIK is numeric and zero-pad to 10 characters.

        Accepts '320193' and returns '0000320193'.
        Rejects non-digit characters and values longer than 10 digits.
        """
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if not re.fullmatch(r"\d{1,10}", stripped):
            raise ValueError(
                "CIK must consist of 1 to 10 digits only. "
                f"Received: {v!r}"
            )
        return stripped.zfill(10)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        """Strip surrounding whitespace from the company name."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Company name must not be blank.")
        return stripped


class CompanyUpdate(BaseModel):
    """
    Request body for PATCH /api/v1/companies/{id}.

    All fields are optional.  Only fields explicitly set in the request
    body are written to the database — Pydantic's model_fields_set is used
    by the repository to build the UPDATE statement.

    Setting a nullable field to ``null`` in JSON clears that field.
    Setting ``is_active`` to ``false`` soft-disables the company without
    deleting it (for full soft-delete, use DELETE /companies/{id}).
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Updated company name.",
    )
    ticker: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Updated ticker symbol.  Will be uppercased and stripped.",
    )
    cik: str | None = Field(
        default=None,
        description="Updated SEC CIK.  Zero-padded to 10 digits.",
    )
    exchange: str | None = Field(
        default=None,
        max_length=50,
        description="Updated listing exchange.",
    )
    sector: str | None = Field(
        default=None,
        max_length=100,
        description="Updated GICS sector.",
    )
    industry: str | None = Field(
        default=None,
        max_length=100,
        description="Updated GICS industry.",
    )
    description: str | None = Field(
        default=None,
        description="Updated company description.",
    )
    website: str | None = Field(
        default=None,
        max_length=500,
        description="Updated corporate website URL.",
    )
    is_active: bool | None = Field(
        default=None,
        description=(
            "Set to false to soft-disable the company.  "
            "Soft-disabled companies are hidden from normal list queries "
            "but their job history is retained."
        ),
    )

    @field_validator("ticker")
    @classmethod
    def _normalise_ticker(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip().upper()

    @field_validator("cik")
    @classmethod
    def _validate_and_pad_cik(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if not re.fullmatch(r"\d{1,10}", stripped):
            raise ValueError(
                "CIK must consist of 1 to 10 digits only. "
                f"Received: {v!r}"
            )
        return stripped.zfill(10)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("Company name must not be blank.")
        return stripped

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> CompanyUpdate:
        """Reject empty PATCH bodies — at least one field must be supplied."""
        if not self.model_fields_set:
            raise ValueError(
                "At least one field must be provided in a PATCH request."
            )
        return self


# ---------------------------------------------------------------------------
# Read models (responses)
# ---------------------------------------------------------------------------


class CompanyResponse(BaseModel):
    """
    Response schema for a single company.

    Returned by:
      - POST   /api/v1/companies          (201)
      - GET    /api/v1/companies/{id}     (200)
      - PATCH  /api/v1/companies/{id}     (200)

    ``from_attributes=True`` allows instantiation directly from a SQLAlchemy
    ORM model instance (Pydantic v2 replacement for ``orm_mode=True``).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="UUID v7 primary key.")
    tenant_id: uuid.UUID = Field(description="Owning tenant workspace UUID.")
    name: str = Field(description="Company name.")
    ticker: str = Field(description="Ticker symbol (uppercased).")
    cik: str | None = Field(description="SEC Central Index Key (10-digit, zero-padded).")
    exchange: str | None = Field(description="Primary listing exchange.")
    sector: str | None = Field(description="GICS sector.")
    industry: str | None = Field(description="GICS industry.")
    description: str | None = Field(description="Free-text company description.")
    website: str | None = Field(description="Corporate website URL.")
    is_active: bool = Field(description="False = soft-disabled.")
    created_at: datetime = Field(description="ISO 8601 creation timestamp (UTC).")
    updated_at: datetime = Field(description="ISO 8601 last-update timestamp (UTC).")


class CompanyListResponse(BaseModel):
    """
    Paginated response envelope for GET /api/v1/companies.

    Pagination contract (M2 Execution Plan, Section 6.4):
      page       — 1-based current page number
      page_size  — number of items per page (default 20, max 100)
      total      — total item count across all pages
      pages      — total page count (ceil(total / page_size))
      items      — items on the current page
    """

    items: list[CompanyResponse] = Field(
        description="Companies on the current page."
    )
    total: int = Field(
        ge=0,
        description="Total number of matching companies across all pages.",
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
        Auto-compute ``pages`` from ``total`` and ``page_size`` when not
        provided.  Callers may omit ``pages`` and let Pydantic compute it.
        """
        if isinstance(data, dict) and "pages" not in data:
            total = data.get("total", 0)
            page_size = data.get("page_size", 1)
            data["pages"] = math.ceil(total / page_size) if page_size else 0
        return data


# ---------------------------------------------------------------------------
# Company Resolver response (M3.2)
# ---------------------------------------------------------------------------


class CompanyResolveResponse(BaseModel):
    """
    Response schema for GET /api/v1/companies/resolve.

    Returns canonical company identification data resolved from an external
    provider (SEC EDGAR for US tickers). Cached in Redis by the resolver
    service; the response always reflects the most recently cached or freshly
    resolved values.

    Milestone: M3.2 — Company Resolver
    """

    ticker: str = Field(
        description="Normalised uppercase ticker symbol (e.g. 'AAPL').",
        examples=["AAPL"],
    )
    company_name: str = Field(
        description="Full legal company name from the data provider.",
        examples=["Apple Inc."],
    )
    cik: str = Field(
        description=(
            "SEC Central Index Key — 10-digit zero-padded string. "
            "Primary identifier for SEC EDGAR filing lookups."
        ),
        examples=["0000320193"],
    )
    exchange: str | None = Field(
        default=None,
        description=(
            "Primary listing exchange when available (e.g. 'Nasdaq', 'NYSE'). "
            "None when the data provider does not return exchange information."
        ),
        examples=["Nasdaq", None],
    )
    country: str = Field(
        description="ISO 3166-1 alpha-2 country code of the primary regulator.",
        examples=["US"],
    )
