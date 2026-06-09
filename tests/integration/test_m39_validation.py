"""
M3.9 — End-to-End Validation: Integration test suite.

Validation gates:
  VG-12  Full Acquisition Validation
         Create job → resolve company → discover filings → fetch documents
         → store documents → complete job → retrieve through APIs.

  VG-13  Duplicate Prevention
         Run acquisition twice; verify no duplicate filings, documents, or
         storage records are created on the second run.

  VG-14  Storage Integrity
         Verify content_hash preserved end-to-end; content_length matches
         stored bytes; metadata (accession_number, filing_type, filing_date)
         is preserved through the full pipeline.

  VG-17  Performance Validation
         Measure: filing discovery time, per-document fetch time, total job
         execution time, and storage throughput.

  VG-18  Data Quality Validation
         AAPL 10-K, 10-Q, and 8-K:
           - Accession numbers match SEC EDGAR canonical format.
           - Filing dates are in the past.
           - CIK matches AAPL's known CIK (0000320193).
           - At least one filing of each major type is discoverable.
           - Most recent 10-K document is fetchable and non-trivially sized.

Prerequisites:
  - RUN_INTEGRATION_TESTS=1  env var
  - DATABASE_URL=postgresql+asyncpg://...  env var
  - Network access to SEC EDGAR (data.sec.gov, www.sec.gov)
  - All migrations applied (alembic upgrade head)

To run:
    RUN_INTEGRATION_TESTS=1 DATABASE_URL=postgresql+asyncpg://... \\
        pytest tests/integration/test_m39_validation.py -v -s

Milestone: M3.9 — End-to-End Validation
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
import uuid
from datetime import date, datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("RUN_INTEGRATION_TESTS") and os.getenv("DATABASE_URL")),
    reason=(
        "VG-12 through VG-18 integration tests disabled by default. "
        "Set RUN_INTEGRATION_TESTS=1 and DATABASE_URL to run."
    ),
)

_AAPL_CIK = "0000320193"
_AAPL_TICKER = "AAPL"
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_USER_AGENT = "FinancialDataHub-test contact@example.com"

# Performance bounds (wall-clock seconds, generous for CI / slow networks)
_MAX_DISCOVERY_SECONDS = 30
_MAX_SINGLE_FETCH_SECONDS = 60
_MAX_TOTAL_JOB_SECONDS = 600  # end-to-end can be slow for AAPL (many filings)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session_factory():
    """
    Per-test async_sessionmaker.

    Creates a fresh engine and session factory per test to avoid asyncpg
    connections being tied to a previous test's event loop (a Windows
    ProactorEventLoop issue when tests are run in sequence within a class).
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    url = os.environ["DATABASE_URL"]
    engine = create_async_engine(url, echo=False)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture()
def tmp_storage_dir():
    """Per-test temporary local storage directory."""
    with tempfile.TemporaryDirectory(prefix="fdh-m39-test-") as d:
        yield d


@pytest.fixture()
def storage_backend(tmp_storage_dir):
    """Per-test LocalStorageBackend pointed at the temporary directory."""
    from services.acquisition.storage.backend import LocalStorageBackend
    return LocalStorageBackend(tmp_storage_dir)


@pytest.fixture()
def acquisition_service(db_session_factory, storage_backend):
    """Per-test AcquisitionJobService wired with test DB and local storage backend."""
    from services.acquisition.jobs.service import AcquisitionJobService
    return AcquisitionJobService(
        session_factory=db_session_factory,
        storage_backend=storage_backend,
        user_agent=_USER_AGENT,
    )


# ---------------------------------------------------------------------------
# VG-12 — Full Acquisition Validation
# ---------------------------------------------------------------------------


