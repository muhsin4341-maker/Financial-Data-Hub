"""
Unit tests — SourceRegistryService.

Strategy
--------
SourceConfigRepository is patched at the import path used by the service.
All repository methods return pre-built MagicMock / SourceConfigResponse objects.
The service is tested in isolation — no database, no HTTP layer.

What is mocked
--------------
- ``SourceConfigRepository``     — all repo methods via AsyncMock
- ``AsyncSession``               — passed to SourceRegistryService constructor

What is NOT mocked (real code runs)
------------------------------------
- SourceRegistryService.create    — business rules, ConflictError on IntegrityError
- SourceRegistryService.get_by_id — NotFoundError on None
- SourceRegistryService.get_by_code — NotFoundError on None
- SourceRegistryService.list      — pagination envelope construction
- SourceRegistryService.update    — NotFoundError on None
- SourceRegistryService.enable    — NotFoundError on None
- SourceRegistryService.disable   — NotFoundError on None
- SourceRegistryService.delete    — warning for active source; NotFoundError on None
- SourceRegistryService._to_response — ORM-to-schema conversion

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.models import ProviderType, SourceConfig
from apps.api.schemas.sources import SourceConfigCreate, SourceConfigUpdate
from apps.api.services.sources import SourceRegistryService
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _make_source_orm(
    source_id: uuid.UUID | None = None,
    code: str = "SEC_EDGAR",
    name: str = "SEC EDGAR",
    provider_type: str = ProviderType.REGULATORY,
    is_active: bool = True,
) -> MagicMock:
    """Build a minimal SourceConfig-like MagicMock for ORM results."""
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


def _make_mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_service_with_mock_repo(
    repo_mock: AsyncMock,
) -> SourceRegistryService:
    """Return a SourceRegistryService whose internal repo is fully replaced."""
    session = _make_mock_session()
    service = SourceRegistryService(session)
    # Replace the repo with a fully mocked version.
    service._repo = repo_mock  # type: ignore[assignment]
    return service


# ---------------------------------------------------------------------------
# SourceRegistryService — create
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceCreate:
    @pytest.mark.anyio
    async def test_create_returns_response_schema(self) -> None:
        mock_source = _make_source_orm()
        repo = AsyncMock()
        repo.create.return_value = mock_source
        service = _make_service_with_mock_repo(repo)

        schema = SourceConfigCreate(code="SEC_EDGAR", name="SEC EDGAR", provider_type="regulatory")
        result = await service.create(schema)

        assert result.code == "SEC_EDGAR"
        assert result.name == "SEC EDGAR"
        assert result.provider_type == "regulatory"
        repo.create.assert_awaited_once_with(schema)

    @pytest.mark.anyio
    async def test_create_raises_conflict_on_integrity_error(self) -> None:
        repo = AsyncMock()
        repo.create.side_effect = IntegrityError("", {}, Exception())
        service = _make_service_with_mock_repo(repo)

        schema = SourceConfigCreate(code="SEC_EDGAR", name="Duplicate", provider_type="regulatory")
        with pytest.raises(ConflictError, match="SEC_EDGAR"):
            await service.create(schema)

    @pytest.mark.anyio
    async def test_create_propagates_schema_code(self) -> None:
        """Service must pass the schema's (already normalised) code to the repo."""
        mock_source = _make_source_orm(code="NSE")
        repo = AsyncMock()
        repo.create.return_value = mock_source
        service = _make_service_with_mock_repo(repo)

        schema = SourceConfigCreate(code="nse", name="NSE India", provider_type="exchange")
        result = await service.create(schema)

        # Schema validator uppercases 'nse' → 'NSE'. Service passes it through.
        assert result.code == "NSE"


