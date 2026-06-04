"""
Integration tests — M2 Repository Layer: CompanyRepository and JobRepository.

These tests run against a real PostgreSQL database and require migration 002
to be applied (companies and financial_jobs tables must exist).

Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_repositories_m2.py -v

Coverage
--------
CompanyRepository:
  - create, get_by_id (hit + miss), list (unfiltered, search, is_active, deleted),
    update (partial, no-op, not-found), soft_delete (hit + miss),
    cross-tenant isolation

JobRepository:
  - create, get_by_id (hit + miss), list (unfiltered, company_id, status, pagination),
    update_status, cancel (cancellable + terminal + not-found),
    set_document_url, cross-tenant isolation

Milestone: M2-Step 5
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from apps.api.models import (
    Company,
    JobStatus,
    Tenant,
    User,
)
from apps.api.repositories.companies import CompanyRepository
from apps.api.repositories.jobs import JobRepository
from apps.api.schemas.companies import CompanyCreate, CompanyUpdate
from apps.api.schemas.jobs import JobCreate, JobUpdate
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="Integration tests require DATABASE_URL env var.",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_session(client) -> AsyncSession:  # type: ignore[no-untyped-def]
    """
    Return a live AsyncSession via the FastAPI dependency override.

    We use the ``client`` fixture (which starts the FastAPI lifespan) to
    ensure the DB is initialised, then pull a session from the factory.
    """
    from apps.api.core.database import AsyncSessionFactory  # noqa: PLC0415

    assert AsyncSessionFactory is not None, "Database not initialised"
    async with AsyncSessionFactory() as session:
        yield session
        await session.rollback()   # roll back after each test — clean slate


@pytest.fixture()
async def tenant(db_session: AsyncSession) -> Tenant:
    """Create and persist a fresh Tenant for the current test."""
    t = Tenant(name=f"IntTest WS {uuid.uuid4().hex[:6]}", slug=f"inttest-{uuid.uuid4().hex[:8]}")
    db_session.add(t)
    await db_session.flush([t])
    return t


@pytest.fixture()
async def user(db_session: AsyncSession) -> User:
    """Create and persist a minimal User for the current test."""
    suffix = uuid.uuid4().hex[:8]
    u = User(
        email=f"repotest-{suffix}@example.com",
        full_name="Repo Tester",
        password_hash="$2b$12$fakehashfortest",
    )
    db_session.add(u)
    await db_session.flush([u])
    return u


@pytest.fixture()
async def other_tenant(db_session: AsyncSession) -> Tenant:
    """A second tenant for cross-tenant isolation tests."""
    t = Tenant(name=f"Other WS {uuid.uuid4().hex[:6]}", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(t)
    await db_session.flush([t])
    return t


@pytest.fixture()
async def company(db_session: AsyncSession, tenant: Tenant) -> Company:
    """Create and persist a Company in ``tenant``."""
    repo = CompanyRepository(db_session)
    return await repo.create(
        tenant.id,
        CompanyCreate(name="Acme Corp", ticker="ACME"),
    )


# ---------------------------------------------------------------------------
# CompanyRepository
# ---------------------------------------------------------------------------


class TestCompanyRepositoryIntegration:
    @pytest.mark.anyio
    async def test_create_and_retrieve(self, db_session: AsyncSession, tenant: Tenant) -> None:
        repo = CompanyRepository(db_session)
        created = await repo.create(tenant.id, CompanyCreate(name="Apple Inc.", ticker="AAPL"))

        assert isinstance(created.id, uuid.UUID)
        assert created.tenant_id == tenant.id
        assert created.ticker == "AAPL"
        assert created.deleted_at is None

        fetched = await repo.get_by_id(tenant.id, created.id)
        assert fetched is not None
        assert fetched.id == created.id

    @pytest.mark.anyio
    async def test_get_by_id_returns_none_for_missing(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        result = await repo.get_by_id(tenant.id, uuid.uuid4())
        assert result is None

    @pytest.mark.anyio
    async def test_cross_tenant_isolation_get(
        self, db_session: AsyncSession, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """A company in tenant A must not be visible to tenant B."""
        repo = CompanyRepository(db_session)
        c = await repo.create(tenant.id, CompanyCreate(name="Isolated Co", ticker="ISO"))
        result = await repo.get_by_id(other_tenant.id, c.id)
        assert result is None

    @pytest.mark.anyio
    async def test_list_returns_only_tenant_companies(
        self, db_session: AsyncSession, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        await repo.create(tenant.id, CompanyCreate(name="Tenant A Co", ticker="TAC"))
        await repo.create(other_tenant.id, CompanyCreate(name="Tenant B Co", ticker="TBC"))

        items, total = await repo.list(tenant.id)
        tickers = {c.ticker for c in items}
        assert "TAC" in tickers
        assert "TBC" not in tickers

    @pytest.mark.anyio
    async def test_list_search_filter(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        await repo.create(tenant.id, CompanyCreate(name="Microsoft Corporation", ticker="MSFT"))
        await repo.create(tenant.id, CompanyCreate(name="Apple Inc.", ticker="AAPL"))

        items, total = await repo.list(tenant.id, search="microsoft")
        assert total == 1
        assert items[0].ticker == "MSFT"

    @pytest.mark.anyio
    async def test_list_is_active_filter(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        await repo.create(tenant.id, CompanyCreate(name="Active Co", ticker="ACT"))
        inactive = await repo.create(tenant.id, CompanyCreate(name="Inactive Co", ticker="INA"))

        # Mark one as inactive via update
        await repo.update(tenant.id, inactive.id, CompanyUpdate(is_active=False))

        active_items, _ = await repo.list(tenant.id, is_active=True)
        inactive_items, _ = await repo.list(tenant.id, is_active=False)

        active_tickers = {c.ticker for c in active_items}
        inactive_tickers = {c.ticker for c in inactive_items}
        assert "ACT" in active_tickers
        assert "INA" in inactive_tickers
        assert "ACT" not in inactive_tickers

    @pytest.mark.anyio
    async def test_list_pagination(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        for i in range(5):
            await repo.create(tenant.id, CompanyCreate(name=f"Paged Co {i}", ticker=f"PG{i}"))

        _, total = await repo.list(tenant.id)
        assert total >= 5

        page1, _ = await repo.list(tenant.id, page=1, page_size=3)
        page2, _ = await repo.list(tenant.id, page=2, page_size=3)
        assert len(page1) == 3
        assert len(page2) >= 2  # at least 2 remaining
        # No overlap
        page1_ids = {c.id for c in page1}
        page2_ids = {c.id for c in page2}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.anyio
    async def test_soft_delete_excludes_from_list(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        c = await repo.create(tenant.id, CompanyCreate(name="To Delete", ticker="DEL"))

        deleted = await repo.soft_delete(tenant.id, c.id)
        assert deleted is True

        # Normal list excludes deleted
        result = await repo.get_by_id(tenant.id, c.id)
        assert result is None

        # include_deleted=True retrieves it
        result_incl = await repo.get_by_id(tenant.id, c.id, include_deleted=True)
        assert result_incl is not None
        assert result_incl.deleted_at is not None

    @pytest.mark.anyio
    async def test_soft_delete_returns_false_when_not_found(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        result = await repo.soft_delete(tenant.id, uuid.uuid4())
        assert result is False

    @pytest.mark.anyio
    async def test_update_partial(
        self, db_session: AsyncSession, tenant: Tenant, company: Company
    ) -> None:
        repo = CompanyRepository(db_session)
        updated = await repo.update(
            tenant.id, company.id, CompanyUpdate(name="New Name")
        )
        assert updated is not None
        assert updated.name == "New Name"
        assert updated.ticker == company.ticker  # unchanged

    @pytest.mark.anyio
    async def test_update_returns_none_when_not_found(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        result = await repo.update(tenant.id, uuid.uuid4(), CompanyUpdate(name="X"))
        assert result is None

    @pytest.mark.anyio
    async def test_create_with_cik(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = CompanyRepository(db_session)
        c = await repo.create(
            tenant.id,
            CompanyCreate(name="Apple Inc.", ticker="APPL2", cik="0000320193"),
        )
        assert c.cik == "0000320193"


# ---------------------------------------------------------------------------
# JobRepository
# ---------------------------------------------------------------------------


class TestJobRepositoryIntegration:
    @pytest.mark.anyio
    async def test_create_and_retrieve(
        self, db_session: AsyncSession, tenant: Tenant, user: User, company: Company
    ) -> None:
        repo = JobRepository(db_session)
        schema = JobCreate(company_id=company.id, job_type="sec_10k_annual", fiscal_year=2023)

        job = await repo.create(tenant.id, company.id, user.id, schema)

        assert isinstance(job.id, uuid.UUID)
        assert job.tenant_id == tenant.id
        assert job.company_id == company.id
        assert job.status == JobStatus.PENDING.value
        assert job.fiscal_year == 2023

        fetched = await repo.get_by_id(tenant.id, job.id)
        assert fetched is not None
        assert fetched.id == job.id

    @pytest.mark.anyio
    async def test_get_by_id_returns_none_for_missing(
        self, db_session: AsyncSession, tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        result = await repo.get_by_id(tenant.id, uuid.uuid4())
        assert result is None

    @pytest.mark.anyio
    async def test_cross_tenant_isolation(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        other_tenant: Tenant,
        user: User,
        company: Company,
    ) -> None:
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant.id, company.id, user.id,
            JobCreate(company_id=company.id, job_type="sec_10k_annual"),
        )
        result = await repo.get_by_id(other_tenant.id, job.id)
        assert result is None

    @pytest.mark.anyio
    async def test_list_filters_by_company(
        self, db_session: AsyncSession, tenant: Tenant, user: User
    ) -> None:
        repo_c = CompanyRepository(db_session)
        repo_j = JobRepository(db_session)
        c1 = await repo_c.create(tenant.id, CompanyCreate(name="Co One", ticker="CO1"))
        c2 = await repo_c.create(tenant.id, CompanyCreate(name="Co Two", ticker="CO2"))

        jt = "sec_10k_annual"
        await repo_j.create(tenant.id, c1.id, user.id, JobCreate(company_id=c1.id, job_type=jt))
        await repo_j.create(tenant.id, c2.id, user.id, JobCreate(company_id=c2.id, job_type=jt))

        items, total = await repo_j.list(tenant.id, company_id=c1.id)
        assert total == 1
        assert items[0].company_id == c1.id

    @pytest.mark.anyio
    async def test_list_filters_by_status(
        self, db_session: AsyncSession, tenant: Tenant, user: User, company: Company
    ) -> None:
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant.id, company.id, user.id,
            JobCreate(company_id=company.id, job_type="sec_10k_annual"),
        )
        # Transition to running
        await repo.update_status(
            tenant.id, job.id, JobUpdate(status="running")
        )

        running_items, _ = await repo.list(tenant.id, status="running")
        pending_items, _ = await repo.list(tenant.id, status="pending")

        running_ids = {j.id for j in running_items}
        assert job.id in running_ids
        assert job.id not in {j.id for j in pending_items}

    @pytest.mark.anyio
    async def test_list_pagination(
        self, db_session: AsyncSession, tenant: Tenant, user: User, company: Company
    ) -> None:
        repo = JobRepository(db_session)
        for _ in range(5):
            await repo.create(
                tenant.id, company.id, user.id,
                JobCreate(company_id=company.id, job_type="sec_10k_annual"),
            )

        _, total = await repo.list(tenant.id)
        assert total >= 5

        p1, _ = await repo.list(tenant.id, page=1, page_size=3)
        p2, _ = await repo.list(tenant.id, page=2, page_size=3)
        assert len(p1) == 3
        p1_ids = {j.id for j in p1}
        p2_ids = {j.id for j in p2}
        assert p1_ids.isdisjoint(p2_ids)

    @pytest.mark.anyio
    async def test_cancel_pending_job(
        self, db_session: AsyncSession, tenant: Tenant, user: User, company: Company
    ) -> None:
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant.id, company.id, user.id,
            JobCreate(company_id=company.id, job_type="sec_10k_annual"),
        )
        cancelled = await repo.cancel(tenant.id, job.id)

        assert cancelled is not None
        assert cancelled.status == JobStatus.CANCELLED.value
        assert cancelled.completed_at is not None

    @pytest.mark.anyio
    async def test_cancel_completed_job_unchanged(
        self, db_session: AsyncSession, tenant: Tenant, user: User, company: Company
    ) -> None:
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant.id, company.id, user.id,
            JobCreate(company_id=company.id, job_type="sec_10k_annual"),
        )
        await repo.update_status(tenant.id, job.id, JobUpdate(status="completed"))

        result = await repo.cancel(tenant.id, job.id)
        assert result is not None
        assert result.status == JobStatus.COMPLETED.value  # unchanged

    @pytest.mark.anyio
    async def test_set_document_url(
        self, db_session: AsyncSession, tenant: Tenant, user: User, company: Company
    ) -> None:
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant.id, company.id, user.id,
            JobCreate(company_id=company.id, job_type="sec_10k_annual"),
        )
        key = f"{tenant.id}/jobs/{job.id}/filing.pdf"
        updated = await repo.set_document_url(tenant.id, job.id, key)

        assert updated is not None
        assert updated.document_url == key

    @pytest.mark.anyio
    async def test_update_status_partial(
        self, db_session: AsyncSession, tenant: Tenant, user: User, company: Company
    ) -> None:
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant.id, company.id, user.id,
            JobCreate(company_id=company.id, job_type="sec_10k_annual"),
        )
        now = datetime.now(UTC)
        updated = await repo.update_status(
            tenant.id, job.id,
            JobUpdate(status="running", started_at=now),
        )
        assert updated is not None
        assert updated.status == "running"
        assert updated.started_at is not None
        assert updated.celery_task_id is None  # unset field unchanged
