"""
Pydantic schemas for AcquisitionJob — M3.7 / M3.8.

Follows the same conventions as schemas/filings.py:
  AcquisitionJobCreate      — write schema for repository.create().
  AcquisitionJobUpdate      — partial update (status, counters, error).
  AcquisitionJobRead        — response schema returned by service / repository.
  AcquisitionJobListResponse — paginated list envelope for GET /acquisition/jobs.

Milestone: M3.7 — Acquisition Jobs
         M3.8 — Acquisition APIs (AcquisitionJobListResponse added)
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AcquisitionJobCreate(BaseModel):
    """Input schema for creating a new AcquisitionJob record."""

    model_config = ConfigDict(from_attributes=True)

    ticker: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Stock ticker symbol (e.g. 'AAPL').",
    )
    job_type: str = Field(
        default="sec_filing_discovery",
        description="Acquisition strategy identifier.",
    )

    @field_validator("ticker")
    @classmethod
    def _normalise_ticker(cls, v: str) -> str:
        return v.strip().upper()


class AcquisitionJobUpdate(BaseModel):
    """Partial update schema for AcquisitionJob — used internally by the service."""

    model_config = ConfigDict(from_attributes=True)

    status: str | None = Field(default=None)
    cik: str | None = Field(default=None)
    company_name: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    filings_discovered: int | None = Field(default=None, ge=0)
    filings_new: int | None = Field(default=None, ge=0)
    documents_fetched: int | None = Field(default=None, ge=0)
    documents_stored: int | None = Field(default=None, ge=0)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)


class AcquisitionJobRead(BaseModel):
    """Response schema for an AcquisitionJob record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticker: str
    cik: str | None
    company_name: str | None
    job_type: str
    status: str
    error_message: str | None
    filings_discovered: int
    filings_new: int
    documents_fetched: int
    documents_stored: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AcquisitionJobListResponse(BaseModel):
    """
    Paginated list envelope for GET /api/v1/acquisition/jobs.

    Pagination contract (consistent with FilingListResponse, JobListResponse):
      page       — 1-based current page number
      page_size  — items per page (default 20, max 100)
      total      — total item count across all pages
      pages      — total page count (ceil(total / page_size))
      items      — jobs on the current page

    Milestone: M3.8 — Acquisition APIs
    """

    items: list[AcquisitionJobRead] = Field(
        description="AcquisitionJob records on the current page."
    )
    total: int = Field(ge=0, description="Total matching jobs across all pages.")
    page: int = Field(ge=1, description="Current page number (1-based).")
    page_size: int = Field(ge=1, le=100, description="Items per page.")
    pages: int = Field(ge=0, description="Total pages (ceil(total / page_size)).")

    @model_validator(mode="before")
    @classmethod
    def _compute_pages(cls, data: Any) -> Any:
        """Auto-compute ``pages`` when not explicitly provided."""
        if isinstance(data, dict) and "pages" not in data:
            total = data.get("total", 0)
            page_size = data.get("page_size", 1)
            data["pages"] = math.ceil(total / page_size) if page_size else 0
        return data