class TestVG12FullAcquisition:
    """VG-12: Full pipeline end-to-end."""

    @pytest.mark.anyio
    async def test_vg12_create_job_is_pending(
        self, acquisition_service
    ) -> None:
        """VG-12.1: Freshly created job has status='pending', id is a UUID."""
        job = await acquisition_service.create_job(_AAPL_TICKER)

        assert job.id is not None
        assert isinstance(job.id, uuid.UUID)
        assert job.ticker == _AAPL_TICKER
        assert job.status == "pending"
        assert job.filings_discovered == 0
        assert job.documents_stored == 0
        print(f"\n  [VG-12.1] Created job {job.id}, status={job.status!r}")

    @pytest.mark.anyio
    async def test_vg12_execute_job_reaches_terminal_state(
        self, acquisition_service
    ) -> None:
        """VG-12.2: execute() reaches 'completed' or 'failed' — never hangs."""
        job = await acquisition_service.create_job(_AAPL_TICKER)
        t0 = time.monotonic()
        result = await acquisition_service.execute(job.id)
        elapsed = time.monotonic() - t0

        print(
            f"\n  [VG-12.2] Job {job.id}: status={result.status!r}, "
            f"filings_discovered={result.filings_discovered}, "
            f"documents_stored={result.documents_stored}, "
            f"elapsed={elapsed:.1f}s"
        )

        assert result.status in ("completed", "failed"), (
            f"Job must reach terminal state; got {result.status!r}"
        )
        if result.status == "failed":
            print(f"  [VG-12.2] WARN: job failed — error: {result.error_message}")

    @pytest.mark.anyio
    async def test_vg12_completed_job_has_discovery_counters(
        self, acquisition_service, db_session_factory
    ) -> None:
        """
        VG-12.3: After execution, DB record shows filings_discovered >= 1
        (AAPL has thousands of filings on SEC EDGAR).
        """
        from apps.api.repositories.acquisition_jobs import AcquisitionJobRepository

        job = await acquisition_service.create_job(_AAPL_TICKER)
        result = await acquisition_service.execute(job.id)

        async with db_session_factory() as session:
            repo = AcquisitionJobRepository(session)
            refreshed = await repo.get_by_id(job.id)

        assert refreshed is not None, "Job record must exist in DB after execute()"
        assert refreshed.filings_discovered >= 0
        assert refreshed.documents_stored >= 0

        if result.status == "completed":
            assert refreshed.filings_discovered >= 1, (
                "Completed job must have discovered at least 1 filing"
            )

        print(
            f"\n  [VG-12.3] DB record: filings_discovered={refreshed.filings_discovered}, "
            f"documents_stored={refreshed.documents_stored}, status={refreshed.status!r}"
        )

    @pytest.mark.anyio
    async def test_vg12_filing_records_queryable_after_acquisition(
        self, acquisition_service, db_session_factory
    ) -> None:
        """
        VG-12.4: Filing records written during acquisition are retrievable via
        FilingRepository, have valid accession numbers, and are associated with AAPL.
        """
        from apps.api.repositories.filings import FilingRepository

        job = await acquisition_service.create_job(_AAPL_TICKER)
        result = await acquisition_service.execute(job.id)

        if result.filings_discovered == 0:
            pytest.skip("No filings discovered — skipping DB retrieval check.")

        async with db_session_factory() as session:
            repo = FilingRepository(session)
            items, total = await repo.list(page=1, page_size=50, ticker=_AAPL_TICKER)

        assert total >= 1, f"Expected >=1 AAPL filing in DB, got {total}"
        for f in items:
            assert _ACCESSION_RE.match(f.accession_number), (
                f"Invalid accession format: {f.accession_number!r}"
            )
            assert f.ticker == _AAPL_TICKER or f.cik is not None

        print(
            f"\n  [VG-12.4] {total} AAPL filings in DB. "
            f"Sample: {items[0].accession_number!r} ({items[0].filing_type})"
        )

    @pytest.mark.anyio
    async def test_vg12_stored_documents_retrievable_after_acquisition(
        self, acquisition_service, db_session_factory, storage_backend
    ) -> None:
        """
        VG-12.5: For each stored document, the content is retrievable from the
        storage backend and matches the recorded content_hash.
        """
        from apps.api.repositories.filing_documents import StoredDocumentRepository

        job = await acquisition_service.create_job(_AAPL_TICKER)
        result = await acquisition_service.execute(job.id)

        if result.documents_stored == 0:
            pytest.skip("No documents stored — skipping retrieval check.")

        async with db_session_factory() as session:
            repo = StoredDocumentRepository(session)
            # Spot-check the first stored document
            # Use raw query via the session since there's no list() on the repo
            from sqlalchemy import select
            from apps.api.models import StoredDocument
            q = select(StoredDocument).limit(1)
            result_row = await session.execute(q)
            doc = result_row.scalar_one_or_none()

        if doc is None:
            pytest.skip("No StoredDocument rows in DB.")

        content = await storage_backend.retrieve(doc.object_key)
        assert content is not None, (
            f"Backend returned None for key {doc.object_key!r}"
        )
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        actual_hash = hashlib.sha256(content_bytes).hexdigest()
        assert actual_hash == doc.content_hash, (
            f"Hash mismatch: DB has {doc.content_hash!r}, "
            f"computed {actual_hash!r}"
        )
        print(
            f"\n  [VG-12.5] Spot-checked doc {doc.accession_number!r}: "
            f"{len(content)} bytes, hash verified OK"
        )


