"""
Unit tests — /api/v1/companies router.

Strategy
--------
A minimal FastAPI application is built per-test with:
  - The companies router
  - The APIError exception handler
  - The get_db dependency overridden with an AsyncMock session
  - The require_authenticated / require_analyst / require_admin dependencies
    overridden to inject a fixed AuthRequestContext — this bypasses the JWT
    middleware and Redis blocklist check entirely in unit tests.

CompanyRepository is patched at the import path used by the router so its
methods return pre-built MagicMock objects.

What is mocked
--------------
- ``CompanyRepository``          — all repo methods
- Auth dependencies              — inject fixed AuthRequestContext
- ``get_db``                     — yields AsyncMock session

What is NOT mocked (real code runs)
------------------------------------
- All Pydantic schema validation (CompanyCreate, CompanyUpdate)
- NotFoundError / ConflictError exception handling and response serialisation
- _to_response / _to_list_response conversion helpers
- HTTP status codes and response body structure

Milestone: M2-Step 6
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_admin,
    require_analyst,
    require_authenticated,
)
from apps.api.routers.companies import router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_COMPANY_ID = uuid.uuid4()
_NOW = datetime.now(UTC)

_ANALYST_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="analyst",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)
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

_COMPANY_DATA: dict[str, Any] = {
    "id": _COMPANY_ID,
    "tenant_id": _TENANT_ID,
    "name": "Acme Corp",
    "ticker": "ACME",
    "cik": None,
    "exchange": None,
    "sector": None,
    "industry": None,
    "description": None,
    "website": None,
    "is_active": True,
    "created_at": _NOW,
    "updated_at": _NOW,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app(ctx: AuthRequestContext) -> FastAPI:
    """
    Build a minimal FastAPI app wired with the companies router.

    Auth dependencies are overridden to return the supplied context so tests
    run without JWT tokens or Redis calls.
    """
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    # Override DB dependency
    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db

    # Override auth dependencies with the supplied context
    app.dependency_overrides[require_authenticated] = lambda: ctx
    app.dependency_overrides[require_analyst] = lambda: ctx
    app.dependency_overrides[require_admin] = lambda: ctx

    app.include_router(router)
    return app


def _mock_company(**overrides: Any) -> MagicMock:
    """Return a MagicMock that mimics a Company ORM object."""
    c = MagicMock()
    data = {**_COMPANY_DATA, **overrides}
    for k, v in data.items():
        setattr(c, k, v)
    return c


# ---------------------------------------------------------------------------
# POST /api/v1/companies
# ---------------------------------------------------------------------------


class TestCreateCompany:
    @pytest.mark.anyio
    async def test_returns_201_on_success(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_company = _mock_company()
        mock_repo = AsyncMock()
        mock_repo.create.return_value = mock_company

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/companies",
                    json={"name": "Acme Corp", "ticker": "acme"},
                )

        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_response_body_shape(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_company = _mock_company()
        mock_repo = AsyncMock()
        mock_repo.create.return_value = mock_company

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/companies",
                    json={"name": "Acme Corp", "ticker": "acme"},
                )

        body = resp.json()
        assert "id" in body
        assert body["ticker"] == "ACME"  # confirm ticker is uppercased by schema
        assert body["name"] == "Acme Corp"
        assert body["tenant_id"] == str(_TENANT_ID)

    @pytest.mark.anyio
    async def test_ticker_uppercased_by_schema(self) -> None:
        """The schema normalises the ticker before it reaches the repository."""
        app = _build_app(_ANALYST_CTX)
        mock_company = _mock_company(ticker="TSLA")
        mock_repo = AsyncMock()
        mock_repo.create.return_value = mock_company

        captured_schema = {}

        async def _capture_create(tenant_id: Any, schema: Any) -> Any:
            captured_schema["ticker"] = schema.ticker
            return mock_company

        mock_repo.create.side_effect = _capture_create

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/companies",
                    json={"name": "Tesla", "ticker": "tsla"},
                )

        assert captured_schema["ticker"] == "TSLA"

    @pytest.mark.anyio
    async def test_duplicate_ticker_returns_409(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.create.side_effect = IntegrityError("", {}, Exception())

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/companies",
                    json={"name": "Duplicate", "ticker": "DUP"},
                )

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.anyio
    async def test_missing_required_fields_returns_422(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/companies", json={"name": "No Ticker"})

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_tenant_id_comes_from_jwt_not_body(self) -> None:
        """The router must inject ctx.tenant_id — no user-supplied tenant_id."""
        app = _build_app(_ANALYST_CTX)
        mock_company = _mock_company()
        mock_repo = AsyncMock()

        captured: dict[str, Any] = {}

        async def _capture(tenant_id: Any, schema: Any) -> Any:
            captured["tenant_id"] = tenant_id
            return mock_company

        mock_repo.create.side_effect = _capture

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/companies",
                    json={"name": "Test", "ticker": "T"},
                )

        assert captured["tenant_id"] == _TENANT_ID


# ---------------------------------------------------------------------------
# GET /api/v1/companies
# ---------------------------------------------------------------------------


class TestListCompanies:
    @pytest.mark.anyio
    async def test_returns_200_with_envelope(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = (
            [_mock_company(), _mock_company(id=uuid.uuid4(), ticker="MSFT")],
            2,
        )

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/companies")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["page"] == 1
        assert body["page_size"] == 20
        assert "pages" in body
        assert len(body["items"]) == 2

    @pytest.mark.anyio
    async def test_pagination_params_forwarded(self) -> None:
        app = _build_app(_VIEWER_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/companies?page=3&page_size=10")

        call_kwargs = mock_repo.list.call_args[1]
        assert call_kwargs["page"] == 3
        assert call_kwargs["page_size"] == 10

    @pytest.mark.anyio
    async def test_search_param_forwarded(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/companies?search=apple")

        call_kwargs = mock_repo.list.call_args[1]
        assert call_kwargs["search"] == "apple"

    @pytest.mark.anyio
    async def test_is_active_filter_forwarded(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/companies?is_active=false")

        call_kwargs = mock_repo.list.call_args[1]
        assert call_kwargs["is_active"] is False

    @pytest.mark.anyio
    async def test_empty_result(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/companies")

        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["pages"] == 0

    @pytest.mark.anyio
    async def test_page_size_max_100(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/companies?page_size=101")

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/companies/{company_id}
# ---------------------------------------------------------------------------


class TestGetCompany:
    @pytest.mark.anyio
    async def test_returns_200_when_found(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_company()

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/companies/{_COMPANY_ID}")

        assert resp.status_code == 200
        assert resp.json()["id"] == str(_COMPANY_ID)

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = None

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/companies/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "COMPANY_NOT_FOUND"

    @pytest.mark.anyio
    async def test_repo_called_with_tenant_id(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_company()

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(f"/api/v1/companies/{_COMPANY_ID}")

        call_args = mock_repo.get_by_id.call_args
        assert call_args[0][0] == _TENANT_ID  # tenant_id first positional arg


# ---------------------------------------------------------------------------
# PATCH /api/v1/companies/{company_id}
# ---------------------------------------------------------------------------


class TestUpdateCompany:
    @pytest.mark.anyio
    async def test_returns_200_on_success(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.update.return_value = _mock_company(name="New Name")

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.patch(
                    f"/api/v1/companies/{_COMPANY_ID}",
                    json={"name": "New Name"},
                )

        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.update.return_value = None

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.patch(
                    f"/api/v1/companies/{uuid.uuid4()}",
                    json={"name": "Ghost"},
                )

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_ticker_conflict_returns_409(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.update.side_effect = IntegrityError("", {}, Exception())

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.patch(
                    f"/api/v1/companies/{_COMPANY_ID}",
                    json={"ticker": "TAKEN"},
                )

        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_empty_patch_body_returns_422(self) -> None:
        """CompanyUpdate rejects empty bodies (model_validator)."""
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.patch(
                    f"/api/v1/companies/{_COMPANY_ID}",
                    json={},
                )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_model_fields_set_not_empty_for_single_field(self) -> None:
        """Repo must receive a schema with model_fields_set containing only the sent field."""
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        captured: dict[str, Any] = {}
        mock_company = _mock_company(name="Updated")
        mock_repo.update.return_value = mock_company

        async def _capture(tid: Any, cid: Any, schema: Any) -> Any:
            captured["fields_set"] = schema.model_fields_set
            return mock_company

        mock_repo.update.side_effect = _capture

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.patch(
                    f"/api/v1/companies/{_COMPANY_ID}",
                    json={"name": "Updated"},
                )

        assert "name" in captured["fields_set"]
        assert "ticker" not in captured["fields_set"]


# ---------------------------------------------------------------------------
# DELETE /api/v1/companies/{company_id}
# ---------------------------------------------------------------------------


class TestDeleteCompany:
    @pytest.mark.anyio
    async def test_returns_204_on_success(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.soft_delete.return_value = True

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.delete(f"/api/v1/companies/{_COMPANY_ID}")

        assert resp.status_code == 204
        assert resp.content == b""

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.soft_delete.return_value = False

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.delete(f"/api/v1/companies/{uuid.uuid4()}")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_repo_called_with_tenant_id(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.soft_delete.return_value = True

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.delete(f"/api/v1/companies/{_COMPANY_ID}")

        call_args = mock_repo.soft_delete.call_args[0]
        assert call_args[0] == _TENANT_ID


# ---------------------------------------------------------------------------
# Role enforcement (dependency override confirms correct guard is wired)
# ---------------------------------------------------------------------------


class TestRoleGuards:
    """
    These tests verify which dependency (require_analyst vs require_admin) is
    declared on each route by temporarily restoring real guards and testing
    that an insufficient role is rejected.

    Because dependency_overrides in the fixture always returns the supplied
    context without checking the role, these tests use a separate app that
    overrides only require_authenticated (not the role guards) so that the
    real role-check logic runs.
    """

    def _app_with_real_role_guards(self, ctx: AuthRequestContext) -> FastAPI:
        """Build app with real role guards but mocked base auth and DB."""
        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app.dependency_overrides[get_db] = _mock_db
        # Only override the base auth context reader — role guards will still run
        app.dependency_overrides[_get_auth_context] = lambda: ctx
        app.include_router(router)
        return app

    @pytest.mark.anyio
    async def test_viewer_cannot_create_company(self) -> None:
        app = self._app_with_real_role_guards(_VIEWER_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/companies",
                json={"name": "Test", "ticker": "T"},
            )

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_analyst_cannot_delete_company(self) -> None:
        app = self._app_with_real_role_guards(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.soft_delete.return_value = True

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.delete(f"/api/v1/companies/{_COMPANY_ID}")

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_admin_can_delete_company(self) -> None:
        app = self._app_with_real_role_guards(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.soft_delete.return_value = True

        with patch("apps.api.routers.companies.CompanyRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.delete(f"/api/v1/companies/{_COMPANY_ID}")

        assert resp.status_code == 204
