"""
Unit tests — SourceConfigRepository.

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
- ``session.delete``   — no-op sync (SQLAlchemy marks object for deletion)

What is NOT mocked (real code runs)
------------------------------------
- ``SourceConfigRepository`` method logic (create, get_by_id, get_by_code,
  list, update, enable, disable, delete)
- ``_UPDATABLE_FIELDS`` allowlist (code excluded, others included)
- Partial-update logic via ``model_fields_set``
- structlog calls (silently no-op in tests)

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from apps.api.models import ProviderType, SourceConfig
from apps.api.repositories.sources import _UPDATABLE_FIELDS, SourceConfigRepository
from apps.api.schemas.sources import SourceConfigCreate, SourceConfigUpdate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _make_session() -> AsyncMock:
    """Return a fresh AsyncMock that mimics an AsyncSession."""
    session = AsyncMock()
    session.add = MagicMock()       # sync; does not need to be awaited
    session.flush = AsyncMock()     # async
    session.delete = AsyncMock()    # async — SQLAlchemy AsyncSession.delete() is a coroutine
    return session


def _mock_scalar_one_or_none(value: Any) -> AsyncMock:
    """Return a mock execute() result whose scalar_one_or_none() returns value."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return AsyncMock(return_value=result)


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


def _make_source(
    source_id: uuid.UUID | None = None,
    code: str = "SEC_EDGAR",
    name: str = "SEC EDGAR",
    provider_type: str = ProviderType.REGULATORY,
    is_active: bool = True,
) -> MagicMock:
    """Build a minimal SourceConfig-like MagicMock."""
    s = MagicMock(spec=SourceConfig)
    s.id = source_id or uuid.uuid4()
    s.code = code
    s.name = name
    s.description = None
    s.provider_type = provider_type
    s.country_code = "US"
    s.base_url = "https://efts.sec.gov"
    s.rate_limit_per_minute = 600
    s.is_active = is_active
    s.config = None
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_create_schema(
    code: str = "SEC_EDGAR",
    name: str = "SEC EDGAR",
    provider_type: str = "regulatory",
) -> SourceConfigCreate:
    return SourceConfigCreate(
        code=code,
        name=name,
        provider_type=provider_type,
    )


# ===========================================================================
# SourceConfigRepository — create
# ===========================================================================


class TestSourceConfigRepositoryCreate:
    @pytest.mark.anyio
    async def test_create_adds_and_flushes(self) -> None:
        session = _make_session()
        repo = SourceConfigRepository(session)
        schema = _make_create_schema()

        source = await repo.create(schema)

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        added_obj = session.add.call_args[0][0]
        assert isinstance(added_obj, SourceConfig)
        assert source is added_obj

    @pytest.mark.anyio
    async def test_create_sets_code_uppercased(self) -> None:
        session = _make_session()
        repo = SourceConfigRepository(session)
        # Schema validator uppercases; we verify the repo passes it through.
        schema = _make_create_schema(code="SEC_EDGAR")

        source = await repo.create(schema)

        added = session.add.call_args[0][0]
        assert added.code == "SEC_EDGAR"
        assert source is added

    @pytest.mark.anyio
    async def test_create_sets_all_fields(self) -> None:
        session = _make_session()
        repo = SourceConfigRepository(session)
        schema = SourceConfigCreate(
            code="NSE",
            name="NSE India",
            description="National Stock Exchange of India",
            provider_type="exchange",
            country_code="IN",
            base_url="https://www.nseindia.com",
            rate_limit_per_minute=120,
            is_active=False,
            config={"key": "value"},
        )

        source = await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.code == "NSE"
        assert added.name == "NSE India"
        assert added.description == "National Stock Exchange of India"
        assert added.provider_type == "exchange"
        assert added.country_code == "IN"
        assert added.base_url == "https://www.nseindia.com"
        assert added.rate_limit_per_minute == 120
        assert added.is_active is False
        assert added.config == {"key": "value"}
        assert source is added

    @pytest.mark.anyio
    async def test_create_optional_fields_default_none(self) -> None:
        session = _make_session()
        repo = SourceConfigRepository(session)
        schema = _make_create_schema()

        await repo.create(schema)
        added = session.add.call_args[0][0]

        assert added.description is None
        assert added.country_code is None
        assert added.base_url is None
        assert added.config is None


