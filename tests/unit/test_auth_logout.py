"""
Unit tests — POST /api/v1/auth/logout.

Strategy
--------
The logout endpoint requires a valid JWT (``require_authenticated``).
Rather than running the full JWTAuthMiddleware stack, the test app overrides
the ``_get_auth_context`` dependency to inject a pre-built AuthRequestContext
for authenticated scenarios and ``None`` for unauthenticated scenarios.

All database and Redis operations are mocked so tests run without
infrastructure.

What is mocked
--------------
- ``_get_auth_context``     — injects a fixed AuthRequestContext or None
- ``AuthRepository``        — all repo methods return AsyncMock objects
- ``_blocklist_jti``        — Redis write; AsyncMock, fail-open tested

What is NOT mocked (runs real code)
-------------------------------------
- ``require_authenticated`` dependency (reads from _get_auth_context override)
- HTTP 204 status assertion
- Cookie deletion header assertion
- AuditLog call argument assertions

Milestone: M1-Step21
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.middleware.auth import AuthRequestContext, _get_auth_context
from apps.api.routers.auth import router

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_FIXED_USER_ID = uuid.uuid4()
_FIXED_TENANT_ID = uuid.uuid4()
_FIXED_JTI = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(*, exp_delta_minutes: int = 10) -> AuthRequestContext:
    """Build an AuthRequestContext with a mock payload whose exp is in the future."""
    payload = MagicMock()
    payload.exp = datetime.now(UTC) + timedelta(minutes=exp_delta_minutes)

    return AuthRequestContext(
        user_id=_FIXED_USER_ID,
        tenant_id=_FIXED_TENANT_ID,
        role="owner",
        jti=_FIXED_JTI,
        payload=payload,
    )


def _make_test_app(ctx: AuthRequestContext | None) -> FastAPI:
    """
    Build a minimal FastAPI app with the auth router.

    ``_get_auth_context`` is overridden to return ``ctx`` (an
    AuthRequestContext for authenticated scenarios, None for anonymous).
    ``get_db`` is overridden to yield an AsyncMock session.
    """
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _override_db() -> Any:
        yield AsyncMock()

    def _override_auth() -> AuthRequestContext | None:
        return ctx

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_get_auth_context] = _override_auth
    app.include_router(router)
    return app


@pytest.fixture()
def ctx() -> AuthRequestContext:
    return _make_ctx()


@pytest.fixture()
def mock_repo(ctx: AuthRequestContext) -> AsyncMock:
    """Happy-path repository mock: refresh token found and not yet revoked."""
    stored_token = MagicMock()
    stored_token.revoked_at = None   # not yet revoked
    stored_token.jti = ctx.jti

    repo = AsyncMock()
    repo.get_refresh_token_by_jti.return_value = stored_token
    repo.revoke_refresh_token.return_value = None
    repo.create_audit_log.return_value = None
    return repo


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestLogoutSuccess:
    async def test_returns_204(self, ctx: AuthRequestContext, mock_repo: AsyncMock) -> None:
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/logout")

        assert resp.status_code == 204

    async def test_no_response_body(self, ctx: AuthRequestContext, mock_repo: AsyncMock) -> None:
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/logout")

        assert resp.content == b""

    async def test_refresh_cookie_cleared(
        self, ctx: AuthRequestContext, mock_repo: AsyncMock
    ) -> None:
        """The Set-Cookie header should clear fdh_refresh (max-age=0 / deletion)."""
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/logout")

        set_cookie = resp.headers.get("set-cookie", "")
        assert "fdh_refresh" in set_cookie

    async def test_refresh_token_revoked(
        self, ctx: AuthRequestContext, mock_repo: AsyncMock
    ) -> None:
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        mock_repo.revoke_refresh_token.assert_awaited_once()

    async def test_jti_looked_up_by_ctx_jti(
        self, ctx: AuthRequestContext, mock_repo: AsyncMock
    ) -> None:
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        mock_repo.get_refresh_token_by_jti.assert_awaited_once_with(_FIXED_JTI)

    async def test_jti_blocklisted(self, ctx: AuthRequestContext, mock_repo: AsyncMock) -> None:
        mock_blocklist = AsyncMock()
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=mock_blocklist),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        mock_blocklist.assert_awaited_once()
        call_kwargs = mock_blocklist.call_args.kwargs
        assert call_kwargs["jti"] == _FIXED_JTI
        # TTL should be > 0 (token still has ~10 min left per fixture)
        assert call_kwargs["ttl_seconds"] > 0

    async def test_audit_log_action_is_user_logout(
        self, ctx: AuthRequestContext, mock_repo: AsyncMock
    ) -> None:
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        audit_kwargs = mock_repo.create_audit_log.call_args.kwargs
        assert audit_kwargs["action"] == "user.logout"
        assert audit_kwargs["entity_type"] == "user"
        assert audit_kwargs["user_id"] == _FIXED_USER_ID
        assert audit_kwargs["tenant_id"] == _FIXED_TENANT_ID

    async def test_ttl_zero_when_token_expired(self, mock_repo: AsyncMock) -> None:
        """When the access token's exp is in the past, TTL is clamped to 0."""
        expired_ctx = _make_ctx(exp_delta_minutes=-5)
        mock_blocklist = AsyncMock()
        app = _make_test_app(expired_ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=mock_blocklist),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        ttl = mock_blocklist.call_args.kwargs["ttl_seconds"]
        assert ttl == 0