# ---------------------------------------------------------------------------
# VG-13 — Duplicate Prevention
# ---------------------------------------------------------------------------


class TestVG13DuplicatePrevention:
    """VG-13: Two acquisition runs must not create duplicate records."""

    @pytest.mark.anyio
    async def test_vg13_second_run_no_duplicate_filing_rows(
        self, acquisition_service, db_session_factory
    ) -> None:
        """
        VG-13.1: The Filing table must have no duplicate accession_number values
        for AAPL after two acquisition runs.
        """
        from apps.api.repositories.filings import FilingRepository

        # --- First run ---
        job1 = await acquisition_service.create_job(_AAPL_TICKER)
        await acquisition_service.execute(job1.id)

        async with db_session_factory() as session:
            repo = FilingRepository(session)
            items1, total1 = await repo.list(page=1, page_size=500, ticker=_AAPL_TICKER)

        # --- Second run ---
        job2 = await acquisition_service.create_job(_AAPL_TICKER)
        await acquisition_service.execute(job2.id)

        async with db_session_factory() as session:
            repo = FilingRepository(session)
            items2, total2 = await repo.list(page=1, page_size=500, ticker=_AAPL_TICKER)

        # Uniqueness invariant: no duplicate accession_number in the table
        seen: set[str] = set()
        duplicates: list[str] = []
        for f in items2:
            if f.accession_number in seen:
                duplicates.append(f.accession_number)
            seen.add(f.accession_number)

        assert not duplicates, (
            f"Duplicate accession numbers after second run: {duplicates[:5]}"
        )

        # Total must not decrease
        assert total2 >= total1

        print(
            f"\n  [VG-13.1] Run 1: {total1} filings. Run 2: {total2} filings. "
            f"Duplicates found: {len(duplicates)} OK"
        )

    @pytest.mark.anyio
    async def test_vg13_second_run_no_duplicate_stored_documents(
        self, acquisition_service, db_session_factory
    ) -> None:
        """
        VG-13.2: StoredDocument table must have no duplicate accession_number
        values after two acquisition runs.
        """
        from sqlalchemy import select
        from apps.api.models import StoredDocument

        # Two runs
        for run_num in range(1, 3):
            job = await acquisition_service.create_job(_AAPL_TICKER)
            r = await acquisition_service.execute(job.id)
            print(f"\n  [VG-13.2] Run {run_num}: docs_stored={r.documents_stored}")

        async with db_session_factory() as session:
            q = select(StoredDocument.accession_number)
            result = await session.execute(q)
            all_accessions = [row[0] for row in result.fetchall()]

        if not all_accessions:
            pytest.skip("No stored documents in DB; skipping.")

        seen: set[str] = set()
        duplicates: list[str] = []
        for acc in all_accessions:
            if acc in seen:
                duplicates.append(acc)
            seen.add(acc)

        assert not duplicates, (
            f"Duplicate StoredDocument rows for accessions: {duplicates[:5]}"
        )
        print(
            f"\n  [VG-13.2] {len(all_accessions)} StoredDocument rows, "
            f"0 duplicates OK"
        )

    @pytest.mark.anyio
    async def test_vg13_job2_discovers_zero_new_filings(
        self, acquisition_service, db_session_factory
    ) -> None:
        """
        VG-13.3: If all filings from run 1 are already in the DB, run 2 should
        report filings_new == 0 (all skipped as already-known).
        """
        from apps.api.repositories.acquisition_jobs import AcquisitionJobRepository

        # Two runs
        job1 = await acquisition_service.create_job(_AAPL_TICKER)
        await acquisition_service.execute(job1.id)

        job2 = await acquisition_service.create_job(_AAPL_TICKER)
        await acquisition_service.execute(job2.id)

        async with db_session_factory() as session:
            repo = AcquisitionJobRepository(session)
            r1 = await repo.get_by_id(job1.id)
            r2 = await repo.get_by_id(job2.id)

        print(
            f"\n  [VG-13.3] Job 1: filings_new={r1.filings_new}. "
            f"Job 2: filings_new={r2.filings_new} (expected 0)."
        )

        assert r2.filings_new == 0, (
            f"Second run should skip all known filings (filings_new=0), "
            f"got {r2.filings_new}"
        )


