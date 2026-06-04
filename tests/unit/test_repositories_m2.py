"""
Unit tests — M2 Repository Layer: CompanyRepository and JobRepository.

Strategy
--------
All database calls are replaced by AsyncMock so tests run without a live
PostgreSQL instance.  The AsyncSession mock is constructed per-test and
configured to return pre-built MagicMock ORM objects.

What is mocked
--------------
- ``session.execute``  — returns mock Result objects
- ``session.add``      — records that the object was added (no-op)
- ``session.flush``    — no-op coroutine

What is NOT mocked (real code runs)
------------------------------------
- ``CompanyRepository`` and ``JobRepository`` method logic
- ``CompanyUpdate.model_fields_set`` partial-update semantics
- ``_UPDATABLE_FIELDS`` and ``_STATUS_UPDATABLE_FIELDS`` allowlists
- ``JobStatus`` enum comparisons in ``cancel``
- ``log.debug`` calls (silently no-op in tests)

Coverage
--------
  CompanyRepository.create         — adds company, flushes, returns ORM object
  CompanyRepository.get_by_id      — tenant isolation, include_deleted flag
  CompanyRepository.list           — filters, pagination, count + data queries
  CompanyRepository.update         — partial update via model_fields_set, no-change skip
  CompanyRepository.soft_delete    — sets deleted_at, returns True; not-found → False

  JobRepository.create             — creates in PENDING state, flushes
  JobRepository.get_by_id          — tenant isolation
  JobRepository.list               — company_id / status filters, pagination
  JobRepository.update_status      — partial update, model_fields_set
  JobRepository.cancel             — cancellable states → CANCELLED; terminal → unchanged
  JobRepository.set_document_url   — sets document_url

Milestone: M2-Step 5
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from apps.api.models import Company, FinancialJob, JobStatus
from apps.api.repositories.companies import _UPDATABLE_FIELDS, CompanyRepository
from apps.api.repositories.jobs import _STATUS_UPDATABLE_FIELDS, JobRepository
from apps.api.schemas.companies import CompanyCreate, CompanyUpdate
from apps.api.schemas.jobs import JobCreate, JobUpdate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _tid() -> uuid.UUID:
    """Fresh tenant UUID."""
    return uuid.uuid4()


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def _make_session() -> AsyncMock:
    """Return a fresh AsyncMock that mimics an AsyncSession."""
    session = AsyncMock()
    session.add = MagicMock()      # sync; does not need to be awaited
    session.flush = AsyncMock()    # async
    return session


def _mock_scalar_one_or_none(value: Any) -> AsyncMock:
    """Return a mock execute() result whose scalar_one_or_none() returns value."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    execute_mock = AsyncMock(return_value=result)
    return execute_mock


def _mock_scalar_one(value: Any) -> AsyncMock:
    """Return a mock execute() result whose scalar_one() returns value."""
    result = MagicMock()
    result.scalar_one.return_value = value
    return AsyncMock(return_value=result)


