"""
Jobs router — CRUD and lifecycle endpoints for FinancialJob management.

Engineering Specification references:
  M2 Execution Plan, Section 2.3   — job lifecycle and API endpoints
  M2 Execution Plan, Section 2.3.3 — job status transitions
  M2 Execution Plan, Section 6.4   — pagination contract
  M2 Execution Plan, Section 9.2   — tenant isolation at repository layer
  M2 Execution Plan, Section 9.5   — role-based access control

Endpoints:
  POST   /api/v1/jobs                         — Create job (role >= analyst)
  GET    /api/v1/jobs                         — List/filter jobs with pagination
  GET    /api/v1/jobs/{id}                    — Get job by ID
  GET    /api/v1/jobs/{id}/status             — Lightweight status poll
  POST   /api/v1/jobs/{id}/cancel             — Cancel a non-terminal job
  POST   /api/v1/jobs/{id}/upload-url         — Generate pre-signed S3 PUT URL
  POST   /api/v1/jobs/{id}/upload-complete    — Register uploaded document + queue task

Authorization (Section 9.5):
  - POST / cancel / upload-* : require_analyst  (ANALYST, ADMIN, or OWNER)
  - GET (all)                : require_authenticated (any valid JWT)

Tenant isolation:
  The tenant_id is derived exclusively from the JWT payload (ctx.tenant_id)
  and passed directly to the repository layer.  It is never taken from the
  request body.  The repository enforces the isolation on every query.

Error codes:
  404 JOB_NOT_FOUND       — job does not exist or belongs to another tenant
  404 COMPANY_NOT_FOUND   — company_id in POST body is unknown in this tenant
  409 CONFLICT            — cancel requested on an already-terminal job
  422 VALIDATION_ERROR    — request body fails Pydantic validation
  401 UNAUTHORIZED        — missing or invalid JWT
  403 FORBIDDEN           — authenticated but insufficient role

M4.4 change:
  upload_complete now:
    1. Persists document_url via JobRepository.set_document_url().
    2. Transitions job status PENDING → QUEUED (celery_task_id populated on
       the RUNNING transition inside the worker, not here).
    3. Commits the document_url + QUEUED status atomically BEFORE dispatching
       so the Celery worker always sees a non-None document_url.
    4. Dispatches process_pdf_extraction_task.apply_async() with job_id,
       tenant_id, and extraction context derived from payload / job fields.
    5. Returns HTTP 202 Accepted with the updated JobResponse.

  Celery task import is deferred inside the handler via a local import to
  avoid circular-import issues at FastAPI startup (the task module imports
  from apps.api which imports from apps.api.routers — a cycle if resolved
  at module level).

Milestone: M2-Step 7 / M4.4
"""

from __future__ import annotations

