"""
Integration tests — POST /api/v1/auth/login.

Requires a live PostgreSQL database with migration 001 applied and a
running Redis instance. Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh@localhost:5432/fdh_dev \\
    JWT_SECRET=test-integration-secret-32chars!! \\
    SECRET_KEY=test-integration-app-secret-32ch \\
    uv run pytest tests/integration/ -v

Milestone: M1-Step19 — integration coverage added at Step 19,
           full green once Alembic migration 001 is applied.
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient

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
def register_payload() -> dict[str, str]:
    suffix = os.urandom(4).hex()
    return {
        "email": f"login-test-{suffix}@example.com",
        "password": "Str0ng!Pass#Integration99",
        "full_name": "Login Tester",
        "workspace_name": f"Login Workspace {suffix}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_login_after_register_returns_200(
    client: AsyncClient, register_payload: dict[str, str]
) -> None:
    reg = await client.post("/api/v1/auth/register", json=register_payload)
    assert reg.status_code == 201

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": register_payload["email"], "password": register_payload["password"]},
    )
    assert resp.status_code == 200, resp.json()


async def test_login_response_has_access_token(
    client: AsyncClient, register_payload: dict[str, str]
) -> None:
    await client.post("/api/v1/auth/register", json=register_payload)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": register_payload["email"], "password": register_payload["password"]},
    )
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["role"] == "owner"


async def test_login_sets_refresh_cookie(
    client: AsyncClient, register_payload: dict[str, str]
) -> None:
    await client.post("/api/v1/auth/register", json=register_payload)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": register_payload["email"], "password": register_payload["password"]},
    )
    assert "fdh_refresh" in resp.cookies or "set-cookie" in resp.headers


async def test_login_wrong_password_returns_401(
    client: AsyncClient, register_payload: dict[str, str]
) -> None:
    await client.post("/api/v1/auth/register", json=register_payload)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": register_payload["email"], "password": "WrongPass!9999"},
    )
    assert resp.status_code == 401


async def test_login_unknown_email_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@nonexistent.example.com", "password": "Anything!Pass#1"},
    )
    assert resp.status_code == 401


async def test_login_case_insensitive_email(
    client: AsyncClient, register_payload: dict[str, str]
) -> None:
    """Email lookup is case-insensitive — UPPER case email logs in successfully."""
    await client.post("/api/v1/auth/register", json=register_payload)
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": register_payload["email"].upper(),
            "password": register_payload["password"],
        },
    )
    assert resp.status_code == 200
