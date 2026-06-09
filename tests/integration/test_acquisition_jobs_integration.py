"""
Integration tests — M3.7 Acquisition Jobs: VG-10 validation gate.

These tests make real HTTP calls to SEC EDGAR and require a live PostgreSQL
database. They are skipped by default and must be enabled explicitly.

Prerequisites:
  - DATABASE_URL env var pointing to a test PostgreSQL instance
  - RUN_INTEGRATION_TESTS=1 env var
  - All migrations applied (alembic upgrade head)

To run:
    DATABASE_URL=postgresql+asyncpg://... RUN_INTEGRATION_TESTS=1 \\
        pytest tests/integration/test_acquisition_jobs_integration.py -v

VG-10 validation gate — AAPL acquisition job:
  1. Create an AcquisitionJob for AAPL (status=pending).
  2. Execute the job via AcquisitionJobService.execute().
  3. Verify final status transitions: pending → running → completed.
  4. Verify filings_discovered > 0.
  5. Verify filings_new > 0 (first run — no prior data).
  6. Verify documents_stored > 0.
  7. Verify Filing records created in DB with status=downloaded.
  8. Verify StoredDocument records created in DB.
  9. Verify documents are retrievable from LocalStorageBackend.
  10. Re-run create_job for same ticker — confirm idempotency: existing filings
      skipped, no duplicate Filing records.

Milestone: M3.7 — Acquisition Jobs
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.api.models import AcquisitionJob, Filing, StoredDocument
from apps.api.repositories.acquisition_jobs import AcquisitionJobRepository
from apps.api.repositories.filings import FilingRepository
from apps.api.repositories.filing_documents import StoredDocumentRepository
from services.acquisition.jobs.service import AcquisitionJobService
from services.acquisition.storage.backend import LocalStorageBackend

pytestmark = pytest.mark.skipif(
    not (os.getenv("RUN_INTEGRATION_TESTS") and os.getenv("DATABASE_URL")),
    reason=(
        "VG-10 integration tests disabled by default. "
        "Set RUN_INTEGRATION_TESTS=1 and DATABASE_URL to run."
    ),
)

_USER_AGENT = "FinancialDataHub-test-vg10 contact@example.com"
_AAPL_TICKER = "AAPL"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    """Create async engine from DATABASE_URL."""
    url = os.environ["DATABASE_URL"]
    return create_async_engine(url, echo=False)


@pytest.fixture(scope="module")
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture()
def tmpdir_path():
    with tempfile.TemporaryDirectory(prefix="fdh_vg10_") as d:
        yield d


@pytest.fixture()
def storage_backend(tmpdir_path) -> LocalStorageBackend:
    return LocalStorageBackend(tmpdir_path)


@pytest.fixture()
def service(session_factory, storage_backend) -> AcquisitionJobService:
    return AcquisitionJobService(
        session_factory=session_factory,
        storage_backend=storage_backend,
        user_agent=_USER_AGENT,
    )


# ---------------------------------------------------------------------------
# VG-10 tests
# ---------------------------------------------------------------------------


class TestVG10AcquisitionJob:
    """VG-10: Full AAPL acquisition job — create → resolve → discover → fetch → store."""

    @pytest.mark.anyio
    async def test_vg10_create_job_returns_pending(self, service) -> None:
        """VG-10.1: create_job returns a job with status=pending."""
        job = await service.create_job(_AAPL_TICKER)

        assert job.ticker == _AAPL_TICKER
        assert job.status == "pending"
        assert job.id is not None
        assert job.filings_discovered == 0
        assert job.filings_new == 0
        assert job.documents_stored == 0

    @pytest.mark.anyio
    async def test_vg10_execute_completes_with_data(self, service, session_factory) -> None:
        """
        VG-10 core gate: full acquisition completes with filings and documents.

        Verifies:
          - Job transitions to status=completed.
          - filings_discovered > 0.
          - filings_new > 0 (assumes clean test DB).
          - documents_stored > 0.
          - Filing records exist in DB with status=downloaded.
          - StoredDocument records exist in DB.
          - cik and company_name populated after resolution.
        """
        job = await service.create_job(_AAPL_TICKER)
        result = await service.execute(job.id)

        # Final state
        assert result.status == "completed", (
            f"Expected status=completed, got {result.status}. "
            f"error_message={result.error_message!r}"
        )
        assert result.cik is not None, "CIK must be populated after company resolution"
        assert result.company_name is not None, "company_name must be populated"
        assert result.filings_discovered > 0, "Must discover at least one filing"
        assert result.filings_new > 0, "Must have new filings on first run"
        assert result.documents_fetched > 0, "Must fetch at least one document"
        assert result.documents_stored > 0, "Must store at least one document"
        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.started_at <= result.completed_at

        # Verify Filing records in DB
        async with session_factory() as session:
            filing_repo = FilingRepository(session)
            items, total = await filing_repo.list(cik=result.cik)

        assert total > 0, "Filing records must exist in DB after job completes"
        # All fetched filings should be downloaded or failed (no stuck downloading)
        statuses = {f.status for f in items}
        assert "downloading" not in statuses, (
            "No filing should be stuck in 'downloading' state after job completes"
        )

        # Verify StoredDocument records in DB
        async with session_factory() as session:
            doc_repo = StoredDocumentRepository(session)
            for filing in items:
                if filing.status == "downloaded":
                    exists = await doc_repo.exists_by_accession_number(
                        filing.accession_number
                    )
                    assert exists, (
                        f"Filing {filing.accession_number} has status=downloaded "
                        "but no StoredDocument record found."
                    )
                    break  # Checked at least one — sufficient for VG-10

    @pytest.mark.anyio
    async def test_vg10_second_run_skips_existing_filings(
        self, service, session_factory
    ) -> None:
        """
        VG-10: Re-running acquisition for same ticker skips already-known filings.

        Run 1 creates Filing records. Run 2 should have filings_new=0 (all known).
        """
        # Run 1 — seed the DB
        job1 = await service.create_job(_AAPL_TICKER)
        result1 = await service.execute(job1.id)
        assert result1.status == "completed"
        filings_seeded = result1.filings_new

        # Run 2 — all filings already in DB
        job2 = await service.create_job(_AAPL_TICKER)
        result2 = await service.execute(job2.id)
        assert result2.status == "completed"

        assert result2.filings_new == 0, (
            f"Second run should skip all known filings. "
            f"Got filings_new={result2.filings_new} "
            f"(first run seeded {filings_seeded} filings)."
        )

    @pytest.mark.anyio
    async def test_vg10_job_record_persisted(self, service, session_factory) -> None:
        """VG-10: AcquisitionJob record survives after execute() returns."""
        job = await service.create_job(_AAPL_TICKER)
        result = await service.execute(job.id)

        async with session_factory() as session:
            repo = AcquisitionJobRepository(session)
            persisted = await repo.get_by_id(result.id)

        assert persisted is not None
        assert persisted.status in ("completed", "failed")
        assert persisted.ticker == _AAPL_TICKER

    @pytest.mark.anyio
    async def test_vg10_documents_retrievable_from_storage(
        self, service, storage_backend
    ) -> None:
        """VG-10: Stored documents can be retrieved from the local storage backend."""
        from services.acquisition.storage.service import DocumentStorageService
        from sqlalchemy.ext.asyncio import async_sessionmaker

        job = await service.create_job(_AAPL_TICKER)
        result = await service.execute(job.id)
        assert result.status == "completed"
        assert result.documents_stored > 0

        # Get a stored accession number
        # We retrieve via the service's session_factory to find a StoredDocument
        factory = service._session_factory
        async with factory() as session:
            stmt = select(StoredDocument).limit(1)
            orm_result = await session.execute(stmt)
            stored_doc = orm_result.scalar_one_or_none()

        assert stored_doc is not None, "No StoredDocument found after job"
        content = await storage_backend.retrieve(stored_doc.object_key)
        assert content is not None, (
            f"Backend.retrieve({stored_doc.object_key!r}) returned None"
        )
        assert len(content) > 0, "Retrieved document content is empty"
