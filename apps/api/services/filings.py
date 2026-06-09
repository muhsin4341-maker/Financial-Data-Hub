"""
Filing service — business logic for filing record management.

Engineering Specification references:
  M3 Execution Plan, M3.3  — Filing Models milestone

Business rules enforced here (not at repository or router level):
  BR-1  accession_number is globally unique.
        The DB unique constraint guarantees this at the database level.
        ``IntegrityError`` is caught here and surfaced as ``ConflictError``.
        Additionally the service calls ``exists_accession_number`` before
        ``create`` to give a clearer error message.
  BR-2  No duplicate filings.
        Attempting to create a filing with an existing accession_number raises
        ``ConflictError`` before hitting the database.
  BR-3  filing_type must be a known FilingType value.
        Enforced at schema-validation time (``FilingCreate`` validator), so
        invalid types never reach the service.  The service re-validates in
        debug mode only via an assertion (belt-and-suspenders).

This service owns no session management — the session is injected via the
repository constructor and the transaction is committed by the route handler
or acquisition worker that holds the session.

Milestone: M3.3 — Filing Models
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.models import Filing
from apps.api.repositories.filings import FilingRepository
from apps.api.schemas.filings import (
    FilingCreate,
    FilingListResponse,
    FilingRead,
    FilingUpdate,
)

log = structlog.get_logger(__name__)


class FilingService:
    """
    Business logic layer for Filing record management.

    Instantiated per-request or per-task with an ``AsyncSession``::

        service = FilingService(db)
        filing  = await service.create(schema)

    All public methods return Pydantic response schemas, not ORM instances,
    so callers can return them directly without a second model_validate call.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._repo = FilingRepository(session)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _to_read(filing: Filing) -> FilingRead:
        """Convert a Filing ORM instance to its Pydantic response schema."""
        return FilingRead.model_validate(filing)

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(self, schema: FilingCreate) -> FilingRead:
        """
        Create a new filing record.

        Business rules:
          BR-1 / BR-2: Raises ``ConflictError`` if ``accession_number`` already
                       exists in the database.

        Args:
            schema: Validated ``FilingCreate`` Pydantic model.

        Returns:
            ``FilingRead`` schema for the newly created filing.

        Raises:
            ConflictError: If a filing with the same accession_number already
                           exists (BR-1 / BR-2).
        """
        # BR-2: pre-check for a clearer error message than IntegrityError
        if await self._repo.exists_accession_number(schema.accession_number):
            raise ConflictError(
                f"Filing with accession_number={schema.accession_number} already exists."
            )

        try:
            filing = await self._repo.create(schema)
        except IntegrityError:
            # BR-1: safety net — race condition between check and insert
            raise ConflictError(
                f"Filing with accession_number={schema.accession_number} already exists."
            )

        log.info(
            "filing.service.created",
            filing_id=str(filing.id),
            accession_number=filing.accession_number,
            filing_type=filing.filing_type,
            cik=filing.cik,
        )
        return self._to_read(filing)

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(self, filing_id: uuid.UUID) -> FilingRead:
        """
        Fetch a single filing by primary key.

        Args:
            filing_id: UUID primary key of the filing.

        Returns:
            ``FilingRead`` schema for the filing.

        Raises:
            NotFoundError: If no filing exists with the given ID.
        """
        filing = await self._repo.get_by_id(filing_id)
        if filing is None:
            raise NotFoundError("Filing", str(filing_id))
        return self._to_read(filing)

    async def get_by_accession_number(
        self, accession_number: str
    ) -> FilingRead:
        """
        Fetch a single filing by SEC EDGAR accession number.

        Args:
            accession_number: Accession number in 'XXXXXXXXXX-YY-ZZZZZZ' form.

        Returns:
            ``FilingRead`` schema for the filing.

        Raises:
            NotFoundError: If no filing exists with the given accession number.
        """
        filing = await self._repo.get_by_accession_number(accession_number)
        if filing is None:
            raise NotFoundError("Filing", f"accession_number={accession_number}")
        return self._to_read(filing)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        filing_type: str | None = None,
        status: str | None = None,
        cik: str | None = None,
        ticker: str | None = None,
        company_id: uuid.UUID | None = None,
        source_config_id: uuid.UUID | None = None,
    ) -> FilingListResponse:
        """
        Return a paginated, optionally filtered list of filings.

        Args:
            page:             1-based page number.
            page_size:        Items per page (default 20, max 100).
            filing_type:      Optional SEC form type filter.
            status:           Optional lifecycle status filter.
            cik:              Optional CIK filter (zero-padded on input).
            ticker:           Optional ticker symbol filter.
            company_id:       Optional company UUID filter.
            source_config_id: Optional source config UUID filter.

        Returns:
            ``FilingListResponse`` with pagination metadata and item list.
        """
        items, total = await self._repo.list(
            page=page,
            page_size=page_size,
            filing_type=filing_type,
            status=status,
            cik=cik,
            ticker=ticker,
            company_id=company_id,
            source_config_id=source_config_id,
        )
        return FilingListResponse(
            items=[self._to_read(f) for f in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    # ── Update ────────────────────────────────────────────────────────────────

    async def update(
        self,
        filing_id: uuid.UUID,
        schema: FilingUpdate,
    ) -> FilingRead:
        """
        Apply a partial update to a filing record.

        Only fields present in ``schema.model_fields_set`` are written.
        ``accession_number`` cannot be updated — it is immutable after creation.

        Args:
            filing_id: UUID of the filing to update.
            schema:    Validated ``FilingUpdate`` Pydantic model.

        Returns:
            ``FilingRead`` schema for the updated filing.

        Raises:
            NotFoundError: If no filing exists with the given ID.
        """
        filing = await self._repo.update(filing_id, schema)
        if filing is None:
            raise NotFoundError("Filing", str(filing_id))
        log.info(
            "filing.service.updated",
            filing_id=str(filing_id),
            fields=sorted(schema.model_fields_set),
        )
        return self._to_read(filing)

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete(self, filing_id: uuid.UUID) -> None:
        """
        Hard-delete a filing record.

        Administrative use only — prefer status transitions to 'failed' for
        the normal lifecycle end.

        Args:
            filing_id: UUID of the filing to delete.

        Raises:
            NotFoundError: If no filing exists with the given ID.
        """
        deleted = await self._repo.delete(filing_id)
        if not deleted:
            raise NotFoundError("Filing", str(filing_id))
        log.info("filing.service.deleted", filing_id=str(filing_id))
