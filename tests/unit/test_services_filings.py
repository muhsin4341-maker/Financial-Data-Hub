"""
Unit tests — FilingService.

Strategy
--------
FilingRepository is patched at the import path used by the service.
All repository methods return pre-built MagicMock / FilingRead objects.
The service is tested in isolation — no database, no HTTP layer.

What is mocked
--------------
- ``FilingRepository``  — all repo methods via AsyncMock
- ``AsyncSession``      — passed to FilingService constructor

What is NOT mocked (real code runs)
------------------------------------
- FilingService.create              — BR-1/BR-2 ConflictError on duplicate accession_number
- FilingService.get_by_id           — NotFoundError on None
- FilingService.get_by_accession_number — NotFoundError on None
- FilingService.list                — pagination envelope construction
- FilingService.update              — NotFoundError on None
- FilingService.delete              — NotFoundError on False
- FilingService._to_read            — ORM-to-schema conversion

Milestone: M3.3 — Filing Models
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.models import Filing, FilingStatus, FilingType
from apps.api.schemas.filings import FilingCreate, FilingRead, FilingUpdate
from apps.api.services.filings import FilingService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_TODAY = date.today()
_ACCESSION = "0000320193-23-000077"
_CIK = "0000320193"


def _make_filing_orm(
    filing_id: uuid.UUID | None = None,
    accession_number: str = _ACCESSION,
    filing_type: str = FilingType.K10,
    cik: str = _CIK,
    status: str = FilingStatus.DISCOVERED,
) -> MagicMock:
    f = MagicMock(spec=Filing)
    f.id = filing_id or uuid.uuid4()
    f.accession_number = accession_number
    f.filing_type = filing_type
    f.cik = cik
    f.filing_date = _TODAY
    f.period_end_date = None
    f.ticker = "AAPL"
    f.title = "Annual report [10-K]"
    f.filing_url = None
    f.document_url = None
    f.status = status
    f.company_id = None
    f.source_config_id = None
    f.filing_metadata = None
    f.created_at = _NOW
    f.updated_at = _NOW
    return f


def _make_service_with_mock_repo() -> tuple[FilingService, MagicMock]:
    """Return a FilingService instance and its mocked repository."""
    session = MagicMock(spec=AsyncSession)
    service = FilingService(session)
    mock_repo = AsyncMock()
    service._repo = mock_repo
    return service, mock_repo


def _make_create_schema(
    accession_number: str = _ACCESSION,
    filing_type: str = "10-K",
    cik: str = _CIK,
    filing_date: date = _TODAY,
) -> FilingCreate:
    return FilingCreate(
        filing_type=filing_type,
        accession_number=accession_number,
        filing_date=filing_date,
        cik=cik,
    )


# ===========================================================================
# FilingService — create
# ===========================================================================


class TestFilingServiceCreate:
    @pytest.mark.anyio
    async def test_create_returns_filing_read(self) -> None:
        service, repo = _make_service_with_mock_repo()
        orm_filing = _make_filing_orm()
        repo.exists_accession_number.return_value = False
        repo.create.return_value = orm_filing

        result = await service.create(_make_create_schema())

        assert isinstance(result, FilingRead)
        assert str(result.id) == str(orm_filing.id)

    @pytest.mark.anyio
    async def test_create_raises_conflict_when_accession_exists(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.exists_accession_number.return_value = True

        with pytest.raises(ConflictError):
            await service.create(_make_create_schema())

        repo.create.assert_not_awaited()

    @pytest.mark.anyio
    async def test_create_raises_conflict_on_integrity_error(self) -> None:
        """Race condition: exists_accession_number returns False but insert fails."""
        service, repo = _make_service_with_mock_repo()
        repo.exists_accession_number.return_value = False
        repo.create.side_effect = IntegrityError(None, None, None)

        with pytest.raises(ConflictError):
            await service.create(_make_create_schema())

    @pytest.mark.anyio
    async def test_create_calls_exists_before_create(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.exists_accession_number.return_value = False
        repo.create.return_value = _make_filing_orm()

        await service.create(_make_create_schema())

        repo.exists_accession_number.assert_awaited_once_with(_ACCESSION)
        repo.create.assert_awaited_once()


# ===========================================================================
# FilingService — get_by_id
# ===========================================================================


class TestFilingServiceGetById:
    @pytest.mark.anyio
    async def test_returns_filing_read_when_found(self) -> None:
        service, repo = _make_service_with_mock_repo()
        orm_filing = _make_filing_orm()
        repo.get_by_id.return_value = orm_filing

        result = await service.get_by_id(orm_filing.id)

        assert isinstance(result, FilingRead)
        assert str(result.id) == str(orm_filing.id)

    @pytest.mark.anyio
    async def test_raises_not_found_when_missing(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_by_id(uuid.uuid4())


# ===========================================================================
# FilingService — get_by_accession_number
# ===========================================================================


class TestFilingServiceGetByAccessionNumber:
    @pytest.mark.anyio
    async def test_returns_filing_read_when_found(self) -> None:
        service, repo = _make_service_with_mock_repo()
        orm_filing = _make_filing_orm()
        repo.get_by_accession_number.return_value = orm_filing

        result = await service.get_by_accession_number(_ACCESSION)

        assert isinstance(result, FilingRead)

    @pytest.mark.anyio
    async def test_raises_not_found_when_missing(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.get_by_accession_number.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_by_accession_number(_ACCESSION)


# ===========================================================================
# FilingService — list
# ===========================================================================


class TestFilingServiceList:
    @pytest.mark.anyio
    async def test_returns_filing_list_response(self) -> None:
        service, repo = _make_service_with_mock_repo()
        filings = [_make_filing_orm(), _make_filing_orm(accession_number="0000320193-23-000088")]
        repo.list.return_value = (filings, 2)

        result = await service.list(page=1, page_size=10)

        assert result.total == 2
        assert result.page == 1
        assert result.page_size == 10
        assert result.pages == 1
        assert len(result.items) == 2

    @pytest.mark.anyio
    async def test_pages_computed_correctly(self) -> None:
        service, repo = _make_service_with_mock_repo()
        filings = [_make_filing_orm()]
        repo.list.return_value = (filings, 25)

        result = await service.list(page=1, page_size=10)

        assert result.pages == 3  # ceil(25/10)

    @pytest.mark.anyio
    async def test_empty_list(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.list.return_value = ([], 0)

        result = await service.list()

        assert result.total == 0
        assert result.items == []
        assert result.pages == 0

    @pytest.mark.anyio
    async def test_passes_filters_to_repo(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.list.return_value = ([], 0)
        company_id = uuid.uuid4()

        await service.list(
            filing_type="10-K",
            status="discovered",
            cik=_CIK,
            company_id=company_id,
            page=2,
            page_size=5,
        )

        repo.list.assert_awaited_once_with(
            page=2,
            page_size=5,
            filing_type="10-K",
            status="discovered",
            cik=_CIK,
            ticker=None,
            company_id=company_id,
            source_config_id=None,
        )


# ===========================================================================
# FilingService — update
# ===========================================================================


class TestFilingServiceUpdate:
    @pytest.mark.anyio
    async def test_returns_updated_filing_read(self) -> None:
        service, repo = _make_service_with_mock_repo()
        orm_filing = _make_filing_orm(status=FilingStatus.DOWNLOADING)
        repo.update.return_value = orm_filing

        schema = FilingUpdate(status="downloading")
        result = await service.update(orm_filing.id, schema)

        assert isinstance(result, FilingRead)

    @pytest.mark.anyio
    async def test_raises_not_found_when_missing(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.update.return_value = None

        schema = FilingUpdate(status="downloading")
        with pytest.raises(NotFoundError):
            await service.update(uuid.uuid4(), schema)


# ===========================================================================
# FilingService — delete
# ===========================================================================


class TestFilingServiceDelete:
    @pytest.mark.anyio
    async def test_succeeds_when_found(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.delete.return_value = True

        await service.delete(uuid.uuid4())  # No exception expected

    @pytest.mark.anyio
    async def test_raises_not_found_when_missing(self) -> None:
        service, repo = _make_service_with_mock_repo()
        repo.delete.return_value = False

        with pytest.raises(NotFoundError):
            await service.delete(uuid.uuid4())