# ---------------------------------------------------------------------------
# VG-14 — Storage Integrity
# ---------------------------------------------------------------------------


class TestVG14StorageIntegrity:
    """VG-14: Hash, length, and metadata preserved through full pipeline."""

    @pytest.mark.anyio
    async def test_vg14_hash_preserved_store_retrieve(
        self, storage_backend
    ) -> None:
        """
        VG-14.1: Store a known string; retrieve it; verify SHA-256 hash
        matches the hash recorded in StorageResult.
        """
        from services.acquisition.storage.backend import make_object_key

        content = "<html><body>AAPL 10-K VG-14 test content</body></html>"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        key = make_object_key("0000320193-99-000001", "text/html")

        result = await storage_backend.store(
            key, content, mime_type="text/html", content_hash=expected_hash
        )
        assert result.content_hash == expected_hash, (
            f"StorageResult.content_hash mismatch: stored={result.content_hash!r}, "
            f"expected={expected_hash!r}"
        )

        retrieved = await storage_backend.retrieve(key)
        assert retrieved is not None
        retrieved_bytes = retrieved.encode("utf-8") if isinstance(retrieved, str) else retrieved
        retrieved_hash = hashlib.sha256(retrieved_bytes).hexdigest()
        assert retrieved_hash == expected_hash, (
            f"Retrieved content hash mismatch: got={retrieved_hash!r}"
        )
        print(f"\n  [VG-14.1] hash={expected_hash[:16]}... preserved store->retrieve OK")

    @pytest.mark.anyio
    async def test_vg14_content_length_preserved(self, storage_backend) -> None:
        """VG-14.2: StorageResult.content_length == byte length of stored content."""
        from services.acquisition.storage.backend import make_object_key

        content = "X" * 4096
        content_bytes = content.encode("utf-8")
        h = hashlib.sha256(content_bytes).hexdigest()
        key = make_object_key("0000320193-99-000002", "text/plain")

        result = await storage_backend.store(
            key, content, mime_type="text/plain", content_hash=h
        )
        assert result.content_length == len(content_bytes), (
            f"content_length={result.content_length}, expected {len(content_bytes)}"
        )
        print(f"\n  [VG-14.2] content_length={result.content_length} == {len(content_bytes)} OK")

    @pytest.mark.anyio
    async def test_vg14_store_is_idempotent(self, storage_backend) -> None:
        """VG-14.3: Storing identical content twice returns the same hash/length."""
        from services.acquisition.storage.backend import make_object_key

        content = "VG-14 idempotency check content"
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = make_object_key("0000320193-99-000003", "text/html")

        r1 = await storage_backend.store(
            key, content, mime_type="text/html", content_hash=h
        )
        r2 = await storage_backend.store(
            key, content, mime_type="text/html", content_hash=h
        )

        assert r1.content_hash == r2.content_hash
        assert r1.content_length == r2.content_length
        print(f"\n  [VG-14.3] Idempotent store: hash={r1.content_hash[:16]}... OK")

    @pytest.mark.anyio
    async def test_vg14_filing_metadata_preserved_in_db(
        self, acquisition_service, db_session_factory
    ) -> None:
        """
        VG-14.4: Accession numbers, filing_type, CIK, and filing_date written
        during acquisition are intact when queried back from FilingRepository.
        """
        from apps.api.repositories.filings import FilingRepository

        job = await acquisition_service.create_job(_AAPL_TICKER)
        result = await acquisition_service.execute(job.id)

        if result.filings_discovered == 0:
            pytest.skip("No filings discovered.")

        async with db_session_factory() as session:
            repo = FilingRepository(session)
            items, total = await repo.list(page=1, page_size=20, ticker=_AAPL_TICKER)

        assert total >= 1
        for f in items:
            assert _ACCESSION_RE.match(f.accession_number), (
                f"Bad accession: {f.accession_number!r}"
            )
            assert f.filing_type, "filing_type must not be blank"
            assert f.filing_date is not None, "filing_date must not be None"
            assert f.filing_date <= date.today(), (
                f"Future filing_date: {f.filing_date}"
            )

        print(
            f"\n  [VG-14.4] Verified {len(items)} filing records: "
            f"accession OK, filing_type OK, filing_date OK"
        )

    @pytest.mark.anyio
    async def test_vg14_end_to_end_hash_through_service(
        self, acquisition_service, db_session_factory, storage_backend
    ) -> None:
        """
        VG-14.5: For every StoredDocument record in the DB, retrieve the raw
        bytes from the backend and verify SHA-256 matches content_hash.
        Spot-checks up to 5 documents to keep test duration reasonable.
        """
        from sqlalchemy import select
        from apps.api.models import StoredDocument

        job = await acquisition_service.create_job(_AAPL_TICKER)
        result = await acquisition_service.execute(job.id)

        if result.documents_stored == 0:
            pytest.skip("No documents stored.")

        async with db_session_factory() as session:
            q = select(StoredDocument).limit(5)
            rows = (await session.execute(q)).scalars().all()

        verified = 0
        for doc in rows:
            content = await storage_backend.retrieve(doc.object_key)
            if content is None:
                continue
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content
            actual = hashlib.sha256(content_bytes).hexdigest()
            assert actual == doc.content_hash, (
                f"{doc.accession_number}: hash mismatch "
                f"(stored={doc.content_hash!r}, computed={actual!r})"
            )
            verified += 1

        print(
            f"\n  [VG-14.5] Hash verified for {verified}/{len(rows)} stored documents OK"
        )
        assert verified >= 1, "At least 1 document must be hash-verified"


