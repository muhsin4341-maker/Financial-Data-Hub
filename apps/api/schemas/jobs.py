"""
FinancialJob request/response Pydantic schemas.

Engineering Specification references:
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables
  M2 Execution Plan, Section 2.3   — job lifecycle and API endpoints
  M2 Execution Plan, Section 6.4   — pagination contract

Schemas:
  JobCreate            — POST /api/v1/jobs request body
  JobUpdate            — internal update model (status transitions, not a public PATCH body)
  JobResponse          — full read model returned by create, list, and detail endpoints
  JobListResponse      — paginated list envelope for GET /api/v1/jobs
  JobStatusResponse    — lightweight status-only response for GET /api/v1/jobs/{id}/status
  UploadUrlRequest     — POST /api/v1/jobs/{id}/upload-url request body
  UploadUrlResponse    — pre-signed URL envelope
  UploadCompleteRequest — POST /api/v1/jobs/{id}/upload-complete request body (M4.4)

Status lifecycle (M2 Execution Plan, Section 2.3.3):
  pending → queued → running → completed
                             → failed
  pending/queued/running → cancelled (via API cancel request)

M4.4 change:
  UploadCompleteRequest extended with optional extraction context fields
  (fiscal_period, filing_date_iso, reporting_standard) so that the router
  can pass accurate metadata to process_pdf_extraction_task without requiring
  a separate API call or a second job-update round-trip.

Milestone: M2-Step 4 / M4.4
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from apps.api.models import JobStatus

# Valid job type prefixes recognised at schema level.
# The full list grows with each data-source integration milestone.
_VALID_JOB_TYPE_PATTERN = r"^[a-z][a-z0-9_]{1,98}[a-z0-9]$"

# Sensible fiscal year range: prevent obviously nonsensical values.
_FISCAL_YEAR_MIN = 1900
_FISCAL_YEAR_MAX = 2100


# ---------------------------------------------------------------------------
# Write models (create / update)
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    """
    Request body for POST /api/v1/jobs.

    Creates a new FinancialJob in PENDING state.  The job is not dispatched
    to Celery until the client calls POST /jobs/{id}/upload-complete (M2-Step 8).
    """

    company_id: uuid.UUID = Field(
        description=(
            "UUID of the company this job extracts data for.  "
            "Must belong to the requesting tenant."
        ),
    )
    job_type: str = Field(
        min_length=3,
        max_length=100,
        description=(
            "Extraction template identifier.  "
            "Must be lowercase with underscores (e.g. 'sec_10k_annual').  "
            "Validated against snake_case pattern."
        ),
        examples=["sec_10k_annual", "sec_10q_quarterly"],
    )
    fiscal_year: int | None = Field(
        default=None,
        ge=_FISCAL_YEAR_MIN,
        le=_FISCAL_YEAR_MAX,
        description=(
            f"Fiscal year being extracted (e.g. 2023).  "
            f"Must be between {_FISCAL_YEAR_MIN} and {_FISCAL_YEAR_MAX}."
        ),
        examples=[2023, 2022],
    )

    @field_validator("job_type")
    @classmethod
    def _validate_job_type(cls, v: str) -> str:
        """
        Enforce snake_case format for job_type identifiers.

        Allows: lowercase letters, digits, underscores.
        Requires: starts with a letter, ends with letter or digit.
        Minimum 3 characters (e.g. 'sec', not just '_x').
        """
        import re  # noqa: PLC0415
        stripped = v.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*[a-z0-9]", stripped):
            raise ValueError(
                "job_type must be snake_case: lowercase letters, digits, and "
                f"underscores; start with a letter; end with letter or digit. "
                f"Received: {v!r}"
            )
        return stripped


class JobUpdate(BaseModel):
    """
    Internal update model for FinancialJob status transitions.

    This is NOT a public PATCH endpoint body — it is used internally by
    repository methods to update job fields after status transitions and
    by the cancel endpoint (POST /jobs/{id}/cancel).

    Only fields explicitly set in the request are applied.
    """

    status: str | None = Field(
        default=None,
        description="New lifecycle status.  Must be a valid JobStatus value.",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description when transitioning to FAILED status.",
    )
    celery_task_id: str | None = Field(
        default=None,
        max_length=255,
        description="Celery task ID assigned on dispatch.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="Set when the worker begins processing.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="Set when the job reaches a terminal state.",
    )

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        """Reject status values that are not valid JobStatus members."""
        if v is None:
            return None
        try:
            JobStatus(v)
        except ValueError:
            valid = [s.value for s in JobStatus]
            raise ValueError(
                f"Invalid job status {v!r}.  "
                f"Must be one of: {valid}"
            ) from None
        return v


# ---------------------------------------------------------------------------
# Read models (responses)
# ---------------------------------------------------------------------------


class JobResponse(BaseModel):
    """
    Full response schema for a single FinancialJob.

    Returned by:
      - POST   /api/v1/jobs              (201)
      - GET    /api/v1/jobs              (200, inside JobListResponse.items)
      - GET    /api/v1/jobs/{id}         (200)
      - POST   /api/v1/jobs/{id}/cancel  (200)

    ``is_terminal`` and ``is_cancellable`` are derived from ``status`` and
    provided for client convenience — they mirror the ORM model properties.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="UUID v7 primary key.")
    tenant_id: uuid.UUID = Field(description="Owning tenant workspace UUID.")
    company_id: uuid.UUID = Field(description="Company this job processes.")
    created_by: uuid.UUID | None = Field(
        description="User who created the job.  None if the creator was deleted."
    )
    status: str = Field(
        description=(
            "Lifecycle state: pending | queued | running | "
            "completed | failed | cancelled"
        ),
    )
    job_type: str = Field(description="Extraction template identifier.")
    fiscal_year: int | None = Field(description="Fiscal year being extracted.")
    document_url: str | None = Field(
        description="S3 key of the uploaded source document."
    )
    result_url: str | None = Field(
        description="S3 key of the generated Excel export (populated in M6)."
    )
    error_message: str | None = Field(
        description="Error description when status = failed."
    )
    celery_task_id: str | None = Field(
        description="Celery task ID for cancellation."
    )
    started_at: datetime | None = Field(
        description="Timestamp when worker began processing."
    )
    completed_at: datetime | None = Field(
        description="Timestamp when job reached a terminal state."
    )
    created_at: datetime = Field(description="ISO 8601 creation timestamp (UTC).")
    updated_at: datetime = Field(description="ISO 8601 last-update timestamp (UTC).")

    # ── Computed convenience fields ───────────────────────────────────────────

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_terminal(self) -> bool:
        """True if the job has reached a final state (completed/failed/cancelled)."""
        return self.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_cancellable(self) -> bool:
        """True if the job can still be cancelled via the API."""
        return self.status in (
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
        )


