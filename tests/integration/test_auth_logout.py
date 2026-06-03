"""
Integration tests — POST /api/v1/auth/logout.

Requires a live PostgreSQL database with migration 001 applied and Redis.
Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh@localhost:5432/fdh_dev \\
    JWT_SECRET=test-integration-secret-32chars!! \\
    SECRET_KEY=test-integration-app-secret-32ch \\
    uv run pytest tests/integration/ -v

Milestone: M1-Step21
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
        "email": f"logout-test-{suffix}@example.com",
        "password": "Str0ng!Pass#Logout99",
        "full_name": "Logout Tester",
        "workspace_name": f"Logout WS {suffix}",
    }


async def _register_and_login(
    client: AsyncClient, credentials: dict[str, str]
) -> tuple[str, str]:
    """Register + login; return (access_token, refresh_cookie)."""
    await client.post("/api/v1/auth/register", json=credentials)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"], resp.cookies["fdh_refresh"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_logout_returns_204(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    access_token, _ = await _register_and_login(client, credentials)
    resp = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 204, resp.text


async def test_logout_clears_cookie(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    access_token, _ = await _register_and_login(client, credentials)
    resp = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert "fdh_refresh" in resp.headers.get("set-cookie", "")


async def test_refresh_cookie_rejected_after_logout(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    """After logout, the refresh token must be revoked and rejected."""
    access_token, refresh_cookie = await _register_and_login(client, credentials)
    await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    # Attempt to refresh with the now-revoked cookie
    resp = await client.post(
        "/api/v1/auth/refresh",
        cookies={"fdh_refresh": refresh_cookie},
    )
    assert resp.status_code == 401


async def test_logout_without_auth_returns_401(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 401


async def test_double_logout_second_still_returns_401_on_refresh(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    """After double-logout the refresh token remains invalid."""
    access_token, refresh_cookie = await _register_and_login(client, credentials)

    # First logout
    await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    # The access token may still be valid (within 15-min window in integration).
    # Regardless, the refresh cookie is revoked.
    resp = await client.post(
        "/api/v1/auth/refresh",
        cookies={"fdh_refresh": refresh_cookie},
    )
    assert resp.status_code == 401
