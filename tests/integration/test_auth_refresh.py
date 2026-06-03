"""
Integration tests — POST /api/v1/auth/refresh.

Requires a live PostgreSQL database with migration 001 applied and Redis.
Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh@localhost:5432/fdh_dev \\
    JWT_SECRET=test-integration-secret-32chars!! \\
    SECRET_KEY=test-integration-app-secret-32ch \\
    uv run pytest tests/integration/ -v

Milestone: M1-Step20
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="Integration tests require DATABASE_URL env var.",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client() -> AsyncClient:  # type: ignore[override]
    from apps.api.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c  # type: ignore[misc]


@pytest.fixture()
def credentials() -> dict[str, str]:
    suffix = os.urandom(4).hex()
    return {
        "email": f"refresh-test-{suffix}@example.com",
        "password": "Str0ng!Pass#Refresh99",
        "full_name": "Refresh Tester",
        "workspace_name": f"Refresh WS {suffix}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_refresh_after_login_returns_200(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    await client.post("/api/v1/auth/register", json=credentials)
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    assert login_resp.status_code == 200
    refresh_cookie = login_resp.cookies.get("fdh_refresh")
    assert refresh_cookie

    resp = await client.post(
        "/api/v1/auth/refresh", cookies={"fdh_refresh": refresh_cookie}
    )
    assert resp.status_code == 200, resp.json()


async def test_refresh_returns_new_access_token(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    await client.post("/api/v1/auth/register", json=credentials)
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    old_token = login_resp.json()["access_token"]
    refresh_cookie = login_resp.cookies["fdh_refresh"]

    resp = await client.post(
        "/api/v1/auth/refresh", cookies={"fdh_refresh": refresh_cookie}
    )
    new_token = resp.json()["access_token"]
    # Tokens should differ (new jti, new expiry)
    assert new_token != old_token


async def test_refresh_sets_new_cookie(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    await client.post("/api/v1/auth/register", json=credentials)
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    old_cookie = login_resp.cookies["fdh_refresh"]

    resp = await client.post(
        "/api/v1/auth/refresh", cookies={"fdh_refresh": old_cookie}
    )
    assert "fdh_refresh" in resp.cookies or "set-cookie" in resp.headers


async def test_old_refresh_token_rejected_after_rotation(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    """After rotation the old cookie must be rejected (revoked_at set)."""
    await client.post("/api/v1/auth/register", json=credentials)
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    old_cookie = login_resp.cookies["fdh_refresh"]

    # First rotation — succeeds
    await client.post("/api/v1/auth/refresh", cookies={"fdh_refresh": old_cookie})

    # Second use of the old (now revoked) cookie — must fail
    resp = await client.post(
        "/api/v1/auth/refresh", cookies={"fdh_refresh": old_cookie}
    )
    assert resp.status_code == 401


async def test_refresh_without_cookie_returns_401(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401


async def test_refresh_with_garbage_token_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/refresh", cookies={"fdh_refresh": "not-a-real-token-xxxx"}
    )
    assert resp.status_code == 401
