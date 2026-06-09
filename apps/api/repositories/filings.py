"""
Filing repository — all database operations for filing record management.

Engineering Specification references:
  M3 Execution Plan, M3.3  — Filing Models milestone

Repository contract (matches SourceConfigRepository conventions from M3.1):
  - No tenant_id argument — filings are global system records, not per-tenant.
    All queries are platform-wide; there is no per-tenant isolation layer.
  - All write methods call ``session.flush([obj])`` after adding/modifying
    objects so that database-generated values are populated before the
    caller's transaction is committed.
  - The session is NEVER committed here; the caller owns the transaction
    boundary (typically via the ``get_db`` FastAPI dependency or a Celery
    task's session context).
  - No soft delete: filings reach terminal states ('downloaded', 'failed')
    via status transitions; ``delete`` performs a hard delete.
    Hard delete is provided for administrative cleanup only.

Pagination:
  ``list`` returns a ``(items, total)`` tuple where ``total`` is the count
  of all matching rows across all pages, and ``items`` is the current page's
  data.  Two queries are executed: a COUNT and a SELECT.

Specialist methods:
  ``list_by_company``      — filter by company_id (FK lookup, indexed).
  ``list_by_filing_type``  — filter by filing_type (indexed).
  ``exists_accession_number`` — boolean duplicate check before insert.

Milestone: M3.3 — Filing Models
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import Filing, FilingDocument, FilingStatus
from apps.api.schemas.filings import FilingCreate, FilingUpdate

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Column allowlist for partial updates
# ---------------------------------------------------------------------------

#: Columns on ``Filing`` that may be modified by ``FilingUpdate``.
#: ``accession_number`` is deliberately excluded — it is immutable after creation.
#: ``id``, ``cik``, ``filing_type``, ``filing_date``, and timestamp columns
#: are never writable via update.
_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "company_id",
    "source_config_id",
    "status",
    "document_url",
    "filing_url",
    "title",
    "ticker",
    "period_end_date",
    "fiscal_year",      # M3.3 — set by extraction pipeline after XBRL parsing
    "fiscal_period",    # M3.3 — set by extraction pipeline after XBRL parsing
    "filing_metadata",
})


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class FilingRepository:
    """
    Database access layer for filing record operations.

    Instantiated per-request or per-task, receiving an ``AsyncSession``::

        repo = FilingRepository(session)
        filing = await repo.get_by_accession_number("0000320193-23-000077")
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(self, schema: FilingCreate) -> Filing:
        """
        Persist a new Filing record.

        All fields from the validated schema are written.  The caller (service
        layer) must call ``exists_accession_number`` before ``create`` to
        enforce BR-2, or catch ``IntegrityError`` for the unique constraint
        violation and raise an appropriate ``ConflictError``.

        Args:
            schema: Validated ``FilingCreate`` Pydantic model.

        Returns:
            Persisted ``Filing`` instance with ``id`` and timestamps populated.
        """
        filing = Filing(
            company_id=schema.company_id,
            source_config_id=schema.source_config_id,
            filing_type=schema.filing_type,
            accession_number=schema.accession_number,
            filing_date=schema.filing_date,
            period_end_date=schema.period_end_date,
            cik=schema.cik,
            ticker=schema.ticker,
            title=schema.title,
            filing_url=schema.filing_url,
            document_url=schema.document_url,
            status=schema.status,
            filing_metadata=schema.filing_metadata,
        )
        self._session.add(filing)
        await self._session.flush([filing])
        log.debug(
            "filing.repository.created",
            filing_id=str(filing.id),
            accession_number=filing.accession_number,
            filing_type=filing.filing_type,
            cik=filing.cik,
        )
        return filing

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(self, filing_id: uuid.UUID) -> Filing | None:
        """
        Fetch a single filing by its primary key.

        Returns ``None`` if no filing exists with the given ID.
        Callers should raise HTTP 404 on ``None``.

        Args:
            filing_id: UUID primary key of the filing.

        Returns:
            ``Filing`` ORM instance or ``None``.
        """
        result = await self._session.execute(
            select(Filing).where(Filing.id == filing_id)
        )
        return result.scalar_one_or_none()

    async def get_by_accession_number(
        self, accession_number: str
    ) -> Filing | None:
        """
        Fetch a single filing by its SEC EDGAR accession number.

        Performs a case-sensitive match on the stored value.
        Callers should ensure the accession number is in canonical form
        (already enforced by ``FilingCreate`` validation).

        Args:
            accession_number: SEC accession number in 'XXXXXXXXXX-YY-ZZZZZZ' form.

        Returns:
            ``Filing`` ORM instance or ``None``.
        """
        result = await self._session.execute(
            select(Filing).where(
                Filing.accession_number == accession_number
            )
        )
        return result.scalar_one_or_none()

    async def exists_accession_number(self, accession_number: str) -> bool:
        """
        Check whether a filing with the given accession number already exists.

        Used by the service layer to enforce BR-2 (duplicate filing prohibited)
        before calling ``create``.  More efficient than ``get_by_accession_number``
        because it uses SELECT 1 rather than selecting the full row.

        Args:
            accession_number: SEC accession number to check.

        Returns:
            True if a filing with this accession number exists, False otherwise.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(Filing)
            .where(Filing.accession_number == accession_number)
        )
        count: int = result.scalar_one()
        return count > 0

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
    ) -> tuple[list[Filing], int]:
        """
        Return a paginated, optionally filtered list of filings.

        Two database queries are executed:
          1. A COUNT to determine the total number of matching rows.
          2. A SELECT with LIMIT / OFFSET to fetch the current page.

        Results are ordered by ``filing_date`` descending (most recent first),
        then by ``created_at`` descending as a tiebreaker.

        Args:
            page:             1-based page number (default 1).
            page_size:        Rows per page (default 20; max 100 enforced by schema).
            filing_type:      Filter by SEC form type (e.g. '10-K').
            status:           Filter by lifecycle status (e.g. 'discovered').
            cik:              Filter by SEC CIK (exact match on padded value).
            ticker:           Filter by ticker symbol (case-insensitive).
            company_id:       Filter by linked company UUID.
            source_config_id: Filter by linked source config UUID.

        Returns:
            ``(items, total)`` tuple.
        """
        conditions: list[Any] = []

        if filing_type is not None:
            conditions.append(Filing.filing_type == filing_type)
        if status is not None:
            conditions.append(Filing.status == status.lower())
        if cik is not None:
            conditions.append(Filing.cik == cik.strip().zfill(10))
        if ticker is not None:
            conditions.append(Filing.ticker == ticker.strip().upper())
        if company_id is not None:
            conditions.append(Filing.company_id == company_id)
        if source_config_id is not None:
            conditions.append(Filing.source_config_id == source_config_id)

        # ── Count query ───────────────────────────────────────────────────────
        count_q = select(func.count()).select_from(Filing)
        if conditions:
            count_q = count_q.where(*conditions)
        count_result = await self._session.execute(count_q)
        total: int = count_result.scalar_one()

        # ── Data query ────────────────────────────────────────────────────────
        offset = (page - 1) * page_size
        data_q = (
            select(Filing)
            .order_by(Filing.filing_date.desc(), Filing.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        if conditions:
            data_q = data_q.where(*conditions)
        data_result = await self._session.execute(data_q)
        items = list(data_result.scalars().all())

        return items, total

    # ── Specialist list methods ───────────────────────────────────────────────

    async def list_by_company(
        self,
        company_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        filing_type: str | None = None,
        status: str | None = None,
    ) -> tuple[list[Filing], int]:
        """
        Return a paginated list of filings linked to a specific company.

        Shorthand for ``list(company_id=company_id, ...)``.  Provided as a
        named method to make intent clear at call sites in acquisition workers.

        Args:
            company_id:  UUID of the company to filter on.
            page:        1-based page number.
            page_size:   Rows per page.
            filing_type: Optional filter by form type.
            status:      Optional filter by lifecycle status.

        Returns:
            ``(items, total)`` tuple.
        """
        return await self.list(
            page=page,
            page_size=page_size,
            company_id=company_id,
            filing_type=filing_type,
            status=status,
        )

    async def list_by_filing_type(
        self,
        filing_type: str,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
    ) -> tuple[list[Filing], int]:
        """
        Return a paginated list of filings of a specific type.

        Shorthand for ``list(filing_type=filing_type, ...)``.

        Args:
            filing_type: SEC form type to filter on (e.g. '10-K').
            page:        1-based page number.
            page_size:   Rows per page.
            status:      Optional filter by lifecycle status.

        Returns:
            ``(items, total)`` tuple.
        """
        return await self.list(
            page=page,
            page_size=page_size,
            filing_type=filing_type,
            status=status,
        )

    # ── Update ────────────────────────────────────────────────────────────────

    async def update(
        self,
        filing_id: uuid.UUID,
        schema: FilingUpdate,
    ) -> Filing | None:
        """
        Apply a partial update to a filing record.

        Only the fields explicitly set in the ``FilingUpdate`` schema are
        written to the database (uses ``schema.model_fields_set``).
        ``accession_number`` is excluded from ``_UPDATABLE_FIELDS`` and can
        never be updated — it is immutable after creation.

        Args:
            filing_id: UUID of the filing to update.
            schema:    Validated ``FilingUpdate`` Pydantic model.

        Returns:
            Updated ``Filing`` ORM instance, or ``None`` if not found.
        """
        filing = await self.get_by_id(filing_id)
        if filing is None:
            return None

        changed = False
        for field in schema.model_fields_set & _UPDATABLE_FIELDS:
            new_value = getattr(schema, field)
            if getattr(filing, field) != new_value:
                setattr(filing, field, new_value)
                changed = True

        if changed:
            filing.updated_at = datetime.now(UTC)
            await self._session.flush([filing])
            log.debug(
                "filing.repository.updated",
                filing_id=str(filing_id),
                accession_number=filing.accession_number,
                fields=sorted(schema.model_fields_set & _UPDATABLE_FIELDS),
            )

        return filing

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete(self, filing_id: uuid.UUID) -> bool:
        """
        Hard-delete a filing record from the database.

        This permanently removes the row.  Intended for administrative
        cleanup only — the normal lifecycle is to transition status to
        'failed' rather than deleting failed records.

        Args:
            filing_id: UUID of the filing to delete.

        Returns:
            True if the filing was found and deleted.
            False if the filing was not found.
        """
        filing = await self.get_by_id(filing_id)
        if filing is None:
            return False

        await self._session.delete(filing)
        await self._session.flush()
        log.debug(
            "filing.repository.deleted",
            filing_id=str(filing_id),
            accession_number=filing.accession_number,
        )
        return True

    # ── M3.3 named interface ──────────────────────────────────────────────────
    # These methods expose the exact contract specified in M3.3 and used by
    # acquisition workers throughout M3.4 – M3.7.  They are thin wrappers or
    # direct implementations that complement the generic CRUD methods above.

    async def create_filing_record(
        self,
        *,
        company_id: uuid.UUID | None,
        filing_type: str,
        accession_number: str,
        filing_date: Any,
        cik: str,
        fiscal_year: int | None = None,
        fiscal_period: str | None = None,
        source_config_id: uuid.UUID | None = None,
        period_end_date: Any | None = None,
        ticker: str | None = None,
        title: str | None = None,
        filing_url: str | None = None,
        document_url: str | None = None,
        status: str = FilingStatus.DISCOVERED.value,
        filing_metadata: dict[str, Any] | None = None,
    ) -> Filing:
        """
        Create and persist a new Filing record (M3.3 named interface).

        Preferred entry point for acquisition workers that need to express
        fiscal coordinates (fiscal_year, fiscal_period) at creation time.
        The generic ``create(schema)`` method does not carry those fields
        because the public ``FilingCreate`` schema predates M3.3's fiscal
        columns; this method bypasses the schema and writes ORM attributes
        directly.

        All field semantics are identical to the ``Filing`` ORM model.
        The caller owns the transaction boundary and must commit (or rely
        on the ``get_db`` dependency to commit on response).

        Args:
            company_id:        Linked company UUID, or None if not yet resolved.
            filing_type:       SEC form type string (e.g. '10-K', '10-Q').
            accession_number:  SEC EDGAR accession number (globally unique).
            filing_date:       Date of submission to SEC EDGAR.
            cik:               10-digit zero-padded SEC CIK string.
            fiscal_year:       4-digit fiscal year (e.g. 2024).  None until known.
            fiscal_period:     Period label: 'FY', 'Q1', 'Q2', 'Q3', 'Q4'. None until known.
            source_config_id:  Source config UUID, or None.
            period_end_date:   Fiscal period end date, or None.
            ticker:            Ticker symbol, or None.
            title:             Human-readable filing title, or None.
            filing_url:        URL to the SEC EDGAR filing index page, or None.
            document_url:      URL to the primary document, or None.
            status:            Initial lifecycle status (default: 'discovered').
            filing_metadata:   Arbitrary metadata blob, or None.

        Returns:
            Persisted ``Filing`` ORM instance with id and timestamps populated.
        """
        filing = Filing(
            company_id=company_id,
            source_config_id=source_config_id,
            filing_type=filing_type,
            accession_number=accession_number,
            filing_date=filing_date,
            period_end_date=period_end_date,
            cik=cik,
            ticker=ticker,
            title=title,
            filing_url=filing_url,
            document_url=document_url,
            status=status,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing_metadata=filing_metadata,
        )
        self._session.add(filing)
        await self._session.flush([filing])
        log.debug(
            "filing.repository.create_filing_record",
            filing_id=str(filing.id),
            accession_number=filing.accession_number,
            filing_type=filing.filing_type,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            cik=cik,
        )
        return filing

    async def add_document_to_filing(
        self,
        *,
        filing_id: uuid.UUID,
        document_type: str,
        source_url: str,
        s3_key: str | None = None,
        file_hash: str | None = None,
    ) -> FilingDocument:
        """
        Create and persist a FilingDocument record attached to a filing.

        Called by the document fetcher (M3.5) when individual documents
        within a filing index are discovered and/or stored.  May be called
        multiple times per filing — once for each document type
        (PRIMARY_HTML, XBRL_XML, PDF, etc.).

        The unique constraint ``uq_filing_documents_filing_source`` on
        ``(filing_id, source_url)`` prevents duplicates.  The caller should
        catch ``IntegrityError`` and treat it as an idempotent no-op, or call
        ``get_filing_by_id`` first to check for existing documents.

        Args:
            filing_id:     UUID of the parent Filing record.
            document_type: Document role: 'XBRL_XML', 'PRIMARY_HTML', 'PDF',
                           'EXHIBIT', or 'R_FILE'.
            source_url:    Original URL the document is fetched from.
            s3_key:        S3 object key after upload.  None until stored.
            file_hash:     SHA-256 hex digest of raw file bytes.  None until fetched.

        Returns:
            Persisted ``FilingDocument`` ORM instance.
        """
        doc = FilingDocument(
            filing_id=filing_id,
            document_type=document_type,
            source_url=source_url,
            s3_key=s3_key,
            file_hash=file_hash,
        )
        self._session.add(doc)
        await self._session.flush([doc])
        log.debug(
            "filing.repository.add_document_to_filing",
            filing_document_id=str(doc.id),
            filing_id=str(filing_id),
            document_type=document_type,
            has_s3_key=s3_key is not None,
            has_file_hash=file_hash is not None,
        )
        return doc

    async def get_filing_by_id(self, filing_id: uuid.UUID) -> Filing | None:
        """
        Fetch a single Filing by its primary key (M3.3 named interface).

        Alias for ``get_by_id`` — provides the name used throughout the M3
        acquisition worker codebase so call sites read uniformly as
        ``repo.get_filing_by_id(job.filing_id)``.

        Args:
            filing_id: UUID primary key of the filing.

        Returns:
            ``Filing`` ORM instance, or ``None`` if not found.
        """
        return await self.get_by_id(filing_id)

    async def update_filing_status(
        self,
        filing_id: uuid.UUID,
        status: str,
    ) -> Filing | None:
        """
        Transition a filing's lifecycle status (M3.3 named interface).

        Convenience method for the single most common update performed by
        acquisition workers: advancing a filing from one lifecycle state to
        the next (e.g. 'discovered' → 'downloading' → 'downloaded').

        The caller is responsible for validating that ``status`` is a member
        of ``FilingStatus`` before calling this method.

        Args:
            filing_id: UUID of the filing to update.
            status:    Target lifecycle status string (e.g. 'downloading').

        Returns:
            Updated ``Filing`` ORM instance with ``status`` and
            ``updated_at`` refreshed, or ``None`` if not found.
        """
        filing = await self.get_by_id(filing_id)
        if filing is None:
            log.warning(
                "filing.repository.update_filing_status.not_found",
                filing_id=str(filing_id),
                target_status=status,
            )
            return None

        previous_status = filing.status
        filing.status = status
        filing.updated_at = datetime.now(UTC)
        await self._session.flush([filing])
        log.debug(
            "filing.repository.update_filing_status",
            filing_id=str(filing_id),
            accession_number=filing.accession_number,
            previous_status=previous_status,
            new_status=status,
        )
        return filing
