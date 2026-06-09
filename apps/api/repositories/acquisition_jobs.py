"""
AcquisitionJob repository — database operations for acquisition job management.

Follows the same conventions as FilingRepository (M3.3) and
StoredDocumentRepository (M3.6):
  - No tenant_id: acquisition jobs are platform-wide records.
  - Session is never committed here; the caller owns the transaction boundary.
  - flush() after add/modify so generated values are available before commit.

Milestone: M3.7 — Acquisition Jobs
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import AcquisitionJob, AcquisitionJobStatus
from apps.api.schemas.acquisition_jobs import AcquisitionJobCreate, AcquisitionJobUpdate

log = structlog.get_logger(__name__)

#: Columns on AcquisitionJob that may be modified via AcquisitionJobUpdate.
_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "status",
    "cik",
    "company_name",
    "error_message",
    "filings_discovered",
    "filings_new",
    "documents_fetched",
    "documents_stored",
    "started_at",
    "completed_at",
})


class AcquisitionJobRepository:
    """
    Database access layer for AcquisitionJob records.

    Instantiated per-session::

        repo = AcquisitionJobRepository(session)
        job = await repo.create(AcquisitionJobCreate(ticker="AAPL"))
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(self, schema: AcquisitionJobCreate) -> AcquisitionJob:
        """
        Persist a new AcquisitionJob with status=pending.

        Returns the ORM instance with id and timestamps populated.
        """
        job = AcquisitionJob(
            ticker=schema.ticker,
            job_type=schema.job_type,
        )
        self._session.add(job)
        await self._session.flush([job])
        log.debug(
            "acquisition_job.repository.created",
            job_id=str(job.id),
            ticker=job.ticker,
        )
        return job

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(self, job_id: uuid.UUID) -> AcquisitionJob | None:
        result = await self._session.execute(
            select(AcquisitionJob).where(AcquisitionJob.id == job_id)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        ticker: str | None = None,
        cik: str | None = None,
    ) -> tuple[list[AcquisitionJob], int]:
        """
        Return a paginated, optionally filtered list of jobs.

        Returns (items, total) — total is the count across all pages.
        Results are ordered by created_at descending (most recent first).
        """
        conditions: list[Any] = []
        if status is not None:
            conditions.append(AcquisitionJob.status == status.lower())
        if ticker is not None:
            conditions.append(AcquisitionJob.ticker == ticker.strip().upper())
        if cik is not None:
            conditions.append(AcquisitionJob.cik == cik.strip().zfill(10))

        count_q = select(func.count()).select_from(AcquisitionJob)
        if conditions:
            count_q = count_q.where(*conditions)
        total: int = (await self._session.execute(count_q)).scalar_one()

        offset = (page - 1) * page_size
        data_q = (
            select(AcquisitionJob)
            .order_by(AcquisitionJob.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        if conditions:
            data_q = data_q.where(*conditions)
        items = list((await self._session.execute(data_q)).scalars().all())

        return items, total

    async def list_by_status(
        self, status: str, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[AcquisitionJob], int]:
        return await self.list(page=page, page_size=page_size, status=status)

    # ── Update ────────────────────────────────────────────────────────────────

    async def update(
        self, job_id: uuid.UUID, schema: AcquisitionJobUpdate
    ) -> AcquisitionJob | None:
        """
        Apply a partial update to an AcquisitionJob record.

        Only fields present in schema.model_fields_set are written.
        Returns the updated job, or None if not found.
        """
        job = await self.get_by_id(job_id)
        if job is None:
            return None

        changed = False
        for field in schema.model_fields_set & _UPDATABLE_FIELDS:
            new_value = getattr(schema, field)
            if getattr(job, field) != new_value:
                setattr(job, field, new_value)
                changed = True

        if changed:
            job.updated_at = datetime.now(UTC)
            await self._session.flush([job])
            log.debug(
                "acquisition_job.repository.updated",
                job_id=str(job_id),
                fields=sorted(schema.model_fields_set & _UPDATABLE_FIELDS),
            )

        return job
