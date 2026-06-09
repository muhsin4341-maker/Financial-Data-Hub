"""
Filing request/response Pydantic schemas.

Engineering Specification references:
  M3 Execution Plan, M3.3  — Filing Models milestone

Schemas:
  FilingCreate       — create request body (used by acquisition workers, not a public API)
  FilingUpdate       — partial update (status transitions, url population, metadata)
  FilingRead         — read model returned for single-filing responses
  FilingListResponse — paginated list envelope

Validation highlights:
  - accession_number is validated for SEC EDGAR format and normalised.
  - filing_type must be one of the known FilingType enum values.
  - status must be one of the known FilingStatus enum values.
  - cik is validated as 1–10 digits and zero-padded to exactly 10 characters.
  - period_end_date is optional (NULL for 8-K and other non-period filings).

Milestone: M3.3 — Filing Models
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.api.models import FilingStatus, FilingType

# ---------------------------------------------------------------------------
# Write models (create / update)
# ---------------------------------------------------------------------------

# Known filing type values (string representations).
_VALID_FILING_TYPES: frozenset[str] = frozenset(ft.value for ft in FilingType)

# Known status values.
_VALID_STATUSES: frozenset[str] = frozenset(fs.value for fs in FilingStatus)

# SEC EDGAR accession number pattern: XXXXXXXXXX-YY-ZZZZZZ
# Where XXXXXXXXXX is the CIK (zero-padded to 10 digits),
# YY is the two-digit year, and ZZZZZZ is a 6-digit sequence number.
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


class FilingCreate(BaseModel):
    """
    Request body for creating a Filing record.

    Used by acquisition workers (M3.4, M3.7) to persist newly discovered
    filings.  All fields required by the database schema as NOT NULL must be
    provided; nullable fields default to None.

    Business rules enforced here:
      BR-1: accession_number is validated and normalised.
      BR-3: filing_type must be a known FilingType value.
    """

    filing_type: str = Field(
        description=(
            "SEC form type. "
            f"Allowed values: {sorted(_VALID_FILING_TYPES)}."
        ),
        examples=["10-K", "10-Q", "8-K"],
    )
    accession_number: str = Field(
        max_length=25,
        description=(
            "SEC EDGAR accession number. "
            "Format: 'XXXXXXXXXX-YY-ZZZZZZ' (e.g. '0000320193-23-000077'). "
            "Globally unique — duplicate will raise ConflictError."
        ),
        examples=["0000320193-23-000077"],
    )
    filing_date: date = Field(
        description="Date on which the filing was submitted to SEC EDGAR.",
        examples=["2023-11-03"],
    )
    cik: str = Field(
        description=(
            "SEC Central Index Key — 1 to 10 digits. "
            "Zero-padded to exactly 10 characters on input."
        ),
        examples=["0000320193", "320193"],
    )

    # Optional fields
    company_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "UUID of the linked company record. "
            "NULL until the filing is matched to a tenant company."
        ),
    )
    source_config_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "UUID of the source config that provided this filing. "
            "NULL if the source is not yet determined."
        ),
    )
    period_end_date: date | None = Field(
        default=None,
        description=(
            "End date of the fiscal period covered by this filing. "
            "NULL for 8-K and other non-periodic filings."
        ),
        examples=["2023-09-30", None],
    )
    ticker: str | None = Field(
        default=None,
        max_length=20,
        description="Ticker symbol at the time of filing.",
        examples=["AAPL", None],
    )
    title: str | None = Field(
        default=None,
        max_length=500,
        description="Human-readable filing title from SEC EDGAR.",
        examples=["Annual report [10-K]"],
    )
    filing_url: str | None = Field(
        default=None,
        max_length=2000,
        description="URL to the filing index page on SEC EDGAR.",
    )
    document_url: str | None = Field(
        default=None,
        max_length=2000,
        description="URL to the primary filing document.",
    )
    status: str = Field(
        default=FilingStatus.DISCOVERED.value,
        description=(
            "Initial lifecycle status. "
            f"Allowed values: {sorted(_VALID_STATUSES)}. "
            "Default: 'discovered'."
        ),
        examples=["discovered"],
    )
    # ── Fiscal period coordinates (M3.3) ──────────────────────────────────────
    # Both default to None — acquisition workers set these after XBRL extraction.
    fiscal_year: int | None = Field(
        default=None,
        ge=1900,
        le=2100,
        description=(
            "4-digit fiscal year (e.g. 2024). "
            "None until populated by the acquisition worker during XBRL extraction."
        ),
        examples=[2024, None],
    )
    fiscal_period: str | None = Field(
        default=None,
        max_length=10,
        description=(
            "Fiscal period label: 'FY', 'Q1', 'Q2', 'Q3', or 'Q4'. "
            "None until populated by the acquisition worker."
        ),
        examples=["FY", "Q1", None],
    )
    filing_metadata: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Arbitrary metadata from the SEC EDGAR API response that does not "
            "fit standard columns (e.g. form_type, items, file_number)."
        ),
    )

    @field_validator("filing_type")
    @classmethod
    def _validate_filing_type(cls, v: str) -> str:
        """Validate that filing_type is one of the known FilingType values."""
        stripped = v.strip()
        if stripped not in _VALID_FILING_TYPES:
            raise ValueError(
                f"filing_type must be one of: {sorted(_VALID_FILING_TYPES)}. "
                f"Received: {v!r}"
            )
        return stripped

    @field_validator("accession_number")
    @classmethod
    def _validate_accession_number(cls, v: str) -> str:
        """
        Validate and normalise the SEC EDGAR accession number.

        Accepts both dashed ('0000320193-23-000077') and stripped
        ('0000320193-23-000077') formats.  Returns the canonical dashed form.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("accession_number must not be blank.")
        if not _ACCESSION_RE.match(stripped):
            raise ValueError(
                "accession_number must match the SEC EDGAR format "
                "'XXXXXXXXXX-YY-ZZZZZZ' (e.g. '0000320193-23-000077'). "
                f"Received: {v!r}"
            )
        return stripped

    @field_validator("cik")
    @classmethod
    def _validate_and_pad_cik(cls, v: str) -> str:
        """Validate that CIK is numeric and zero-pad to 10 characters."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("cik must not be blank.")
        if not re.fullmatch(r"\d{1,10}", stripped):
            raise ValueError(
                "cik must consist of 1 to 10 digits only. "
                f"Received: {v!r}"
            )
        return stripped.zfill(10)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        """Validate that status is one of the known FilingStatus values."""
        stripped = v.strip().lower()
        if stripped not in _VALID_STATUSES:
            raise ValueError(
                f"status must be one of: {sorted(_VALID_STATUSES)}. "
                f"Received: {v!r}"
            )
        return stripped

    @field_validator("ticker")
    @classmethod
    def _normalise_ticker(cls, v: str | None) -> str | None:
        """Strip whitespace and uppercase the ticker symbol."""
        if v is None:
            return None
        stripped = v.strip().upper()
        return stripped if stripped else None


class FilingUpdate(BaseModel):
    """
    Request body for partially updating a Filing record.

    All fields are optional.  Only fields explicitly set in the request are
    written to the database — the repository uses ``model_fields_set`` to
    determine which fields the caller provided.

    NOTE: ``accession_number`` is intentionally absent — it is immutable
    after creation.  Changing the accession number would break referential
    integrity with the document store and audit trail.

    Typical use cases:
      - Status transition (e.g. 'discovered' → 'downloading').
      - Populating document_url after a successful download.
      - Linking company_id after company resolution.
      - Recording error details in filing_metadata on failure.
    """

    company_id: uuid.UUID | None = Field(
        default=None,
        description="Updated linked company UUID.",
    )
    source_config_id: uuid.UUID | None = Field(
        default=None,
        description="Updated source config UUID.",
    )
    status: str | None = Field(
        default=None,
        description=(
            f"Updated lifecycle status. Allowed: {sorted(_VALID_STATUSES)}."
        ),
    )
    document_url: str | None = Field(
        default=None,
        max_length=2000,
        description="Updated primary document URL (set after download).",
    )
    filing_url: str | None = Field(
        default=None,
        max_length=2000,
        description="Updated filing index URL.",
    )
    title: str | None = Field(
        default=None,
        max_length=500,
        description="Updated filing title.",
    )
    ticker: str | None = Field(
        default=None,
        max_length=20,
        description="Updated ticker symbol.",
    )
    period_end_date: date | None = Field(
        default=None,
        description="Updated fiscal period end date.",
    )
    # ── Fiscal period coordinates (M3.3) ──────────────────────────────────────
    # Set by the extraction pipeline after XBRL parsing (M3.9).
    fiscal_year: int | None = Field(
        default=None,
        ge=1900,
        le=2100,
        description=(
            "Updated 4-digit fiscal year. "
            "Set by the extraction pipeline after XBRL parsing."
        ),
        examples=[2024, None],
    )
    fiscal_period: str | None = Field(
        default=None,
        max_length=10,
        description=(
            "Updated fiscal period label: 'FY', 'Q1', 'Q2', 'Q3', or 'Q4'. "
            "Set by the extraction pipeline after XBRL parsing."
        ),
        examples=["Q3", None],
    )
    filing_metadata: dict[str, Any] | None = Field(
        default=None,
        description="Updated or merged metadata blob.",
    )

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip().lower()
        if stripped not in _VALID_STATUSES:
            raise ValueError(
                f"status must be one of: {sorted(_VALID_STATUSES)}. "
                f"Received: {v!r}"
            )
        return stripped

    @field_validator("ticker")
    @classmethod
    def _normalise_ticker(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip().upper()
        return stripped if stripped else None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> FilingUpdate:
        """Reject empty update bodies — at least one field must be supplied."""
        if not self.model_fields_set:
            raise ValueError(
                "At least one field must be provided in an update request."
            )
        return self


# ---------------------------------------------------------------------------
# Read models (responses)
# ---------------------------------------------------------------------------


class FilingRead(BaseModel):
    """
    Response schema for a single Filing record.

    Returned by FilingService read methods.
    ``from_attributes=True`` allows instantiation directly from SQLAlchemy
    ORM instances (Pydantic v2 replacement for ``orm_mode=True``).

    Milestone: M3.3 — Filing Models
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="UUID v7 primary key.")
    company_id: uuid.UUID | None = Field(
        description="Linked company UUID. None until company resolution."
    )
    source_config_id: uuid.UUID | None = Field(
        description="Source config UUID. None if source not yet determined."
    )
    filing_type: str = Field(
        description="SEC form type (e.g. '10-K', '10-Q', '8-K')."
    )
    accession_number: str = Field(
        description="SEC EDGAR accession number (globally unique)."
    )
    filing_date: date = Field(
        description="Date the filing was submitted to SEC EDGAR."
    )
    period_end_date: date | None = Field(
        description="Fiscal period end date. None for non-periodic filings."
    )
    cik: str = Field(
        description="SEC CIK — 10-digit zero-padded string."
    )
    ticker: str | None = Field(
        description="Ticker symbol at time of filing."
    )
    title: str | None = Field(
        description="Human-readable filing title."
    )
    filing_url: str | None = Field(
        description="URL to the filing index page on SEC EDGAR."
    )
    document_url: str | None = Field(
        description="URL to the primary filing document."
    )
    status: str = Field(
        description="Current lifecycle status."
    )
    # ── Fiscal period coordinates (M3.3) ──────────────────────────────────────
    fiscal_year: int | None = Field(
        description=(
            "4-digit fiscal year (e.g. 2024). "
            "None until populated by the extraction pipeline."
        )
    )
    fiscal_period: str | None = Field(
        description=(
            "Fiscal period label: 'FY', 'Q1', 'Q2', 'Q3', or 'Q4'. "
            "None until populated by the extraction pipeline."
        )
    )
    filing_metadata: dict[str, Any] | None = Field(
        description="Arbitrary metadata blob from the data source."
    )
    created_at: datetime = Field(
        description="ISO 8601 creation timestamp (UTC)."
    )
    updated_at: datetime = Field(
        description="ISO 8601 last-update timestamp (UTC)."
    )


class FilingListResponse(BaseModel):
    """
    Paginated response envelope for filing list queries.

    Pagination contract (consistent with M2 companies / M3.1 sources):
      page       — 1-based current page number
      page_size  — number of items per page (default 20, max 100)
      total      — total item count across all pages
      pages      — total page count (ceil(total / page_size))
      items      — items on the current page

    Milestone: M3.3 — Filing Models
    """

    items: list[FilingRead] = Field(
        description="Filing records on the current page."
    )
    total: int = Field(
        ge=0,
        description="Total number of matching filings across all pages.",
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
        """Auto-compute ``pages`` from ``total`` and ``page_size`` when not provided."""
        if isinstance(data, dict) and "pages" not in data:
            total = data.get("total", 0)
            page_size = data.get("page_size", 1)
            data["pages"] = math.ceil(total / page_size) if page_size else 0
        return data
