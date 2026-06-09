"""
Unit tests — /api/v1/sources router.

Strategy
--------
A minimal FastAPI application is built per-test with:
  - The sources router
  - The APIError exception handler
  - The get_db dependency overridden with an AsyncMock session
  - The require_authenticated / require_admin dependencies overridden to
    inject a fixed AuthRequestContext, bypassing JWT middleware entirely.

SourceRegistryService is patched at the import path used by the router
so its methods return pre-built response objects.

What is mocked
--------------
- ``SourceRegistryService``     — all service methods
- Auth dependencies              — inject fixed AuthRequestContext
- ``get_db``                     — yields AsyncMock session

What is NOT mocked (real code runs)
------------------------------------
- All Pydantic schema validation (SourceConfigCreate, SourceConfigUpdate)
- NotFoundError / ConflictError exception handling and response serialisation
- HTTP status codes and response body structure

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, ConflictError, NotFoundError, api_error_handler
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_admin,
    require_authenticated,
)
from apps.api.routers.sources import router
from apps.api.schemas.sources import SourceConfigResponse
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_TENANT_ID = uuid.uuid4()
_SOURCE_ID = uuid.uuid4()
_NOW = datetime.now(UTC)

_ADMIN_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="admin",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)
_VIEWER_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="viewer",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)

_SOURCE_DATA: dict[str, Any] = {
    "id": _SOURCE_ID,
    "code": "SEC_EDGAR",
    "name": "SEC EDGAR",
    "description": None,
    "provider_type": "regulatory",
    "country_code": "US",
    "base_url": "https://efts.sec.gov",
    "rate_limit_per_minute": 600,
    "is_active": True,
    "config": None,
    "created_at": _NOW,
    "updated_at": _NOW,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app(ctx: AuthRequestContext) -> FastAPI:
    """Build a minimal FastAPI app wired with the sources router."""
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: ctx
    app.dependency_overrides[require_admin] = lambda: ctx
    app.include_router(router)
    return app


def _mock_source_response(**overrides: Any) -> SourceConfigResponse:
    """Return a SourceConfigResponse built from _SOURCE_DATA with optional overrides."""
    data = {**_SOURCE_DATA, **overrides}
    return SourceConfigResponse.model_validate(data)


# ---------------------------------------------------------------------------
# POST /api/v1/sources
# ---------------------------------------------------------------------------


class TestCreateSource:
    @pytest.mark.anyio
    async def test_returns_201_on_success(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_response = _mock_source_response()
        mock_service = AsyncMock()
        mock_service.create.return_value = mock_response

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/sources",
                    json={"code": "sec_edgar", "name": "SEC EDGAR", "provider_type": "regulatory"},
                )

        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_response_body_shape(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_response = _mock_source_response()
        mock_service = AsyncMock()
        mock_service.create.return_value = mock_response

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/sources",
                    json={"code": "SEC_EDGAR", "name": "SEC EDGAR", "provider_type": "regulatory"},
                )

        body = resp.json()
        assert body["id"] == str(_SOURCE_ID)
        assert body["code"] == "SEC_EDGAR"
        assert body["provider_type"] == "regulatory"
        assert "is_active" in body

    @pytest.mark.anyio
    async def test_code_uppercased_by_schema(self) -> None:
        """The schema validator normalises code to uppercase before reaching service."""
        app = _build_app(_ADMIN_CTX)
        mock_response = _mock_source_response(code="NSE")
        mock_service = AsyncMock()
        mock_service.create.return_value = mock_response

        captured: dict[str, Any] = {}

        async def _capture_create(schema: Any) -> Any:
            captured["code"] = schema.code
            return mock_response

        mock_service.create.side_effect = _capture_create

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/sources",
                    json={"code": "nse", "name": "NSE India", "provider_type": "exchange"},
                )

        assert captured["code"] == "NSE"

    @pytest.mark.anyio
    async def test_duplicate_code_returns_409(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.create.side_effect = ConflictError("A source with code 'SEC_EDGAR' already exists.")

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/sources",
                    json={"code": "SEC_EDGAR", "name": "Duplicate", "provider_type": "regulatory"},
                )

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.anyio
    async def test_invalid_provider_type_returns_422(self) -> None:
        app = _build_app(_ADMIN_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/sources",
                json={"code": "TEST", "name": "Test", "provider_type": "invalid_type"},
            )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_missing_required_fields_returns_422(self) -> None:
        app = _build_app(_ADMIN_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/sources",
                json={"name": "No Code"},
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/sources
# ---------------------------------------------------------------------------


class TestListSources:
    @pytest.mark.anyio
    async def test_returns_200_with_envelope(self) -> None:
        from apps.api.schemas.sources import SourceConfigListResponse  # noqa: PLC0415

        app = _build_app(_VIEWER_CTX)
        mock_service = AsyncMock()
        mock_service.list.return_value = SourceConfigListResponse(
            items=[_mock_source_response(), _mock_source_response(id=uuid.uuid4(), code="NSE")],
            total=2,
            page=1,
            page_size=20,
            pages=1,
        )

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/sources")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["page"] == 1
        assert body["page_size"] == 20
        assert "pages" in body
        assert len(body["items"]) == 2

    @pytest.mark.anyio
    async def test_pagination_params_forwarded(self) -> None:
        from apps.api.schemas.sources import SourceConfigListResponse  # noqa: PLC0415

        app = _build_app(_VIEWER_CTX)
        mock_service = AsyncMock()
        mock_service.list.return_value = SourceConfigListResponse(
            items=[], total=0, page=2, page_size=5, pages=0
        )

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/sources?page=2&page_size=5")

        call_kwargs = mock_service.list.call_args[1]
        assert call_kwargs["page"] == 2
        assert call_kwargs["page_size"] == 5

    @pytest.mark.anyio
    async def test_is_active_filter_forwarded(self) -> None:
        from apps.api.schemas.sources import SourceConfigListResponse  # noqa: PLC0415

        app = _build_app(_VIEWER_CTX)
        mock_service = AsyncMock()
        mock_service.list.return_value = SourceConfigListResponse(
            items=[], total=0, page=1, page_size=20, pages=0
        )

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/sources?is_active=false")

        call_kwargs = mock_service.list.call_args[1]
        assert call_kwargs["is_active"] is False

    @pytest.mark.anyio
    async def test_page_size_max_100(self) -> None:
        app = _build_app(_ADMIN_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/sources?page_size=101")

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/sources/{source_id}
# ---------------------------------------------------------------------------


class TestGetSource:
    @pytest.mark.anyio
    async def test_returns_200_when_found(self) -> None:
        app = _build_app(_VIEWER_CTX)
        mock_service = AsyncMock()
        mock_service.get_by_id.return_value = _mock_source_response()

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/sources/{_SOURCE_ID}")

        assert resp.status_code == 200
        assert resp.json()["id"] == str(_SOURCE_ID)
        assert resp.json()["code"] == "SEC_EDGAR"

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_VIEWER_CTX)
        mock_service = AsyncMock()
        mock_service.get_by_id.side_effect = NotFoundError("SourceConfig", str(uuid.uuid4()))

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/sources/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SOURCECONFIG_NOT_FOUND"


# ---------------------------------------------------------------------------
# PATCH /api/v1/sources/{source_id}
# ---------------------------------------------------------------------------


class TestUpdateSource:
    @pytest.mark.anyio
    async def test_returns_200_on_success(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.update.return_value = _mock_source_response(name="New Name")

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.patch(
                    f"/api/v1/sources/{_SOURCE_ID}",
                    json={"name": "New Name"},
                )

        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.update.side_effect = NotFoundError("SourceConfig", str(uuid.uuid4()))

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.patch(
                    f"/api/v1/sources/{uuid.uuid4()}",
                    json={"name": "Ghost"},
                )

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_empty_patch_body_returns_422(self) -> None:
        """SourceConfigUpdate rejects empty bodies (model_validator)."""
        app = _build_app(_ADMIN_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/api/v1/sources/{_SOURCE_ID}",
                json={},
            )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_invalid_provider_type_returns_422(self) -> None:
        app = _build_app(_ADMIN_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/api/v1/sources/{_SOURCE_ID}",
                json={"provider_type": "bad_type"},
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/v1/sources/{source_id}
# ---------------------------------------------------------------------------


class TestDeleteSource:
    @pytest.mark.anyio
    async def test_returns_204_on_success(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.delete.return_value = None

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.delete(f"/api/v1/sources/{_SOURCE_ID}")

        assert resp.status_code == 204
        assert resp.content == b""

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.delete.side_effect = NotFoundError("SourceConfig", str(uuid.uuid4()))

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.delete(f"/api/v1/sources/{uuid.uuid4()}")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/sources/{source_id}/enable
# ---------------------------------------------------------------------------


class TestEnableSource:
    @pytest.mark.anyio
    async def test_returns_200_with_active_source(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.enable.return_value = _mock_source_response(is_active=True)

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/sources/{_SOURCE_ID}/enable")

        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.enable.side_effect = NotFoundError("SourceConfig", str(uuid.uuid4()))

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/sources/{uuid.uuid4()}/enable")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/sources/{source_id}/disable
# ---------------------------------------------------------------------------


class TestDisableSource:
    @pytest.mark.anyio
    async def test_returns_200_with_inactive_source(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.disable.return_value = _mock_source_response(is_active=False)

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/sources/{_SOURCE_ID}/disable")

        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.disable.side_effect = NotFoundError("SourceConfig", str(uuid.uuid4()))

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/sources/{uuid.uuid4()}/disable")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleGuards:
    """
    Verify that the correct auth guard is wired on each route.
    Uses a separate app that only overrides the base auth context reader,
    so the real role-check logic runs.
    """

    def _app_with_real_role_guards(self, ctx: AuthRequestContext) -> FastAPI:
        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app.dependency_overrides[get_db] = _mock_db
        app.dependency_overrides[_get_auth_context] = lambda: ctx
        app.include_router(router)
        return app

    @pytest.mark.anyio
    async def test_viewer_cannot_create_source(self) -> None:
        app = self._app_with_real_role_guards(_VIEWER_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/sources",
                json={"code": "TEST", "name": "Test", "provider_type": "manual"},
            )

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_viewer_cannot_delete_source(self) -> None:
        app = self._app_with_real_role_guards(_VIEWER_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(f"/api/v1/sources/{_SOURCE_ID}")

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_viewer_cannot_enable_source(self) -> None:
        app = self._app_with_real_role_guards(_VIEWER_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/v1/sources/{_SOURCE_ID}/enable")

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_viewer_cannot_disable_source(self) -> None:
        app = self._app_with_real_role_guards(_VIEWER_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/v1/sources/{_SOURCE_ID}/disable")

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_viewer_can_list_sources(self) -> None:
        """GET /sources must be accessible to any authenticated user."""
        from apps.api.schemas.sources import SourceConfigListResponse  # noqa: PLC0415

        app = self._app_with_real_role_guards(_VIEWER_CTX)
        mock_service = AsyncMock()
        mock_service.list.return_value = SourceConfigListResponse(
            items=[], total=0, page=1, page_size=20, pages=0
        )

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/sources")

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_admin_can_create_source(self) -> None:
        app = self._app_with_real_role_guards(_ADMIN_CTX)
        mock_service = AsyncMock()
        mock_service.create.return_value = _mock_source_response()

        with patch("apps.api.routers.sources.SourceRegistryService", return_value=mock_service):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/sources",
                    json={"code": "TEST", "name": "Test", "provider_type": "manual"},
                )

        assert resp.status_code == 201
