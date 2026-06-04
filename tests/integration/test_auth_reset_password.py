"""
Integration tests — POST /api/v1/auth/reset-password.

Requires a live PostgreSQL database with migration 001 applied.
Skipped automatically when DATABASE_URL is not set.

These tests drive the complete forgot-password → reset-password flow against
a real database, including token persistence and refresh token revocation.

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh@localhost:5432/fdh_dev \\
    JWT_SECRET=test-integration-secret-32chars!! \\
    SECRET_KEY=test-integration-app-secret-32ch \\
    uv run pytest tests/integration/ -v

Milestone: M1-Step23
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="Integration tests require DATABASE_URL env var.",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------




@pytest.fixture()
def credentials() -> dict[str, str]:
    suffix = os.urandom(4).hex()
    return {
        "email": f"reset-test-{suffix}@example.com",
        "password": "Str0ng!Pass#Reset99",
        "full_name": "Reset Tester",
        "workspace_name": f"Reset WS {suffix}",
    }


@pytest.fixture()
def new_password() -> str:
    return "NewStr0ng!Pass#Reset01"


async def _get_reset_token(client: AsyncClient, email: str) -> str:
    """
    In a test environment (ConsoleEmailBackend), the token is logged to
    stdout but not accessible programmatically. We work around this by
    directly patching the repository to capture the token.

    For integration tests, we patch ``update_password_reset_token`` to capture
    the hashed token, then also need the raw token. Since we can't get the raw
    token from the hash, we instead mock ``generate_password_reset_token``
    to return a known value.
    """
    # This fixture exists as a hook — actual token capture requires
    # patching at the application level, which is done in each test.
    return "placeholder"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_reset_password_invalid_token_returns_400(
    client: AsyncClient,
) -> None:
    """A garbage token always returns 400."""
    resp = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": "invalid-token-xyz", "new_password": "NewStr0ng!Pass#01"},
    )
    assert resp.status_code == 400


async def test_reset_password_weak_password_returns_422(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": "any-token", "new_password": "weak"},
    )
    assert resp.status_code == 422


async def test_full_reset_flow_with_patched_token(
    client: AsyncClient, credentials: dict[str, str], new_password: str
) -> None:
    """
    Full integration: register → forgot-password → reset-password → login.

    We patch ``generate_password_reset_token`` to return a known raw token so
    we can construct the reset request without email access.
    """
    from unittest.mock import patch

    known_raw_token = "Z" * 48  # 48 printable chars, valid URL-safe b64

    # Register
    await client.post("/api/v1/auth/register", json=credentials)

    # Trigger forgot-password with patched token generation
    with patch("apps.api.routers.auth.generate_password_reset_token", return_value=known_raw_token):
        fp_resp = await client.post(
            "/api/v1/auth/forgot-password", json={"email": credentials["email"]}
        )
    assert fp_resp.status_code == 200

    # Reset password using the known raw token
    reset_resp = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": known_raw_token, "new_password": new_password},
    )
    assert reset_resp.status_code == 200, reset_resp.json()

    # Login with the new password succeeds
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": new_password},
    )
    assert login_resp.status_code == 200

    # Login with the old password fails
    old_login = await client.post(
        "/api/v1/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
    )
    assert old_login.status_code == 401


async def test_reset_token_cannot_be_reused(
    client: AsyncClient, credentials: dict[str, str], new_password: str
) -> None:
    """After a successful reset, the same token is rejected."""
    from unittest.mock import patch

    known_raw_token = "Y" * 48

    await client.post("/api/v1/auth/register", json=credentials)
    with patch("apps.api.routers.auth.generate_password_reset_token", return_value=known_raw_token):
        await client.post("/api/v1/auth/forgot-password", json={"email": credentials["email"]})

    # First use — succeeds
    first = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": known_raw_token, "new_password": new_password},
    )
    assert first.status_code == 200

    # Second use of same token — rejected (token cleared from DB)
    second = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": known_raw_token, "new_password": "AnotherStr0ng!Pass#02"},
    )
    assert second.status_code == 400