import math
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import get_settings
from apps.api.core.database import get_db
from apps.api.core.exceptions import ConflictError, NotFoundError, ValidationError
from apps.api.core.s3 import get_s3_client, make_safe_filename
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_analyst,
    require_authenticated,
)
from apps.api.models import JobStatus, ValidationResultRecord
from apps.api.repositories.companies import CompanyRepository
from apps.api.repositories.jobs import JobRepository
from apps.api.schemas.jobs import (
    JobCreate,
    JobListResponse,
    JobResponse,
    JobStatusResponse,
    JobUpdate,
    UploadCompleteRequest,
    UploadUrlRequest,
    UploadUrlResponse,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_response(job: object) -> JobResponse:
    """Convert a FinancialJob ORM instance to its Pydantic response schema."""
    return JobResponse.model_validate(job)


def _to_list_response(
    items: list,  # type: ignore[type-arg]
    total: int,
    page: int,
    page_size: int,
) -> JobListResponse:
    """Build a paginated list response from repository results."""
    pages = math.ceil(total / page_size) if page_size else 0
    return JobListResponse(
        items=[_to_response(j) for j in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/jobs
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=JobResponse,
    status_code=201,
    summary="Create a job",
    description=(
        "Create a new FinancialJob in PENDING state for the authenticated tenant.  "
        "The company_id must belong to the same tenant.  "
        "Requires ANALYST role or above."
    ),
)
async def create_job(
    payload: JobCreate,
    ctx: AuthRequestContext = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
) -> JobResponse:
    """
    Create a new FinancialJob.

    Steps:
      1. Validate that the referenced company exists in this tenant.
      2. Persist via JobRepository.create, injecting tenant_id and created_by from JWT.
      3. Return 201 with JobResponse.
    """
    # Verify the company belongs to this tenant before creating the job.
    company_repo = CompanyRepository(db)
    company = await company_repo.get_by_id(ctx.tenant_id, payload.company_id)
    if company is None:
        raise NotFoundError("Company", str(payload.company_id))

    job_repo = JobRepository(db)
    job = await job_repo.create(
        ctx.tenant_id,
        payload.company_id,
        ctx.user_id,
        payload,
    )

    log.info(
        "job.created",
        job_id=str(job.id),
        tenant_id=str(ctx.tenant_id),
        company_id=str(payload.company_id),
        job_type=job.job_type,
    )
    return _to_response(job)


# ---------------------------------------------------------------------------
# GET /api/v1/jobs
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=JobListResponse,
    status_code=200,
    summary="List jobs",
    description=(
        "Return a paginated list of jobs in the authenticated tenant workspace.  "
        "Supports filtering by company and status."
    ),
)
async def list_jobs(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(20, ge=1, le=100, description="Items per page (max 100)."),
    company_id: uuid.UUID | None = Query(
        None, description="Filter to jobs for this company UUID."
    ),
    status: str | None = Query(
        None,
        description=(
            "Filter by lifecycle status: "
            "pending | queued | running | completed | failed | cancelled."
        ),
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    repo = JobRepository(db)
    items, total = await repo.list(
        ctx.tenant_id,
        company_id=company_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return _to_list_response(items, total, page, page_size)


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    status_code=200,
    summary="Get a job by ID",
    description=(
        "Return the full detail of a single job.  "
        "Returns 404 if the job does not exist or belongs to a different tenant."
    ),
)
async def get_job(
    job_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> JobResponse:
    repo = JobRepository(db)
    job = await repo.get_by_id(ctx.tenant_id, job_id)
    if job is None:
        raise NotFoundError("Job", str(job_id))
    return _to_response(job)


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{job_id}/status
# ---------------------------------------------------------------------------


@router.get(
    "/{job_id}/status",
    response_model=JobStatusResponse,
    status_code=200,
    summary="Get job status",
    description=(
        "Return a lightweight status-only snapshot for polling.  "
        "Cheaper than fetching the full job payload.  "
        "Returns 404 if the job does not exist or belongs to a different tenant."
    ),
)
async def get_job_status(
    job_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> JobStatusResponse:
    repo = JobRepository(db)
    job = await repo.get_by_id(ctx.tenant_id, job_id)
    if job is None:
        raise NotFoundError("Job", str(job_id))
    return JobStatusResponse.model_validate(job)


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


@router.post(
    "/{job_id}/cancel",
    response_model=JobResponse,
    status_code=200,
    summary="Cancel a job",
    description=(
        "Transition a non-terminal job to CANCELLED state.  "
        "Only PENDING, QUEUED, or RUNNING jobs can be cancelled.  "
        "Returns 409 if the job is already in a terminal state.  "
        "Requires ANALYST role or above."
    ),
)
async def cancel_job(
    job_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
) -> Response | JobResponse:
    repo = JobRepository(db)

    # Fetch first to distinguish 404 from 409.
    job = await repo.get_by_id(ctx.tenant_id, job_id)
    if job is None:
        raise NotFoundError("Job", str(job_id))

    if job.is_terminal:
        raise ConflictError(
            f"Job '{job_id}' is already in a terminal state ({job.status}) "
            "and cannot be cancelled."
        )

    cancelled = await repo.cancel(ctx.tenant_id, job_id)
    if cancelled is None:
        raise NotFoundError("Job", str(job_id))

    log.info(
        "job.cancelled",
        job_id=str(job_id),
        tenant_id=str(ctx.tenant_id),
    )
    return _to_response(cancelled)


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{job_id}/upload-url   (M2-Step 8)
# ---------------------------------------------------------------------------

#: Pre-signed URL lifetime — matches JWT access token lifetime (15 min).
_PRESIGNED_URL_EXPIRY_SECONDS: int = 15 * 60


@router.post(
    "/{job_id}/upload-url",
    response_model=UploadUrlResponse,
    status_code=200,
    summary="Generate a pre-signed upload URL",
    description=(
        "Return a pre-signed S3 PUT URL that the client uses to upload the "
        "source document directly to S3 without routing bytes through the API "
        "server.  Valid for 15 minutes.  "
        "The job must exist in this tenant and must not be in a terminal state.  "
        "After uploading, call POST /jobs/{id}/upload-complete to register the "
        "document with the job.  "
        "Requires ANALYST role or above."
    ),
)
async def generate_upload_url(
    job_id: uuid.UUID,
    payload: UploadUrlRequest,
    ctx: AuthRequestContext = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    s3: Any = Depends(get_s3_client),
) -> UploadUrlResponse:
    """
    Generate a pre-signed S3 PUT URL.

    Steps:
      1. Fetch job; return 404 if not found in tenant.
      2. Reject with 409 if job is already in a terminal state.
      3. Sanitise the filename to produce a safe S3 key component.
      4. Build the S3 key: {tenant_id}/jobs/{job_id}/{safe_filename}.
      5. Call boto3.generate_presigned_url (synchronous — no network call).
      6. Return URL + key + expiry.
    """
    repo = JobRepository(db)
    job = await repo.get_by_id(ctx.tenant_id, job_id)
    if job is None:
        raise NotFoundError("Job", str(job_id))

    if job.is_terminal:
        raise ConflictError(
            f"Job '{job_id}' is in a terminal state ({job.status}); "
            "document upload is not accepted."
        )

    settings = get_settings()
    safe_name = make_safe_filename(payload.filename)
    key = f"{ctx.tenant_id}/jobs/{job_id}/{safe_name}"

    presigned_url: str = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.s3_documents_bucket,
            "Key": key,
            "ContentType": "application/octet-stream",
        },
        ExpiresIn=_PRESIGNED_URL_EXPIRY_SECONDS,
    )

    # In Docker dev, the S3 client connects via http://localstack:4566 but the
    # browser must reach LocalStack via http://localhost:4566 (port-mapped).
    # Rewrite the host portion of the pre-signed URL if configured.
    if settings.s3_presigned_url_base and settings.aws_endpoint_url:
        presigned_url = presigned_url.replace(
            settings.aws_endpoint_url,
            settings.s3_presigned_url_base,
            1,
        )

    log.info(
        "job.upload_url.generated",
        job_id=str(job_id),
        tenant_id=str(ctx.tenant_id),
        key=key,
        expires_in=_PRESIGNED_URL_EXPIRY_SECONDS,
    )
    return UploadUrlResponse(
        url=presigned_url,
        key=key,
        expires_in=_PRESIGNED_URL_EXPIRY_SECONDS,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{job_id}/upload-complete   (M2-Step 8 / M4.4)
# ---------------------------------------------------------------------------

#: Valid extraction context defaults used when the client omits optional fields.
_DEFAULT_FISCAL_PERIOD: str = "FY"
_DEFAULT_REPORTING_STANDARD: str = "US_GAAP"

#: Job type substrings that indicate an annual filing.
#: Used to derive fiscal_period when the client does not supply it explicitly.
#: Quarterly jobs also default to "FY" — determining Q1/Q2/Q3/Q4 requires
#: a period_end_date that is not present on FinancialJob.  Clients running
#: quarterly extractions MUST supply fiscal_period in UploadCompleteRequest.
_ANNUAL_JOB_TYPE_TOKENS: frozenset[str] = frozenset({"annual", "10k", "20f", "6k"})


def _derive_fiscal_period(job_type: str, requested: str | None) -> str:
    """
    Return the fiscal_period string to forward to the extraction task.

    Priority order:
      1. Client-supplied value in UploadCompleteRequest (most authoritative).
      2. Inferred from job.job_type substring match (annual tokens → 'FY').
      3. Hard default 'FY'.

    Args:
        job_type:  FinancialJob.job_type (e.g. 'sec_10k_annual', 'sec_10q_q3').
        requested: payload.fiscal_period from the request body, or None.

    Returns:
        Normalised fiscal period string: 'FY' | 'Q1' | 'Q2' | 'Q3' | 'Q4'.
    """
    if requested:
        return requested.strip().upper()
    lower = job_type.lower()
    if any(token in lower for token in _ANNUAL_JOB_TYPE_TOKENS):
        return "FY"
    return _DEFAULT_FISCAL_PERIOD


@router.post(
    "/{job_id}/upload-complete",
    response_model=JobResponse,
    status_code=202,
    summary="Confirm document upload and queue extraction",
    description=(
        "Mark the source document as received, transition the job to QUEUED, "
        "and dispatch the AI extraction background task.  "
        "Call this after a successful PUT to the pre-signed URL returned by "
        "POST /jobs/{id}/upload-url.  "
        "The ``key`` field is validated against the expected prefix "
        "``{tenant_id}/jobs/{job_id}/`` to prevent arbitrary key injection.  "
        "Returns **202 Accepted** — the job is now QUEUED; poll "
        "GET /jobs/{id}/status for RUNNING → COMPLETED / FAILED transitions.  "
        "Requires ANALYST role or above."
    ),
)
async def upload_complete(
    job_id: uuid.UUID,
    payload: UploadCompleteRequest,
    ctx: AuthRequestContext = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
) -> JobResponse:
    """
    Register the uploaded document and dispatch the extraction Celery task.

    M4.4 steps:
      1. Fetch job; return 404 if not found in this tenant.
      2. Reject with 409 if the job is already in a terminal state (idempotency).
      3. Validate the S3 key prefix: must be {tenant_id}/jobs/{job_id}/.
      4. Persist document_url via JobRepository.set_document_url() (flushes).
      5. Transition job status → QUEUED via JobRepository.update_status() (flushes).
      6. Commit the transaction atomically BEFORE dispatching the task so the
         Celery worker always reads a non-None document_url regardless of broker
         delivery speed.  The get_db dependency's implicit commit after this
         function returns is a no-op since the transaction is already committed.
      7. Derive extraction context (fiscal_period, filing_date_iso,
         reporting_standard) from payload with job-based fallbacks.
      8. Dispatch process_pdf_extraction_task.apply_async() — non-blocking;
         returns immediately after placing the message on the broker.
      9. Return HTTP 202 Accepted with the updated JobResponse (status=queued).

    Deferred import note:
      process_pdf_extraction_task is imported inside this function (not at
      module level) to break the circular import cycle:
        apps.api.routers.jobs
          -> workers.tasks.extraction_tasks
            -> apps.api.core.database
              -> (back to apps.api at startup)
      The deferred import executes at first request, by which time all modules
      are fully initialised.
    """
    from datetime import date as _date

    # Deferred — breaks the circular import cycle described in the docstring.
    from workers.tasks.extraction_tasks import process_pdf_extraction_task

    repo = JobRepository(db)

    # ── Step 1: Fetch job ──────────────────────────────────────────────────────
    job = await repo.get_by_id(ctx.tenant_id, job_id)
    if job is None:
        raise NotFoundError("Job", str(job_id))

    # ── Step 2: Idempotency guard ──────────────────────────────────────────────
    # A terminal job already completed, failed, or was cancelled.  Re-uploading
    # would be a no-op at best; silently dispatching a second extraction would
    # produce duplicate data (ON CONFLICT DO NOTHING would absorb it, but we
    # should reject early rather than silently accept the call).
    if job.is_terminal:
        raise ConflictError(
            f"Job '{job_id}' is already in a terminal state ({job.status}) "
            "and cannot accept a document upload.  "
            "Create a new job to start a fresh extraction."
        )

    # ── Step 3: Validate S3 key ownership ─────────────────────────────────────
    # Prevent arbitrary key injection: the key must be scoped to this exact
    # tenant + job combination, exactly as the upload-url endpoint generated.
    expected_prefix = f"{ctx.tenant_id}/jobs/{job_id}/"
    if not payload.key.startswith(expected_prefix):
        raise ValidationError(
            f"key must start with '{expected_prefix}'.  "
            "Use the key returned by POST /jobs/{id}/upload-url."
        )

    # ── Step 4: Persist document_url ──────────────────────────────────────────
    updated = await repo.set_document_url(ctx.tenant_id, job_id, payload.key)
    if updated is None:
        # Race: job was deleted between the fetch and this write.
        raise NotFoundError("Job", str(job_id))

    # ── Step 5: Transition → QUEUED ───────────────────────────────────────────
    # Celery task ID is populated by the worker on the RUNNING transition;
    # we leave celery_task_id=None here and let the task set it.
    queued = await repo.update_status(
        ctx.tenant_id,
        job_id,
        JobUpdate(status=JobStatus.QUEUED.value),
    )
    if queued is None:
        raise NotFoundError("Job", str(job_id))

    # ── Step 6: Commit BEFORE dispatch ────────────────────────────────────────
    # Critical ordering: commit document_url + QUEUED status durably so the
    # Celery worker always sees a populated document_url when it fetches the job.
    # The get_db context manager will call commit() again on exit — that is a
    # safe no-op when the session has no pending changes.
    await db.commit()

    log.info(
        "job.upload.committed",
        job_id=str(job_id),
        tenant_id=str(ctx.tenant_id),
        key=payload.key,
        new_status=queued.status,
    )

    # ── Step 7: Derive extraction context ─────────────────────────────────────
    fiscal_period: str = _derive_fiscal_period(queued.job_type, payload.fiscal_period)
    filing_date_iso: str = payload.filing_date_iso or _date.today().isoformat()
    reporting_standard: str = (
        (payload.reporting_standard or "").strip().upper()
        or _DEFAULT_REPORTING_STANDARD
    )

    # ── Step 8: Dispatch Celery task ──────────────────────────────────────────
    # apply_async() is non-blocking — it enqueues the message and returns
    # a Celery AsyncResult immediately.  All task arguments are JSON-safe
    # primitives (str); no ORM instances cross the broker boundary.
    task_result = process_pdf_extraction_task.apply_async(
        kwargs={
            "job_id": str(job_id),
            "tenant_id": str(ctx.tenant_id),
            "fiscal_period": fiscal_period,
            "filing_date_iso": filing_date_iso,
            "reporting_standard": reporting_standard,
        },
        # Route to the dedicated extraction queue when multi-queue Celery
        # routing is configured; falls back to the default queue otherwise.
        queue="extraction",
        # Retry broker connection on transient failures (e.g. Redis restart)
        # before surfacing the error to the caller.
        retry=True,
        retry_policy={
            "max_retries": 3,
            "interval_start": 0.5,
            "interval_step": 0.5,
            "interval_max": 3.0,
        },
    )

    log.info(
        "job.extraction.dispatched",
        job_id=str(job_id),
        tenant_id=str(ctx.tenant_id),
        celery_task_id=task_result.id,
        fiscal_period=fiscal_period,
        filing_date_iso=filing_date_iso,
        reporting_standard=reporting_standard,
    )

    # ── Step 9: Return 202 Accepted ───────────────────────────────────────────
    # The extraction task is now enqueued.  The client should poll
    # GET /api/v1/jobs/{id}/status for RUNNING → COMPLETED / FAILED.
    return _to_response(queued)


# ---------------------------------------------------------------------------
# Inline schemas — validation result (M4.4F)
# ---------------------------------------------------------------------------
# These are declared here rather than in schemas/jobs.py to keep the
# validation-dashboard types co-located with their single consumer endpoint.
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _BaseModel  # noqa: E402, PLC0415


class ValidationFindingResponse(_BaseModel):
    """A single rule check result from the dual-dimension validation engine."""

    rule_id: str
    severity: str      # "CRITICAL" | "WARNING" | "INFO"
    message: str
    expected: float | None = None
    actual: float | None = None
    delta: float | None = None


class ValidationDeductionResponse(_BaseModel):
    """A confidence-score deduction entry from Amendment V1.2 §1.8."""

    rule_id: str
    points: int
    reason: str


class ValidationResultResponse(_BaseModel):
    """
    Full validation audit record for one extraction run.

    Returned by GET /api/v1/jobs/{id}/validation.
    Mirrors the validation_results table row exactly.
    """

    model_config = {"from_attributes": True}

    id: str
    job_id: str | None
    accession_number: str
    company_id: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    items_validated: int
    is_exportable: bool
    critical_count: int
    warning_count: int
    confidence_score: int            # [0, 100]
    findings: list[ValidationFindingResponse]
    deductions: list[ValidationDeductionResponse]
    summary_text: str | None
    created_at: str                  # ISO 8601


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{id}/validation
# ---------------------------------------------------------------------------


@router.get(
    "/{job_id}/validation",
    response_model=ValidationResultResponse,
    status_code=200,
    summary="Get validation result for a job",
    description=(
        "Return the most recent validation audit record for a financial extraction "
        "job.  The record contains the pipeline confidence score, per-rule "
        "findings (CRITICAL / WARNING / INFO), confidence deductions, and the "
        "``is_exportable`` flag that gates the Excel export pipeline.\n\n"
        "Returns **404 JOB_NOT_FOUND** when the job does not exist in this "
        "tenant.  Returns **404 VALIDATION_NOT_FOUND** when the job has not "
        "yet completed an extraction run (no validation record exists)."
    ),
)
async def get_job_validation(
    job_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Retrieve the latest validation result for *job_id*.

    Tenant isolation:  The job is looked up first via JobRepository which
    enforces tenant scoping — a job belonging to another tenant returns 404,
    not 403, to prevent enumeration.
    """
    from datetime import datetime  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    # ── 1. Confirm job exists and belongs to this tenant ──────────────────────
    repo = JobRepository(db)
    job = await repo.get(str(job_id), str(ctx.tenant_id))
    if job is None:
        raise NotFoundError("JOB_NOT_FOUND", f"Job {job_id} not found.")

    # ── 2. Fetch most recent validation result for this job ───────────────────
    stmt = (
        select(ValidationResultRecord)
        .where(ValidationResultRecord.job_id == job_id)
        .order_by(ValidationResultRecord.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    vr: ValidationResultRecord | None = result.scalar_one_or_none()

    if vr is None:
        raise NotFoundError(
            "VALIDATION_NOT_FOUND",
            f"No validation result found for job {job_id}. "
            "The extraction pipeline may not have run yet.",
        )

    # ── 3. Normalise JSONB lists (may be None if an old row pre-dates these) ──
    raw_findings: list[dict] = vr.findings or []
    raw_deductions: list[dict] = vr.deductions or []

    findings = [
        ValidationFindingResponse(
            rule_id=f.get("rule_id", "UNKNOWN"),
            severity=f.get("severity", "INFO"),
            message=f.get("message", ""),
            expected=float(f["expected"]) if f.get("expected") is not None else None,
            actual=float(f["actual"])     if f.get("actual")   is not None else None,
            delta=float(f["delta"])       if f.get("delta")     is not None else None,
        )
        for f in raw_findings
    ]

    deductions = [
        ValidationDeductionResponse(
            rule_id=d.get("rule_id", "UNKNOWN"),
            points=int(d.get("points", 0)),
            reason=d.get("reason", ""),
        )
        for d in raw_deductions
    ]

    created_str = (
        vr.created_at.isoformat()
        if isinstance(vr.created_at, datetime)
        else str(vr.created_at)
    )

    return ValidationResultResponse(
        id=str(vr.id),
        job_id=str(vr.job_id) if vr.job_id else None,
        accession_number=vr.accession_number,
        company_id=str(vr.company_id) if vr.company_id else None,
        fiscal_year=vr.fiscal_year,
        fiscal_period=vr.fiscal_period,
        items_validated=vr.items_validated,
        is_exportable=vr.is_exportable,
        critical_count=vr.critical_count,
        warning_count=vr.warning_count,
        confidence_score=vr.confidence_score,
        findings=findings,
        deductions=deductions,
        summary_text=vr.summary_text,
        created_at=created_str,
    )