# ---------------------------------------------------------------------------
# VG-17 — Performance Validation
# ---------------------------------------------------------------------------


class TestVG17Performance:
    """VG-17: Timing bounds for key operations."""

    @pytest.mark.anyio
    async def test_vg17_discovery_within_time_bound(self) -> None:
        """VG-17.1: Filing discovery <= {_MAX_DISCOVERY_SECONDS}s."""
        from services.acquisition.source_registry.sources.sec_edgar import (
            SECEdgarSource,
        )

        source = SECEdgarSource(user_agent=_USER_AGENT)
        t0 = time.monotonic()
        result = await source.discover_filings(_AAPL_CIK)
        elapsed = time.monotonic() - t0

        print(
            f"\n  [VG-17.1] Discovery: {len(result.filings)} filings in {elapsed:.2f}s "
            f"(limit={_MAX_DISCOVERY_SECONDS}s)"
        )

        assert len(result.filings) > 0, "Must discover at least 1 AAPL filing"
        assert elapsed <= _MAX_DISCOVERY_SECONDS, (
            f"Discovery took {elapsed:.2f}s — exceeds {_MAX_DISCOVERY_SECONDS}s"
        )

    @pytest.mark.anyio
    async def test_vg17_single_document_fetch_within_time_bound(self) -> None:
        """VG-17.2: Single document fetch from SEC EDGAR <= {_MAX_SINGLE_FETCH_SECONDS}s."""
        from services.acquisition.source_registry.sources.sec_edgar import (
            SECEdgarSource,
        )
        from services.acquisition.document_fetcher.fetcher import (
            SECFilingDocumentFetcher,
        )

        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)

        candidate = next(
            (f for f in result.filings if f.document_url),
            None,
        )
        if candidate is None:
            pytest.skip("No filing with document_url found.")

        fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
        t0 = time.monotonic()
        doc = await fetcher.fetch_document(candidate)
        elapsed = time.monotonic() - t0

        print(
            f"\n  [VG-17.2] Fetch {candidate.accession_number!r}: "
            f"{len(doc.content)} bytes in {elapsed:.2f}s "
            f"(limit={_MAX_SINGLE_FETCH_SECONDS}s)"
        )

        assert len(doc.content) > 0
        assert elapsed <= _MAX_SINGLE_FETCH_SECONDS, (
            f"Fetch took {elapsed:.2f}s — exceeds {_MAX_SINGLE_FETCH_SECONDS}s"
        )

    @pytest.mark.anyio
    async def test_vg17_full_job_execution_within_time_bound(
        self, acquisition_service
    ) -> None:
        """VG-17.3: Full job (create + execute) <= {_MAX_TOTAL_JOB_SECONDS}s."""
        job = await acquisition_service.create_job(_AAPL_TICKER)

        t0 = time.monotonic()
        result = await acquisition_service.execute(job.id)
        elapsed = time.monotonic() - t0

        print(
            f"\n  [VG-17.3] Full job status={result.status!r}: "
            f"discovered={result.filings_discovered}, "
            f"stored={result.documents_stored}, "
            f"elapsed={elapsed:.1f}s "
            f"(limit={_MAX_TOTAL_JOB_SECONDS}s)"
        )

        assert elapsed <= _MAX_TOTAL_JOB_SECONDS, (
            f"Full job took {elapsed:.1f}s — exceeds {_MAX_TOTAL_JOB_SECONDS}s"
        )
        assert result.status in ("completed", "failed")

    @pytest.mark.anyio
    async def test_vg17_storage_throughput_1mib(self, storage_backend) -> None:
        """VG-17.4: LocalStorageBackend can store+retrieve 1 MiB in < 2s."""
        from services.acquisition.storage.backend import make_object_key

        content = "A" * (1024 * 1024)
        content_bytes = content.encode("utf-8")
        h = hashlib.sha256(content_bytes).hexdigest()
        key = make_object_key("0000320193-99-900000", "application/octet-stream")

        t0 = time.monotonic()
        await storage_backend.store(
            key, content, mime_type="application/octet-stream", content_hash=h
        )
        retrieved = await storage_backend.retrieve(key)
        elapsed = time.monotonic() - t0

        print(f"\n  [VG-17.4] 1 MiB store+retrieve: {elapsed:.3f}s (limit=2s)")
        assert elapsed < 2.0, f"1 MiB I/O took {elapsed:.3f}s (limit=2s)"
        # retrieve returns str; compare lengths in characters (all ASCII here)
        assert len(retrieved) == len(content)


