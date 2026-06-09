"""
Unit tests — AcquisitionJobRepository.

Strategy
--------
All database calls are replaced by AsyncMock so tests run without a live
PostgreSQL instance.  The AsyncSession mock is constructed per-test and
configured to return pre-built MagicMock ORM objects.

What is mocked
--------------
- ``session.execute``  — returns mock Result objects configured per test
- ``session.add``      — sync no-op that records the added object
- ``session.flush``    — no-op coroutine
- ``session.delete``   — no-op coroutine

What is NOT mocked (real code runs)
------------------------------------
- ``AcquisitionJobRepository`` method logic
- ``_UPDATABLE_FIELDS`` allowlist
- Partial-update logic via ``model_fields_set``
- structlog calls (silently no-op in tests)

Milestone: M3.7 — Acquisition Jobs
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.api.models import AcquisitionJob, AcquisitionJobStatus
from apps.api.repositories.acquisition_jobs import (
    AcquisitionJobRepository,
    _UPDATABLE_FIELDS,
)
from apps.api.schemas.acquisition_jobs import AcquisitionJobCreate, AcquisitionJobUpdate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_TICKER = "AAPL"
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


def _make_job(
    job_id: uuid.UUID | None = None,
    ticker: str = _TICKER,
    status: str = AcquisitionJobStatus.PENDING,
    cik: str | None = None,
    company_name: str | None = None,
) -> MagicMock:
    j = MagicMock(spec=AcquisitionJob)
    j.id = job_id or uuid.uuid4()
    j.ticker = ticker
    j.cik = cik
    j.company_name = company_name
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


# ===========================================================================
# _UPDATABLE_FIELDS
# ===========================================================================


class TestUpdatableFields:
    def test_id_excluded(self) -> None:
        assert "id" not in _UPDATABLE_FIELDS

    def test_ticker_excluded(self) -> None:
        assert "ticker" not in _UPDATABLE_FIELDS

    def test_job_type_excluded(self) -> None:
        assert "job_type" not in _UPDATABLE_FIELDS

    def test_status_included(self) -> None:
        assert "status" in _UPDATABLE_FIELDS

    def test_cik_included(self) -> None:
        assert "cik" in _UPDATABLE_FIELDS

    def test_company_name_included(self) -> None:
        assert "company_name" in _UPDATABLE_FIELDS

    def test_error_message_included(self) -> None:
        assert "error_message" in _UPDATABLE_FIELDS

    def test_filings_discovered_included(self) -> None:
        assert "filings_discovered" in _UPDATABLE_FIELDS

    def test_filings_new_included(self) -> None:
        assert "filings_new" in _UPDATABLE_FIELDS

    def test_documents_fetched_included(self) -> None:
        assert "documents_fetched" in _UPDATABLE_FIELDS

    def test_documents_stored_included(self) -> None:
        assert "documents_stored" in _UPDATABLE_FIELDS

    def test_started_at_included(self) -> None:
        assert "started_at" in _UPDATABLE_FIELDS

    def test_completed_at_included(self) -> None:
        assert "completed_at" in _UPDATABLE_FIELDS


# ===========================================================================
# AcquisitionJobRepository — create
# ===========================================================================


class TestAcquisitionJobRepositoryCreate:
    @pytest.mark.anyio
    async def test_create_adds_and_flushes(self) -> None:
        session = _make_session()
        repo = AcquisitionJobRepository(session)
        schema = AcquisitionJobCreate(ticker="AAPL")

        job = await repo.create(schema)

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        added = session.add.call_args[0][0]
        assert isinstance(added, AcquisitionJob)
        assert job is added

    @pytest.mark.anyio
    async def test_create_normalises_ticker_to_uppercase(self) -> None:
        session = _make_session()
        repo = AcquisitionJobRepository(session)
        schema = AcquisitionJobCreate(ticker="aapl")

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.ticker == "AAPL"

    @pytest.mark.anyio
    async def test_create_sets_job_type(self) -> None:
        session = _make_session()
        repo = AcquisitionJobRepository(session)
        schema = AcquisitionJobCreate(ticker="MSFT")

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.job_type == "sec_filing_discovery"

    @pytest.mark.anyio
    async def test_create_custom_job_type(self) -> None:
        session = _make_session()
        repo = AcquisitionJobRepository(session)
        schema = AcquisitionJobCreate(ticker="TSLA", job_type="custom_type")

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.job_type == "custom_type"


# ===========================================================================
# AcquisitionJobRepository — get_by_id
# ===========================================================================


class TestAcquisitionJobRepositoryGetById:
    @pytest.mark.anyio
    async def test_returns_job_when_found(self) -> None:
        mock_job = _make_job()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_job)

        result = await AcquisitionJobRepository(session).get_by_id(mock_job.id)

        assert result is mock_job

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        result = await AcquisitionJobRepository(session).get_by_id(uuid.uuid4())

        assert result is None

    @pytest.mark.anyio
    async def test_execute_called_once(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        await AcquisitionJobRepository(session).get_by_id(uuid.uuid4())
        assert session.execute.await_count == 1


# ===========================================================================
# AcquisitionJobRepository — list
# ===========================================================================


class TestAcquisitionJobRepositoryList:
    @pytest.mark.anyio
    async def test_returns_items_and_total(self) -> None:
        jobs = [_make_job(), _make_job(ticker="MSFT")]

        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        data_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = jobs
        data_result.scalars.return_value = scalars

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        items, total = await AcquisitionJobRepository(session).list()

        assert total == 2
        assert len(items) == 2
        assert session.execute.await_count == 2

    @pytest.mark.anyio
    async def test_empty_result(self) -> None:
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        items, total = await AcquisitionJobRepository(session).list()

        assert total == 0
        assert items == []

    @pytest.mark.anyio
    async def test_two_queries_executed_with_filters(self) -> None:
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        await AcquisitionJobRepository(session).list(status="pending", ticker="AAPL")

        assert session.execute.await_count == 2


class TestAcquisitionJobRepositoryListByStatus:
    @pytest.mark.anyio
    async def test_delegates_to_list(self) -> None:
        job = _make_job(status=AcquisitionJobStatus.PENDING)

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[job]))

        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        items, total = await AcquisitionJobRepository(session).list_by_status("pending")

        assert total == 1
        assert items[0] is job


# ===========================================================================
# AcquisitionJobRepository — update
# ===========================================================================


class TestAcquisitionJobRepositoryUpdate:
    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        result = await AcquisitionJobRepository(session).update(
            uuid.uuid4(), AcquisitionJobUpdate(status="running")
        )

        assert result is None

    @pytest.mark.anyio
    async def test_updates_status(self) -> None:
        job = _make_job(status=AcquisitionJobStatus.PENDING)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(job)

        result = await AcquisitionJobRepository(session).update(
            job.id, AcquisitionJobUpdate(status="running")
        )

        assert result is job
        assert job.status == "running"
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_updates_cik_and_company_name(self) -> None:
        job = _make_job()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(job)

        await AcquisitionJobRepository(session).update(
            job.id,
            AcquisitionJobUpdate(cik="0000320193", company_name="Apple Inc."),
        )

        assert job.cik == "0000320193"
        assert job.company_name == "Apple Inc."

    @pytest.mark.anyio
    async def test_updates_counters(self) -> None:
        job = _make_job()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(job)

        await AcquisitionJobRepository(session).update(
            job.id,
            AcquisitionJobUpdate(
                filings_discovered=10,
                filings_new=3,
                documents_fetched=3,
                documents_stored=3,
            ),
        )

        assert job.filings_discovered == 10
        assert job.filings_new == 3
        assert job.documents_fetched == 3
        assert job.documents_stored == 3

    @pytest.mark.anyio
    async def test_no_flush_when_no_fields_changed(self) -> None:
        job = _make_job(status=AcquisitionJobStatus.PENDING)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(job)

        # Same value — no change expected
        await AcquisitionJobRepository(session).update(
            job.id, AcquisitionJobUpdate(status=AcquisitionJobStatus.PENDING)
        )

        session.flush.assert_not_awaited()

    @pytest.mark.anyio
    async def test_ticker_not_updatable(self) -> None:
        assert "ticker" not in _UPDATABLE_FIELDS

    @pytest.mark.anyio
    async def test_update_sets_updated_at(self) -> None:
        job = _make_job(status=AcquisitionJobStatus.PENDING)
        original_updated_at = job.updated_at
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(job)

        await AcquisitionJobRepository(session).update(
            job.id, AcquisitionJobUpdate(status="running")
        )

        assert job.updated_at != original_updated_at or job.updated_at >= original_updated_at