# ---------------------------------------------------------------------------
# SourceRegistryService — get_by_id
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceGetById:
    @pytest.mark.anyio
    async def test_get_by_id_returns_response(self) -> None:
        sid = uuid.uuid4()
        mock_source = _make_source_orm(source_id=sid)
        repo = AsyncMock()
        repo.get_by_id.return_value = mock_source
        service = _make_service_with_mock_repo(repo)

        result = await service.get_by_id(sid)

        assert result.id == sid
        repo.get_by_id.assert_awaited_once_with(sid)

    @pytest.mark.anyio
    async def test_get_by_id_raises_not_found(self) -> None:
        repo = AsyncMock()
        repo.get_by_id.return_value = None
        service = _make_service_with_mock_repo(repo)

        with pytest.raises(NotFoundError):
            await service.get_by_id(uuid.uuid4())

    @pytest.mark.anyio
    async def test_get_by_id_not_found_error_code(self) -> None:
        """Error code should be SOURCECONFIG_NOT_FOUND."""
        repo = AsyncMock()
        repo.get_by_id.return_value = None
        service = _make_service_with_mock_repo(repo)

        try:
            await service.get_by_id(uuid.uuid4())
        except NotFoundError as e:
            assert e.code == "SOURCECONFIG_NOT_FOUND"


# ---------------------------------------------------------------------------
# SourceRegistryService — get_by_code
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceGetByCode:
    @pytest.mark.anyio
    async def test_get_by_code_returns_response(self) -> None:
        mock_source = _make_source_orm(code="SEC_EDGAR")
        repo = AsyncMock()
        repo.get_by_code.return_value = mock_source
        service = _make_service_with_mock_repo(repo)

        result = await service.get_by_code("SEC_EDGAR")

        assert result.code == "SEC_EDGAR"
        repo.get_by_code.assert_awaited_once_with("SEC_EDGAR")

    @pytest.mark.anyio
    async def test_get_by_code_raises_not_found(self) -> None:
        repo = AsyncMock()
        repo.get_by_code.return_value = None
        service = _make_service_with_mock_repo(repo)

        with pytest.raises(NotFoundError):
            await service.get_by_code("UNKNOWN")


# ---------------------------------------------------------------------------
# SourceRegistryService — list
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceList:
    @pytest.mark.anyio
    async def test_list_returns_paginated_response(self) -> None:
        sources = [_make_source_orm(), _make_source_orm(code="NSE")]
        repo = AsyncMock()
        repo.list.return_value = (sources, 2)
        service = _make_service_with_mock_repo(repo)

        result = await service.list(page=1, page_size=20)

        assert result.total == 2
        assert result.page == 1
        assert result.page_size == 20
        assert result.pages == 1
        assert len(result.items) == 2

    @pytest.mark.anyio
    async def test_list_forwards_filters(self) -> None:
        repo = AsyncMock()
        repo.list.return_value = ([], 0)
        service = _make_service_with_mock_repo(repo)

        await service.list(
            page=2,
            page_size=5,
            provider_type="regulatory",
            country_code="US",
            is_active=True,
        )

        repo.list.assert_awaited_once_with(
            page=2,
            page_size=5,
            provider_type="regulatory",
            country_code="US",
            is_active=True,
        )

    @pytest.mark.anyio
    async def test_list_empty_result(self) -> None:
        repo = AsyncMock()
        repo.list.return_value = ([], 0)
        service = _make_service_with_mock_repo(repo)

        result = await service.list()

        assert result.total == 0
        assert result.items == []
        assert result.pages == 0

    @pytest.mark.anyio
    async def test_list_pages_computed_correctly(self) -> None:
        sources = [_make_source_orm() for _ in range(3)]
        repo = AsyncMock()
        repo.list.return_value = (sources, 10)
        service = _make_service_with_mock_repo(repo)

        result = await service.list(page=1, page_size=3)

        # ceil(10 / 3) = 4 pages
        assert result.pages == 4
        assert result.total == 10


# ---------------------------------------------------------------------------
# SourceRegistryService — update
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceUpdate:
    @pytest.mark.anyio
    async def test_update_returns_updated_response(self) -> None:
        sid = uuid.uuid4()
        mock_source = _make_source_orm(source_id=sid, name="Updated Name")
        repo = AsyncMock()
        repo.update.return_value = mock_source
        service = _make_service_with_mock_repo(repo)

        result = await service.update(sid, SourceConfigUpdate(name="Updated Name"))

        assert result.name == "Updated Name"
        repo.update.assert_awaited_once()

    @pytest.mark.anyio
    async def test_update_raises_not_found(self) -> None:
        repo = AsyncMock()
        repo.update.return_value = None
        service = _make_service_with_mock_repo(repo)

        with pytest.raises(NotFoundError):
            await service.update(uuid.uuid4(), SourceConfigUpdate(name="Ghost"))


