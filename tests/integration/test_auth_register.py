"""
Integration tests — POST /api/v1/auth/register.

These tests require a live PostgreSQL database and a running Redis instance
(via docker compose up db redis).  They are skipped automatically when the
DATABASE_URL environment variable is not set or the DB is unreachable.

To run integration tests:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh@localhost:5432/fdh_dev \\
    JWT_SECRET=test-integration-secret-32chars!! \\
    SECRET_KEY=test-integration-app-secret-32ch \\
    uv run pytest tests/integration/ -v

Milestone: M1-Step18 — integration coverage added at Step 18,
           full green once Alembic migration 001 is applied (M0 gate).
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Skip entire module when no live DB is configured
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason=(
        "Integration tests require DATABASE_URL env var. "
        "Run: docker compose up -d db redis, then set DATABASE_URL."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------




@pytest.fixture()
def valid_payload() -> dict[str, str]:
    return {
        "email": f"integration-{os.urandom(4).hex()}@example.com",
        "password": "Str0ng!Pass#Integration99",
        "full_name": "Integration Tester",
        "workspace_name": f"Test Workspace {os.urandom(3).hex()}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_register_returns_201(client: AsyncClient, valid_payload: dict[str, str]) -> None:
    resp = await client.post("/api/v1/auth/register", json=valid_payload)
    assert resp.status_code == 201, resp.json()


async def test_register_response_contains_access_token(
    client: AsyncClient, valid_payload: dict[str, str]
) -> None:
    resp = await client.post("/api/v1/auth/register", json=valid_payload)
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["role"] == "owner"


async def test_register_sets_refresh_cookie(
    client: AsyncClient, valid_payload: dict[str, str]
) -> None:
    resp = await client.post("/api/v1/auth/register", json=valid_payload)
    assert "fdh_refresh" in resp.cookies or "set-cookie" in resp.headers


async def test_register_duplicate_email_returns_409(
    client: AsyncClient, valid_payload: dict[str, str]
) -> None:
    # First registration should succeed.
    first = await client.post("/api/v1/auth/register", json=valid_payload)
    assert first.status_code == 201

    # Second registration with the same email should fail.
    second = await client.post("/api/v1/auth/register", json=valid_payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"


async def test_register_weak_password_returns_422(
    client: AsyncClient, valid_payload: dict[str, str]
) -> None:
    payload = {**valid_payload, "password": "weak"}
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 422