class JobListResponse(BaseModel):
    """
    Paginated response envelope for GET /api/v1/jobs.

    Pagination contract matches CompanyListResponse (M2 Execution Plan, Section 6.4).
    """

    items: list[JobResponse] = Field(description="Jobs on the current page.")
    total: int = Field(
        ge=0,
        description="Total matching jobs across all pages.",
    )
    page: int = Field(ge=1, description="Current page number (1-based).")
    page_size: int = Field(ge=1, le=100, description="Items per page.")
    pages: int = Field(ge=0, description="Total page count.")

    @model_validator(mode="before")
    @classmethod
    def _compute_pages(cls, data: Any) -> Any:
        if isinstance(data, dict) and "pages" not in data:
            total = data.get("total", 0)
            page_size = data.get("page_size", 1)
            data["pages"] = math.ceil(total / page_size) if page_size else 0
        return data


# ---------------------------------------------------------------------------
# S3 upload schemas (Step 8)
# ---------------------------------------------------------------------------


class UploadUrlRequest(BaseModel):
    """
    Request body for POST /api/v1/jobs/{id}/upload-url.

    The filename is sanitised server-side before embedding in the S3 key.
    """

    filename: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Original filename of the source document "
            "(e.g. 'annual_report_2023.pdf').  "
            "Sanitised before use in the S3 key."
        ),
        examples=["annual_report_2023.pdf", "10-K_2023.pdf"],
    )


