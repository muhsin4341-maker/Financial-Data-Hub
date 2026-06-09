"""
Export Router — Synchronous and asynchronous Excel export endpoints.

Endpoint:
  GET /api/v1/jobs/{job_id}/export

  Invokes ``ExcelExportService`` to build a fully styled, multi-period
  .xlsx workbook for the requested FinancialJob and streams it back to the
  caller as a file download attachment.

Authentication / authorisation:
  ``require_authenticated`` — any holder of a valid JWT may download exports
  for jobs that belong to their tenant.  The service layer enforces tenant
  isolation through the passed ``AsyncSession`` (all queries are filtered by
  the job's ``company_id``, which was created by the tenant).

Response:
  HTTP 200  application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
            Content-Disposition: attachment; filename="<company>_<ticker>_<period>_Financial_Export.xlsx"

  HTTP 404  EXPORT_JOB_NOT_FOUND       — job_id does not exist
            EXPORT_COMPANY_NOT_FOUND   — company record missing (data integrity)
            EXPORT_NO_DATA             — no FinancialLineItem rows for the job
  HTTP 422  VALIDATION_ERROR           — job_id is not a valid UUID
  HTTP 500  (unhandled) — unexpected internal error (logged, re-raised)

Error response shape (consistent with all other routers):
  {
    "error": {
      "code": "EXPORT_NO_DATA",
      "message": "...",
      "details": { "job_id": "<uuid>" },
      "request_id": "<id>"
    }
  }

Filename sanitisation:
  ``_build_export_filename()`` strips every character that is not
  alphanumeric, a hyphen, or an underscore; collapses runs of underscores;
  and enforces a 128-character length cap before appending the ``.xlsx``
  extension.  This prevents path-traversal characters from appearing in the
  Content-Disposition header.

Separation of concerns:
  This module contains only HTTP-layer logic — routing, dependency injection,
  response assembly, and error translation.  All workbook generation lives in
  ``services.export.excel_generator.ExcelExportService``.

Milestone: M6.4 — FastAPI Download Endpoint and Streaming Interface
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError
from apps.api.middleware.auth import AuthRequestContext, require_authenticated

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/jobs",
    tags=["export"],
)

# IANA media type for Office Open XML spreadsheets (xlsx).
_XLSX_MEDIA_TYPE: str = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def _sanitize_filename_segment(text: str) -> str:
    """
    Strip unsafe characters from a single filename component.

    Keeps only ASCII letters, digits, hyphens, and underscores.  Leading/
    trailing underscores and consecutive underscore runs are collapsed.

    Args:
        text: Raw string (company name, ticker, period label, etc.)

    Returns:
        Safe ASCII slug (may be empty if the input has no safe characters).

    Examples::
        "Apple Inc."     → "Apple_Inc"
        "us-gaap (FY)"   → "us-gaap_FY"
        "  __Hello__  "  → "Hello"
    """
    # Replace any run of non-alphanumeric-non-hyphen chars with a single _
    slug = re.sub(r"[^A-Za-z0-9\-]+", "_", text.strip())
    # Collapse leading/trailing underscores
    slug = slug.strip("_")
    return slug


def _build_export_filename(
    company_name: str,
    ticker: str,
    fiscal_period: str,
    fiscal_year: int,
) -> str:
    """
    Construct the Content-Disposition attachment filename.

    Format::
        {company}_{ticker}_{period}_{year}_Financial_Export.xlsx

    Examples::
        "Apple Inc.", "AAPL", "FY", 2024
            → "Apple_Inc_AAPL_FY_2024_Financial_Export.xlsx"

        "Tata Consultancy Services", "TCS", "Q1", 2025
            → "Tata_Consultancy_Services_TCS_Q1_2025_Financial_Export.xlsx"

    The combined basename is capped at 128 characters before the ``.xlsx``
    extension to comply with filesystem and browser filename constraints.

    Args:
        company_name:   Full legal name of the company.
        ticker:         Stock ticker symbol.
        fiscal_period:  Period string ("FY" | "Q1"–"Q4" | "H1" | "H2").
        fiscal_year:    Fiscal year integer.

    Returns:
        Complete filename string including the ``.xlsx`` extension.
    """
    parts = [
        _sanitize_filename_segment(company_name),
        _sanitize_filename_segment(ticker),
        _sanitize_filename_segment(fiscal_period),
        str(fiscal_year),
        "Financial_Export",
    ]
    # Drop any segment that came out empty after sanitisation
    basename = "_".join(p for p in parts if p)
    # Cap total basename length
    if len(basename) > 128:
        basename = basename[:128].rstrip("_")
    return f"{basename}.xlsx"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/{job_id}/export",
    summary="Download Excel financial export for a job",
    description=(
        "Generates and streams a fully styled, multi-period .xlsx workbook "
        "containing Income Statement, Balance Sheet, Cash Flow, and Audit Log "
        "sheets for the requested FinancialJob.  The workbook incorporates all "
        "FX-translated USD values alongside the original reported figures."
    ),
    response_class=Response,
    responses={
        200: {
            "description": "Excel workbook file download",
            "content": {_XLSX_MEDIA_TYPE: {}},
            "headers": {
                "Content-Disposition": {
                    "description": "Attachment filename",
                    "schema": {"type": "string"},
                }
            },
        },
        404: {"description": "Job not found, company missing, or no data available"},
        401: {"description": "Missing or invalid JWT"},
    },
)
async def download_export(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    ctx: AuthRequestContext = Depends(require_authenticated),
) -> Response:
    """
    Build and stream a ``.xlsx`` financial export for *job_id*.

    Processing flow:
      1. Load a minimal ``FinancialJob`` snapshot (for the filename).
      2. Invoke ``ExcelExportService.export()`` with the same session — the
         SQLAlchemy identity map caches the job row so the re-load inside
         the service is free (no second round-trip to the database).
      3. Assemble ``Content-Disposition`` from the job's fiscal metadata.
      4. Return the raw xlsx bytes as a ``Response`` with the IANA xlsx
         media type.

    Args:
        job_id:  UUID of the FinancialJob to export (path parameter).
        session: Injected ``AsyncSession`` (one per request, auto-committed).
        ctx:     Injected auth context (user_id, tenant_id, role).

    Returns:
        ``fastapi.Response`` carrying the xlsx byte payload.

    Raises:
        APIError(404, EXPORT_JOB_NOT_FOUND)       — job does not exist.
        APIError(404, EXPORT_COMPANY_NOT_FOUND)    — company record missing.
        APIError(404, EXPORT_NO_DATA)              — no line items for job.
    """
    bound_log = log.bind(
        job_id=str(job_id),
        user_id=str(ctx.user_id),
        tenant_id=str(ctx.tenant_id),
    )
    bound_log.info("export_endpoint.request_received")

    # ── Step 1: Minimal job pre-load for filename metadata ────────────────────
    # We load the FinancialJob here so we can construct a meaningful filename
    # before — or in the event the service raises — responding to the client.
    # The identity map ensures the service's own session.get() call is cached.
    from apps.api.models import Company, FinancialJob  # deferred to avoid circular import

    job: FinancialJob | None = await session.get(FinancialJob, job_id)
    if job is None:
        raise APIError(
            code="EXPORT_JOB_NOT_FOUND",
            message=f"FinancialJob {job_id} not found.",
            status_code=404,
            details={"job_id": str(job_id)},
        )

    company: Company | None = await session.get(Company, job.company_id)
    if company is None:
        raise APIError(
            code="EXPORT_COMPANY_NOT_FOUND",
            message=(
                f"Company {job.company_id} linked to job {job_id} not found. "
                "This indicates a data integrity issue — please contact support."
            ),
            status_code=404,
            details={"job_id": str(job_id), "company_id": str(job.company_id)},
        )

    # Resolve fiscal metadata with safe fallbacks
    fiscal_year: int = job.fiscal_year or 0
    fiscal_period: str = (getattr(job, "fiscal_period", "FY") or "FY")

    # ── Step 2: Invoke the export service ─────────────────────────────────────
    from services.export.excel_generator import (  # deferred to avoid circular import
        ExcelExportService,
        ExportCompanyNotFoundError,
        ExportJobNotFoundError,
        ExportNoDataError,
    )

    service = ExcelExportService()

    try:
        xlsx_bytes: bytes = await service.export(job_id=job_id, session=session)

    except ExportJobNotFoundError as exc:
        bound_log.warning("export_endpoint.job_not_found", error=str(exc))
        raise APIError(
            code="EXPORT_JOB_NOT_FOUND",
            message=str(exc),
            status_code=404,
            details={"job_id": str(job_id)},
        ) from exc

    except ExportCompanyNotFoundError as exc:
        bound_log.warning("export_endpoint.company_not_found", error=str(exc))
        raise APIError(
            code="EXPORT_COMPANY_NOT_FOUND",
            message=str(exc),
            status_code=404,
            details={"job_id": str(job_id), "company_id": str(job.company_id)},
        ) from exc

    except ExportNoDataError as exc:
        bound_log.warning(
            "export_endpoint.no_data",
            error=str(exc),
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
        )
        raise APIError(
            code="EXPORT_NO_DATA",
            message=str(exc),
            status_code=404,
            details={
                "job_id": str(job_id),
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
            },
        ) from exc

    except Exception:
        # Unexpected internal error — let FastAPI's default 500 handler take over
        # so structured error logging is consistent with other unhandled paths.
        bound_log.exception("export_endpoint.unexpected_error")
        raise

    # ── Step 3: Assemble response ──────────────────────────────────────────────
    filename = _build_export_filename(
        company_name=company.name,
        ticker=company.ticker or "UNKNOWN",
        fiscal_period=fiscal_period,
        fiscal_year=fiscal_year,
    )

    bound_log.info(
        "export_endpoint.response_ready",
        filename=filename,
        size_bytes=len(xlsx_bytes),
    )

    return Response(
        content=xlsx_bytes,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            # RFC 6266: inline attachment with ASCII filename.
            # The filename* parameter (RFC 5987 UTF-8 encoding) is omitted here
            # because company names stored in the DB are normalised to ASCII by
            # the sanitiser; a future milestone can add filename* for Unicode.
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Prevent content-sniffing attacks (belt-and-suspenders alongside
            # the explicit media type already set on the Response).
            "X-Content-Type-Options": "nosniff",
            # Expose the generated filename to JavaScript callers that consume
            # this endpoint programmatically (e.g. SPA download trigger).
            "X-Export-Filename": filename,
        },
    )


# ---------------------------------------------------------------------------
# Pydantic schemas — async export pipeline (B5)
# ---------------------------------------------------------------------------


class AsyncExportTriggerResponse(BaseModel):
    """Returned immediately by POST /{job_id}/export/async."""

    export_job_id: str
    status: str
    message: str
    job_id: str
    queue: str


class ExportStatusResponse(BaseModel):
    """Returned by GET /export/{export_job_id}/status."""

    id: str
    job_id: str
    tenant_id: str
    status: str
    download_url: str | None
    error_message: str | None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# POST /{job_id}/export/async  — B5: Trigger async export
# ---------------------------------------------------------------------------


@router.post(
    "/{job_id}/export/async",
    response_model=AsyncExportTriggerResponse,
    status_code=202,
    summary="Trigger an asynchronous Excel export",
    description=(
        "Creates an ExcelExportJob record (status=PENDING) and dispatches "
        "``generate_excel_export_task`` to the QUEUE_EXPORT Celery worker.  "
        "Returns immediately with the export_job_id that the client uses to "
        "poll GET /api/v1/jobs/export/{id}/status for progress.  "
        "Requires an authenticated user session."
    ),
)
async def trigger_async_export(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    ctx: AuthRequestContext = Depends(require_authenticated),
) -> AsyncExportTriggerResponse:
    """
    Dispatch a background Excel export for *job_id*.

    Steps:
      1. Verify the FinancialJob exists and belongs to this tenant.
      2. Verify the job is in ``completed`` status (no export without data).
      3. INSERT an ExcelExportJob record with status=PENDING.
      4. Dispatch ``generate_excel_export_task.apply_async(...)`` to QUEUE_EXPORT.
      5. Return 202 Accepted with the new export_job_id.
    """
    bound_log = log.bind(
        job_id=str(job_id),
        user_id=str(ctx.user_id),
        tenant_id=str(ctx.tenant_id),
    )
    bound_log.info("async_export.trigger_received")

    # ── 1. Verify FinancialJob ─────────────────────────────────────────────────
    from apps.api.models import ExcelExportJob, ExcelExportStatus, FinancialJob  # deferred

    fin_job: FinancialJob | None = await session.get(FinancialJob, job_id)
    if fin_job is None or fin_job.tenant_id != ctx.tenant_id:
        raise APIError(
            code="EXPORT_JOB_NOT_FOUND",
            message=f"FinancialJob {job_id} not found.",
            status_code=404,
            details={"job_id": str(job_id)},
        )

    # ── 2. Guard: job must be completed ───────────────────────────────────────
    if fin_job.status != "completed":
        raise APIError(
            code="EXPORT_JOB_NOT_COMPLETED",
            message=(
                f"FinancialJob {job_id} is not completed (current status: "
                f"{fin_job.status!r}).  Export is only available for completed jobs."
            ),
            status_code=409,
            details={"job_id": str(job_id), "status": fin_job.status},
        )

    # ── 3. Create ExcelExportJob record ───────────────────────────────────────
    from apps.api.models import gen_uuid7  # deferred

    export_job = ExcelExportJob(
        id=gen_uuid7(),
        tenant_id=ctx.tenant_id,
        job_id=job_id,
        company_id=fin_job.company_id,
        requested_by=ctx.user_id,
        status=ExcelExportStatus.PENDING,
    )
    session.add(export_job)
    await session.commit()
    await session.refresh(export_job)

    # ── 4. Dispatch Celery task ───────────────────────────────────────────────
    from workers.queues import QUEUE_EXPORT  # deferred
    from workers.tasks.export_tasks import generate_excel_export_task  # deferred

    try:
        generate_excel_export_task.apply_async(
            args=[str(export_job.id)],
            queue=QUEUE_EXPORT,
        )
    except Exception as exc:  # broker unreachable
        bound_log.warning(
            "async_export.broker_unreachable",
            error=str(exc),
            export_job_id=str(export_job.id),
        )
        raise APIError(
            code="EXPORT_BROKER_UNAVAILABLE",
            message=(
                "Export task could not be dispatched — the message broker is "
                "temporarily unavailable.  Please retry in a few seconds."
            ),
            status_code=503,
            details={"export_job_id": str(export_job.id)},
        ) from exc

    bound_log.info(
        "async_export.dispatched",
        export_job_id=str(export_job.id),
        queue=QUEUE_EXPORT,
    )

    return AsyncExportTriggerResponse(
        export_job_id=str(export_job.id),
        status=ExcelExportStatus.PENDING,
        message="Export job queued. Poll the status endpoint for progress.",
        job_id=str(job_id),
        queue=QUEUE_EXPORT,
    )


# ---------------------------------------------------------------------------
# GET /export/{export_job_id}/status  — B5: Poll export status
# ---------------------------------------------------------------------------


@router.get(
    "/export/{export_job_id}/status",
    response_model=ExportStatusResponse,
    status_code=200,
    summary="Poll the status of an asynchronous Excel export job",
    description=(
        "Returns the current lifecycle state of the background export job.  "
        "When ``status`` is ``SUCCESS``, the ``download_url`` field contains a "
        "pre-signed S3 GET URL valid for ``export_signed_url_expiry_seconds`` "
        "(default 24 hours).  "
        "When ``status`` is ``FAILED``, ``error_message`` contains a diagnostic "
        "snippet.  "
        "Tenant-scoped — callers can only access their own export records."
    ),
)
async def get_export_status(
    export_job_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    ctx: AuthRequestContext = Depends(require_authenticated),
) -> ExportStatusResponse:
    """
    Return the current status of an ExcelExportJob.

    Steps:
      1. Fetch ExcelExportJob by primary key.
      2. Verify it belongs to the caller's tenant (row-level isolation).
      3. Return status + download_url (SUCCESS) or error_message (FAILED).
    """
    from apps.api.models import ExcelExportJob  # deferred

    export_job: ExcelExportJob | None = await session.get(
        ExcelExportJob, export_job_id
    )

    if export_job is None or export_job.tenant_id != ctx.tenant_id:
        raise APIError(
            code="EXPORT_RECORD_NOT_FOUND",
            message=f"Export job {export_job_id} not found.",
            status_code=404,
            details={"export_job_id": str(export_job_id)},
        )

    return ExportStatusResponse(
        id=str(export_job.id),
        job_id=str(export_job.job_id),
        tenant_id=str(export_job.tenant_id),
        status=export_job.status,
        download_url=export_job.download_url,
        error_message=export_job.error_message,
        created_at=export_job.created_at.isoformat(),
        updated_at=export_job.updated_at.isoformat(),
    )
