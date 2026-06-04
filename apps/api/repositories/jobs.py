"""
FinancialJob repository — all database operations for job management.

Engineering Specification references:
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables
  M2 Execution Plan, Section 2.3.3 — job lifecycle state transitions
  M2 Execution Plan, Section 6.5   — tenant isolation: enforced at repository layer

Repository contract (matches M1 AuthRepository conventions):
  - All public methods accept ``tenant_id`` as the first positional argument.
  - The session is NEVER committed here; the caller owns the transaction.
  - ``flush([obj])`` is called after writes to populate generated values.

Lifecycle state transitions (M2 Execution Plan, Section 2.3.3):
  pending  → queued     Celery task accepted (M4+)
  queued   → running    Worker picks up the task (M4+)
  running  → completed  Extraction + export finished (M4+)
  running  → failed     Unhandled exception in worker (M4+)
  pending/queued/running → cancelled  API cancel request (M2)

Note on cancel:
  ``cancel`` sets ``status = 'cancelled'`` and ``completed_at = NOW()``.
  If a Celery task ID is present, the caller is responsible for revoking the
  Celery task (the repository layer is not coupled to Celery).

Milestone: M2-Step 5
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import FinancialJob, JobStatus
from apps.api.schemas.jobs import JobCreate, JobUpdate

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Column allowlist for status-transition updates
# ---------------------------------------------------------------------------

#: Fields on ``FinancialJob`` that ``update_status`` may modify.
#: Read-only fields (id, tenant_id, company_id, job_type, created_at) are
#: excluded to prevent accidental overwrites.
_STATUS_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "status",
    "error_message",
    "celery_task_id",
    "started_at",
    "completed_at",
})


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class JobRepository:
    """
    Database access layer for FinancialJob operations.

    Instantiated per-request inside route handlers, receiving the
    ``AsyncSession`` from the ``get_db`` FastAPI dependency::

        repo = JobRepository(db)
        job = await repo.get_by_id(ctx.tenant_id, job_id)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(
        self,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        created_by: uuid.UUID,
        schema: JobCreate,
    ) -> FinancialJob:
        """
        Persist a new FinancialJob in PENDING state.

        The job is not dispatched to Celery at this point — the caller
        triggers dispatch after the source document is uploaded
        (POST /jobs/{id}/upload-complete in M2-Step 8).

        Args:
            tenant_id:  Tenant scope (injected from JWT payload).
            company_id: Company this job processes. Must belong to the tenant.
            created_by: UUID of the authenticated user creating the job.
            schema:     Validated ``JobCreate`` Pydantic model.

        Returns:
            Persisted ``FinancialJob`` with ``id`` and timestamps populated.
        """
        job = FinancialJob(
            tenant_id=tenant_id,
            company_id=company_id,
            created_by=created_by,
            job_type=schema.job_type,
            fiscal_year=schema.fiscal_year,
            status=JobStatus.PENDING.value,
        )
        self._session.add(job)
        await self._session.flush([job])
        log.debug(
            "job.repository.created",
            job_id=str(job.id),
            tenant_id=str(tenant_id),
            company_id=str(company_id),
            job_type=job.job_type,
        )
        return job

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> FinancialJob | None:
        """
        Fetch a single job by its primary key, scoped to the tenant.

        Returns ``None`` if the job does not exist or belongs to a different
        tenant.  Callers should return HTTP 404 on ``None`` — do NOT return 403
        for wrong-tenant rows, as that would leak existence information.

        Args:
            tenant_id: Tenant scope from the authenticated request.
            job_id:    UUID of the job to fetch.

        Returns:
            ``FinancialJob`` ORM instance or ``None``.
        """
        result = await self._session.execute(
            select(FinancialJob).where(
                FinancialJob.id == job_id,
                FinancialJob.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        tenant_id: uuid.UUID,
        *,
        company_id: uuid.UUID | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FinancialJob], int]:
        """
        Return a paginated, optionally filtered list of jobs.

        Two database queries are executed:
          1. A COUNT with the same WHERE conditions.
          2. A SELECT with LIMIT / OFFSET.

        Args:
            tenant_id:  Tenant scope.
            company_id: When provided, only jobs for this company are returned.
            status:     When provided, only jobs with this status are returned.
                        Must be a valid ``JobStatus`` value.
            page:       1-based page number.
            page_size:  Rows per page.

        Returns:
            ``(items, total)`` tuple.
        """
        conditions: list[Any] = [FinancialJob.tenant_id == tenant_id]

        if company_id is not None:
            conditions.append(FinancialJob.company_id == company_id)
        if status is not None:
            conditions.append(FinancialJob.status == status)

        # ── Count query ───────────────────────────────────────────────────────
        count_result = await self._session.execute(
            select(func.count()).select_from(FinancialJob).where(*conditions)
        )
        total: int = count_result.scalar_one()

        # ── Data query ────────────────────────────────────────────────────────
        offset = (page - 1) * page_size
        data_result = await self._session.execute(
            select(FinancialJob)
            .where(*conditions)
            .order_by(FinancialJob.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list(data_result.scalars().all())

        return items, total

    # ── Status transitions ────────────────────────────────────────────────────

    async def update_status(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        schema: JobUpdate,
    ) -> FinancialJob | None:
        """
        Apply a status-transition update to a job.

        Only the fields in ``JobUpdate.model_fields_set`` that are also in
        ``_STATUS_UPDATABLE_FIELDS`` are written.  This mirrors the
        ``CompanyRepository.update`` pattern for partial updates.

        Used internally by worker callbacks and the cancel endpoint.  The
        caller is responsible for validating that the transition is legal
        (e.g. only non-terminal jobs can be cancelled).

        Args:
            tenant_id: Tenant scope.
            job_id:    UUID of the job to update.
            schema:    Validated ``JobUpdate`` Pydantic model.

        Returns:
            Updated ``FinancialJob`` ORM instance, or ``None`` if not found.
        """
        job = await self.get_by_id(tenant_id, job_id)
        if job is None:
            return None

        changed = False
        for field in schema.model_fields_set & _STATUS_UPDATABLE_FIELDS:
            new_value = getattr(schema, field)
            if getattr(job, field) != new_value:
                setattr(job, field, new_value)
                changed = True

        if changed:
            job.updated_at = datetime.now(UTC)
            await self._session.flush([job])
            log.debug(
                "job.repository.status_updated",
                job_id=str(job_id),
                tenant_id=str(tenant_id),
                fields=sorted(schema.model_fields_set & _STATUS_UPDATABLE_FIELDS),
                new_status=job.status,
            )

        return job

    async def cancel(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> FinancialJob | None:
        """
        Cancel a job by transitioning it to the CANCELLED terminal state.

        Only jobs in a cancellable state (PENDING, QUEUED, RUNNING) may be
        cancelled.  If the job is already in a terminal state, this method
        returns the job unchanged — the caller decides whether to raise an
        error or treat it as idempotent.

        Note on Celery:
          If ``job.celery_task_id`` is set, the caller should revoke the
          Celery task via ``celery_app.control.revoke(task_id, terminate=True)``
          before calling this method (or concurrently).  The repository is
          not coupled to Celery.

        Args:
            tenant_id: Tenant scope.
            job_id:    UUID of the job to cancel.

        Returns:
            The ``FinancialJob`` ORM instance (possibly unchanged if already
            terminal), or ``None`` if the job was not found.
        """
        job = await self.get_by_id(tenant_id, job_id)
        if job is None:
            return None

        # Only transition if the job is in a cancellable state.
        if job.status not in (
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
        ):
            log.debug(
                "job.repository.cancel_skipped_terminal",
                job_id=str(job_id),
                current_status=job.status,
            )
            return job

        now = datetime.now(UTC)
        job.status = JobStatus.CANCELLED.value
        job.completed_at = now
        job.updated_at = now
        await self._session.flush([job])
        log.debug(
            "job.repository.cancelled",
            job_id=str(job_id),
            tenant_id=str(tenant_id),
        )
        return job

    # ── Convenience ──────────────────────────────────────────────────────────

    async def set_document_url(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        document_url: str,
    ) -> FinancialJob | None:
        """
        Record the S3 key of the uploaded source document.

        Called by POST /jobs/{id}/upload-complete (M2-Step 8) after the
        client confirms that the pre-signed upload was successful.

        Args:
            tenant_id:    Tenant scope.
            job_id:       UUID of the job.
            document_url: S3 object key in the format
                          ``{tenant_id}/jobs/{job_id}/{filename}``.

        Returns:
            Updated ``FinancialJob`` ORM instance, or ``None`` if not found.
        """
        job = await self.get_by_id(tenant_id, job_id)
        if job is None:
            return None

        job.document_url = document_url
        job.updated_at = datetime.now(UTC)
        await self._session.flush([job])
        log.debug(
            "job.repository.document_url_set",
            job_id=str(job_id),
            tenant_id=str(tenant_id),
        )
        return job