# ---------------------------------------------------------------------------
# SourceRegistryService — enable
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceEnable:
    @pytest.mark.anyio
    async def test_enable_returns_active_source(self) -> None:
        sid = uuid.uuid4()
        mock_source = _make_source_orm(source_id=sid, is_active=True)
        repo = AsyncMock()
        repo.enable.return_value = mock_source
        service = _make_service_with_mock_repo(repo)

        result = await service.enable(sid)

        assert result.is_active is True
        repo.enable.assert_awaited_once_with(sid)

    @pytest.mark.anyio
    async def test_enable_raises_not_found(self) -> None:
        repo = AsyncMock()
        repo.enable.return_value = None
        service = _make_service_with_mock_repo(repo)

        with pytest.raises(NotFoundError):
            await service.enable(uuid.uuid4())


# ---------------------------------------------------------------------------
# SourceRegistryService — disable
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceDisable:
    @pytest.mark.anyio
    async def test_disable_returns_inactive_source(self) -> None:
        sid = uuid.uuid4()
        mock_source = _make_source_orm(source_id=sid, is_active=False)
        repo = AsyncMock()
        repo.disable.return_value = mock_source
        service = _make_service_with_mock_repo(repo)

        result = await service.disable(sid)

        assert result.is_active is False
        repo.disable.assert_awaited_once_with(sid)

    @pytest.mark.anyio
    async def test_disable_raises_not_found(self) -> None:
        repo = AsyncMock()
        repo.disable.return_value = None
        service = _make_service_with_mock_repo(repo)

        with pytest.raises(NotFoundError):
            await service.disable(uuid.uuid4())


# ---------------------------------------------------------------------------
# SourceRegistryService — delete
# ---------------------------------------------------------------------------


class TestSourceRegistryServiceDelete:
    @pytest.mark.anyio
    async def test_delete_succeeds_for_existing_source(self) -> None:
        sid = uuid.uuid4()
        mock_source = _make_source_orm(source_id=sid, is_active=False)
        repo = AsyncMock()
        repo.get_by_id.return_value = mock_source
        repo.delete.return_value = True
        service = _make_service_with_mock_repo(repo)

        # Should not raise — just returns None
        await service.delete(sid)

        repo.delete.assert_awaited_once_with(sid)

    @pytest.mark.anyio
    async def test_delete_raises_not_found_when_missing(self) -> None:
        repo = AsyncMock()
        repo.get_by_id.return_value = None
        service = _make_service_with_mock_repo(repo)

        with pytest.raises(NotFoundError):
            await service.delete(uuid.uuid4())

    @pytest.mark.anyio
    async def test_delete_active_source_issues_warning(self) -> None:
        """Deleting an active source must emit a structlog warning."""
        sid = uuid.uuid4()
        mock_source = _make_source_orm(source_id=sid, is_active=True)
        repo = AsyncMock()
        repo.get_by_id.return_value = mock_source
        repo.delete.return_value = True
        service = _make_service_with_mock_repo(repo)

        # Patch structlog to capture the warning call.
        with patch("apps.api.services.sources.log") as mock_log:
            await service.delete(sid)
            # Verify a warning was emitted (not just info).
            mock_log.warning.assert_called_once()
            warning_args = mock_log.warning.call_args[0][0]
            assert "deleting_active" in warning_args or "active" in warning_args

    @pytest.mark.anyio
    async def test_delete_inactive_source_no_warning(self) -> None:
        """Deleting an already-disabled source must NOT emit a warning."""
        sid = uuid.uuid4()
        mock_source = _make_source_orm(source_id=sid, is_active=False)
        repo = AsyncMock()
        repo.get_by_id.return_value = mock_source
        repo.delete.return_value = True
        service = _make_service_with_mock_repo(repo)

        with patch("apps.api.services.sources.log") as mock_log:
            await service.delete(sid)
            mock_log.warning.assert_not_called()