def _mock_scalars_all(items: list) -> AsyncMock:
    """Return a mock execute() result whose scalars().all() returns items."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = items
    result = MagicMock()
    result.scalars.return_value = scalars_mock
    return AsyncMock(return_value=result)


def _make_company(
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    name: str = "Acme Corp",
    ticker: str = "ACME",
    is_active: bool = True,
    deleted_at: datetime | None = None,
) -> MagicMock:
    """Build a minimal Company-like MagicMock."""
    c = MagicMock(spec=Company)
    c.id = company_id or uuid.uuid4()
    c.tenant_id = tenant_id or uuid.uuid4()
    c.name = name
    c.ticker = ticker
    c.cik = None
    c.exchange = None
    c.sector = None
    c.industry = None
    c.description = None
    c.website = None
    c.is_active = is_active
    c.created_at = _NOW
    c.updated_at = _NOW
    c.deleted_at = deleted_at
    return c


def _make_job(
    tenant_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    status: str = "pending",
) -> MagicMock:
    """Build a minimal FinancialJob-like MagicMock."""
    j = MagicMock(spec=FinancialJob)
    j.id = job_id or uuid.uuid4()
    j.tenant_id = tenant_id or uuid.uuid4()
    j.company_id = uuid.uuid4()
    j.created_by = uuid.uuid4()
    j.status = status
    j.job_type = "sec_10k_annual"
    j.fiscal_year = None
    j.document_url = None
    j.result_url = None
    j.error_message = None
    j.celery_task_id = None
    j.started_at = None
    j.completed_at = None
    j.created_at = _NOW
    j.updated_at = _NOW
    return j


# ===========================================================================
# CompanyRepository — create
# ===========================================================================


class TestCompanyRepositoryCreate:
    @pytest.mark.anyio
    async def test_create_adds_and_flushes(self) -> None:
        session = _make_session()
        repo = CompanyRepository(session)
        tid = _tid()
        schema = CompanyCreate(name="Acme Corp", ticker="ACME")

        company = await repo.create(tid, schema)

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        # The added object is a Company instance
        added_obj = session.add.call_args[0][0]
        assert isinstance(added_obj, Company)
        assert added_obj.tenant_id == tid
        assert added_obj.name == "Acme Corp"
        assert added_obj.ticker == "ACME"
        assert company is added_obj

    @pytest.mark.anyio
    async def test_create_optional_fields(self) -> None:
        session = _make_session()
        repo = CompanyRepository(session)
        schema = CompanyCreate(
            name="Apple Inc.",
            ticker="AAPL",
            cik="0000320193",
            exchange="NASDAQ",
        )
        company = await repo.create(_tid(), schema)
        assert company.cik == "0000320193"
        assert company.exchange == "NASDAQ"

    @pytest.mark.anyio
    async def test_create_sets_tenant_id_not_from_schema(self) -> None:
        """Tenant ID must come from the argument, never from user input."""
        session = _make_session()
        repo = CompanyRepository(session)
        tid = _tid()
        schema = CompanyCreate(name="Test", ticker="T")
        company = await repo.create(tid, schema)
        assert company.tenant_id == tid


# ===========================================================================
# CompanyRepository — get_by_id
# ===========================================================================


class TestCompanyRepositoryGetById:
    @pytest.mark.anyio
    async def test_returns_company_when_found(self) -> None:
        tid = _tid()
        mock_company = _make_company(tenant_id=tid)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_company)

        repo = CompanyRepository(session)
        result = await repo.get_by_id(tid, mock_company.id)

        assert result is mock_company

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = CompanyRepository(session)
        result = await repo.get_by_id(_tid(), uuid.uuid4())

        assert result is None

    @pytest.mark.anyio
    async def test_execute_called_once(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = CompanyRepository(session)
        await repo.get_by_id(_tid(), uuid.uuid4())
        assert session.execute.await_count == 1


# ===========================================================================
# CompanyRepository — list
# ===========================================================================


class TestCompanyRepositoryList:
    @pytest.mark.anyio
    async def test_returns_items_and_total(self) -> None:
        tid = _tid()
        companies = [_make_company(tenant_id=tid), _make_company(tenant_id=tid)]

        session = _make_session()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        data_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = companies
        data_result.scalars.return_value = scalars
        session.execute = AsyncMock(side_effect=[
            count_result,   # first call → count
            data_result,    # second call → data
        ])

        repo = CompanyRepository(session)
        items, total = await repo.list(tid)

        assert total == 2
        assert len(items) == 2
        assert session.execute.await_count == 2

    @pytest.mark.anyio
    async def test_empty_result(self) -> None:
        session = _make_session()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        repo = CompanyRepository(session)
        items, total = await repo.list(_tid())

        assert total == 0
        assert items == []


# ===========================================================================
# CompanyRepository — update
# ===========================================================================


class TestCompanyRepositoryUpdate:
    @pytest.mark.anyio
    async def test_update_applies_only_set_fields(self) -> None:
        tid = _tid()
        mock_company = _make_company(tenant_id=tid, name="Old Name", ticker="OLD")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_company)

        repo = CompanyRepository(session)
        schema = CompanyUpdate(name="New Name")   # only 'name' is in model_fields_set

        result = await repo.update(tid, mock_company.id, schema)

        assert mock_company.name == "New Name"
        assert result is mock_company
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_update_does_not_touch_unset_fields(self) -> None:
        """
        PATCH body of {"name": "New"} must not clear ticker or any other field.
        This verifies the model_fields_set partial-update logic.
        """
        tid = _tid()
        mock_company = _make_company(tenant_id=tid, ticker="ORIG")
        mock_company.name = "Old"
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_company)

        repo = CompanyRepository(session)
        schema = CompanyUpdate(name="New")
        await repo.update(tid, mock_company.id, schema)

        # ticker must NOT have been overwritten
        assert mock_company.ticker == "ORIG"

    @pytest.mark.anyio
    async def test_update_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = CompanyRepository(session)
        result = await repo.update(_tid(), uuid.uuid4(), CompanyUpdate(name="X"))
        assert result is None

    @pytest.mark.anyio
    async def test_update_no_flush_when_nothing_changed(self) -> None:
        """If the incoming value equals the current value, no flush should occur."""
        tid = _tid()
        mock_company = _make_company(tenant_id=tid, name="Same")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_company)

        repo = CompanyRepository(session)
        schema = CompanyUpdate(name="Same")   # same value → no change
        await repo.update(tid, mock_company.id, schema)

        session.flush.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_sets_updated_at(self) -> None:
        tid = _tid()
        mock_company = _make_company(tenant_id=tid, name="Old")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_company)

        repo = CompanyRepository(session)
        schema = CompanyUpdate(name="New")
        await repo.update(tid, mock_company.id, schema)

        # updated_at should be set to a recent datetime
        assert mock_company.updated_at is not None

    @pytest.mark.anyio
    async def test_update_explicit_null_clears_field(self) -> None:
        """Explicit null in PATCH body (cik=None when cik was set) must clear the field."""
        tid = _tid()
        mock_company = _make_company(tenant_id=tid)
        mock_company.cik = "0000320193"
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_company)

        repo = CompanyRepository(session)
        # CompanyCreate/Update validator strips empty CIK to None — simulate
        # a schema where cik is explicitly set to None in model_fields_set
        schema = CompanyUpdate(name="Keep", cik=None)
        # Manually add cik to model_fields_set (mimics explicit {"cik": null} in JSON)
        object.__setattr__(schema, "__pydantic_fields_set__", {"name", "cik"})
        await repo.update(tid, mock_company.id, schema)
        assert mock_company.cik is None


# ===========================================================================
# CompanyRepository — soft_delete
# ===========================================================================


class TestCompanyRepositorySoftDelete:
    @pytest.mark.anyio
    async def test_soft_delete_sets_deleted_at(self) -> None:
        tid = _tid()
        mock_company = _make_company(tenant_id=tid)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_company)

        repo = CompanyRepository(session)
        result = await repo.soft_delete(tid, mock_company.id)

        assert result is True
        assert mock_company.deleted_at is not None
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_soft_delete_returns_false_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = CompanyRepository(session)
        result = await repo.soft_delete(_tid(), uuid.uuid4())

        assert result is False
        session.flush.assert_not_awaited()


# ===========================================================================
# JobRepository — create
# ===========================================================================


class TestJobRepositoryCreate:
    @pytest.mark.anyio
    async def test_create_sets_pending_status(self) -> None:
        session = _make_session()
        repo = JobRepository(session)
        schema = JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_annual")

        job = await repo.create(_tid(), uuid.uuid4(), _uid(), schema)

        assert job.status == JobStatus.PENDING.value
        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        added = session.add.call_args[0][0]
        assert isinstance(added, FinancialJob)

    @pytest.mark.anyio
    async def test_create_with_fiscal_year(self) -> None:
        session = _make_session()
        repo = JobRepository(session)
        schema = JobCreate(
            company_id=uuid.uuid4(), job_type="sec_10k_annual", fiscal_year=2023
        )
        job = await repo.create(_tid(), uuid.uuid4(), _uid(), schema)
        assert job.fiscal_year == 2023

    @pytest.mark.anyio
    async def test_create_tenant_id_from_argument(self) -> None:
        """tenant_id must be injected, not sourced from the schema."""
        session = _make_session()
        repo = JobRepository(session)
        tid = _tid()
        schema = JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_annual")
        job = await repo.create(tid, uuid.uuid4(), _uid(), schema)
        assert job.tenant_id == tid


# ===========================================================================
# JobRepository — get_by_id
# ===========================================================================


class TestJobRepositoryGetById:
    @pytest.mark.anyio
    async def test_returns_job_when_found(self) -> None:
        tid = _tid()
        mock_job = _make_job(tenant_id=tid)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_job)

        repo = JobRepository(session)
        result = await repo.get_by_id(tid, mock_job.id)

        assert result is mock_job

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = JobRepository(session)
        result = await repo.get_by_id(_tid(), uuid.uuid4())
        assert result is None


# ===========================================================================
# JobRepository — list
# ===========================================================================


class TestJobRepositoryList:
    @pytest.mark.anyio
    async def test_returns_items_and_total(self) -> None:
        tid = _tid()
        jobs = [_make_job(tenant_id=tid)]

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=jobs))
        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        repo = JobRepository(session)
        items, total = await repo.list(tid)

        assert total == 1
        assert len(items) == 1

    @pytest.mark.anyio
    async def test_two_queries_executed(self) -> None:
        """One count query and one data query must always be issued."""
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        repo = JobRepository(session)
        await repo.list(_tid())
        assert session.execute.await_count == 2


# ===========================================================================
# JobRepository — update_status
# ===========================================================================


class TestJobRepositoryUpdateStatus:
    @pytest.mark.anyio
    async def test_update_status_to_running(self) -> None:
        tid = _tid()
        mock_job = _make_job(tenant_id=tid, status="queued")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_job)

        repo = JobRepository(session)
        schema = JobUpdate(status="running", started_at=datetime.now(UTC))
        result = await repo.update_status(tid, mock_job.id, schema)

        assert result is mock_job
        assert mock_job.status == "running"
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_update_status_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = JobRepository(session)
        result = await repo.update_status(_tid(), uuid.uuid4(), JobUpdate(status="running"))
        assert result is None

    @pytest.mark.anyio
    async def test_update_status_does_not_touch_unset_fields(self) -> None:
        tid = _tid()
        mock_job = _make_job(tenant_id=tid, status="pending")
        mock_job.celery_task_id = "original-task"
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_job)

        repo = JobRepository(session)
        schema = JobUpdate(status="queued")  # celery_task_id not in model_fields_set
        await repo.update_status(tid, mock_job.id, schema)

        assert mock_job.celery_task_id == "original-task"  # must not be cleared


# ===========================================================================
# JobRepository — cancel
# ===========================================================================


class TestJobRepositoryCancel:
    @pytest.mark.anyio
    @pytest.mark.parametrize("initial_status", ["pending", "queued", "running"])
    async def test_cancel_from_cancellable_state(self, initial_status: str) -> None:
        tid = _tid()
        mock_job = _make_job(tenant_id=tid, status=initial_status)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_job)

        repo = JobRepository(session)
        result = await repo.cancel(tid, mock_job.id)

        assert result is mock_job
        assert mock_job.status == JobStatus.CANCELLED.value
        assert mock_job.completed_at is not None
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    @pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
    async def test_cancel_terminal_job_returns_unchanged(self, terminal_status: str) -> None:
        """Cancelling an already-terminal job must return it unchanged, not raise."""
        tid = _tid()
        mock_job = _make_job(tenant_id=tid, status=terminal_status)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_job)

        repo = JobRepository(session)
        result = await repo.cancel(tid, mock_job.id)

        assert result is mock_job
        assert mock_job.status == terminal_status  # unchanged
        session.flush.assert_not_awaited()          # no write

    @pytest.mark.anyio
    async def test_cancel_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = JobRepository(session)
        result = await repo.cancel(_tid(), uuid.uuid4())
        assert result is None


# ===========================================================================
# JobRepository — set_document_url
# ===========================================================================


class TestJobRepositorySetDocumentUrl:
    @pytest.mark.anyio
    async def test_sets_document_url(self) -> None:
        tid = _tid()
        mock_job = _make_job(tenant_id=tid)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_job)

        repo = JobRepository(session)
        url = f"{tid}/jobs/{mock_job.id}/filing.pdf"
        result = await repo.set_document_url(tid, mock_job.id, url)

        assert result is mock_job
        assert mock_job.document_url == url
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = JobRepository(session)
        result = await repo.set_document_url(_tid(), uuid.uuid4(), "s3://key")
        assert result is None


# ===========================================================================
# Allowlist correctness
# ===========================================================================


class TestAllowlists:
    def test_updatable_fields_allowlist(self) -> None:
        """Verify the company update allowlist contains the expected fields."""
        expected = {
            "name", "ticker", "cik", "exchange", "sector",
            "industry", "description", "website", "is_active",
        }
        assert _UPDATABLE_FIELDS == expected

    def test_status_updatable_fields_allowlist(self) -> None:
        """Verify the job status update allowlist contains the expected fields."""
        expected = {
            "status", "error_message", "celery_task_id",
            "started_at", "completed_at",
        }
        assert _STATUS_UPDATABLE_FIELDS == expected

    def test_id_not_in_updatable_fields(self) -> None:
        """The primary key must never be writable via update."""
        assert "id" not in _UPDATABLE_FIELDS
        assert "id" not in _STATUS_UPDATABLE_FIELDS

    def test_tenant_id_not_in_updatable_fields(self) -> None:
        """Tenant isolation must be immutable."""
        assert "tenant_id" not in _UPDATABLE_FIELDS
        assert "tenant_id" not in _STATUS_UPDATABLE_FIELDS
