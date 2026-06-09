"""
Unit tests — GET /api/v1/companies/resolve (Company Resolver endpoint).

Strategy
--------
A minimal FastAPI application is built per-test with:
  - The companies router
  - The APIError exception handler
  - The require_authenticated dependency overridden with a fixed AuthRequestContext
  - The _get_resolver dependency overridden to return a mock CompanyResolverService

CompanyResolverService is patched via dependency_overrides so the real
SEC EDGAR HTTP calls are never made.

What is mocked
--------------
- CompanyResolverService     — resolve_by_ticker / resolve_by_cik return pre-built CompanyInfo
- Auth dependencies          — inject fixed AuthRequestContext
- _get_resolver dependency   — returns mock service

What is NOT mocked (real code runs)
------------------------------------
- Pydantic validation of CompanyResolveResponse
- NotFoundError / ValidationError exception handling
- Route registration order (ensures /resolve matches before /{company_id})
- HTTP status codes and response body structure

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_authenticated,
)
from apps.api.routers.companies import _get_resolver, router
from services.acquisition.company_resolver.provider import CompanyInfo

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_TENANT_ID = uuid.uuid4()

_AUTH_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="analyst",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)

_AAPL_INFO = CompanyInfo(
    ticker="AAPL",
    company_name="Apple Inc.",
    cik="0000320193",
    exchange=None,
    country="US",
)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(
    resolver_mock: object,
) -> FastAPI:
    """
    Build a minimal FastAPI app with the companies router and mocked dependencies.
    """
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(router)

    # Override auth dependency
    app.dependency_overrides[require_authenticated] = lambda: _AUTH_CTX

    # Override DB dependency (companies CRUD routes use it)
    async def _fake_db():  # noqa: ANN202
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db

    # Override resolver dependency
    app.dependency_overrides[_get_resolver] = lambda: resolver_mock

    return app


def _make_resolver(
    ticker_result: CompanyInfo | None = _AAPL_INFO,
    cik_result: CompanyInfo | None = _AAPL_INFO,
) -> MagicMock:
    resolver = MagicMock()
    resolver.resolve_by_ticker = AsyncMock(return_value=ticker_result)
    resolver.resolve_by_cik = AsyncMock(return_value=cik_result)
    return resolver


# ---------------------------------------------------------------------------
# Successful resolution tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_by_ticker_returns_200() -> None:
    """GET /companies/resolve?ticker=AAPL returns 200 with CompanyResolveResponse."""
    resolver = _make_resolver()
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/companies/resolve", params={"ticker": "AAPL"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["company_name"] == "Apple Inc."
    assert body["cik"] == "0000320193"
    assert body["country"] == "US"


@pytest.mark.anyio
async def test_resolve_by_cik_returns_200() -> None:
    """GET /companies/resolve?cik=0000320193 returns 200 with CompanyResolveResponse."""
    resolver = _make_resolver()
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/companies/resolve", params={"cik": "0000320193"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cik"] == "0000320193"
    assert body["ticker"] == "AAPL"


@pytest.mark.anyio
async def test_resolve_uses_resolver_service() -> None:
    """The route delegates to CompanyResolverService.resolve_by_ticker."""
    resolver = _make_resolver()
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/api/v1/companies/resolve", params={"ticker": "AAPL"})

    resolver.resolve_by_ticker.assert_called_once_with("AAPL")


@pytest.mark.anyio
async def test_resolve_by_cik_uses_resolver_service() -> None:
    """The route delegates to CompanyResolverService.resolve_by_cik."""
    resolver = _make_resolver()
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/api/v1/companies/resolve", params={"cik": "0000320193"})

    resolver.resolve_by_cik.assert_called_once_with("0000320193")


# ---------------------------------------------------------------------------
# 404 — not found
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_unknown_ticker_returns_404() -> None:
    """Returns 404 when the resolver cannot find the ticker."""
    resolver = _make_resolver(ticker_result=None)
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/companies/resolve", params={"ticker": "INVALID_XYZ"}
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "COMPANYINFO_NOT_FOUND"


@pytest.mark.anyio
async def test_resolve_unknown_cik_returns_404() -> None:
    """Returns 404 when the resolver cannot find the CIK."""
    resolver = _make_resolver(cik_result=None)
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/companies/resolve", params={"cik": "9999999999"}
        )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 422 — validation errors (missing / conflicting params)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_missing_params_returns_422() -> None:
    """Returns 422 when neither ticker nor cik is provided."""
    resolver = _make_resolver()
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/companies/resolve")

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_resolve_both_params_returns_422() -> None:
    """Returns 422 when both ticker and cik are provided."""
    resolver = _make_resolver()
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/companies/resolve",
            params={"ticker": "AAPL", "cik": "0000320193"},
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Auth — 401 without credentials
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_requires_authentication() -> None:
    """Returns 401 when no auth context is injected (no override)."""
    resolver = _make_resolver()
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(router)
    # No override for require_authenticated — real dependency raises 401

    async def _fake_db():  # noqa: ANN202
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[_get_resolver] = lambda: resolver

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/companies/resolve", params={"ticker": "AAPL"})

    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Route ordering — /resolve must not be swallowed by /{company_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_path_not_treated_as_company_id() -> None:
    """
    FastAPI must route /companies/resolve to the resolve handler,
    not attempt to parse 'resolve' as a UUID for /{company_id}.
    """
    resolver = _make_resolver()
    app = _build_app(resolver)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/companies/resolve", params={"ticker": "AAPL"}
        )

    # If routing was wrong, FastAPI would return 422 (UUID parse failure).
    # A 200 or 404 proves the right handler was invoked.
    assert resp.status_code != 422