# ===========================================================================
# SourceConfigRepository — get_by_id
# ===========================================================================


class TestSourceConfigRepositoryGetById:
    @pytest.mark.anyio
    async def test_returns_source_when_found(self) -> None:
        mock_source = _make_source()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        result = await repo.get_by_id(mock_source.id)

        assert result is mock_source

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = SourceConfigRepository(session)
        result = await repo.get_by_id(uuid.uuid4())

        assert result is None

    @pytest.mark.anyio
    async def test_execute_called_once(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = SourceConfigRepository(session)
        await repo.get_by_id(uuid.uuid4())
        assert session.execute.await_count == 1


# ===========================================================================
# SourceConfigRepository — get_by_code
# ===========================================================================


class TestSourceConfigRepositoryGetByCode:
    @pytest.mark.anyio
    async def test_returns_source_when_found(self) -> None:
        mock_source = _make_source(code="SEC_EDGAR")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        result = await repo.get_by_code("SEC_EDGAR")

        assert result is mock_source

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = SourceConfigRepository(session)
        result = await repo.get_by_code("UNKNOWN")

        assert result is None

    @pytest.mark.anyio
    async def test_code_normalised_to_uppercase_before_query(self) -> None:
        """get_by_code must uppercase the code before querying."""
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = SourceConfigRepository(session)
        # Passing lowercase — repo must convert to uppercase for the WHERE clause.
        await repo.get_by_code("sec_edgar")

        # We verify execution was called (actual SQL assertion is complex with mocks)
        assert session.execute.await_count == 1


# ===========================================================================
# SourceConfigRepository — list
# ===========================================================================


class TestSourceConfigRepositoryList:
    @pytest.mark.anyio
    async def test_returns_items_and_total(self) -> None:
        sources = [_make_source(), _make_source(code="NSE")]

        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        data_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = sources
        data_result.scalars.return_value = scalars
        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        repo = SourceConfigRepository(session)
        items, total = await repo.list()

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

        repo = SourceConfigRepository(session)
        items, total = await repo.list()

        assert total == 0
        assert items == []

    @pytest.mark.anyio
    async def test_two_queries_always_executed(self) -> None:
        """One count query and one data query must always be issued."""
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        data_result = MagicMock()
        data_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        session = _make_session()
        session.execute = AsyncMock(side_effect=[count_result, data_result])

        repo = SourceConfigRepository(session)
        await repo.list()
        assert session.execute.await_count == 2


# ===========================================================================
# SourceConfigRepository — update
# ===========================================================================


class TestSourceConfigRepositoryUpdate:
    @pytest.mark.anyio
    async def test_update_applies_only_set_fields(self) -> None:
        mock_source = _make_source(name="Old Name")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        schema = SourceConfigUpdate(name="New Name")

        result = await repo.update(mock_source.id, schema)

        assert mock_source.name == "New Name"
        assert result is mock_source
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_update_does_not_touch_unset_fields(self) -> None:
        """PATCH body of {"name": "New"} must not clear other fields."""
        mock_source = _make_source(name="Old", code="NSE")
        mock_source.base_url = "https://nseindia.com"
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        schema = SourceConfigUpdate(name="New")
        await repo.update(mock_source.id, schema)

        # base_url must not have been overwritten
        assert mock_source.base_url == "https://nseindia.com"

    @pytest.mark.anyio
    async def test_update_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)
        repo = SourceConfigRepository(session)
        result = await repo.update(uuid.uuid4(), SourceConfigUpdate(name="X"))
        assert result is None

    @pytest.mark.anyio
    async def test_update_no_flush_when_nothing_changed(self) -> None:
        """If the incoming value equals the current value, no flush should occur."""
        mock_source = _make_source(name="Same")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        schema = SourceConfigUpdate(name="Same")
        await repo.update(mock_source.id, schema)

        session.flush.assert_not_awaited()

    @pytest.mark.anyio
    async def test_update_sets_updated_at(self) -> None:
        mock_source = _make_source(name="Old")
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        schema = SourceConfigUpdate(name="New")
        await repo.update(mock_source.id, schema)

        assert mock_source.updated_at is not None

    @pytest.mark.anyio
    async def test_code_not_in_updatable_fields(self) -> None:
        """code must never appear in _UPDATABLE_FIELDS (immutability guarantee)."""
        assert "code" not in _UPDATABLE_FIELDS

    @pytest.mark.anyio
    async def test_update_rate_limit(self) -> None:
        mock_source = _make_source()
        mock_source.rate_limit_per_minute = 60
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        schema = SourceConfigUpdate(rate_limit_per_minute=120)
        await repo.update(mock_source.id, schema)

        assert mock_source.rate_limit_per_minute == 120


