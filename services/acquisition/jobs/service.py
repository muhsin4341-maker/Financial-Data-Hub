"""
Acquisition job service — orchestrates the full SEC filing acquisition workflow.

Responsibilities:
  - Create AcquisitionJob records.
  - Execute the end-to-end acquisition pipeline:
      1. Resolve company (ticker → CIK) via CompanyResolverService.
      2. Discover filings via SECEdgarSource.
      3. Filter new filings (FilingRepository.exists_accession_number).
      4. Create Filing records in the database.
      5. Fetch documents via SECFilingDocumentFetcher.
      6. Store documents via DocumentStorageService.
      7. Update Filing status and AcquisitionJob counters.
  - Track per-filing failures without aborting the job.
  - Mark jobs as completed or failed.

Architecture position:
  AcquisitionJobService (M3.7)  ← this module
    ├── CompanyResolverService  (M3.2)
    ├── SECEdgarSource          (M3.4)
    ├── FilingRepository / FilingService (M3.3)
    ├── SECFilingDocumentFetcher (M3.5)
    └── DocumentStorageService  (M3.6)

Session management:
  The service receives a ``session_factory`` (async_sessionmaker) rather than
  a single session.  Each atomic unit of work opens its own session and commits
  independently — this prevents a failed document fetch from rolling back a
  successfully created Filing record.

  Commit points:
    1. Mark job as RUNNING.
    2. Update job with resolved CIK + company_name.
    3. Update job with filings_discovered count.
    4. Per filing: create Filing record (status=downloading).
    5. Per filing (on success): update Filing to downloaded + store document.
    6. Per filing (on failure): update Filing to failed.
    7. Final: mark job COMPLETED or FAILED with all counters.

Deduplication:
  - FilingRepository.exists_accession_number — skip filings already in DB.
  - DocumentStorageService.store — idempotent (returns existing record on hit).

Error policy:
  - Company resolution failure → job FAILED immediately.
  - Filing discovery failure   → job FAILED immediately.
  - Per-filing document errors → log warning, mark Filing FAILED, continue.
  - Unhandled exception        → job FAILED with traceback summary.

Milestone: M3.7 — Acquisition Jobs
"""

from __future__ import annotations

import traceback
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.models import AcquisitionJobStatus, FilingStatus
from apps.api.repositories.acquisition_jobs import AcquisitionJobRepository
from apps.api.repositories.filings import FilingRepository
from apps.api.schemas.acquisition_jobs import (
    AcquisitionJobCreate,
    AcquisitionJobRead,
    AcquisitionJobUpdate,
)
from services.acquisition.company_resolver.resolver import CompanyResolverService
from services.acquisition.document_fetcher.fetcher import (
    DocumentFetchError,
    SECFilingDocumentFetcher,
)
from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
from services.acquisition.storage.backend import StorageBackend
from services.acquisition.storage.service import DocumentStorageService

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Fiscal period derivation helper
# ---------------------------------------------------------------------------

def _derive_fiscal_coords(
    filing_type: str,
    period_end_date: object | None,
) -> tuple[int | None, str | None]:
    """
    Derive ``fiscal_year`` and ``fiscal_period`` from filing type and period end date.

    This is a best-effort derivation from the data SEC EDGAR provides at
    discovery time.  The extraction pipeline (M3.9) may later overwrite these
    values with more precise figures extracted from XBRL data.

    Rules:
      Annual filings (10-K, 20-F, 6-K):
        fiscal_year   = period_end_date.year
        fiscal_period = 'FY'

      Quarterly filings (10-Q):
        fiscal_year   = period_end_date.year
        fiscal_period = derived from period_end_date.month:
                          Jan–Mar  → 'Q1'
                          Apr–Jun  → 'Q2'
                          Jul–Sep  → 'Q3'
                          Oct–Dec  → 'Q4'

      Other types (8-K, DEF 14A, etc.):
        fiscal_year   = period_end_date.year  (if available; else None)
        fiscal_period = None  (not applicable for event-driven filings)

      No period_end_date:
        fiscal_year = None, fiscal_period = None

    Args:
        filing_type:     SEC form type string (e.g. '10-K', '10-Q', '8-K').
        period_end_date: ``datetime.date`` instance or None.

    Returns:
        ``(fiscal_year, fiscal_period)`` tuple.  Either element may be None.
    """
    if period_end_date is None:
        return None, None

    # period_end_date is typed as object to avoid a hard datetime import here;
    # it always carries .year and .month when not None (it is a date instance).
    year: int = period_end_date.year   # type: ignore[union-attr]
    month: int = period_end_date.month  # type: ignore[union-attr]

    if filing_type in ("10-K", "20-F", "6-K"):
        return year, "FY"

    if filing_type == "10-Q":
        if month <= 3:
            period = "Q1"
        elif month <= 6:
            period = "Q2"
        elif month <= 9:
            period = "Q3"
        else:
            period = "Q4"
        return year, period

    # 8-K, DEF 14A, and any future types — year known, period not applicable.
    return year, None


