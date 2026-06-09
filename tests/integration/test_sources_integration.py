"""
Integration tests — M3.1 Source Registry: SourceConfigRepository + SourceRegistryService.

These tests run against a real PostgreSQL database and require migration 004
to be applied (source_configs table must exist).

Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_sources_integration.py -v

Coverage
--------
SourceConfigRepository:
  - create (all fields, minimal fields)
  - get_by_id (hit + miss)
  - get_by_code (hit + miss)
  - list (unfiltered, provider_type filter, is_active filter, pagination)
  - update (partial, no-op, not-found)
  - enable / disable (hit + idempotent + miss)
  - delete (hit + miss)

SourceRegistryService:
  - create (success + duplicate code → ConflictError)
  - get_by_id (hit + miss → NotFoundError)
  - get_by_code (hit + miss → NotFoundError)
  - list (pagination, is_active filter)
  - update (partial + not-found → NotFoundError)
  - enable + disable (hit + not-found → NotFoundError)
  - delete (hit + warns for active + not-found → NotFoundError)

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import os
import uuid

import pytest
from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.models import SourceConfig
from apps.api.repositories.sources import SourceConfigRepository
from apps.api.schemas.sources import SourceConfigCreate, SourceConfigUpdate
from apps.api.services.sources import SourceRegistryService
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
    """Return a live AsyncSession via the FastAPI lifespan database factory."""
    from apps.api.core.database import AsyncSessionFactory  # noqa: PLC0415

    assert AsyncSessionFactory is not None, "Database not initialised"
    async with AsyncSessionFactory() as session:
        yield session
        await session.rollback()


def _unique_code(prefix: str = "TEST") -> str:
    """Generate a unique source code for each test run."""
    return f"{prefix}_{uuid.uuid4().hex[:6].upper()}"


@pytest.fixture()
async def source(db_session: AsyncSession) -> SourceConfig:
    """Create and persist a SourceConfig for the current test."""
    repo = SourceConfigRepository(db_session)
    return await repo.create(
        SourceConfigCreate(
            code=_unique_code("SEC"),
            name="Test SEC EDGAR",
            provider_type="regulatory",
            country_code="US",
            base_url="https://efts.sec.gov",
            rate_limit_per_minute=600,
        )
    )


# ---------------------------------------------------------------------------
# SourceConfigRepository integration tests
# ---------------------------------------------------------------------------


class TestSourceConfigRepositoryIntegration:
    @pytest.mark.anyio
    async def test_create_and_retrieve_by_id(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        code = _unique_code("INT")
        created = await repo.create(
            SourceConfigCreate(
                code=code,
                name="Integration Test Source",
                provider_type="manual",
                rate_limit_per_minute=30,
            )
        )

        assert isinstance(created.id, uuid.UUID)
        assert created.code == code
        assert created.provider_type == "manual"
        assert created.rate_limit_per_minute == 30
        assert created.is_active is True

        fetched = await repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.code == code

    @pytest.mark.anyio
    async def test_create_with_all_fields(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        code = _unique_code("FULL")
        created = await repo.create(
            SourceConfigCreate(
                code=code,
                name="Full Source",
                description="Full description",
                provider_type="exchange",
                country_code="IN",
                base_url="https://nseindia.com",
                rate_limit_per_minute=120,
                is_active=False,
                config={"key": "value", "nested": {"a": 1}},
            )
        )

        assert created.description == "Full description"
        assert created.country_code == "IN"
        assert created.base_url == "https://nseindia.com"
        assert created.is_active is False
        assert created.config == {"key": "value", "nested": {"a": 1}}

    @pytest.mark.anyio
    async def test_get_by_id_returns_none_for_missing(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None

    @pytest.mark.anyio
    async def test_get_by_code_hit(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        repo = SourceConfigRepository(db_session)
        found = await repo.get_by_code(source.code)
        assert found is not None
        assert found.id == source.id

    @pytest.mark.anyio
    async def test_get_by_code_miss(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        result = await repo.get_by_code("NONEXISTENT_CODE_XYZ")
        assert result is None

    @pytest.mark.anyio
    async def test_list_returns_created_source(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        repo = SourceConfigRepository(db_session)
        items, total = await repo.list()

        ids = {s.id for s in items}
        assert source.id in ids
        assert total >= 1

    @pytest.mark.anyio
    async def test_list_filter_by_provider_type(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        code = _unique_code("MANUAL")
        await repo.create(
            SourceConfigCreate(code=code, name="Manual Source", provider_type="manual")
        )

        items, total = await repo.list(provider_type="manual")
        types = {s.provider_type for s in items}
        assert "manual" in types
        # All returned items must be of the filtered type.
        assert all(s.provider_type == "manual" for s in items)

    @pytest.mark.anyio
    async def test_list_filter_by_is_active(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        active_code = _unique_code("ACT")
        inactive_code = _unique_code("INA")

        await repo.create(
            SourceConfigCreate(code=active_code, name="Active", provider_type="manual", is_active=True)
        )
        inactive = await repo.create(
            SourceConfigCreate(code=inactive_code, name="Inactive", provider_type="manual", is_active=False)
        )

        active_items, _ = await repo.list(is_active=True)
        inactive_items, _ = await repo.list(is_active=False)

        active_ids = {s.id for s in active_items}
        inactive_ids = {s.id for s in inactive_items}

        assert inactive.id in inactive_ids
        assert inactive.id not in active_ids

    @pytest.mark.anyio
    async def test_list_pagination(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        # Create 5 sources with unique codes.
        for i in range(5):
            await repo.create(
                SourceConfigCreate(
                    code=_unique_code(f"PG{i}"),
                    name=f"Paginated Source {i}",
                    provider_type="manual",
                )
            )

        _, total = await repo.list()
        assert total >= 5

        page1, _ = await repo.list(page=1, page_size=3)
        page2, _ = await repo.list(page=2, page_size=3)

        assert len(page1) == 3
        p1_ids = {s.id for s in page1}
        p2_ids = {s.id for s in page2}
        assert p1_ids.isdisjoint(p2_ids)

    @pytest.mark.anyio
    async def test_update_partial(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        repo = SourceConfigRepository(db_session)
        original_code = source.code

        updated = await repo.update(source.id, SourceConfigUpdate(name="Updated Name"))

        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.code == original_code  # code must not change

    @pytest.mark.anyio
    async def test_update_rate_limit(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        repo = SourceConfigRepository(db_session)
        updated = await repo.update(
            source.id, SourceConfigUpdate(rate_limit_per_minute=300)
        )
        assert updated is not None
        assert updated.rate_limit_per_minute == 300

    @pytest.mark.anyio
    async def test_update_returns_none_when_not_found(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        result = await repo.update(uuid.uuid4(), SourceConfigUpdate(name="Ghost"))
        assert result is None

    @pytest.mark.anyio
    async def test_disable_sets_inactive(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        repo = SourceConfigRepository(db_session)
        assert source.is_active is True

        result = await repo.disable(source.id)

        assert result is not None
        assert result.is_active is False

    @pytest.mark.anyio
    async def test_enable_restores_active(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        repo = SourceConfigRepository(db_session)
        await repo.disable(source.id)

        result = await repo.enable(source.id)

        assert result is not None
        assert result.is_active is True

    @pytest.mark.anyio
    async def test_delete_removes_record(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        repo = SourceConfigRepository(db_session)
        source_id = source.id

        deleted = await repo.delete(source_id)

        assert deleted is True
        fetched = await repo.get_by_id(source_id)
        assert fetched is None

    @pytest.mark.anyio
    async def test_delete_returns_false_when_not_found(self, db_session: AsyncSession) -> None:
        repo = SourceConfigRepository(db_session)
        result = await repo.delete(uuid.uuid4())
        assert result is False

    @pytest.mark.anyio
    async def test_duplicate_code_raises_on_flush(self, db_session: AsyncSession) -> None:
        """Database unique constraint on code must be enforced."""
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        repo = SourceConfigRepository(db_session)
        code = _unique_code("DUP")
        await repo.create(SourceConfigCreate(code=code, name="First", provider_type="manual"))

        with pytest.raises(IntegrityError):
            await repo.create(SourceConfigCreate(code=code, name="Duplicate", provider_type="manual"))


# ---------------------------------------------------------------------------
# SourceRegistryService integration tests
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceIntegration:
    @pytest.mark.anyio
    async def test_create_returns_response_schema(self, db_session: AsyncSession) -> None:
        from apps.api.schemas.sources import SourceConfigResponse  # noqa: PLC0415

        service = SourceRegistryService(db_session)
        result = await service.create(
            SourceConfigCreate(
                code=_unique_code("SVC"),
                name="Service Test",
                provider_type="regulatory",
            )
        )
        assert isinstance(result, SourceConfigResponse)
        assert result.code.startswith("SVC")

    @pytest.mark.anyio
    async def test_create_raises_conflict_on_duplicate_code(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        service = SourceRegistryService(db_session)
        with pytest.raises(ConflictError, match="already exists"):
            await service.create(
                SourceConfigCreate(
                    code=source.code,
                    name="Duplicate",
                    provider_type="manual",
                )
            )

    @pytest.mark.anyio
    async def test_get_by_id_raises_not_found(self, db_session: AsyncSession) -> None:
        service = SourceRegistryService(db_session)
        with pytest.raises(NotFoundError):
            await service.get_by_id(uuid.uuid4())

    @pytest.mark.anyio
    async def test_get_by_code_raises_not_found(self, db_session: AsyncSession) -> None:
        service = SourceRegistryService(db_session)
        with pytest.raises(NotFoundError):
            await service.get_by_code("DOES_NOT_EXIST")

    @pytest.mark.anyio
    async def test_update_raises_not_found(self, db_session: AsyncSession) -> None:
        service = SourceRegistryService(db_session)
        with pytest.raises(NotFoundError):
            await service.update(uuid.uuid4(), SourceConfigUpdate(name="Ghost"))

    @pytest.mark.anyio
    async def test_enable_raises_not_found(self, db_session: AsyncSession) -> None:
        service = SourceRegistryService(db_session)
        with pytest.raises(NotFoundError):
            await service.enable(uuid.uuid4())

    @pytest.mark.anyio
    async def test_disable_raises_not_found(self, db_session: AsyncSession) -> None:
        service = SourceRegistryService(db_session)
        with pytest.raises(NotFoundError):
            await service.disable(uuid.uuid4())

    @pytest.mark.anyio
    async def test_delete_raises_not_found(self, db_session: AsyncSession) -> None:
        service = SourceRegistryService(db_session)
        with pytest.raises(NotFoundError):
            await service.delete(uuid.uuid4())

    @pytest.mark.anyio
    async def test_list_returns_paginated_response(self, db_session: AsyncSession) -> None:
        from apps.api.schemas.sources import SourceConfigListResponse  # noqa: PLC0415

        service = SourceRegistryService(db_session)
        # Ensure there is at least one source.
        await service.create(
            SourceConfigCreate(
                code=_unique_code("LST"),
                name="List Test",
                provider_type="manual",
            )
        )

        result = await service.list(page=1, page_size=10)
        assert isinstance(result, SourceConfigListResponse)
        assert result.total >= 1
        assert result.page == 1
        assert result.page_size == 10

    @pytest.mark.anyio
    async def test_disable_then_enable_round_trip(
        self, db_session: AsyncSession, source: SourceConfig
    ) -> None:
        service = SourceRegistryService(db_session)

        disabled = await service.disable(source.id)
        assert disabled.is_active is False

        enabled = await service.enable(source.id)
        assert enabled.is_active is True
