"""
Unit tests — Rate Limiter Middleware (M1-Step16).

Tests cover:
  - _should_skip path filter (health, OpenAPI endpoints)
  - _resolve_limit tier selection (anonymous vs authenticated)
  - 429 response structure (status code, headers, body schema)
  - Allowed request: correct X-RateLimit-* headers attached
  - Blocked request: 429 returned, call_next NOT invoked
  - Unauthenticated: rate limited per IP address
  - Authenticated: rate limited per user_id (free tier in M1)
  - Redis unavailable: fail open — request passes without rate limiting
  - Redis eval error: fail open — request passes without rate limiting
  - Skip list: health endpoints pass without Redis interaction
  - Different users have independent counters (tenant isolation)
  - Lua script result parsing (remaining, reset_ms from mock)

Engineering Spec references:
  Part 3, Section 11.2, Decision 2  — Rate limiting tiers + headers

Milestone: M1-Step16
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.core.security import create_access_token
from apps.api.middleware.auth import JWTAuthMiddleware
from apps.api.middleware.rate_limit import (
    RateLimitMiddleware,
    _resolve_limit,
    _should_skip,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_settings() -> MagicMock:
    s = MagicMock()
    s.jwt_secret = "test-jwt-secret-must-be-long-enough-for-hs256"
    s.jwt_algorithm = "HS256"
    s.jwt_access_token_expire_minutes = 15
    s.jwt_refresh_token_expire_days = 30
    s.secret_key = "test-app-secret-key-32bytes-xxxxx"
    s.redis_url = "redis://localhost:6379/0"
    s.rate_limit_unauthenticated = 20
    s.rate_limit_free_tier = 60
    s.rate_limit_pro_tier = 300
    s.rate_limit_enterprise_tier = 1000
    return s


@pytest.fixture()
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_token(uid: uuid.UUID, tid: uuid.UUID, role: str, settings: MagicMock) -> str:
    token, _ = create_access_token(uid, tid, role, settings=settings)
    return token


def _make_redis(remaining: int = 50, reset_ms: float = 1_700_000_060_000.0) -> MagicMock:
    """
    Build a mock Redis client whose eval() returns a controlled result.

    remaining >= 0 → request allowed
    remaining = -1 → request blocked
    """
    redis_mock = AsyncMock()
    redis_mock.eval = AsyncMock(return_value=[remaining, reset_ms])
    return redis_mock


def _build_app(settings: MagicMock, redis_mock: MagicMock | None) -> FastAPI:
    """
    Minimal FastAPI app with the full three-middleware stack:
      JWTAuthMiddleware → AuditMiddleware (skipped here) → RateLimitMiddleware
    The AuditMiddleware is omitted for simplicity; AuditMiddleware tests are
    covered separately in test_audit_middleware.py.
    """
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, settings=settings, redis_client=redis_mock)
    app.add_middleware(JWTAuthMiddleware, settings=settings)
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    @app.get("/api/v1/resource")
    async def resource() -> dict[str, str]:
        return {"data": "ok"}

    @app.post("/api/v1/jobs")
    async def create_job() -> dict[str, str]:
        return {"job_id": "123"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict[str, str]:
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Test: _should_skip path filter
# ---------------------------------------------------------------------------


class TestShouldSkip:
    def test_health_skipped(self) -> None:
        assert _should_skip("/health") is True

    def test_health_ready_skipped(self) -> None:
        assert _should_skip("/health/ready") is True

    def test_health_detailed_skipped(self) -> None:
        assert _should_skip("/health/detailed") is True

    def test_docs_prefix_skipped(self) -> None:
        assert _should_skip("/api/v1/docs") is True
        assert _should_skip("/api/v1/docs/anything") is True

    def test_redoc_skipped(self) -> None:
        assert _should_skip("/api/v1/redoc") is True

    def test_openapi_json_skipped(self) -> None:
        assert _should_skip("/api/v1/openapi.json") is True

    def test_api_resource_not_skipped(self) -> None:
        assert _should_skip("/api/v1/resource") is False

    def test_api_jobs_not_skipped(self) -> None:
        assert _should_skip("/api/v1/jobs") is False

    def test_auth_not_skipped(self) -> None:
        assert _should_skip("/api/v1/auth/login") is False


# ---------------------------------------------------------------------------
# Test: _resolve_limit tier selection
# ---------------------------------------------------------------------------


class TestResolveLimit:
    def test_anonymous_gets_unauthenticated_limit(self, mock_settings: MagicMock) -> None:
        limit, tier = _resolve_limit(None, mock_settings)
        assert limit == mock_settings.rate_limit_unauthenticated
        assert tier == "anon"

    def test_authenticated_gets_free_tier_limit(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        ctx = MagicMock()
        ctx.user_id = user_id
        ctx.tenant_id = tenant_id
        limit, tier = _resolve_limit(ctx, mock_settings)
        assert limit == mock_settings.rate_limit_free_tier
        assert tier == "auth"

    def test_free_tier_limit_value(self, mock_settings: MagicMock) -> None:
        ctx = MagicMock()
        limit, _ = _resolve_limit(ctx, mock_settings)
        assert limit == 60

    def test_unauthenticated_limit_value(self, mock_settings: MagicMock) -> None:
        limit, _ = _resolve_limit(None, mock_settings)
        assert limit == 20


# ---------------------------------------------------------------------------
# Test: allowed request — headers set correctly
# ---------------------------------------------------------------------------


class TestAllowedRequest:
    @pytest.mark.anyio
    async def test_headers_set_on_allowed_request(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """X-RateLimit-* headers must be present on every allowed response."""
        redis_mock = _make_redis(remaining=45, reset_ms=1_700_000_060_000.0)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers
        assert "X-RateLimit-Reset" in resp.headers

    @pytest.mark.anyio
    async def test_ratelimit_limit_header_matches_tier(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """X-RateLimit-Limit must equal the configured tier limit."""
        redis_mock = _make_redis(remaining=59)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.headers["X-RateLimit-Limit"] == "60"  # free tier

    @pytest.mark.anyio
    async def test_ratelimit_remaining_header_reflects_slots_left(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        redis_mock = _make_redis(remaining=42)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.headers["X-RateLimit-Remaining"] == "42"

    @pytest.mark.anyio
    async def test_anonymous_request_uses_unauthenticated_limit(
        self, mock_settings: MagicMock
    ) -> None:
        """Unauthenticated requests use the lower unauthenticated limit."""
        redis_mock = _make_redis(remaining=19)
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/resource")

        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "20"  # unauthenticated limit


# ---------------------------------------------------------------------------
# Test: blocked request — 429 response
# ---------------------------------------------------------------------------


class TestBlockedRequest:
    @pytest.mark.anyio
    async def test_429_returned_when_limit_exceeded(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """When remaining = -1, middleware must return 429."""
        redis_mock = _make_redis(remaining=-1, reset_ms=1_700_000_060_000.0)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 429

    @pytest.mark.anyio
    async def test_429_body_follows_standard_error_schema(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """
        429 body must follow standard error envelope
        (Spec Part 1, §2.2, Decision 4).
        """
        redis_mock = _make_redis(remaining=-1)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert "message" in body["error"]
        assert "details" in body["error"]
        assert "limit" in body["error"]["details"]
        assert "reset_at" in body["error"]["details"]
        assert "retry_after_seconds" in body["error"]["details"]

    @pytest.mark.anyio
    async def test_429_headers_present(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """429 must include all three X-RateLimit-* headers plus Retry-After."""
        redis_mock = _make_redis(remaining=-1, reset_ms=1_700_000_060_000.0)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.headers["X-RateLimit-Limit"] == "60"
        assert resp.headers["X-RateLimit-Remaining"] == "0"
        assert "X-RateLimit-Reset" in resp.headers
        assert "Retry-After" in resp.headers
        assert "X-Request-ID" in resp.headers

    @pytest.mark.anyio
    async def test_call_next_not_invoked_when_blocked(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """
        When rate limited, the middleware must short-circuit — the route
        handler must NOT be called.
        """
        redis_mock = _make_redis(remaining=-1)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        route_called = []

        @app.get("/api/v1/probe")
        async def probe() -> dict[str, str]:
            route_called.append(True)
            return {"called": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/probe",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 429
        assert len(route_called) == 0, "Route handler must NOT be called when rate limited"

    @pytest.mark.anyio
    async def test_anonymous_429_uses_unauthenticated_limit(self, mock_settings: MagicMock) -> None:
        redis_mock = _make_redis(remaining=-1)
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/resource")

        assert resp.status_code == 429
        assert resp.headers["X-RateLimit-Limit"] == "20"


# ---------------------------------------------------------------------------
# Test: Redis unavailable — fail open
# ---------------------------------------------------------------------------


class TestRedisFailOpen:
    @pytest.mark.anyio
    async def test_redis_connection_error_does_not_block_request(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """When Redis eval raises, the request must still succeed (fail open)."""
        redis_mock = AsyncMock()
        redis_mock.eval = AsyncMock(side_effect=ConnectionError("Redis down"))
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_redis_unavailable_no_rate_limit_headers(self, mock_settings: MagicMock) -> None:
        """When Redis is down, X-RateLimit-* headers must NOT be set (no enforcement)."""
        redis_mock = AsyncMock()
        redis_mock.eval = AsyncMock(side_effect=OSError("Redis timeout"))
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/resource")

        assert resp.status_code == 200
        assert "X-RateLimit-Limit" not in resp.headers

    @pytest.mark.anyio
    async def test_redis_none_client_fails_open(self, mock_settings: MagicMock) -> None:
        """With redis_client=None and no Redis server, requests must pass through."""
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, settings=mock_settings, redis_client=None)
        app.add_middleware(JWTAuthMiddleware, settings=mock_settings)
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        @app.get("/api/v1/resource")
        async def resource() -> dict[str, str]:
            return {"data": "ok"}

        with patch(
            "apps.api.middleware.rate_limit.aioredis.from_url",
            side_effect=Exception("cannot connect"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/resource")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Skip list — no Redis call on skipped paths
# ---------------------------------------------------------------------------


class TestSkippedPaths:
    @pytest.mark.anyio
    async def test_health_passes_without_redis(self, mock_settings: MagicMock) -> None:
        """Health endpoints must never trigger a Redis call."""
        redis_mock = _make_redis()
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        redis_mock.eval.assert_not_called()

    @pytest.mark.anyio
    async def test_health_ready_not_rate_limited(self, mock_settings: MagicMock) -> None:
        redis_mock = _make_redis()
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health/ready")

        assert resp.status_code == 200
        redis_mock.eval.assert_not_called()

    @pytest.mark.anyio
    async def test_no_ratelimit_headers_on_skipped_paths(self, mock_settings: MagicMock) -> None:
        """Skipped paths must not carry X-RateLimit-* headers."""
        redis_mock = _make_redis()
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")

        assert "X-RateLimit-Limit" not in resp.headers


# ---------------------------------------------------------------------------
# Test: Per-user isolation (tenant-aware)
# ---------------------------------------------------------------------------


class TestPerUserIsolation:
    @pytest.mark.anyio
    async def test_different_users_use_different_redis_keys(self, mock_settings: MagicMock) -> None:
        """
        Each user must have an independent rate limit counter.
        Verified by checking that Redis eval is called with different keys.
        """
        uid_a, tid_a = uuid.uuid4(), uuid.uuid4()
        uid_b, tid_b = uuid.uuid4(), uuid.uuid4()
        tok_a = _make_token(uid_a, tid_a, "analyst", mock_settings)
        tok_b = _make_token(uid_b, tid_b, "viewer", mock_settings)

        redis_mock = _make_redis(remaining=50)
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {tok_a}"},
            )
            await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {tok_b}"},
            )

        assert redis_mock.eval.call_count == 2
        # Extract the Redis key from each call (KEYS[1] = positional arg index 2)
        call_args_list = redis_mock.eval.call_args_list
        key_a = call_args_list[0][0][2]  # eval(script, 1, KEY, ...)
        key_b = call_args_list[1][0][2]

        assert key_a != key_b, "Different users must have different Redis keys"
        assert str(uid_a) in key_a, f"Key A should contain user_id_a: {key_a}"
        assert str(uid_b) in key_b, f"Key B should contain user_id_b: {key_b}"

    @pytest.mark.anyio
    async def test_unauthenticated_uses_ip_key(self, mock_settings: MagicMock) -> None:
        """Unauthenticated requests must be keyed by IP, not user ID."""
        redis_mock = _make_redis(remaining=18)
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/api/v1/resource")

        redis_mock.eval.assert_called_once()
        key = redis_mock.eval.call_args[0][2]
        assert key.startswith("ratelimit:ip:"), f"Expected ip key, got: {key}"

    @pytest.mark.anyio
    async def test_authenticated_uses_user_key(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """Authenticated requests must be keyed by user_id."""
        redis_mock = _make_redis(remaining=55)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        key = redis_mock.eval.call_args[0][2]
        assert key == f"ratelimit:user:{user_id}"

    @pytest.mark.anyio
    async def test_x_forwarded_for_used_for_ip_key(self, mock_settings: MagicMock) -> None:
        """X-Forwarded-For first entry must be used as the IP for the rate limit key."""
        redis_mock = _make_redis(remaining=15)
        app = _build_app(mock_settings, redis_mock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get(
                "/api/v1/resource",
                headers={"X-Forwarded-For": "198.51.100.1, 10.0.0.1"},
            )

        key = redis_mock.eval.call_args[0][2]
        assert key == "ratelimit:ip:198.51.100.1"


# ---------------------------------------------------------------------------
# Test: Lua script argument passing
# ---------------------------------------------------------------------------


class TestLuaScriptArgs:
    @pytest.mark.anyio
    async def test_lua_script_called_with_correct_window(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """
        The Lua script must receive window_ms=60000 (1 minute sliding window).
        """
        redis_mock = _make_redis(remaining=50)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        call_args = redis_mock.eval.call_args[0]
        # eval(script, num_keys, key, now_ms, window_ms, limit, member)
        # indices:  0       1      2    3       4         5      6
        window_ms_arg = call_args[4]
        assert window_ms_arg == "60000", f"Expected 60000, got {window_ms_arg}"

    @pytest.mark.anyio
    async def test_lua_script_called_with_correct_limit(
        self, mock_settings: MagicMock, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """The limit passed to Lua must match the configured free tier (60)."""
        redis_mock = _make_redis(remaining=50)
        app = _build_app(mock_settings, redis_mock)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get(
                "/api/v1/resource",
                headers={"Authorization": f"Bearer {token}"},
            )

        call_args = redis_mock.eval.call_args[0]
        limit_arg = call_args[5]
        assert limit_arg == "60"