# ---------------------------------------------------------------------------
# Tests — unauthenticated request (401)
# ---------------------------------------------------------------------------


class TestLogoutUnauthenticated:
    async def test_no_token_returns_401(self) -> None:
        app = _make_test_app(None)  # _get_auth_context returns None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/logout")

        assert resp.status_code == 401

    async def test_no_token_error_code(self) -> None:
        app = _make_test_app(None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/logout")

        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_no_token_no_db_writes(self) -> None:
        app = _make_test_app(None)
        repo = AsyncMock()
        with patch("apps.api.routers.auth.AuthRepository", return_value=repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        repo.get_refresh_token_by_jti.assert_not_awaited()
        repo.create_audit_log.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — idempotency (already-revoked refresh token)
# ---------------------------------------------------------------------------


class TestLogoutIdempotent:
    async def test_already_revoked_token_still_returns_204(
        self, ctx: AuthRequestContext
    ) -> None:
        """
        If the refresh token is already revoked (e.g. double-logout), the
        endpoint must still succeed: jti is blocklisted and audit log written.
        """
        already_revoked = MagicMock()
        already_revoked.revoked_at = datetime.now(UTC)  # already revoked

        repo = AsyncMock()
        repo.get_refresh_token_by_jti.return_value = already_revoked
        repo.create_audit_log.return_value = None

        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/logout")

        assert resp.status_code == 204
        # revoke_refresh_token was NOT called again (already revoked)
        repo.revoke_refresh_token.assert_not_awaited()

    async def test_refresh_token_not_found_still_returns_204(
        self, ctx: AuthRequestContext
    ) -> None:
        """
        If no refresh token exists for this jti (already cleaned up or never
        issued), logout still succeeds — the access token jti is blocklisted.
        """
        repo = AsyncMock()
        repo.get_refresh_token_by_jti.return_value = None  # not found
        repo.create_audit_log.return_value = None

        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/logout")

        assert resp.status_code == 204
        repo.revoke_refresh_token.assert_not_awaited()

    async def test_blocklist_called_even_when_token_not_found(
        self, ctx: AuthRequestContext
    ) -> None:
        repo = AsyncMock()
        repo.get_refresh_token_by_jti.return_value = None
        repo.create_audit_log.return_value = None

        mock_blocklist = AsyncMock()
        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth._blocklist_jti", new=mock_blocklist),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        mock_blocklist.assert_awaited_once()

    async def test_audit_log_written_even_when_token_not_found(
        self, ctx: AuthRequestContext
    ) -> None:
        repo = AsyncMock()
        repo.get_refresh_token_by_jti.return_value = None
        repo.create_audit_log.return_value = None

        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/auth/logout")

        repo.create_audit_log.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests — Redis fail-open
# ---------------------------------------------------------------------------


class TestLogoutRedisFailOpen:
    async def test_redis_failure_does_not_block_logout(
        self, ctx: AuthRequestContext, mock_repo: AsyncMock
    ) -> None:
        """
        A Redis outage must not prevent logout. The DB revoked_at flag is the
        authoritative revocation; the blocklist is a fast-path cache.
        """

        async def _raises(*_: object, **__: object) -> None:
            raise OSError("Redis unreachable")

        app = _make_test_app(ctx)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth._blocklist_jti", new=_raises),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/logout")

        # Logout succeeded despite Redis failure
        assert resp.status_code == 204
        # Refresh token was still revoked in the DB
        mock_repo.revoke_refresh_token.assert_awaited_once()
        # Audit log was still written
        mock_repo.create_audit_log.assert_awaited_once()