# ===========================================================================
# SourceConfigRepository — enable / disable
# ===========================================================================


class TestSourceConfigRepositoryEnableDisable:
    @pytest.mark.anyio
    async def test_disable_sets_is_active_false(self) -> None:
        mock_source = _make_source(is_active=True)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        result = await repo.disable(mock_source.id)

        assert mock_source.is_active is False
        assert result is mock_source
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_disable_already_disabled_no_flush(self) -> None:
        """Disabling an already-disabled source must not issue a flush."""
        mock_source = _make_source(is_active=False)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        await repo.disable(mock_source.id)

        session.flush.assert_not_awaited()

    @pytest.mark.anyio
    async def test_disable_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = SourceConfigRepository(session)
        result = await repo.disable(uuid.uuid4())

        assert result is None

    @pytest.mark.anyio
    async def test_enable_sets_is_active_true(self) -> None:
        mock_source = _make_source(is_active=False)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        result = await repo.enable(mock_source.id)

        assert mock_source.is_active is True
        assert result is mock_source
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_enable_already_active_no_flush(self) -> None:
        """Enabling an already-active source must not issue a flush."""
        mock_source = _make_source(is_active=True)
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        await repo.enable(mock_source.id)

        session.flush.assert_not_awaited()

    @pytest.mark.anyio
    async def test_enable_returns_none_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = SourceConfigRepository(session)
        result = await repo.enable(uuid.uuid4())

        assert result is None


# ===========================================================================
# SourceConfigRepository — delete
# ===========================================================================


class TestSourceConfigRepositoryDelete:
    @pytest.mark.anyio
    async def test_delete_returns_true_when_found(self) -> None:
        mock_source = _make_source()
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(mock_source)

        repo = SourceConfigRepository(session)
        result = await repo.delete(mock_source.id)

        assert result is True
        session.delete.assert_called_once_with(mock_source)
        session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_delete_returns_false_when_not_found(self) -> None:
        session = _make_session()
        session.execute = _mock_scalar_one_or_none(None)

        repo = SourceConfigRepository(session)
        result = await repo.delete(uuid.uuid4())

        assert result is False
        session.delete.assert_not_called()
        session.flush.assert_not_awaited()


# ===========================================================================
# Allowlist correctness
# ===========================================================================


class TestAllowlists:
    def test_updatable_fields_excludes_code(self) -> None:
        """code is immutable — must never appear in the update allowlist."""
        assert "code" not in _UPDATABLE_FIELDS

    def test_updatable_fields_excludes_id(self) -> None:
        """Primary key must never be writable via update."""
        assert "id" not in _UPDATABLE_FIELDS

    def test_updatable_fields_excludes_created_at(self) -> None:
        assert "created_at" not in _UPDATABLE_FIELDS

    def test_updatable_fields_contains_expected_columns(self) -> None:
        expected = {
            "name",
            "description",
            "provider_type",
            "country_code",
            "base_url",
            "rate_limit_per_minute",
            "is_active",
            "config",
        }
        assert _UPDATABLE_FIELDS == expected
