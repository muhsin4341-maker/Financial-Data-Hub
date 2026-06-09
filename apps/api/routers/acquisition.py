"""
Acquisition router — lifecycle endpoints for SEC filing acquisition jobs.

Endpoints:
  POST /api/v1/acquisition/jobs              — Create & dispatch acquisition job
  GET  /api/v1/acquisition/jobs              — List jobs with pagination + filters
  GET  /api/v1/acquisition/jobs/{job_id}     — Get job by ID (status + counters)
  POST /api/v1/acquisition/jobs/{job_id}/retry — Retry a failed job

Authorization:
  - POST / retry : require_analyst  (ANALYST, ADMIN, or OWNER)
  - GET (all)    : require_authenticated (any valid JWT)

Acquisition jobs are platform-wide records (no tenant_id).  Auth context is
required to identify the acting user for audit logs, but no per-tenant
filtering is applied — all authenticated users can see all jobs.

Architecture:
  POST endpoints use AcquisitionJobService (needs AsyncSessionFactory for
  per-operation session management).  GET endpoints use AcquisitionJobRepository
  directly (shares the request-scoped session from get_db for efficiency).

Celery dispatch:
  After creating a job, this router enqueues it to the QUEUE_FETCH Celery
  queue.  If the broker is unreachable, the dispatch failure is logged but
  the HTTP response still returns 202 — the job remains in 'pending' state
  and can be retried via POST /retry.

Error codes:
  404 ACQUISITIONJOB_NOT_FOUND — job does not exist
  409 CONFLICT                 — retry requested on a non-failed job
  422 VALIDATION_ERROR         — request body fails Pydantic validation
  401 UNAUTHORIZED             — missing or invalid JWT
  403 FORBIDDEN                — authenticated but insufficient role

Milestone: M3.8 — Acquisition APIs
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.database import get_db
from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_analyst,
    require_authenticated,
)
from apps.api.repositories.acquisition_jobs import AcquisitionJobRepository
from apps.api.schemas.acquisition_jobs import (
    AcquisitionJobCreate,
    AcquisitionJobListResponse,
    AcquisitionJobRead,
)
from services.acquisition.jobs.service import AcquisitionJobService
from services.acquisition.storage.backend import StorageBackend

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/acquisition", tags=["acquisition"])


# ---------------------------------------------------------------------------
# Internal helpers — dependency factories
# ---------------------------------------------------------------------------


def _get_storage_backend() -> StorageBackend:
    """
    Return the configured document storage backend.

    Uses S3StorageBackend when AWS credentials are present in settings;
    falls back to LocalStorageBackend(``/tmp/fdh-documents``) for development.
    """
    from apps.api.core.config import get_settings
    settings = get_settings()
    if settings.aws_access_key_id:
        from apps.api.core.s3 import make_s3_client
        from services.acquisition.storage.backend import S3StorageBackend
        return S3StorageBackend(make_s3_client(), settings.s3_documents_bucket)
    from services.acquisition.storage.backend import LocalStorageBackend
    return LocalStorageBackend("/tmp/fdh-documents")


def _get_acquisition_service() -> AcquisitionJobService:
    """
    Build an AcquisitionJobService using the module-level AsyncSessionFactory.

    The service opens its own short-lived sessions per unit of work
    (see AcquisitionJobService docstring for rationale).  Using the
    global factory here avoids a long-lived request session being passed
    into a service that commits independently.
    """
    from apps.api.core.config import get_settings
    from apps.api.core.database import AsyncSessionFactory

    if AsyncSessionFactory is None:
        raise RuntimeError(
            "Database not initialised. "
            "Ensure init_db() is called in the application lifespan."
        )
    settings = get_settings()
    return AcquisitionJobService(
        session_factory=AsyncSessionFactory,
        storage_backend=_get_storage_backend(),
        user_agent=settings.edgar_user_agent,
    )


def _dispatch_celery(job_id: uuid.UUID) -> None:
    """
    Enqueue the acquisition job to the Celery QUEUE_FETCH queue.

    Fails open: if the broker is unreachable, a warning is logged but the
    caller is not interrupted.  The job stays in 'pending' state and can be
    retried via POST /retry.
    """
    try:
        from workers.tasks.acquisition_tasks import run_acquisition_job
        run_acquisition_job.delay(str(job_id))
        log.info("acquisition_job.celery_dispatched", job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "acquisition_job.celery_dispatch_failed",
            job_id=str(job_id),
            error=str(exc),
        )


def _to_list_response(
    items: list,
    total: int,
    page: int,
    page_size: int,
) -> AcquisitionJobListResponse:
    """Build paginated list response from repository results."""
    return AcquisitionJobListResponse(
        items=[AcquisitionJobRead.model_validate(j) for j in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/acquisition/jobs
# ---------------------------------------------------------------------------


@router.post(
    "/jobs",
    response_model=AcquisitionJobRead,
    status_code=202,
    summary="Create and dispatch an acquisition job",
    description=(
        "Create a new SEC filing acquisition job for the given ticker and "
        "dispatch it to the Celery worker queue.  "
        "The job is created synchronously (status=pending) and the response "
        "is returned immediately — the actual acquisition runs asynchronously.  "
        "Poll GET /acquisition/jobs/{id} to track progress.  "
        "Requires ANALYST role or above."
    ),
)
async def create_acquisition_job(
    payload: AcquisitionJobCreate,
    ctx: AuthRequestContext = Depends(require_analyst),  # noqa: ARG001
) -> AcquisitionJobRead:
    """
    Create a pending AcquisitionJob and enqueue it to Celery.

    Steps:
      1. Validate ticker (normalised to uppercase by schema).
      2. Persist via AcquisitionJobService.create_job (status=pending).
      3. Dispatch to Celery QUEUE_FETCH.
      4. Return 202 Accepted with job metadata.
    """
    service = _get_acquisition_service()
    job = await service.create_job(payload.ticker)

    log.info(
        "acquisition_job.api.created",
        job_id=str(job.id),
        ticker=job.ticker,
        actor_user_id=str(ctx.user_id),
    )

    _dispatch_celery(job.id)
    return job


# ---------------------------------------------------------------------------
# GET /api/v1/acquisition/jobs
# ---------------------------------------------------------------------------


@router.get(
    "/jobs",
    response_model=AcquisitionJobListResponse,
    status_code=200,
    summary="List acquisition jobs",
    description=(
        "Return a paginated list of all acquisition jobs.  "
        "Supports filtering by status and ticker.  "
        "Results are ordered by creation time (most recent first)."
    ),
)
async def list_acquisition_jobs(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        20, ge=1, le=100, description="Items per page (max 100)."
    ),
    status: str | None = Query(
        None,
        description=(
            "Filter by lifecycle status: "
            "pending | running | completed | failed."
        ),
    ),
    ticker: str | None = Query(
        None,
        description="Filter by ticker symbol (case-insensitive, e.g. 'AAPL').",
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> AcquisitionJobListResponse:
    repo = AcquisitionJobRepository(db)
    items, total = await repo.list(
        page=page,
        page_size=page_size,
        status=status,
        ticker=ticker,
    )
    return _to_list_response(items, total, page, page_size)


# ---------------------------------------------------------------------------
# GET /api/v1/acquisition/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get(
    "/jobs/{job_id}",
    response_model=AcquisitionJobRead,
    status_code=200,
    summary="Get an acquisition job by ID",
    description=(
        "Return the full detail of a single acquisition job, including "
        "status, progress counters, and timing.  "
        "Returns 404 if the job does not exist."
    ),
)
async def get_acquisition_job(
    job_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> AcquisitionJobRead:
    repo = AcquisitionJobRepository(db)
    job = await repo.get_by_id(job_id)
    if job is None:
        raise NotFoundError("AcquisitionJob", str(job_id))
    return AcquisitionJobRead.model_validate(job)


# ---------------------------------------------------------------------------
# POST /api/v1/acquisition/jobs/{job_id}/retry
# ---------------------------------------------------------------------------


@router.post(
    "/jobs/{job_id}/retry",
    response_model=AcquisitionJobRead,
    status_code=202,
    summary="Retry a failed acquisition job",
    description=(
        "Create a new acquisition job using the same ticker as the failed job "
        "and dispatch it to the worker queue.  "
        "Only jobs with status=failed may be retried.  "
        "Attempting to retry a pending, running, or completed job returns 409.  "
        "Returns 404 if the original job does not exist.  "
        "Requires ANALYST role or above."
    ),
)
async def retry_acquisition_job(
    job_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_analyst),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> AcquisitionJobRead:
    """
    Retry a failed acquisition job by creating a fresh pending job.

    Steps:
      1. Load the original job; 404 if not found.
      2. Verify status=failed; 409 otherwise.
      3. Create a new AcquisitionJob for the same ticker (clean state).
      4. Dispatch new job to Celery.
      5. Return 202 Accepted with new job metadata.

    A new job is preferred over resetting the existing record to preserve
    the original failure audit trail.
    """
    # 1. Load original job
    repo = AcquisitionJobRepository(db)
    original = await repo.get_by_id(job_id)
    if original is None:
        raise NotFoundError("AcquisitionJob", str(job_id))

    # 2. Validate state
    if original.status != "failed":
        raise ConflictError(
            f"Job '{job_id}' has status='{original.status}'. "
            "Only failed jobs may be retried."
        )

    # 3. Create new job
    service = _get_acquisition_service()
    new_job = await service.create_job(original.ticker)

    log.info(
        "acquisition_job.api.retry",
        original_job_id=str(job_id),
        new_job_id=str(new_job.id),
        ticker=new_job.ticker,
        actor_user_id=str(ctx.user_id),
    )

    # 4. Dispatch
    _dispatch_celery(new_job.id)
    return new_job
