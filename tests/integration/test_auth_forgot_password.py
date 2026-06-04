"""
Integration tests — POST /api/v1/auth/forgot-password.

Requires a live PostgreSQL database with migration 001 applied.
Skipped automatically when DATABASE_URL is not set.

These tests verify the full forgot-password flow against a real database,
including token persistence on the User record. Email delivery is handled
by ConsoleEmailBackend (logs to stdout — no external calls needed).

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh@localhost:5432/fdh_dev \\
    JWT_SECRET=test-integration-secret-32chars!! \\
    SECRET_KEY=test-integration-app-secret-32ch \\
    uv run pytest tests/integration/ -v

Milestone: M1-Step22
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
        "email": f"forgot-test-{suffix}@example.com",
        "password": "Str0ng!Pass#Forgot99",
        "full_name": "Forgot Tester",
        "workspace_name": f"Forgot WS {suffix}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_forgot_password_returns_200_for_existing_user(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    await client.post("/api/v1/auth/register", json=credentials)
    resp = await client.post(
        "/api/v1/auth/forgot-password", json={"email": credentials["email"]}
    )
    assert resp.status_code == 200, resp.json()


async def test_forgot_password_returns_200_for_unknown_email(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "nobody-at-all@nonexistent-domain.example.com"},
    )
    assert resp.status_code == 200


async def test_forgot_password_same_response_for_both_cases(
    client: AsyncClient, credentials: dict[str, str]
) -> None:
    """Enumeration protection: body identical for existing and non-existing."""
    await client.post("/api/v1/auth/register", json=credentials)

    r_existing = await client.post(
        "/api/v1/auth/forgot-password", json={"email": credentials["email"]}
    )
    r_unknown = await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "definitely-not-registered@example.com"},
    )

    assert r_existing.json() == r_unknown.json()
    assert r_existing.status_code == r_unknown.status_code == 200


async def test_forgot_password_invalid_email_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "not-an-email"}
    )
    assert resp.status_code == 422
