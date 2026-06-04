"""
Integration tests — TD-1: Redis JTI blocklist read in JWTAuthMiddleware.

Verifies that an access token whose JTI has been written to the Redis blocklist
(by logout or token rotation) is rejected by the middleware with HTTP 401,
even though the JWT signature and expiry are still valid.

Requires a live PostgreSQL database and Redis instance.
Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh@localhost:5432/fdh_dev \\
    JWT_SECRET=test-integration-secret-32chars!! \\
    SECRET_KEY=test-integration-app-secret-32ch \\
    uv run pytest tests/integration/test_td1_blocklist.py -v

Milestone: M2-TD1
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
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(
    client: AsyncClient,
) -> tuple[str, str]:
    """Register a unique user, login, and return (access_token, refresh_cookie)."""
    suffix = os.urandom(4).hex()
    creds = {
        "email": f"td1-test-{suffix}@example.com",
        "password": "Str0ng!Pass#TD1test99",
        "full_name": "TD1 Tester",
        "workspace_name": f"TD1 WS {suffix}",
    }
    reg = await client.post("/api/v1/auth/register", json=creds)
    assert reg.status_code == 201, reg.text

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"], login.cookies["fdh_refresh"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_access_token_rejected_after_logout(client: AsyncClient) -> None:
    """
    TD-1 core scenario: after logout the access token JTI is written to the
    Redis blocklist.  Any subsequent request bearing that token must receive
    HTTP 401, not 204 or any other success code.

    Before TD-1: logout endpoint would return 204 again (token still valid).
    After TD-1:  JWTAuthMiddleware detects blocklisted JTI → auth_context=None
                 → require_authenticated raises 401.
    """
    access_token, _ = await _register_and_login(client)

    # Confirm the token works before logout
    pre_logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert pre_logout.status_code == 204, pre_logout.text

    # After logout, the same access token must be rejected by the middleware
    post_logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert post_logout.status_code == 401, (
        f"Expected 401 after logout (TD-1 blocklist), got {post_logout.status_code}. "
        "This indicates the Redis JTI blocklist read is not wired in JWTAuthMiddleware."
    )
    assert post_logout.json()["error"]["code"] == "UNAUTHORIZED"


async def test_access_token_rejected_after_token_rotation(client: AsyncClient) -> None:
    """
    Token rotation (POST /refresh) also blocklists the old access token JTI.
    The old access token must be rejected after rotation.
    """
    access_token, refresh_cookie = await _register_and_login(client)

    # Rotate tokens — old access token JTI goes into blocklist
    refresh_resp = await client.post(
        "/api/v1/auth/refresh",
        cookies={"fdh_refresh": refresh_cookie},
    )
    assert refresh_resp.status_code == 200, refresh_resp.text

    # Old access token must now be rejected
    old_token_resp = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert old_token_resp.status_code == 401, (
        f"Expected 401 for rotated-away access token, got {old_token_resp.status_code}. "
        "TD-1 blocklist check should reject tokens invalidated by rotation."
    )


async def test_new_token_works_after_logout_old_token_blocked(
    client: AsyncClient,
) -> None:
    """
    After logout + re-login, the new access token must work normally.
    This confirms the blocklist targets only the revoked JTI, not all tokens
    for the user.
    """
    suffix = os.urandom(4).hex()
    creds = {
        "email": f"td1-relogin-{suffix}@example.com",
        "password": "Str0ng!Pass#TD1relogin99",
        "full_name": "TD1 Relogin",
        "workspace_name": f"TD1 Relogin WS {suffix}",
    }
    await client.post("/api/v1/auth/register", json=creds)

    login1 = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    old_token = login1.json()["access_token"]

    # Logout — old token JTI blocklisted
    await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {old_token}"},
    )

    # Re-login — new token has a different JTI
    login2 = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    new_token = login2.json()["access_token"]

    # New token must work
    resp = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert resp.status_code == 204, (
        f"New token after re-login should work, got {resp.status_code}. "
        "TD-1 blocklist must be JTI-specific, not user-wide."
    )
