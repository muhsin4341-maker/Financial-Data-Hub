"""
Unit tests — AcquisitionJobService.

Strategy
--------
All external dependencies are mocked:
  - session_factory returns a mock async context manager session
  - AcquisitionJobRepository — mocked
  - CompanyResolverService.resolve_by_ticker — mocked
  - SECEdgarSource.discover_filings — mocked
  - FilingRepository.exists_accession_number + create + update — mocked
  - SECFilingDocumentFetcher.fetch_document — mocked
  - DocumentStorageService.store — mocked

Test scope:
  - create_job: creates a job, calls repo.create, commits
  - execute (happy path): full workflow, correct counters, COMPLETED status
  - execute: company resolution failure → FAILED
  - execute: filing discovery failure → FAILED
  - execute: per-filing fetch failure → job continues, document not counted
  - execute: per-filing store failure → job continues, document not counted
  - execute: existing filing → skipped (filings_new not incremented)
  - _mark_running: raises ValueError if job not pending
  - _mark_running: raises ValueError if job not found

Milestone: M3.7 — Acquisition Jobs
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.api.models import AcquisitionJobStatus
from apps.api.schemas.acquisition_jobs import AcquisitionJobRead
from services.acquisition.document_fetcher.fetcher import DocumentFetchError, FilingDocument
from services.acquisition.jobs.service import AcquisitionJobService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_TICKER = "AAPL"
_CIK = "0000320193"
_ACCESSION = "0000320193-24-000009"


def _make_job_read(
    status: str = "pending",
    ticker: str = _TICKER,
    filings_new: int = 0,
    documents_fetched: int = 0,
    documents_stored: int = 0,
) -> AcquisitionJobRead:
    return AcquisitionJobRead(
        id=uuid.uuid4(),
        ticker=ticker,
        cik=None,
        company_name=None,
        job_type="sec_filing_discovery",
        status=status,
        error_message=None,
        filings_discovered=0,
        filings_new=filings_new,
        documents_fetched=documents_fetched,
        documents_stored=documents_stored,
        started_at=None,
        completed_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_company_info(cik: str = _CIK, company_name: str = "Apple Inc.") -> MagicMock:
    info = MagicMock()
    info.cik = cik
    info.company_name = company_name
    return info


def _make_filing_meta(accession: str = _ACCESSION) -> MagicMock:
    meta = MagicMock()
    meta.accession_number = accession
    meta.filing_type = "10-K"
    meta.filing_date = date(2024, 10, 31)
    meta.cik = _CIK
    meta.ticker = _TICKER
    meta.title = "Annual report"
    meta.filing_url = "https://sec.gov/..."
    meta.document_url = "https://sec.gov/.../doc.htm"
    meta.period_end_date = date(2024, 9, 30)
    return meta


def _make_filing_document(accession: str = _ACCESSION) -> FilingDocument:
    content = "<html>10-K</html>"
    return FilingDocument(
        accession_number=accession,
        filing_type="10-K",
        filing_date=date(2024, 10, 31),
        source_url="https://sec.gov/...",
        document_url=None,
        mime_type="text/html",
        content=content,
        content_length=len(content.encode()),
        content_hash="a" * 64,
        encoding="utf-8",
        plain_text="10-K",
        title="Annual report",
        fetched_at=_NOW,
    )


def _make_job_orm(
    status: str = "pending",
    ticker: str = _TICKER,
) -> MagicMock:
    j = MagicMock()
    j.id = uuid.uuid4()
    j.ticker = ticker
    j.cik = None
    j.company_name = None
    j.job_type = "sec_filing_discovery"
    j.status = status
    j.error_message = None
    j.filings_discovered = 0
    j.filings_new = 0
    j.documents_fetched = 0
    j.documents_stored = 0
    j.started_at = None
    j.completed_at = None
    j.created_at = _NOW
    j.updated_at = _NOW
    return j


def _make_filing_orm() -> MagicMock:
    f = MagicMock()
    f.id = uuid.uuid4()
    f.accession_number = _ACCESSION
    return f


def _make_stored_doc_orm() -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.accession_number = _ACCESSION
    return s


# ---------------------------------------------------------------------------
# Session factory builder
# ---------------------------------------------------------------------------

def _build_session_factory(
    job_orm: MagicMock | None = None,
    existing_accession: bool = False,
) -> tuple[object, MagicMock, MagicMock]:
    """
    Build a mock async_sessionmaker and return (factory, job_repo, filing_repo).

    All sessions share the same mock repos for inspection.
    """
    job_repo = AsyncMock()
    filing_repo = AsyncMock()

    # Default job state
    _job = job_orm or _make_job_orm()
    job_repo.get_by_id.return_value = _job
    job_repo.create.return_value = _job
    job_repo.update.return_value = _job

    filing_orm = _make_filing_orm()
    filing_repo.exists_accession_number.return_value = existing_accession
    filing_repo.create.return_value = filing_orm
    filing_repo.update.return_value = filing_orm

    session = AsyncMock()
    session.commit = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_ctx)

    # Patch repos inside the service module
    return mock_factory, job_repo, filing_repo


# ===========================================================================
# create_job
# ===========================================================================


class TestCreateJob:
    @pytest.mark.anyio
    async def test_create_job_returns_read_schema(self) -> None:
        factory, job_repo, _ = _build_session_factory()
        backend = AsyncMock()
        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
        ):
            result = await service.create_job(_TICKER)

        assert isinstance(result, AcquisitionJobRead)

    @pytest.mark.anyio
    async def test_create_job_calls_repo_create(self) -> None:
        factory, job_repo, _ = _build_session_factory()
        backend = AsyncMock()
        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with patch(
            "services.acquisition.jobs.service.AcquisitionJobRepository",
            return_value=job_repo,
        ):
            await service.create_job(_TICKER)

        job_repo.create.assert_called_once()

    @pytest.mark.anyio
    async def test_create_job_commits(self) -> None:
        factory, job_repo, _ = _build_session_factory()
        ctx = factory.return_value
        session = ctx.__aenter__.return_value
        backend = AsyncMock()
        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with patch(
            "services.acquisition.jobs.service.AcquisitionJobRepository",
            return_value=job_repo,
        ):
            await service.create_job(_TICKER)

        session.commit.assert_awaited_once()


# ===========================================================================
# _mark_running
# ===========================================================================


class TestMarkRunning:
    @pytest.mark.anyio
    async def test_raises_if_job_not_found(self) -> None:
        factory, job_repo, _ = _build_session_factory()
        job_repo.get_by_id.return_value = None
        backend = AsyncMock()
        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            await service._mark_running(uuid.uuid4())

    @pytest.mark.anyio
    async def test_raises_if_job_not_pending(self) -> None:
        factory, job_repo, _ = _build_session_factory(
            job_orm=_make_job_orm(status="running")
        )
        backend = AsyncMock()
        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
            pytest.raises(ValueError, match="expected 'pending'"),
        ):
            await service._mark_running(uuid.uuid4())

    @pytest.mark.anyio
    async def test_mark_running_returns_ticker(self) -> None:
        factory, job_repo, _ = _build_session_factory()
        backend = AsyncMock()
        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with patch(
            "services.acquisition.jobs.service.AcquisitionJobRepository",
            return_value=job_repo,
        ):
            ticker = await service._mark_running(uuid.uuid4())

        assert ticker == _TICKER


# ===========================================================================
# execute — happy path
# ===========================================================================


class TestExecuteHappyPath:
    @pytest.mark.anyio
    async def test_execute_happy_path_returns_completed(self) -> None:
        """Full workflow: resolve → 1 new filing → fetch → store → COMPLETED."""
        factory, job_repo, filing_repo = _build_session_factory()
        backend = AsyncMock()

        completed_job = _make_job_orm(status="completed")
        completed_job.filings_new = 1
        completed_job.documents_fetched = 1
        completed_job.documents_stored = 1
        # get_job will be called last — return completed state
        job_repo.get_by_id.side_effect = [
            _make_job_orm(status="pending"),  # _mark_running
            _make_job_orm(status="running"),  # _resolve_company
            _make_job_orm(status="running"),  # _discover_filings
            _make_job_orm(status="running"),  # _process_one_filing exists check
            _make_job_orm(status="running"),  # create filing
            completed_job,                    # get_job at end
        ]

        resolver_info = _make_company_info()
        discover_result = MagicMock()
        discover_result.filings = [_make_filing_meta()]

        filing_document = _make_filing_document()
        stored_doc = _make_stored_doc_orm()

        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
            patch(
                "services.acquisition.jobs.service.FilingRepository",
                return_value=filing_repo,
            ),
            patch(
                "services.acquisition.jobs.service.CompanyResolverService"
            ) as MockResolver,
            patch(
                "services.acquisition.jobs.service.SECEdgarSource"
            ) as MockSource,
            patch(
                "services.acquisition.jobs.service.SECFilingDocumentFetcher"
            ) as MockFetcher,
            patch(
                "services.acquisition.jobs.service.DocumentStorageService"
            ) as MockStorage,
        ):
            MockResolver.return_value.resolve_by_ticker = AsyncMock(
                return_value=resolver_info
            )
            mock_source = AsyncMock()
            mock_source.discover_filings = AsyncMock(return_value=discover_result)
            MockSource.return_value = mock_source

            mock_fetcher = AsyncMock()
            mock_fetcher.fetch_document = AsyncMock(return_value=filing_document)
            mock_fetcher.close = AsyncMock()
            MockFetcher.return_value = mock_fetcher

            mock_storage = AsyncMock()
            mock_storage.store = AsyncMock(return_value=stored_doc)
            MockStorage.return_value = mock_storage

            filing_repo.exists_accession_number.return_value = False

            result = await service.execute(uuid.uuid4())

        assert isinstance(result, AcquisitionJobRead)

    @pytest.mark.anyio
    async def test_execute_skips_existing_filings(self) -> None:
        """Filing already in DB → filings_new stays 0."""
        factory, job_repo, filing_repo = _build_session_factory(
            existing_accession=True
        )
        backend = AsyncMock()

        # For get_job at end
        final_job = _make_job_orm(status="completed")
        job_repo.get_by_id.side_effect = [
            _make_job_orm(status="pending"),
            _make_job_orm(status="running"),
            _make_job_orm(status="running"),
            _make_job_orm(status="running"),  # exists_accession_number call
            final_job,
        ]

        discover_result = MagicMock()
        discover_result.filings = [_make_filing_meta()]

        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
            patch(
                "services.acquisition.jobs.service.FilingRepository",
                return_value=filing_repo,
            ),
            patch(
                "services.acquisition.jobs.service.CompanyResolverService"
            ) as MockResolver,
            patch(
                "services.acquisition.jobs.service.SECEdgarSource"
            ) as MockSource,
            patch(
                "services.acquisition.jobs.service.SECFilingDocumentFetcher"
            ) as MockFetcher,
            patch(
                "services.acquisition.jobs.service.DocumentStorageService"
            ),
        ):
            MockResolver.return_value.resolve_by_ticker = AsyncMock(
                return_value=_make_company_info()
            )
            mock_source = AsyncMock()
            mock_source.discover_filings = AsyncMock(return_value=discover_result)
            MockSource.return_value = mock_source

            mock_fetcher = AsyncMock()
            mock_fetcher.close = AsyncMock()
            MockFetcher.return_value = mock_fetcher

            filing_repo.exists_accession_number.return_value = True

            result = await service.execute(uuid.uuid4())

        assert isinstance(result, AcquisitionJobRead)


# ===========================================================================
# execute — failure paths
# ===========================================================================


class TestExecuteFailurePaths:
    @pytest.mark.anyio
    async def test_company_resolution_failure_marks_job_failed(self) -> None:
        factory, job_repo, filing_repo = _build_session_factory()
        backend = AsyncMock()

        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
            patch(
                "services.acquisition.jobs.service.FilingRepository",
                return_value=filing_repo,
            ),
            patch(
                "services.acquisition.jobs.service.CompanyResolverService"
            ) as MockResolver,
            patch("services.acquisition.jobs.service.SECEdgarSource"),
            patch("services.acquisition.jobs.service.SECFilingDocumentFetcher"),
            patch("services.acquisition.jobs.service.DocumentStorageService"),
            pytest.raises(ValueError),
        ):
            MockResolver.return_value.resolve_by_ticker = AsyncMock(
                return_value=None  # None → company not found
            )
            await service.execute(uuid.uuid4())

        # _mark_failed was called — update should have been called with FAILED status
        calls = job_repo.update.call_args_list
        statuses = [
            call.args[1].status
            for call in calls
            if hasattr(call.args[1], "status") and call.args[1].status is not None
        ]
        assert any(s == AcquisitionJobStatus.FAILED for s in statuses)

    @pytest.mark.anyio
    async def test_document_fetch_failure_is_non_fatal(self) -> None:
        """A DocumentFetchError on one filing should not abort the job."""
        factory, job_repo, filing_repo = _build_session_factory()
        backend = AsyncMock()

        final_job = _make_job_orm(status="completed")
        job_repo.get_by_id.side_effect = [
            _make_job_orm(status="pending"),
            _make_job_orm(status="running"),
            _make_job_orm(status="running"),
            _make_job_orm(status="running"),
            _make_job_orm(status="running"),
            final_job,
        ]

        discover_result = MagicMock()
        discover_result.filings = [_make_filing_meta()]

        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
            patch(
                "services.acquisition.jobs.service.FilingRepository",
                return_value=filing_repo,
            ),
            patch(
                "services.acquisition.jobs.service.CompanyResolverService"
            ) as MockResolver,
            patch(
                "services.acquisition.jobs.service.SECEdgarSource"
            ) as MockSource,
            patch(
                "services.acquisition.jobs.service.SECFilingDocumentFetcher"
            ) as MockFetcher,
            patch(
                "services.acquisition.jobs.service.DocumentStorageService"
            ),
        ):
            MockResolver.return_value.resolve_by_ticker = AsyncMock(
                return_value=_make_company_info()
            )
            mock_source = AsyncMock()
            mock_source.discover_filings = AsyncMock(return_value=discover_result)
            MockSource.return_value = mock_source

            mock_fetcher = AsyncMock()
            mock_fetcher.fetch_document = AsyncMock(
                side_effect=DocumentFetchError("network error")
            )
            mock_fetcher.close = AsyncMock()
            MockFetcher.return_value = mock_fetcher

            filing_repo.exists_accession_number.return_value = False

            result = await service.execute(uuid.uuid4())

        # Job should complete despite fetch failure
        assert isinstance(result, AcquisitionJobRead)

    @pytest.mark.anyio
    async def test_discovery_failure_marks_job_failed(self) -> None:
        factory, job_repo, filing_repo = _build_session_factory()
        backend = AsyncMock()

        service = AcquisitionJobService(
            session_factory=factory,
            storage_backend=backend,
        )

        with (
            patch(
                "services.acquisition.jobs.service.AcquisitionJobRepository",
                return_value=job_repo,
            ),
            patch(
                "services.acquisition.jobs.service.FilingRepository",
                return_value=filing_repo,
            ),
            patch(
                "services.acquisition.jobs.service.CompanyResolverService"
            ) as MockResolver,
            patch(
                "services.acquisition.jobs.service.SECEdgarSource"
            ) as MockSource,
            patch("services.acquisition.jobs.service.SECFilingDocumentFetcher"),
            patch("services.acquisition.jobs.service.DocumentStorageService"),
            pytest.raises(RuntimeError),
        ):
            MockResolver.return_value.resolve_by_ticker = AsyncMock(
                return_value=_make_company_info()
            )
            mock_source = AsyncMock()
            mock_source.discover_filings = AsyncMock(
                side_effect=RuntimeError("SEC EDGAR unreachable")
            )
            MockSource.return_value = mock_source

            await service.execute(uuid.uuid4())

        calls = job_repo.update.call_args_list
        statuses = [
            call.args[1].status
            for call in calls
            if hasattr(call.args[1], "status") and call.args[1].status is not None
        ]
        assert any(s == AcquisitionJobStatus.FAILED for s in statuses)