class UploadUrlResponse(BaseModel):
    """
    Response from POST /api/v1/jobs/{id}/upload-url.

    The client should:
      1. HTTP PUT the document body to ``url``.
      2. Call POST /api/v1/jobs/{id}/upload-complete with the ``key``.
    """

    url: str = Field(
        description=(
            "Pre-signed S3 PUT URL.  "
            "Valid for ``expires_in`` seconds from generation time."
        )
    )
    key: str = Field(
        description=(
            "S3 object key.  "
            "Pass this value to POST /jobs/{id}/upload-complete."
        )
    )
    expires_in: int = Field(
        description="URL validity in seconds (900 = 15 minutes)."
    )


class UploadCompleteRequest(BaseModel):
    """
    Request body for POST /api/v1/jobs/{id}/upload-complete.

    The ``key`` must match the value returned by the preceding upload-url call.
    The server validates the key prefix against ``{tenant_id}/jobs/{job_id}/``
    to prevent arbitrary key injection.

    M4.4 — Extraction context fields:
      The three optional fields below are forwarded directly to
      process_pdf_extraction_task so the Celery worker has accurate metadata
      without requiring a second API call.  All three default to safe values
      when omitted:
        fiscal_period      — defaults to 'FY' (annual).
        filing_date_iso    — defaults to today's date in the router.
        reporting_standard — defaults to 'US_GAAP'.

    These fields are intentionally optional so that the client-side contract
    is non-breaking: existing callers that only send ``key`` continue to work
    exactly as before; the defaults produce valid extraction parameters.
    """

    key: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "S3 object key as returned by POST /jobs/{id}/upload-url.  "
            "Must start with ``{tenant_id}/jobs/{job_id}/``."
        ),
    )

    # ── Extraction context — M4.4 ─────────────────────────────────────────────
    # Forwarded to process_pdf_extraction_task by the upload_complete handler.
    fiscal_period: str | None = Field(
        default=None,
        max_length=10,
        description=(
            "Fiscal period label for the uploaded document: "
            "'FY', 'Q1', 'Q2', 'Q3', or 'Q4'.  "
            "Defaults to 'FY' (annual) when omitted."
        ),
        examples=["FY", "Q3", None],
    )
    filing_date_iso: str | None = Field(
        default=None,
        description=(
            "Date the document was filed with the regulator, in ISO 8601 format "
            "(YYYY-MM-DD).  "
            "Defaults to today's date when omitted."
        ),
        examples=["2024-02-02", None],
    )
    reporting_standard: str | None = Field(
        default=None,
        max_length=20,
        description=(
            "Accounting standard of the uploaded document: "
            "'US_GAAP', 'IFRS', or 'IND_AS'.  "
            "Defaults to 'US_GAAP' when omitted."
        ),
        examples=["US_GAAP", "IFRS", "IND_AS", None],
    )


class JobStatusResponse(BaseModel):
    """
    Lightweight status-only response for GET /api/v1/jobs/{id}/status.

    Designed for efficient polling without fetching the full job payload.
    Only the fields needed to determine job progress are returned.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Job UUID.")
    status: str = Field(description="Current lifecycle status.")
    started_at: datetime | None = Field(
        description="When the worker began processing."
    )
    completed_at: datetime | None = Field(
        description="When the job reached a terminal state."
    )
    error_message: str | None = Field(
        description="Error description if status = failed."
    )