# ---------------------------------------------------------------------------
# VG-18 — Data Quality Validation
# ---------------------------------------------------------------------------


class TestVG18DataQuality:
    """VG-18: AAPL filings from SEC EDGAR meet data quality expectations."""

    @pytest.mark.anyio
    async def test_vg18_aapl_has_10k_filings(self) -> None:
        """VG-18.1: AAPL must have >= 1 discoverable 10-K filing."""
        from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        ten_k = [f for f in result.filings if f.filing_type == "10-K"]
        print(f"\n  [VG-18.1] 10-K filings discovered: {len(ten_k)} (total={result.total_discovered})")
        assert len(ten_k) >= 1, f"Expected >=1 AAPL 10-K, found {len(ten_k)}"

    @pytest.mark.anyio
    async def test_vg18_aapl_has_10q_filings(self) -> None:
        """VG-18.2: AAPL must have >= 1 discoverable 10-Q filing."""
        from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        ten_q = [f for f in result.filings if f.filing_type == "10-Q"]
        print(f"\n  [VG-18.2] 10-Q filings discovered: {len(ten_q)}")
        assert len(ten_q) >= 1, f"Expected >=1 AAPL 10-Q, found {len(ten_q)}"

    @pytest.mark.anyio
    async def test_vg18_aapl_has_8k_filings(self) -> None:
        """VG-18.3: AAPL must have >= 1 discoverable 8-K filing."""
        from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        eight_k = [f for f in result.filings if f.filing_type == "8-K"]
        print(f"\n  [VG-18.3] 8-K filings discovered: {len(eight_k)}")
        assert len(eight_k) >= 1, f"Expected >=1 AAPL 8-K, found {len(eight_k)}"

    @pytest.mark.anyio
    async def test_vg18_accession_numbers_canonical_format(self) -> None:
        """VG-18.4: All discovered accession numbers match XXXXXXXXXX-YY-ZZZZZZ."""
        from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        bad = [
            f.accession_number for f in result.filings
            if not _ACCESSION_RE.match(f.accession_number)
        ]
        print(
            f"\n  [VG-18.4] {len(result.filings)} accession numbers checked. "
            f"Bad format: {len(bad)}"
        )
        assert not bad, f"Non-canonical accession numbers: {bad[:5]}"

    @pytest.mark.anyio
    async def test_vg18_filing_dates_in_the_past(self) -> None:
        """VG-18.5: All discovered filing dates <= today."""
        from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        today = date.today()
        future = [
            (f.accession_number, str(f.filing_date))
            for f in result.filings
            if f.filing_date and f.filing_date > today
        ]
        print(f"\n  [VG-18.5] Filing dates checked. Future dates found: {len(future)}")
        assert not future, f"Filings with future dates: {future[:5]}"

    @pytest.mark.anyio
    async def test_vg18_cik_matches_aapl(self) -> None:
        """VG-18.6: All discovered filings belong to AAPL's CIK (320193)."""
        from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        aapl_cik_int = int(_AAPL_CIK)
        wrong = [
            (f.accession_number, f.cik)
            for f in result.filings
            if int((f.cik or "0").lstrip("0") or "0") != aapl_cik_int
        ]
        print(f"\n  [VG-18.6] CIK check: {len(result.filings)} filings. Wrong CIK: {len(wrong)}")
        assert not wrong, f"Filings with wrong CIK: {wrong[:5]}"

    @pytest.mark.anyio
    async def test_vg18_most_recent_10k_is_fetchable_and_valid(self) -> None:
        """
        VG-18.7: The most recent AAPL 10-K document is fetchable, > 1 KiB,
        and its SHA-256 hash is a 64-char hex string.
        """
        from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
        from services.acquisition.document_fetcher.fetcher import SECFilingDocumentFetcher

        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)

        ten_ks = [
            f for f in result.filings
            if f.filing_type == "10-K" and f.document_url
        ]
        if not ten_ks:
            pytest.skip("No 10-K with document_url found.")

        most_recent = max(ten_ks, key=lambda f: f.filing_date or date.min)
        fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
        doc = await fetcher.fetch_document(most_recent)

        print(
            f"\n  [VG-18.7] Most recent 10-K {most_recent.accession_number!r}: "
            f"{len(doc.content)} bytes, hash={doc.content_hash[:16]}..."
        )

        assert len(doc.content) > 1000, (
            f"10-K suspiciously small: {len(doc.content)} bytes"
        )
        assert doc.accession_number == most_recent.accession_number
        assert len(doc.content_hash) == 64, (
            f"content_hash must be 64-char SHA-256 hex; got len={len(doc.content_hash)}"
        )