class AcquisitionJobService:
    """
    Orchestrates the full SEC filing acquisition pipeline for one company.

    Basic usage (development / testing — LocalStorageBackend)::

        from sqlalchemy.ext.asyncio import async_sessionmaker
        from services.acquisition.storage.backend import LocalStorageBackend

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        backend = LocalStorageBackend("/tmp/filings")
        service = AcquisitionJobService(
            session_factory=session_factory,
            storage_backend=backend,
        )

        job = await service.create_job("AAPL")
        result = await service.execute(job.id)
        print(result.status)          # "completed"
        print(result.documents_stored) # number of documents persisted

    Production (S3StorageBackend)::

        from services.acquisition.storage.backend import S3StorageBackend
        from apps.api.core.s3 import make_s3_client
        from apps.api.core.config import get_settings

        settings = get_settings()
        backend  = S3StorageBackend(make_s3_client(), settings.s3_documents_bucket)
        service  = AcquisitionJobService(
            session_factory=session_factory,
            storage_backend=backend,
            user_agent=settings.edgar_user_agent,
            redis_client=redis_client,
        )
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage_backend: StorageBackend,
        user_agent: str | None = None,
        redis_client: object | None = None,
    ) -> None:
        """
        Args:
            session_factory:  SQLAlchemy async session factory used to open
                              per-operation sessions with independent commits.
            storage_backend:  S3StorageBackend or LocalStorageBackend for
                              persisting fetched filing documents.
            user_agent:       SEC-required User-Agent string. When None, resolved
                              from settings.edgar_user_agent (Amendment V1.2 §4.1).
            redis_client:     Optional Redis client for company resolution
                              caching and document fetch caching.
        """
        # Amendment V1.2 §4.1: resolve User-Agent from settings so that a real
        # contact address is always sent — never fall back to a placeholder domain.
        if user_agent is None:
            from apps.api.core.config import get_settings
            user_agent = get_settings().edgar_user_agent
        self._session_factory = session_factory
        self._storage_backend = storage_backend
        self._user_agent = user_agent
        self._redis = redis_client

    # ── Public API ─────────────────────────────────────────────────────────────

    async def create_job(self, ticker: str) -> AcquisitionJobRead:
        """
        Create a new AcquisitionJob with status=pending.

        Args:
            ticker: Stock ticker symbol (e.g. 'AAPL').  Normalised to uppercase.

        Returns:
            AcquisitionJobRead with id, ticker, status='pending', and timestamps.
        """
        async with self._session_factory() as session:
            repo = AcquisitionJobRepository(session)
            schema = AcquisitionJobCreate(ticker=ticker)
            job = await repo.create(schema)
            await session.commit()
            log.info(
                "acquisition_job.created",
                job_id=str(job.id),
                ticker=job.ticker,
            )
            return AcquisitionJobRead.model_validate(job)

    async def get_job(self, job_id: uuid.UUID) -> AcquisitionJobRead | None:
        """Fetch a single job by ID. Returns None if not found."""
        async with self._session_factory() as session:
            repo = AcquisitionJobRepository(session)
            job = await repo.get_by_id(job_id)
            if job is None:
                return None
            return AcquisitionJobRead.model_validate(job)

    async def execute(self, job_id: uuid.UUID) -> AcquisitionJobRead:
        """
        Execute the full acquisition workflow for an existing job.

        Workflow:
          1. Load job; assert status=pending; mark RUNNING.
          2. Resolve ticker → CIK via CompanyResolverService.
          3. Discover filings via SECEdgarSource.
          4. For each filing:
             a. Skip if already in filings table.
             b. Create Filing record (status=downloading).
             c. Fetch document via SECFilingDocumentFetcher.
             d. Store document via DocumentStorageService.
             e. Update Filing status to downloaded / failed.
          5. Update job counters and mark COMPLETED (or FAILED on error).

        Args:
            job_id: UUID of the AcquisitionJob to execute.

        Returns:
            AcquisitionJobRead reflecting the final job state.

        Raises:
            ValueError: If the job does not exist or is not in pending state.
        """
        # ── 1. Load job and mark RUNNING ──────────────────────────────────────
        ticker = await self._mark_running(job_id)

        try:
            # ── 2. Resolve company ────────────────────────────────────────────
            cik = await self._resolve_company(job_id, ticker)

            # ── 3. Discover filings ───────────────────────────────────────────
            filings = await self._discover_filings(job_id, cik)

            # ── 4. Process each filing ────────────────────────────────────────
            filings_new, documents_fetched, documents_stored = \
                await self._process_filings(job_id, filings)

            # ── 5. Mark completed ─────────────────────────────────────────────
            await self._mark_completed(
                job_id,
                filings_new=filings_new,
                documents_fetched=documents_fetched,
                documents_stored=documents_stored,
            )

        except Exception as exc:
            await self._mark_failed(job_id, exc)
            raise

        result = await self.get_job(job_id)
        assert result is not None
        return result

    # ── Workflow steps ─────────────────────────────────────────────────────────

    async def _mark_running(self, job_id: uuid.UUID) -> str:
        """Load the job, assert it is pending, mark it running. Returns ticker."""
        async with self._session_factory() as session:
            repo = AcquisitionJobRepository(session)
            job = await repo.get_by_id(job_id)
            if job is None:
                raise ValueError(f"AcquisitionJob {job_id} not found")
            if job.status != AcquisitionJobStatus.PENDING:
                raise ValueError(
                    f"Job {job_id} has status={job.status!r}; expected 'pending'."
                )
            ticker = job.ticker
            await repo.update(
                job_id,
                AcquisitionJobUpdate(
                    status=AcquisitionJobStatus.RUNNING,
                    started_at=datetime.now(UTC),
                ),
            )
            await session.commit()
            log.info("acquisition_job.running", job_id=str(job_id), ticker=ticker)
            return ticker

    async def _resolve_company(self, job_id: uuid.UUID, ticker: str) -> str:
        """Resolve ticker → CIK. Raises CompanyResolutionError on failure. Returns CIK string."""
        from services.acquisition.company_resolver.provider import CompanyResolutionError
        resolver = CompanyResolverService(user_agent=self._user_agent, redis_client=self._redis)
        try:
            info = await resolver.resolve_by_ticker(ticker)
        except CompanyResolutionError:
            raise  # Let the typed exception propagate to execute() → _mark_failed()

        async with self._session_factory() as session:
            repo = AcquisitionJobRepository(session)
            await repo.update(
                job_id,
                AcquisitionJobUpdate(cik=info.cik, company_name=info.company_name),
            )
            await session.commit()

        log.info(
            "acquisition_job.company_resolved",
            job_id=str(job_id),
            ticker=ticker,
            cik=info.cik,
            company_name=info.company_name,
        )
        return info.cik

    async def _discover_filings(self, job_id: uuid.UUID, cik: str) -> list:
        """Discover all filings for the company. Returns list of FilingMetadata."""
        source = SECEdgarSource(user_agent=self._user_agent)
        result = await source.discover_filings(cik)
        filings = result.filings

        async with self._session_factory() as session:
            repo = AcquisitionJobRepository(session)
            await repo.update(
                job_id,
                AcquisitionJobUpdate(filings_discovered=len(filings)),
            )
            await session.commit()

        log.info(
            "acquisition_job.filings_discovered",
            job_id=str(job_id),
            cik=cik,
            count=len(filings),
        )
        return filings

    async def _process_filings(
        self, job_id: uuid.UUID, filings: list
    ) -> tuple[int, int, int]:
        """
        Process each discovered filing: create DB record, fetch, store.

        Returns (filings_new, documents_fetched, documents_stored).
        """
        filings_new = 0
        documents_fetched = 0
        documents_stored = 0

        fetcher = SECFilingDocumentFetcher(
            user_agent=self._user_agent,
            redis_client=self._redis,
        )
        try:
            for filing_meta in filings:
                result = await self._process_one_filing(
                    job_id, filing_meta, fetcher
                )
                is_new, fetched, stored = result
                if is_new:
                    filings_new += 1
                if fetched:
                    documents_fetched += 1
                if stored:
                    documents_stored += 1
        finally:
            await fetcher.close()

        return filings_new, documents_fetched, documents_stored

    async def _process_one_filing(
        self,
        job_id: uuid.UUID,
        filing_meta: object,
        fetcher: SECFilingDocumentFetcher,
    ) -> tuple[bool, bool, bool]:
        """
        Process a single filing.

        Returns (is_new, fetched, stored).
        """
        accession = filing_meta.accession_number

        # 4a. Skip if already in filings table.
        async with self._session_factory() as session:
            filing_repo = FilingRepository(session)
            if await filing_repo.exists_accession_number(accession):
                log.debug(
                    "acquisition_job.filing_already_known",
                    accession_number=accession,
                )
                return False, False, False

        # 4b. Create Filing record with status=downloading.
        # Derive fiscal coordinates at creation time from SEC metadata so the
        # columns are never NULL for filings that carry a period_end_date.
        # The extraction pipeline (M3.9) may later refine these values from XBRL.
        fiscal_year, fiscal_period = _derive_fiscal_coords(
            filing_meta.filing_type,
            filing_meta.period_end_date,
        )

        filing_id: uuid.UUID | None = None
        try:
            async with self._session_factory() as session:
                filing_repo = FilingRepository(session)
                filing_record = await filing_repo.create_filing_record(
                    company_id=None,  # resolved after acquisition; linked in M3.7
                    filing_type=filing_meta.filing_type,
                    accession_number=accession,
                    filing_date=filing_meta.filing_date,
                    cik=filing_meta.cik,
                    ticker=filing_meta.ticker,
                    title=filing_meta.title,
                    filing_url=filing_meta.filing_url,
                    document_url=filing_meta.document_url,
                    period_end_date=filing_meta.period_end_date,
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    status=FilingStatus.DOWNLOADING.value,
                )
                filing_id = filing_record.id
                await session.commit()
                log.debug(
                    "acquisition_job.filing_created",
                    accession_number=accession,
                    filing_id=str(filing_id),
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                )
        except Exception as exc:
            log.warning(
                "acquisition_job.filing_create_failed",
                accession_number=accession,
                error=str(exc),
            )
            return True, False, False

        # 4c. Fetch document.
        doc = None
        try:
            doc = await fetcher.fetch_document(filing_meta)
            log.debug(
                "acquisition_job.document_fetched",
                accession_number=accession,
                content_length=doc.content_length,
            )
        except DocumentFetchError as exc:
            log.warning(
                "acquisition_job.document_fetch_failed",
                accession_number=accession,
                error=str(exc),
            )
        except Exception as exc:
            log.warning(
                "acquisition_job.document_fetch_error",
                accession_number=accession,
                error=str(exc),
            )

        if doc is None:
            # Mark filing as failed.
            if filing_id is not None:
                await self._update_filing_status(filing_id, FilingStatus.FAILED.value)
            return True, False, False

        # 4d. Store document.
        stored = False
        try:
            async with self._session_factory() as session:
                storage_service = DocumentStorageService(
                    backend=self._storage_backend,
                    session=session,
                )
                await storage_service.store(doc)
                await session.commit()
            stored = True
            log.debug(
                "acquisition_job.document_stored",
                accession_number=accession,
            )
        except Exception as exc:
            log.warning(
                "acquisition_job.document_store_failed",
                accession_number=accession,
                error=str(exc),
            )

        # 4e. Update filing status.
        if filing_id is not None:
            new_status = FilingStatus.DOWNLOADED.value if stored else FilingStatus.FAILED.value
            await self._update_filing_status(filing_id, new_status)

        return True, True, stored

    async def _update_filing_status(self, filing_id: uuid.UUID, status: str) -> None:
        """Update a Filing record's status in its own session."""
        try:
            async with self._session_factory() as session:
                filing_repo = FilingRepository(session)
                await filing_repo.update_filing_status(filing_id, status)
                await session.commit()
        except Exception as exc:
            log.warning(
                "acquisition_job.filing_status_update_failed",
                filing_id=str(filing_id),
                status=status,
                error=str(exc),
            )

    async def _mark_completed(
        self,
        job_id: uuid.UUID,
        *,
        filings_new: int,
        documents_fetched: int,
        documents_stored: int,
    ) -> None:
        async with self._session_factory() as session:
            repo = AcquisitionJobRepository(session)
            await repo.update(
                job_id,
                AcquisitionJobUpdate(
                    status=AcquisitionJobStatus.COMPLETED,
                    completed_at=datetime.now(UTC),
                    filings_new=filings_new,
                    documents_fetched=documents_fetched,
                    documents_stored=documents_stored,
                ),
            )
            await session.commit()
        log.info(
            "acquisition_job.completed",
            job_id=str(job_id),
            filings_new=filings_new,
            documents_fetched=documents_fetched,
            documents_stored=documents_stored,
        )

    async def _mark_failed(self, job_id: uuid.UUID, exc: Exception) -> None:
        error_summary = f"{type(exc).__name__}: {exc}"
        try:
            async with self._session_factory() as session:
                repo = AcquisitionJobRepository(session)
                await repo.update(
                    job_id,
                    AcquisitionJobUpdate(
                        status=AcquisitionJobStatus.FAILED,
                        completed_at=datetime.now(UTC),
                        error_message=error_summary,
                    ),
                )
                await session.commit()
        except Exception as inner:
            log.error(
                "acquisition_job.failed_to_mark_failed",
                job_id=str(job_id),
                inner_error=str(inner),
            )
        log.error(
            "acquisition_job.failed",
            job_id=str(job_id),
            error=error_summary,
        )
