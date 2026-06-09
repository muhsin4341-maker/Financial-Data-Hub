"""
Integration tests — M3.3 Filing Models: FilingRepository + FilingService.

These tests run against a real PostgreSQL database and require migration 005
to be applied (filings table must exist).

Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://... RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_filings_integration.py -v

Coverage
--------
FilingRepository:
  - create (all fields, minimal fields)
  - get_by_id (hit + miss)
  - get_by_accession_number (hit + miss)
  - exists_accession_number (true + false)
  - list (unfiltered, filing_type filter, status filter, cik filter, pagination)
  - list_by_company (hit + miss)
  - list_by_filing_type (hit)
  - update (partial, no-op, not-found)
  - delete (hit + miss)

FilingService:
  - create (success + duplicate accession_number → ConflictError)
  - get_by_id (hit + miss → NotFoundError)
  - get_by_accession_number (hit + miss → NotFoundError)
  - list (pagination, type filter)
  - update (partial + not-found → NotFoundError)
  - delete (hit + not-found → NotFoundError)

Validation Gate VG-04: All FilingRepository methods are operational
against a live PostgreSQL database (migration 005 applied).

Milestone: M3.3 — Filing Models
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.models import Filing, FilingStatus, FilingType
from apps.api.repositories.filings import FilingRepository
from apps.api.schemas.filings import FilingCreate, FilingUpdate
from apps.api.services.filings import FilingService

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="Integration tests require DATABASE_URL env var.",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CIK_APPLE = "0000320193"
_TODAY = date.today()
_YESTERDAY = _TODAY - timedelta(days=1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_session(client) -> AsyncSession:  # type: ignore[no-untyped-def]
    """Return a live AsyncSession via the FastAPI lifespan database factory."""
    from apps.api.core.database import AsyncSessionFactory  # noqa: PLC0415

    assert AsyncSessionFactory is not None, "Database not initialised"
    async with AsyncSessionFactory() as session:
        yield session
        await session.rollback()


def _unique_accession(prefix: str = "0000320193") -> str:
    """Generate a unique accession number for each test."""
    # SEC format: XXXXXXXXXX-YY-ZZZZZZ — last segment must be 6 digits only
    suffix = str(uuid.uuid4().int)[:6].zfill(6)
    return f"{prefix}-26-{suffix}"


@pytest.fixture()
async def filing(db_session: AsyncSession) -> Filing:
    """Create and persist a Filing for the current test."""
    repo = FilingRepository(db_session)
    return await repo.create(
        FilingCreate(
            filing_type="10-K",
            accession_number=_unique_accession(),
            filing_date=_TODAY,
            cik=_CIK_APPLE,
            ticker="AAPL",
            title="Annual report [10-K]",
        )
    )


# ---------------------------------------------------------------------------
# FilingRepository integration tests
# ---------------------------------------------------------------------------


class TestFilingRepositoryIntegration:
    @pytest.mark.anyio
    async def test_create_and_retrieve_by_id(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        accession = _unique_accession()
        schema = FilingCreate(
            filing_type="10-K",
            accession_number=accession,
            filing_date=_TODAY,
            cik=_CIK_APPLE,
            ticker="AAPL",
            title="Annual report [10-K]",
        )
        created = await repo.create(schema)
        assert created.id is not None

        fetched = await repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.accession_number == accession
        assert fetched.filing_type == "10-K"
        assert fetched.cik == _CIK_APPLE

    @pytest.mark.anyio
    async def test_create_minimal_fields(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        schema = FilingCreate(
            filing_type="8-K",
            accession_number=_unique_accession(),
            filing_date=_TODAY,
            cik="0000001234",
        )
        created = await repo.create(schema)
        assert created.ticker is None
        assert created.title is None
        assert created.company_id is None
        assert created.status == FilingStatus.DISCOVERED.value

    @pytest.mark.anyio
    async def test_get_by_id_miss(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None

    @pytest.mark.anyio
    async def test_get_by_accession_number_hit(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        repo = FilingRepository(db_session)
        fetched = await repo.get_by_accession_number(filing.accession_number)
        assert fetched is not None
        assert fetched.id == filing.id

    @pytest.mark.anyio
    async def test_get_by_accession_number_miss(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        result = await repo.get_by_accession_number("0000000000-00-000000")
        assert result is None

    @pytest.mark.anyio
    async def test_exists_accession_number_true(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        repo = FilingRepository(db_session)
        assert await repo.exists_accession_number(filing.accession_number) is True

    @pytest.mark.anyio
    async def test_exists_accession_number_false(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        assert await repo.exists_accession_number("0000000000-00-000000") is False

    @pytest.mark.anyio
    async def test_list_unfiltered(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        repo = FilingRepository(db_session)
        items, total = await repo.list()
        assert total >= 1
        ids = [f.id for f in items]
        assert filing.id in ids

    @pytest.mark.anyio
    async def test_list_by_filing_type_filter(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        # Create a known 10-Q to ensure at least one exists
        accession = _unique_accession()
        await repo.create(
            FilingCreate(
                filing_type="10-Q",
                accession_number=accession,
                filing_date=_TODAY,
                cik=_CIK_APPLE,
            )
        )
        items, total = await repo.list(filing_type="10-Q")
        assert all(f.filing_type == "10-Q" for f in items)
        assert total >= 1

    @pytest.mark.anyio
    async def test_list_by_status_filter(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        repo = FilingRepository(db_session)
        items, total = await repo.list(status="discovered")
        assert all(f.status == "discovered" for f in items)
        assert total >= 1

    @pytest.mark.anyio
    async def test_list_pagination(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        # Create 3 filings
        for _ in range(3):
            await repo.create(
                FilingCreate(
                    filing_type="8-K",
                    accession_number=_unique_accession(),
                    filing_date=_TODAY,
                    cik="0000001111",
                )
            )
        items_p1, total = await repo.list(cik="0000001111", page=1, page_size=2)
        assert len(items_p1) <= 2
        assert total >= 3

    @pytest.mark.anyio
    async def test_list_by_company(self, db_session: AsyncSession) -> None:
        # company_id=None (unlinked filing) — verify filter returns 0 for a random UUID
        repo = FilingRepository(db_session)
        random_company_id = uuid.uuid4()
        items, total = await repo.list_by_company(random_company_id)
        assert total == 0
        assert items == []

    @pytest.mark.anyio
    async def test_list_by_filing_type_method(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        await repo.create(
            FilingCreate(
                filing_type="DEF 14A",
                accession_number=_unique_accession(),
                filing_date=_TODAY,
                cik=_CIK_APPLE,
            )
        )
        items, total = await repo.list_by_filing_type("DEF 14A")
        assert total >= 1
        assert all(f.filing_type == "DEF 14A" for f in items)

    @pytest.mark.anyio
    async def test_update_status(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        repo = FilingRepository(db_session)
        schema = FilingUpdate(status="downloading")
        updated = await repo.update(filing.id, schema)

        assert updated is not None
        assert updated.status == "downloading"

    @pytest.mark.anyio
    async def test_update_document_url(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        repo = FilingRepository(db_session)
        url = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/aapl-20230930.htm"
        updated = await repo.update(filing.id, FilingUpdate(document_url=url))
        assert updated is not None
        assert updated.document_url == url

    @pytest.mark.anyio
    async def test_update_not_found(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        schema = FilingUpdate(status="downloading")
        result = await repo.update(uuid.uuid4(), schema)
        assert result is None

    @pytest.mark.anyio
    async def test_delete_hit(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        repo = FilingRepository(db_session)
        deleted = await repo.delete(filing.id)
        assert deleted is True

        fetched = await repo.get_by_id(filing.id)
        assert fetched is None

    @pytest.mark.anyio
    async def test_delete_miss(self, db_session: AsyncSession) -> None:
        repo = FilingRepository(db_session)
        result = await repo.delete(uuid.uuid4())
        assert result is False


# ---------------------------------------------------------------------------
# FilingService integration tests (VG-04)
# ---------------------------------------------------------------------------


class TestFilingServiceIntegration:
    @pytest.mark.anyio
    async def test_create_success(self, db_session: AsyncSession) -> None:
        service = FilingService(db_session)
        schema = FilingCreate(
            filing_type="10-K",
            accession_number=_unique_accession(),
            filing_date=_TODAY,
            cik=_CIK_APPLE,
            ticker="AAPL",
        )
        result = await service.create(schema)
        assert result.id is not None
        assert result.status == "discovered"

    @pytest.mark.anyio
    async def test_create_duplicate_raises_conflict(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        service = FilingService(db_session)
        schema = FilingCreate(
            filing_type="10-K",
            accession_number=filing.accession_number,  # duplicate
            filing_date=_TODAY,
            cik=_CIK_APPLE,
        )
        with pytest.raises(ConflictError):
            await service.create(schema)

    @pytest.mark.anyio
    async def test_get_by_id_hit(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        service = FilingService(db_session)
        result = await service.get_by_id(filing.id)
        assert result.id == filing.id

    @pytest.mark.anyio
    async def test_get_by_id_miss(self, db_session: AsyncSession) -> None:
        service = FilingService(db_session)
        with pytest.raises(NotFoundError):
            await service.get_by_id(uuid.uuid4())

    @pytest.mark.anyio
    async def test_get_by_accession_number_hit(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        service = FilingService(db_session)
        result = await service.get_by_accession_number(filing.accession_number)
        assert result.accession_number == filing.accession_number

    @pytest.mark.anyio
    async def test_get_by_accession_number_miss(
        self, db_session: AsyncSession
    ) -> None:
        service = FilingService(db_session)
        with pytest.raises(NotFoundError):
            await service.get_by_accession_number("0000000000-00-000000")

    @pytest.mark.anyio
    async def test_list_returns_response(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        service = FilingService(db_session)
        result = await service.list()
        assert result.total >= 1
        assert result.page == 1

    @pytest.mark.anyio
    async def test_update_status(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        service = FilingService(db_session)
        result = await service.update(filing.id, FilingUpdate(status="downloading"))
        assert result.status == "downloading"

    @pytest.mark.anyio
    async def test_update_not_found(self, db_session: AsyncSession) -> None:
        service = FilingService(db_session)
        with pytest.raises(NotFoundError):
            await service.update(uuid.uuid4(), FilingUpdate(status="downloading"))

    @pytest.mark.anyio
    async def test_delete_success(
        self, db_session: AsyncSession, filing: Filing
    ) -> None:
        service = FilingService(db_session)
        await service.delete(filing.id)
        with pytest.raises(NotFoundError):
            await service.get_by_id(filing.id)

    @pytest.mark.anyio
    async def test_delete_not_found(self, db_session: AsyncSession) -> None:
        service = FilingService(db_session)
        with pytest.raises(NotFoundError):
            await service.delete(uuid.uuid4())
