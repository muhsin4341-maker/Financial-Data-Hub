"""
Unit tests — FilingRepository.

Strategy
--------
All database calls are replaced by AsyncMock so tests run without a live
PostgreSQL instance.  The AsyncSession mock is constructed per-test and
configured to return pre-built MagicMock ORM objects.

What is mocked
--------------
- ``session.execute``  — returns mock Result objects configured per test
- ``session.add``      — records that the object was added (sync, no-op)
- ``session.flush``    — no-op coroutine
- ``session.delete``   — async no-op

What is NOT mocked (real code runs)
------------------------------------
- ``FilingRepository`` method logic (all 9 methods)
- ``_UPDATABLE_FIELDS`` allowlist (accession_number excluded)
- Partial-update logic via ``model_fields_set``
- structlog calls (silently no-op in tests)

Milestone: M3.3 — Filing Models
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.api.models import Filing, FilingStatus, FilingType
from apps.api.repositories.filings import FilingRepository, _UPDATABLE_FIELDS
from apps.api.schemas.filings import FilingCreate, FilingUpdate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_TODAY = date.today()

_ACCESSION = "0000320193-23-000077"
_CIK = "0000320193"


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    return session


def _mock_scalar_one_or_none(value: Any) -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return AsyncMock(return_value=result)


def _mock_scalar_one(value: Any) -> AsyncMock:
    result = MagicMock()
    result.scalar_one.return_value = value
    return AsyncMock(return_value=result)


def _mock_scalars_all(items: list) -> AsyncMock:
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = items
    result = MagicMock()
    result.scalars.return_value = scalars_mock
    return AsyncMock(return_value=result)


def _make_filing(
    filing_id: uuid.UUID | None = None,
    accession_number: str = _ACCESSION,
    filing_type: str = FilingType.K10,
    cik: str = _CIK,
    filing_date: date = _TODAY,
    status: str = FilingStatus.DISCOVERED,
    company_id: uuid.UUID | None = None,
) -> MagicMock:
    f = MagicMock(spec=Filing)
    f.id = filing_id or uuid.uuid4()
    f.accession_number = accession_number
    f.filing_type = filing_type
    f.cik = cik
    f.filing_date = filing_date
    f.period_end_date = None
    f.ticker = "AAPL"
    f.title = "Annual report [10-K]"
    f.filing_url = None
    f.document_url = None
    f.status = status
    f.company_id = company_id
    f.source_config_id = None
    f.filing_metadata = None
    f.created_at = _NOW
    f.updated_at = _NOW
    return f


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
# _UPDATABLE_FIELDS
# ===========================================================================


class TestUpdatableFields:
    def test_accession_number_excluded(self) -> None:
        assert "accession_number" not in _UPDATABLE_FIELDS

    def test_id_excluded(self) -> None:
        assert "id" not in _UPDATABLE_FIELDS

    def test_cik_excluded(self) -> None:
        assert "cik" not in _UPDATABLE_FIELDS

    def test_filing_type_excluded(self) -> None:
        assert "filing_type" not in _UPDATABLE_FIELDS

    def test_status_included(self) -> None:
        assert "status" in _UPDATABLE_FIELDS

    def test_company_id_included(self) -> None:
        assert "company_id" in _UPDATABLE_FIELDS

    def test_document_url_included(self) -> None:
        assert "document_url" in _UPDATABLE_FIELDS


# ===========================================================================
# FilingRepository — create
# ===========================================================================


class TestFilingRepositoryCreate:
    @pytest.mark.anyio
    async def test_create_adds_and_flushes(self) -> None:
        session = _make_session()
        repo = FilingRepository(session)
        schema = _make_create_schema()

        filing = await repo.create(schema)

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        added = session.add.call_args[0][0]
        assert isinstance(added, Filing)
        assert filing is added

    @pytest.mark.anyio
    async def test_create_sets_required_fields(self) -> None:
        session = _make_session()
        repo = FilingRepository(session)
        schema = _make_create_schema()

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.filing_type == "10-K"
        assert added.accession_number == _ACCESSION
        assert added.cik == _CIK
        assert added.filing_date == _TODAY

    @pytest.mark.anyio
    async def test_create_defaults_status_to_discovered(self) -> None:
        session = _make_session()
        repo = FilingRepository(session)
        schema = _make_create_schema()

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.status == FilingStatus.DISCOVERED.value

    @pytest.mark.anyio
    async def test_create_sets_optional_fields(self) -> None:
        session = _make_session()
        repo = FilingRepository(session)
        company_id = uuid.uuid4()
        schema = FilingCreate(
            filing_type="10-Q",
            accession_number="0000320193-23-000088",
            filing_date=_TODAY,
            cik="320193",
            ticker="AAPL",
            title="Quarterly report [10-Q]",
            company_id=company_id,
            filing_metadata={"form_type": "10-Q"},
        )

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.ticker == "AAPL"
        assert added.title == "Quarterly report [10-Q]"
        assert added.company_id == company_id
        assert added.filing_metadata == {"form_type": "10-Q"}

    @pytest.mark.anyio
    async def test_create_optional_fields_default_none(self) -> None:
        session = _make_session()
        repo = FilingRepository(session)
        schema = _make_create_schema()

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.company_id is None
        assert added.source_config_id is None
        assert added.ticker is None
        assert added.title is None
        assert added.filing_url is None
        assert added.document_url is None
        assert added.filing_metadata is None


# ===========================================================================
# FilingRepository — get_by_id
# ===========================================================================


class TestFilingRepositoryGetById:
    @pytest.mark.anyio
    async def test_returns_filing_when_found(self) -> None:
        mock_filing = _make_filing()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_filing)

        result = await FilingRepository(session).get_by_id(mock_filing.id)

        assert result is mock_filing

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        result = await FilingRepository(session).get_by_id(uuid.uuid4())

        assert result is None

    @pytest.mark.anyio
    async def test_execute_called_once(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        await FilingRepository(session).get_by_id(uuid.uuid4())
        assert session.execute.await_count == 1


# ===========================================================================
# FilingRepository — get_by_accession_number
# ===========================================================================


class TestFilingRepositoryGetByAccessionNumber:
    @pytest.mark.anyio
    async def test_returns_filing_when_found(self) -> None:
        mock_filing = _make_filing()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_filing)

        result = await FilingRepository(session).get_by_accession_number(_ACCESSION)

        assert result is mock_filing

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        result = await FilingRepository(session).get_by_accession_number("0000000000-00-000000")

        assert result is None


# ===========================================================================
# FilingRepository — exists_accession_number
# ===========================================================================


class TestFilingRepositoryExistsAccessionNumber:
    @pytest.mark.anyio
    async def test_returns_true_when_exists(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one(1)

        result = await FilingRepository(session).exists_accession_number(_ACCESSION)

        assert result is True

    @pytest.mark.anyio
    async def test_returns_false_when_not_exists(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one(0)

        result = await FilingRepository(session).exists_accession_number(_ACCESSION)

        assert result is False

    @pytest.mark.anyio
    async def test_execute_called_once(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one(0)
        await FilingRepository(session).exists_accession_number(_ACCESSION)
        assert session.execute.await_count == 1


# ===========================================================================
# FilingRepository — list
# ===========================================================================


class TestFilingRepositoryList:
    @pytest.mark.anyio
    async def test_returns_items_and_total(self) -> None:
        filings = [_make_filing(), _make_filing(accession_number="0000320193-23-000088")]

        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        data_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = filings
        data_result.scalars.return_value = scalars

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        items, total = await FilingRepository(session).list()

        assert total == 2
        assert len(items) == 2
        assert session.execute.await_count == 2

    @pytest.mark.anyio
    async def test_empty_result(self) -> None:
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        data_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = []
        data_result.scalars.return_value = scalars

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        items, total = await FilingRepository(session).list()

        assert total == 0
        assert items == []

    @pytest.mark.anyio
    async def test_two_queries_executed(self) -> None:
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        await FilingRepository(session).list(filing_type="10-K", status="discovered")

        assert session.execute.await_count == 2


# ===========================================================================
# FilingRepository — list_by_company
# ===========================================================================


class TestFilingRepositoryListByCompany:
    @pytest.mark.anyio
    async def test_delegates_to_list(self) -> None:
        company_id = uuid.uuid4()
        filing = _make_filing(company_id=company_id)

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[filing]))

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        items, total = await FilingRepository(session).list_by_company(company_id)

        assert total == 1
        assert items[0] is filing


# ===========================================================================
# FilingRepository — list_by_filing_type
# ===========================================================================


class TestFilingRepositoryListByFilingType:
    @pytest.mark.anyio
    async def test_delegates_to_list(self) -> None:
        filing = _make_filing(filing_type="10-K")

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[filing]))

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        items, total = await FilingRepository(session).list_by_filing_type("10-K")

        assert total == 1
        assert items[0] is filing


# ===========================================================================
# FilingRepository — update
# ===========================================================================


class TestFilingRepositoryUpdate:
    @pytest.mark.anyio
    async def test_returns_none_when_filing_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        schema = FilingUpdate(status="downloading")
        result = await FilingRepository(session).update(uuid.uuid4(), schema)

        assert result is None

    @pytest.mark.anyio
    async def test_updates_status_field(self) -> None:
        filing = _make_filing(status=FilingStatus.DISCOVERED)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(filing)

        schema = FilingUpdate(status="downloading")
        result = await FilingRepository(session).update(filing.id, schema)

        assert result is filing
        assert filing.status == "downloading"
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_updates_document_url(self) -> None:
        filing = _make_filing()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(filing)

        url = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/aapl-20230930.htm"
        schema = FilingUpdate(document_url=url)
        await FilingRepository(session).update(filing.id, schema)

        assert filing.document_url == url

    @pytest.mark.anyio
    async def test_no_flush_when_no_fields_changed(self) -> None:
        filing = _make_filing(status=FilingStatus.DISCOVERED)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(filing)

        # Same value as existing — no change
        schema = FilingUpdate(status=FilingStatus.DISCOVERED.value)
        await FilingRepository(session).update(filing.id, schema)

        session.flush.assert_not_awaited()

    @pytest.mark.anyio
    async def test_accession_number_not_updatable(self) -> None:
        # accession_number is not in FilingUpdate at all (schema omits it),
        # so model_fields_set never contains it.
        assert "accession_number" not in _UPDATABLE_FIELDS


# ===========================================================================
# FilingRepository — delete
# ===========================================================================


class TestFilingRepositoryDelete:
    @pytest.mark.anyio
    async def test_returns_true_when_deleted(self) -> None:
        filing = _make_filing()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(filing)

        result = await FilingRepository(session).delete(filing.id)

        assert result is True
        session.delete.assert_awaited_once_with(filing)
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_returns_false_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        result = await FilingRepository(session).delete(uuid.uuid4())

        assert result is False
        session.delete.assert_not_awaited()
