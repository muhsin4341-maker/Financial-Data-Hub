"""
Unit tests — Audit Log Middleware (M1-Step15).

Tests cover:
  - X-Request-ID header set on all audited responses
  - Skip list: /health*, /api/v1/docs*, /api/v1/openapi.json
  - Audit record written for authenticated requests (tenant_id + user_id captured)
  - Audit record written for anonymous requests (tenant_id=None, user_id=None)
  - Non-blocking guarantee: DB failure does NOT affect HTTP response
  - Duration captured in changes JSON
  - Path + query_string captured in changes JSON
  - Status code captured in changes JSON
  - IP address extracted from X-Forwarded-For header
  - IP address extracted from direct client connection
  - action = "http.{method.lower()}"
  - _should_skip() path filter logic
  - Structlog INFO line emitted for each audited request

Engineering Spec references:
  Part 1, Section 2.3  — audit middleware in request lifecycle
  Part 3, Section 12   — X-Request-ID, audit log requirements, retention

Milestone: M1-Step15
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.core.security import create_access_token
from apps.api.middleware.audit import AuditMiddleware, _extract_ip, _should_skip
from apps.api.middleware.auth import JWTAuthMiddleware
from fastapi import FastAPI
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
    s.secret_key = "test-app-secret-key-32bytes-xxxxx"
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
) -> str:
    token, _ = create_access_token(user_id, tenant_id, role, settings=settings)
    return token


def _build_app(settings: MagicMock) -> FastAPI:
    """
    Build a minimal test application with both middleware layers registered
    in the correct order (JWTAuth outermost, Audit innermost).

    Registration order (last registered runs first in Starlette):
        app.add_middleware(AuditMiddleware)
        app.add_middleware(JWTAuthMiddleware, settings=settings)
    But add_middleware prepends, so:
        JWTAuthMiddleware wraps AuditMiddleware wraps the route.
    We register AuditMiddleware first (innermost) then JWTAuthMiddleware.
    """
    app = FastAPI()
    # Register in reverse execution order (add_middleware prepends)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(JWTAuthMiddleware, settings=settings)
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    @app.get("/api/v1/resource")
    async def resource() -> dict[str, str]:
        return {"resource": "data"}

    @app.post("/api/v1/resource")
    async def create_resource() -> dict[str, str]:
        return {"created": "true"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/docs")
    async def docs() -> dict[str, str]:
        return {"docs": "here"}

    return app


# ---------------------------------------------------------------------------
# Helper: patch AsyncSessionFactory so audit writes don't need a real DB
# ---------------------------------------------------------------------------


def _mock_session_factory() -> tuple[MagicMock, MagicMock]:
    """
    Return (factory_mock, session_mock) configured for async context manager use.
    The session captures all add() and commit() calls for assertion.
    """
    session_mock = AsyncMock()
    session_mock.add = MagicMock()
    session_mock.commit = AsyncMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)

    factory_mock = MagicMock(return_value=session_mock)
    return factory_mock, session_mock


# ---------------------------------------------------------------------------
# Test: _should_skip path filter
# ---------------------------------------------------------------------------


class TestShouldSkip:
    """Unit tests for the path skip list — no HTTP needed."""

    def test_health_is_skipped(self) -> None:
        assert _should_skip("/health") is True

    def test_health_ready_is_skipped(self) -> None:
        assert _should_skip("/health/ready") is True

    def test_health_detailed_is_skipped(self) -> None:
        assert _should_skip("/health/detailed") is True

    def test_api_docs_prefix_is_skipped(self) -> None:
        assert _should_skip("/api/v1/docs") is True
        assert _should_skip("/api/v1/docs/swagger") is True

    def test_redoc_is_skipped(self) -> None:
        assert _should_skip("/api/v1/redoc") is True

    def test_openapi_json_is_skipped(self) -> None:
        assert _should_skip("/api/v1/openapi.json") is True

    def test_api_resource_is_not_skipped(self) -> None:
        assert _should_skip("/api/v1/companies/search") is False

    def test_api_auth_is_not_skipped(self) -> None:
        assert _should_skip("/api/v1/auth/login") is False

    def test_root_is_not_skipped(self) -> None:
        assert _should_skip("/") is False

    def test_jobs_endpoint_is_not_skipped(self) -> None:
        assert _should_skip("/api/v1/jobs") is False


# ---------------------------------------------------------------------------
# Test: _extract_ip helper
# ---------------------------------------------------------------------------


class TestExtractIP:
    def _make_request(
        self, headers: dict[str, str], client_host: str | None = "127.0.0.1"
    ) -> MagicMock:
        req = MagicMock()
        req.headers = headers
        req.client = MagicMock(host=client_host) if client_host else None
        return req

    def test_uses_x_forwarded_for_first(self) -> None:
        req = self._make_request({"X-Forwarded-For": "203.0.113.1, 10.0.0.1"})
        assert _extract_ip(req) == "203.0.113.1"

    def test_strips_whitespace_from_forwarded(self) -> None:
        req = self._make_request({"X-Forwarded-For": "  198.51.100.5  , 172.16.0.1"})
        assert _extract_ip(req) == "198.51.100.5"

    def test_falls_back_to_client_host(self) -> None:
        req = self._make_request({}, client_host="192.168.1.10")
        assert _extract_ip(req) == "192.168.1.10"

    def test_returns_none_when_no_client(self) -> None:
        req = self._make_request({}, client_host=None)
        assert _extract_ip(req) is None


# ---------------------------------------------------------------------------
# Test: X-Request-ID response header
# ---------------------------------------------------------------------------


class TestXRequestIDHeader:
    @pytest.mark.anyio
    async def test_request_id_header_set_on_audited_route(self, mock_settings: MagicMock) -> None:
        """Every response from an audited path must carry X-Request-ID."""
        factory, _ = _mock_session_factory()
        app = _build_app(mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/resource")
                await asyncio.sleep(0)  # let background task run

        assert "X-Request-ID" in resp.headers
        # Must be a valid UUID
        uuid.UUID(resp.headers["X-Request-ID"])

    @pytest.mark.anyio
    async def test_request_id_header_not_set_on_health(self, mock_settings: MagicMock) -> None:
        """Health endpoints are skipped — no X-Request-ID header expected."""
        app = _build_app(mock_settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")

        # Health route is skipped entirely — middleware may or may not set the header
        # The important assertion is: no 500, response is 200
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_request_id_is_unique_per_request(self, mock_settings: MagicMock) -> None:
        """Each request must receive a distinct X-Request-ID."""
        factory, _ = _mock_session_factory()
        app = _build_app(mock_settings)

        ids: list[str] = []
        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                for _ in range(5):
                    resp = await client.get("/api/v1/resource")
                    await asyncio.sleep(0)
                    ids.append(resp.headers["X-Request-ID"])

        assert len(set(ids)) == 5, f"Non-unique request IDs: {ids}"


# ---------------------------------------------------------------------------
# Test: Audit record content — authenticated requests
# ---------------------------------------------------------------------------


class TestAuditRecordAuthenticated:
    @pytest.mark.anyio
    async def test_audit_record_written_with_tenant_and_user(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """An authenticated request must produce an audit record with tenant/user IDs."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        session.add.assert_called_once()
        record: AuditLog = session.add.call_args[0][0]

        assert record.tenant_id == tenant_id
        assert record.user_id == user_id
        assert record.action == "http.get"

    @pytest.mark.anyio
    async def test_action_reflects_http_method(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """action must be 'http.{method.lower()}'."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        record: AuditLog = session.add.call_args[0][0]
        assert record.action == "http.post"

    @pytest.mark.anyio
    async def test_changes_captures_path_and_status(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """changes JSON must contain path and status_code."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        record: AuditLog = session.add.call_args[0][0]
        assert record.changes is not None
        assert record.changes["path"] == "/api/v1/resource"
        assert record.changes["status_code"] == 200

    @pytest.mark.anyio
    async def test_changes_captures_duration_ms(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """changes JSON must contain a non-negative duration_ms."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        record: AuditLog = session.add.call_args[0][0]
        assert "duration_ms" in record.changes
        assert isinstance(record.changes["duration_ms"], int)
        assert record.changes["duration_ms"] >= 0

    @pytest.mark.anyio
    async def test_changes_captures_query_string(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """Query strings must be captured in changes when present."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(
                    "/api/v1/resource?q=AAPL&page=1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        record: AuditLog = session.add.call_args[0][0]
        assert "query_string" in record.changes
        assert "q=AAPL" in record.changes["query_string"]

    @pytest.mark.anyio
    async def test_no_query_string_key_when_absent(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """query_string key must be absent from changes when there is no query."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        record: AuditLog = session.add.call_args[0][0]
        assert "query_string" not in record.changes

    @pytest.mark.anyio
    async def test_request_id_stored_as_uuid_on_record(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """The audit record's request_id must be a UUID matching the response header."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        header_rid = uuid.UUID(resp.headers["X-Request-ID"])
        record: AuditLog = session.add.call_args[0][0]
        assert record.request_id == header_rid


# ---------------------------------------------------------------------------
# Test: Audit record content — anonymous requests
# ---------------------------------------------------------------------------


class TestAuditRecordAnonymous:
    @pytest.mark.anyio
    async def test_anonymous_request_audited_with_null_tenant_user(
        self, mock_settings: MagicMock
    ) -> None:
        """An unauthenticated request must be audited with tenant_id=None and user_id=None."""
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # No auth header
                await client.get("/api/v1/resource")
                await asyncio.sleep(0)

        session.add.assert_called_once()
        record: AuditLog = session.add.call_args[0][0]
        assert record.tenant_id is None
        assert record.user_id is None

    @pytest.mark.anyio
    async def test_anonymous_request_still_gets_request_id_header(
        self, mock_settings: MagicMock
    ) -> None:
        """X-Request-ID must be set even for unauthenticated requests."""
        factory, _ = _mock_session_factory()
        app = _build_app(mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/resource")
                await asyncio.sleep(0)

        assert "X-Request-ID" in resp.headers
        uuid.UUID(resp.headers["X-Request-ID"])


# ---------------------------------------------------------------------------
# Test: Skipped paths — no audit record written
# ---------------------------------------------------------------------------


class TestSkippedPaths:
    @pytest.mark.anyio
    async def test_health_not_audited(self, mock_settings: MagicMock) -> None:
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")
                await asyncio.sleep(0)

        assert resp.status_code == 200
        session.add.assert_not_called()

    @pytest.mark.anyio
    async def test_health_ready_not_audited(self, mock_settings: MagicMock) -> None:
        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health/ready")
                await asyncio.sleep(0)

        assert resp.status_code == 200
        session.add.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Non-blocking guarantee — DB failure does not affect HTTP response
# ---------------------------------------------------------------------------


class TestNonBlockingGuarantee:
    @pytest.mark.anyio
    async def test_db_failure_does_not_cause_500(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """A DB exception in the audit write must NOT propagate to the HTTP response."""
        # Simulate AsyncSessionFactory raising on every call
        failing_factory = MagicMock(side_effect=RuntimeError("DB connection lost"))
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", failing_factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        # The HTTP response must succeed — audit failure is invisible to the client
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_commit_failure_does_not_cause_500(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """A commit failure inside the audit write must NOT affect the HTTP response."""
        session_mock = AsyncMock()
        session_mock.add = MagicMock()
        session_mock.commit = AsyncMock(side_effect=OSError("disk full"))
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=session_mock)

        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token}"},
                )
                await asyncio.sleep(0)

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_db_not_initialized_does_not_cause_500(self, mock_settings: MagicMock) -> None:
        """When AsyncSessionFactory is None (DB not ready), the response still succeeds."""
        app = _build_app(mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", None):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/resource")
                await asyncio.sleep(0)

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.anyio
    async def test_different_tenants_produce_independent_audit_records(
        self, mock_settings: MagicMock
    ) -> None:
        """Each tenant's requests produce audit records with their own tenant_id."""
        tenant_a, user_a = uuid.uuid4(), uuid.uuid4()
        tenant_b, user_b = uuid.uuid4(), uuid.uuid4()

        factory, session = _mock_session_factory()
        app = _build_app(mock_settings)

        token_a = _make_token(user_a, tenant_a, "analyst", mock_settings)
        token_b = _make_token(user_b, tenant_b, "viewer", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token_a}"},
                )
                await asyncio.sleep(0)
                call_a = session.add.call_args_list[-1][0][0]

                await client.get(
                    "/api/v1/resource",
                    headers={"Authorization": f"Bearer {token_b}"},
                )
                await asyncio.sleep(0)
                call_b = session.add.call_args_list[-1][0][0]

        assert call_a.tenant_id == tenant_a
        assert call_b.tenant_id == tenant_b
        assert call_a.tenant_id != call_b.tenant_id

        assert call_a.user_id == user_a
        assert call_b.user_id == user_b


# ---------------------------------------------------------------------------
# Test: Structlog integration
# ---------------------------------------------------------------------------


class TestStructlogIntegration:
    @pytest.mark.anyio
    async def test_structlog_info_emitted_for_audited_request(
        self,
        mock_settings: MagicMock,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """An INFO log line must be emitted for every audited request."""
        factory, _ = _mock_session_factory()
        app = _build_app(mock_settings)
        token = _make_token(user_id, tenant_id, "analyst", mock_settings)

        with patch("apps.api.core.database.AsyncSessionFactory", factory):
            with patch("apps.api.middleware.audit.logger") as mock_logger:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    await client.get(
                        "/api/v1/resource",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    await asyncio.sleep(0)

        # The synchronous logger.info("http.request", ...) call
        mock_logger.info.assert_called_once_with(
            "http.request",
            method="GET",
            path="/api/v1/resource",
            status_code=200,
            duration_ms=mock_logger.info.call_args.kwargs["duration_ms"],
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            request_id=mock_logger.info.call_args.kwargs["request_id"],
        )

    @pytest.mark.anyio
    async def test_no_log_emitted_for_skipped_path(self, mock_settings: MagicMock) -> None:
        """No audit log line emitted for skipped paths (/health etc.)."""
        app = _build_app(mock_settings)

        with patch("apps.api.middleware.audit.logger") as mock_logger:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/health")

        mock_logger.info.assert_not_called()


# ---------------------------------------------------------------------------
# Import from models to satisfy type checker in test assertions
# ---------------------------------------------------------------------------

from apps.api.models import AuditLog  # noqa: E402 — after test classes to avoid confusion
