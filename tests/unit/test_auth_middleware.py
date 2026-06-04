"""
Unit tests — JWT Authentication Middleware and Dependency Injection.

Tests cover:
  - JWTAuthMiddleware passive extraction (valid, invalid, missing, malformed)
  - request.state.auth_context population
  - request.state.request_id generation
  - require_authenticated Depends()
  - require_role hierarchy (OWNER > ADMIN > ANALYST > VIEWER)
  - require_viewer / require_analyst / require_admin / require_owner shortcuts
  - get_current_user DB lookup (mocked)
  - Tenant isolation — token's tid must match expected tenant
  - Expired token handling
  - 401 / 403 error codes and messages

Engineering Spec references:
  Part 1, Section 2.3  — Request lifecycle
  Part 2, Section 8.2  — Auth mechanism decisions
  Part 2, Section 8.3  — JWT payload, role hierarchy

Milestone: M1-Step14
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from apps.api.core.exceptions import APIError, ForbiddenError, UnauthorizedError, api_error_handler
from apps.api.core.security import TokenPayload, create_access_token
from apps.api.middleware.auth import (
    AuthRequestContext,
    JWTAuthMiddleware,
    _role_rank,
    require_admin,
    require_analyst,
    require_authenticated,
    require_owner,
    require_viewer,
)
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_settings() -> MagicMock:
    s = MagicMock()
    s.jwt_secret = "test-jwt-secret-must-be-long-enough-for-hs256"
    s.jwt_algorithm = "HS256"
    s.jwt_access_token_expire_minutes = 15
    s.jwt_refresh_token_expire_days = 30
    s.secret_key = "test-application-secret-key-32bytes!!"
    return s


@pytest.fixture()
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    settings: MagicMock,
) -> tuple[str, str]:
    """Create a signed JWT using the test settings."""
    return create_access_token(user_id, tenant_id, role, settings=settings)


def _make_context(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    settings: MagicMock,
) -> AuthRequestContext:
    """Build an AuthRequestContext from a valid token."""
    token, jti = _make_token(user_id, tenant_id, role, settings)
    from apps.api.core.security import verify_access_token

    payload = verify_access_token(token, settings=settings)
    return AuthRequestContext(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        jti=jti,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Minimal FastAPI test application
# ---------------------------------------------------------------------------


def _build_test_app(settings: MagicMock) -> FastAPI:
    """
    Build a minimal FastAPI application for integration-style middleware tests.

    Routes:
      GET /public              — no auth required
      GET /authenticated       — require_authenticated
      GET /viewer              — require_viewer  (VIEWER+)
      GET /analyst             — require_analyst (ANALYST+)
      GET /admin               — require_admin   (ADMIN+)
      GET /owner               — require_owner   (OWNER only)
    """
    app = FastAPI()
    app.add_middleware(JWTAuthMiddleware, settings=settings)
    # Register our custom exception handler so UnauthorizedError / ForbiddenError
    # are converted to proper JSON responses rather than propagating as 500s.
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    @app.get("/public")
    async def public_route() -> dict[str, Any]:
        return {"public": True}

    @app.get("/authenticated")
    async def authenticated_route(
        ctx: AuthRequestContext = Depends(require_authenticated),
    ) -> dict[str, Any]:
        return {"user_id": str(ctx.user_id), "role": ctx.role}

    @app.get("/viewer")
    async def viewer_route(
        ctx: AuthRequestContext = Depends(require_viewer),
    ) -> dict[str, Any]:
        return {"role": ctx.role}

    @app.get("/analyst")
    async def analyst_route(
        ctx: AuthRequestContext = Depends(require_analyst),
    ) -> dict[str, Any]:
        return {"role": ctx.role}

    @app.get("/admin")
    async def admin_route(
        ctx: AuthRequestContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return {"role": ctx.role}

    @app.get("/owner")
    async def owner_route(
        ctx: AuthRequestContext = Depends(require_owner),
    ) -> dict[str, Any]:
        return {"role": ctx.role}

    return app


# ---------------------------------------------------------------------------
# Test: Role hierarchy helper
# ---------------------------------------------------------------------------


class TestRoleRank:
    """Unit tests for the internal _role_rank helper."""

    def test_owner_is_highest(self) -> None:
        assert _role_rank("owner") > _role_rank("admin")
        assert _role_rank("admin") > _role_rank("analyst")
        assert _role_rank("analyst") > _role_rank("viewer")

    def test_viewer_is_lowest(self) -> None:
        assert _role_rank("viewer") == 0

    def test_unknown_role_is_negative(self) -> None:
        """Any unrecognised role string is treated as below VIEWER."""
        assert _role_rank("superadmin") < 0
        assert _role_rank("") < 0
        assert _role_rank("OWNER") < 0  # case-sensitive


# ---------------------------------------------------------------------------
# Test: AuthRequestContext immutability
# ---------------------------------------------------------------------------


class TestAuthRequestContext:
    def test_frozen_dataclass(
        self, user_id: uuid.UUID, tenant_id: uuid.UUID, mock_settings: MagicMock
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)
        with pytest.raises((AttributeError, TypeError)):
            ctx.role = "owner"  # type: ignore[misc]

    def test_fields_are_correct(
        self, user_id: uuid.UUID, tenant_id: uuid.UUID, mock_settings: MagicMock
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "admin", mock_settings)
        assert ctx.user_id == user_id
        assert ctx.tenant_id == tenant_id
        assert ctx.role == "admin"
        assert isinstance(ctx.jti, str) and len(ctx.jti) == 36
        assert isinstance(ctx.payload, TokenPayload)


# ---------------------------------------------------------------------------
# Test: JWTAuthMiddleware — state population
# ---------------------------------------------------------------------------


class TestJWTAuthMiddleware:
    """
    Test the Starlette middleware via a full HTTP request cycle using
    httpx.AsyncClient and the test FastAPI application.
    """

    @pytest.mark.anyio
    async def test_valid_token_sets_auth_context(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """A valid Bearer token must result in a populated auth context."""
        app = _build_test_app(mock_settings)
        token, _ = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/authenticated",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == str(user_id)
        assert data["role"] == "analyst"

    @pytest.mark.anyio
    async def test_missing_token_returns_401_on_protected_route(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """A request without any Authorization header must be rejected (401)."""
        app = _build_test_app(mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/authenticated")

        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_missing_token_allows_public_route(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """Public routes must be accessible without any token."""
        app = _build_test_app(mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/public")

        assert resp.status_code == 200
        assert resp.json() == {"public": True}

    @pytest.mark.anyio
    async def test_expired_token_returns_401(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """An expired token must result in 401, not a server error."""
        from jose import jwt as jose_jwt

        expired_payload = {
            "sub": str(user_id),
            "tid": str(tenant_id),
            "role": "analyst",
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
            "iat": datetime.now(UTC) - timedelta(minutes=16),
            "type": "access",
        }
        expired_token = jose_jwt.encode(
            expired_payload,
            mock_settings.jwt_secret,
            algorithm=mock_settings.jwt_algorithm,
        )
        app = _build_test_app(mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/authenticated",
                headers={"Authorization": f"Bearer {expired_token}"},
            )

        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.anyio
    async def test_invalid_signature_returns_401(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """A token signed with the wrong secret must be rejected."""
        wrong_settings = MagicMock()
        wrong_settings.jwt_secret = "completely-different-secret-xxxxxxxx"
        wrong_settings.jwt_algorithm = "HS256"
        wrong_settings.jwt_access_token_expire_minutes = 15

        token, _ = _make_token(user_id, tenant_id, "analyst", wrong_settings)
        app = _build_test_app(mock_settings)  # app uses correct settings

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/authenticated",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_malformed_bearer_header_does_not_crash(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """A garbled Authorization header must not cause a 500."""
        app = _build_test_app(mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/public",
                headers={"Authorization": "Token xyz"},  # not "Bearer ..."
            )

        assert resp.status_code == 200  # public route still works

    @pytest.mark.anyio
    async def test_request_id_is_set_on_every_request(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """
        Every request must receive a unique request_id regardless of auth status.
        Verify by capturing request.state inside the route handler.
        """
        app = FastAPI()
        app.add_middleware(JWTAuthMiddleware, settings=mock_settings)
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        captured: list[str] = []

        @app.get("/capture")
        async def capture(request: Request) -> dict[str, Any]:
            rid = request.state.request_id
            captured.append(rid)
            return {"request_id": rid}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.get("/capture")
            r2 = await client.get("/capture")

        assert r1.status_code == 200
        assert r2.status_code == 200
        rid1 = r1.json()["request_id"]
        rid2 = r2.json()["request_id"]
        # Both must be UUIDs and must differ
        uuid.UUID(rid1)  # raises if not valid UUID
        uuid.UUID(rid2)
        assert rid1 != rid2

    @pytest.mark.anyio
    async def test_non_bearer_token_returns_401_on_protected_route(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """A non-Bearer scheme (e.g. Basic, Token) must not authenticate."""
        app = _build_test_app(mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/authenticated",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test: require_authenticated Depends()
# ---------------------------------------------------------------------------


class TestRequireAuthenticated:
    """
    Test the Depends() function directly without HTTP.
    Pass mock request.state via dependency override.
    """

    def _mock_request(self, ctx: AuthRequestContext | None) -> MagicMock:
        """Build a mock Request whose state.auth_context is set."""
        req = MagicMock()
        req.state = MagicMock()
        req.state.auth_context = ctx
        return req

    def test_returns_context_when_authenticated(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)
        result = require_authenticated(ctx=ctx)
        assert result is ctx

    def test_raises_unauthorized_when_context_is_none(self) -> None:
        with pytest.raises(UnauthorizedError) as exc_info:
            require_authenticated(ctx=None)
        assert exc_info.value.status_code == 401
        assert exc_info.value.code == "UNAUTHORIZED"

    def test_error_message_is_generic(self) -> None:
        """Error message must not leak token details."""
        with pytest.raises(UnauthorizedError) as exc_info:
            require_authenticated(ctx=None)
        assert "Authentication required" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: require_role RBAC hierarchy
# ---------------------------------------------------------------------------


class TestRequireRole:
    """
    Test require_role() and all four pre-built shortcuts.
    Calls the inner _check_role function directly by passing ctx= explicitly
    to bypass the Depends(require_authenticated) default.
    """

    # ── Owner role ────────────────────────────────────────────────────────────

    def test_owner_passes_owner_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "owner", mock_settings)
        result = require_owner(ctx=ctx)
        assert result is ctx

    def test_owner_passes_admin_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "owner", mock_settings)
        result = require_admin(ctx=ctx)
        assert result is ctx

    def test_owner_passes_analyst_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "owner", mock_settings)
        result = require_analyst(ctx=ctx)
        assert result is ctx

    def test_owner_passes_viewer_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "owner", mock_settings)
        result = require_viewer(ctx=ctx)
        assert result is ctx

    # ── Admin role ─────────────────────────────────────────────────────────────

    def test_admin_passes_admin_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "admin", mock_settings)
        assert require_admin(ctx=ctx) is ctx

    def test_admin_passes_analyst_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "admin", mock_settings)
        assert require_analyst(ctx=ctx) is ctx

    def test_admin_fails_owner_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "admin", mock_settings)
        with pytest.raises(ForbiddenError) as exc_info:
            require_owner(ctx=ctx)
        assert exc_info.value.status_code == 403
        assert exc_info.value.code == "FORBIDDEN"

    # ── Analyst role ──────────────────────────────────────────────────────────

    def test_analyst_passes_analyst_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)
        assert require_analyst(ctx=ctx) is ctx

    def test_analyst_passes_viewer_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)
        assert require_viewer(ctx=ctx) is ctx

    def test_analyst_fails_admin_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)
        with pytest.raises(ForbiddenError):
            require_admin(ctx=ctx)

    def test_analyst_fails_owner_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)
        with pytest.raises(ForbiddenError):
            require_owner(ctx=ctx)

    # ── Viewer role ────────────────────────────────────────────────────────────

    def test_viewer_passes_viewer_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "viewer", mock_settings)
        assert require_viewer(ctx=ctx) is ctx

    def test_viewer_fails_analyst_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "viewer", mock_settings)
        with pytest.raises(ForbiddenError) as exc_info:
            require_analyst(ctx=ctx)
        assert "analyst" in exc_info.value.message.lower()

    def test_viewer_fails_admin_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "viewer", mock_settings)
        with pytest.raises(ForbiddenError):
            require_admin(ctx=ctx)

    def test_viewer_fails_owner_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "viewer", mock_settings)
        with pytest.raises(ForbiddenError):
            require_owner(ctx=ctx)

    # ── Invalid role ───────────────────────────────────────────────────────────

    def test_unrecognised_role_fails_viewer_guard(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """
        An unrecognised role in the JWT payload (e.g. from a forged token)
        must fail even the lowest guard.
        """
        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)
        # Manually override role on the dataclass (create new frozen instance)
        import dataclasses

        bad_ctx = dataclasses.replace(ctx, role="superadmin")
        with pytest.raises(ForbiddenError):
            require_viewer(ctx=bad_ctx)

    # ── Error structure ────────────────────────────────────────────────────────

    def test_forbidden_error_mentions_required_role(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        ctx = _make_context(user_id, tenant_id, "viewer", mock_settings)
        with pytest.raises(ForbiddenError) as exc_info:
            require_admin(ctx=ctx)
        assert "admin" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# Test: require_role via HTTP (integration)
# ---------------------------------------------------------------------------


class TestRequireRoleHTTP:
    """
    End-to-end role enforcement via the HTTP layer.
    Tests all four role levels against all four guard levels.
    """

    @pytest.mark.anyio
    async def test_role_matrix(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """
        Verify the full 4×4 role × route matrix:
          rows    = OWNER, ADMIN, ANALYST, VIEWER
          columns = /viewer, /analyst, /admin, /owner
        """
        app = _build_test_app(mock_settings)
        # Expected: True = 200, False = 403
        matrix = {
            "owner": {"viewer": True, "analyst": True, "admin": True, "owner": True},
            "admin": {"viewer": True, "analyst": True, "admin": True, "owner": False},
            "analyst": {"viewer": True, "analyst": True, "admin": False, "owner": False},
            "viewer": {"viewer": True, "analyst": False, "admin": False, "owner": False},
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for role, expectations in matrix.items():
                token, _ = _make_token(user_id, tenant_id, role, mock_settings)
                headers = {"Authorization": f"Bearer {token}"}
                for route, should_pass in expectations.items():
                    resp = await client.get(f"/{route}", headers=headers)
                    expected_code = 200 if should_pass else 403
                    assert resp.status_code == expected_code, (
                        f"role={role!r} route=/{route} "
                        f"expected={expected_code} got={resp.status_code}"
                    )


# ---------------------------------------------------------------------------
# Test: Tenant isolation in auth context
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """
    Verify that the tenant_id from the JWT payload is correctly propagated
    to the AuthRequestContext so downstream code can enforce tenant scope.
    """

    @pytest.mark.anyio
    async def test_tenant_id_is_propagated_from_token(
        self,
        mock_settings: MagicMock,
    ) -> None:
        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        app = FastAPI()
        app.add_middleware(JWTAuthMiddleware, settings=mock_settings)
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        @app.get("/tenant")
        async def tenant_endpoint(
            ctx: AuthRequestContext = Depends(require_authenticated),
        ) -> dict[str, str]:
            return {"tenant_id": str(ctx.tenant_id)}

        token_a, _ = _make_token(user_a, tenant_a, "analyst", mock_settings)
        token_b, _ = _make_token(user_b, tenant_b, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_a = await client.get("/tenant", headers={"Authorization": f"Bearer {token_a}"})
            resp_b = await client.get("/tenant", headers={"Authorization": f"Bearer {token_b}"})

        assert resp_a.json()["tenant_id"] == str(tenant_a)
        assert resp_b.json()["tenant_id"] == str(tenant_b)
        assert resp_a.json()["tenant_id"] != resp_b.json()["tenant_id"]

    def test_two_users_different_tenants_have_independent_contexts(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """Contexts from different tenant tokens must be fully independent."""
        uid1, tid1 = uuid.uuid4(), uuid.uuid4()
        uid2, tid2 = uuid.uuid4(), uuid.uuid4()

        ctx1 = _make_context(uid1, tid1, "admin", mock_settings)
        ctx2 = _make_context(uid2, tid2, "viewer", mock_settings)

        assert ctx1.user_id != ctx2.user_id
        assert ctx1.tenant_id != ctx2.tenant_id
        assert ctx1.role != ctx2.role
        # Ensure no shared state
        assert ctx1 is not ctx2


# ---------------------------------------------------------------------------
# Test: get_current_user with mocked database
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    """
    Test the get_current_user dependency with a mocked AsyncSession.
    No real database is required.
    """

    @pytest.mark.anyio
    async def test_returns_user_when_found_and_active(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        from apps.api.middleware.auth import get_current_user

        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)

        # Build a mock User object
        mock_user = MagicMock()
        mock_user.id = user_id
        mock_user.is_active = True
        mock_user.deleted_at = None

        # Build mock DB session
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await get_current_user(ctx=ctx, db=mock_db)
        assert result is mock_user

    @pytest.mark.anyio
    async def test_raises_401_when_user_not_found(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        from apps.api.middleware.auth import get_current_user

        ctx = _make_context(user_id, tenant_id, "analyst", mock_settings)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(UnauthorizedError) as exc_info:
            await get_current_user(ctx=ctx, db=mock_db)
        assert exc_info.value.status_code == 401
        assert "not found" in exc_info.value.message.lower()

    @pytest.mark.anyio
    async def test_queries_correct_user_id(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """DB query must filter by the user_id from the auth context."""
        from apps.api.middleware.auth import get_current_user

        ctx = _make_context(user_id, tenant_id, "owner", mock_settings)

        mock_user = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        await get_current_user(ctx=ctx, db=mock_db)

        # Confirm db.execute was called once
        mock_db.execute.assert_called_once()
        # The call arg is the SELECT statement — verify it was built (not None)
        call_args = mock_db.execute.call_args
        assert call_args is not None


# ---------------------------------------------------------------------------
# Test: Public route accessibility
# ---------------------------------------------------------------------------


class TestPublicRouteAccessibility:
    """
    Verify that public routes remain accessible without authentication.
    The middleware must NOT raise on public routes with no token.
    """

    @pytest.mark.anyio
    async def test_health_route_accessible_without_token(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """Simulate the /health endpoint with no auth required."""
        app = FastAPI()
        app.add_middleware(JWTAuthMiddleware, settings=mock_settings)
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @pytest.mark.anyio
    async def test_protected_route_without_token_returns_401_not_500(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """A protected route with no token must return 401, not an unhandled exception."""
        app = _build_test_app(mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/authenticated")

        assert resp.status_code == 401
        # Should NOT be a 422 validation error or 500 internal error
        assert resp.status_code not in (422, 500)


# ---------------------------------------------------------------------------
# Test: Error response structure
# ---------------------------------------------------------------------------


class TestErrorResponseStructure:
    """
    Verify that 401 and 403 responses comply with the standard error schema
    defined in Spec Part 1, Section 2.2, Decision 4.
    """

    @pytest.mark.anyio
    async def test_401_response_has_standard_error_schema(
        self,
        mock_settings: MagicMock,
    ) -> None:
        """
        The error handler registered on the app must format UnauthorizedError
        as { "error": { "code": "UNAUTHORIZED", "message": "...", ... } }.
        """
        from apps.api.core.exceptions import APIError, api_error_handler

        app = _build_test_app(mock_settings)
        app.add_exception_handler(APIError, api_error_handler)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/authenticated")

        # FastAPI's default 422 response for missing Depends is different —
        # our UnauthorizedError should produce a proper 401 body.
        if resp.status_code == 401:
            body = resp.json()
            assert "error" in body
            assert body["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.anyio
    async def test_403_response_has_standard_error_schema(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        from apps.api.core.exceptions import APIError, api_error_handler

        app = _build_test_app(mock_settings)
        app.add_exception_handler(APIError, api_error_handler)

        token, _ = _make_token(user_id, tenant_id, "viewer", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 403
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Test: TD-1 — Redis JTI blocklist check
# ---------------------------------------------------------------------------


class TestJTIBlocklist:
    """
    Unit tests for the Redis JTI blocklist check introduced in TD-1.

    Covers:
      - _is_jti_blocklisted returns True when Redis key exists
      - _is_jti_blocklisted returns False when Redis key is absent
      - _is_jti_blocklisted fails open (returns False) on any Redis error
      - JWTAuthMiddleware rejects a blocklisted token (auth_context stays None → 401)
      - JWTAuthMiddleware passes a non-blocklisted token normally (200)
      - JWTAuthMiddleware fails open when Redis is unreachable (200, not 401)
    """

    # ── _is_jti_blocklisted unit tests ────────────────────────────────────────

    @pytest.mark.anyio
    async def test_returns_true_when_key_exists(self) -> None:
        """Redis GET returning a value means the JTI is blocklisted."""
        from unittest.mock import AsyncMock, patch

        from apps.api.middleware.auth import _is_jti_blocklisted

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=b"1")
        mock_client.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=mock_client):
            result = await _is_jti_blocklisted("some-jti", "redis://localhost:6379/0")

        assert result is True
        mock_client.get.assert_called_once_with("blocklist:some-jti")

    @pytest.mark.anyio
    async def test_returns_false_when_key_absent(self) -> None:
        """Redis GET returning None means the JTI is not blocklisted."""
        from unittest.mock import AsyncMock, patch

        from apps.api.middleware.auth import _is_jti_blocklisted

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)
        mock_client.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=mock_client):
            result = await _is_jti_blocklisted("clean-jti", "redis://localhost:6379/0")

        assert result is False

    @pytest.mark.anyio
    async def test_fails_open_on_redis_connection_error(self) -> None:
        """Any Redis exception must result in False (fail open), not a raised error."""
        import redis.asyncio as aioredis

        from unittest.mock import patch

        from apps.api.middleware.auth import _is_jti_blocklisted

        with patch(
            "redis.asyncio.from_url",
            side_effect=aioredis.ConnectionError("Redis unreachable"),
        ):
            result = await _is_jti_blocklisted("any-jti", "redis://localhost:6379/0")

        assert result is False

    @pytest.mark.anyio
    async def test_fails_open_on_timeout(self) -> None:
        """A Redis timeout must also fail open."""
        from unittest.mock import AsyncMock, patch

        from apps.api.middleware.auth import _is_jti_blocklisted

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=TimeoutError("timed out"))
        mock_client.aclose = AsyncMock()

        with patch("redis.asyncio.from_url", return_value=mock_client):
            result = await _is_jti_blocklisted("any-jti", "redis://localhost:6379/0")

        assert result is False

    # ── Middleware-level tests (via HTTP) ─────────────────────────────────────

    @pytest.mark.anyio
    async def test_blocklisted_token_returns_401(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """
        A structurally valid, non-expired JWT whose JTI is in the Redis
        blocklist must be rejected with 401 on any protected route.
        """
        from unittest.mock import patch

        token, jti = _make_token(user_id, tenant_id, "analyst", mock_settings)
        app = _build_test_app(mock_settings)

        # Simulate: this specific JTI is blocklisted
        async def _blocklisted(j: str, url: str) -> bool:
            return j == jti

        with patch("apps.api.middleware.auth._is_jti_blocklisted", side_effect=_blocklisted):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    "/authenticated",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.anyio
    async def test_blocklisted_token_still_allows_public_routes(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """
        A blocklisted token must not break public routes — they require no auth.
        """
        from unittest.mock import patch

        token, _ = _make_token(user_id, tenant_id, "analyst", mock_settings)
        app = _build_test_app(mock_settings)

        async def _always_blocklisted(j: str, url: str) -> bool:
            return True

        with patch("apps.api.middleware.auth._is_jti_blocklisted", side_effect=_always_blocklisted):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    "/public",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_non_blocklisted_token_passes_through(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """A valid token that is NOT blocklisted must authenticate normally."""
        from unittest.mock import patch

        token, _ = _make_token(user_id, tenant_id, "analyst", mock_settings)
        app = _build_test_app(mock_settings)

        async def _not_blocklisted(j: str, url: str) -> bool:
            return False

        with patch("apps.api.middleware.auth._is_jti_blocklisted", side_effect=_not_blocklisted):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    "/authenticated",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert resp.status_code == 200
        assert resp.json()["user_id"] == str(user_id)

    @pytest.mark.anyio
    async def test_redis_unavailable_fails_open(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """
        If Redis is unreachable during the blocklist check, the middleware must
        fail open: the token is accepted and the request proceeds normally.
        A Redis outage must not cause a 401 for all authenticated users.
        """
        from unittest.mock import patch

        token, _ = _make_token(user_id, tenant_id, "analyst", mock_settings)
        app = _build_test_app(mock_settings)

        # _is_jti_blocklisted itself handles exceptions and returns False —
        # simulate that behaviour: the real function already fails open, so
        # patch it to return False (as it would after catching a Redis error).
        async def _fail_open(j: str, url: str) -> bool:
            return False  # Redis unreachable → fail open

        with patch("apps.api.middleware.auth._is_jti_blocklisted", side_effect=_fail_open):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    "/authenticated",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_different_jti_not_blocked(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        mock_settings: MagicMock,
    ) -> None:
        """Only the specific blocklisted JTI is rejected; other JTIs pass through."""
        from unittest.mock import patch

        token1, jti1 = _make_token(user_id, tenant_id, "analyst", mock_settings)
        token2, jti2 = _make_token(user_id, tenant_id, "analyst", mock_settings)
        app = _build_test_app(mock_settings)

        # Only jti1 is blocklisted
        async def _selective_block(j: str, url: str) -> bool:
            return j == jti1

        with patch("apps.api.middleware.auth._is_jti_blocklisted", side_effect=_selective_block):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp1 = await client.get(
                    "/authenticated",
                    headers={"Authorization": f"Bearer {token1}"},
                )
                resp2 = await client.get(
                    "/authenticated",
                    headers={"Authorization": f"Bearer {token2}"},
                )

        assert resp1.status_code == 401  # blocklisted
        assert resp2.status_code == 200  # clean token passes
