"""
Unit tests — POST /api/v1/auth/login.

Strategy
--------
Database calls are replaced by AsyncMock so tests run without PostgreSQL.
The test application includes only the auth router and APIError handler.
``get_db`` is overridden with a mock session; ``AuthRepository`` is patched
so the mock session is never touched.

What is mocked
--------------
- ``AuthRepository``        — repo methods return pre-built MagicMock objects
- ``verify_password``       — avoids bcrypt overhead; returns True/False per test
- ``create_access_token``   — returns a fixed (token, jti) pair

What is NOT mocked (runs real code)
-------------------------------------
- ``LoginRequest`` schema validation (Pydantic v2 + email normalisation)
- UnauthorizedError / ConflictError exception handling and serialisation
- Cookie setting on the response
- AuthResponse serialisation

Milestone: M1-Step19
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.routers.auth import router

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_EMAIL = "alice@example.com"
_VALID_PASSWORD = "Str0ng!Pass#99"

_VALID_PAYLOAD: dict[str, str] = {
    "email": _VALID_EMAIL,
    "password": _VALID_PASSWORD,
}

_FIXED_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.login.token"
_FIXED_JTI = str(uuid.uuid4())
_FIXED_USER_ID = uuid.uuid4()
_FIXED_TENANT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_user() -> MagicMock:
    u = MagicMock()
    u.id = _FIXED_USER_ID
    u.email = _VALID_EMAIL
    u.full_name = "Alice Smith"
    u.password_hash = "$2b$12$hashed_password_value"
    u.is_active = True
    u.deleted_at = None
    return u


@pytest.fixture()
def mock_membership() -> MagicMock:
    m = MagicMock()
    m.tenant_id = _FIXED_TENANT_ID
    m.role = "owner"
    m.is_active = True
    m.deleted_at = None
    return m


@pytest.fixture()
def mock_repo(mock_user: MagicMock, mock_membership: MagicMock) -> AsyncMock:
    """Pre-configured AuthRepository mock for the happy path."""
    repo = AsyncMock()
    repo.get_user_by_email.return_value = mock_user
    repo.get_active_membership.return_value = mock_membership
    repo.update_last_login.return_value = None
    repo.create_refresh_token.return_value = AsyncMock()
    repo.create_audit_log.return_value = None
    return repo


@pytest.fixture()
def test_app() -> FastAPI:
    """Minimal FastAPI app with only the auth router."""
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _override_get_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestLoginSuccess:
    async def test_returns_200(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 200

    async def test_response_body_shape(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        body = resp.json()
        assert body["access_token"] == _FIXED_ACCESS_TOKEN
        assert body["token_type"] == "bearer"
        assert uuid.UUID(body["user_id"]) == _FIXED_USER_ID
        assert uuid.UUID(body["tenant_id"]) == _FIXED_TENANT_ID
        assert body["role"] == "owner"

    async def test_refresh_cookie_is_set(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert "fdh_refresh" in resp.cookies or "set-cookie" in resp.headers

    async def test_email_normalised_before_lookup(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """Mixed-case email is normalised to lowercase before the DB lookup."""
        payload = {**_VALID_PAYLOAD, "email": "ALICE@EXAMPLE.COM"}
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=payload)

        call_args = mock_repo.get_user_by_email.call_args
        assert call_args.args[0] == "alice@example.com"

    async def test_last_login_updated(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        mock_repo.update_last_login.assert_awaited_once()

    async def test_refresh_token_record_persisted(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        mock_repo.create_refresh_token.assert_awaited_once()
        call_kwargs = mock_repo.create_refresh_token.call_args.kwargs
        assert call_kwargs["jti"] == _FIXED_JTI
        assert call_kwargs["tenant_id"] == _FIXED_TENANT_ID

    async def test_audit_log_action_is_user_login(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        audit_call = mock_repo.create_audit_log.call_args
        assert audit_call.kwargs["action"] == "user.login"
        assert audit_call.kwargs["entity_type"] == "user"

    async def test_access_token_carries_correct_tenant_and_role(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """create_access_token is called with IDs and role from the membership."""
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ) as mock_create,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        mock_create.assert_called_once_with(
            user_id=_FIXED_USER_ID,
            tenant_id=_FIXED_TENANT_ID,
            role="owner",
        )


# ---------------------------------------------------------------------------
# Tests — invalid credentials (401)
# ---------------------------------------------------------------------------


class TestLoginInvalidCredentials:
    async def test_unknown_email_returns_401(self, test_app: FastAPI) -> None:
        repo = AsyncMock()
        repo.get_user_by_email.return_value = None  # email not found

        with patch("apps.api.routers.auth.AuthRepository", return_value=repo):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 401

    async def test_wrong_password_returns_401(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=False),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 401

    async def test_unknown_email_error_code(self, test_app: FastAPI) -> None:
        repo = AsyncMock()
        repo.get_user_by_email.return_value = None

        with patch("apps.api.routers.auth.AuthRepository", return_value=repo):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_wrong_password_error_code(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=False),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_unknown_email_and_wrong_password_same_message(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """Both error cases return identical messages to prevent enumeration."""
        repo_missing = AsyncMock()
        repo_missing.get_user_by_email.return_value = None

        with patch("apps.api.routers.auth.AuthRepository", return_value=repo_missing):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as c1:
                r1 = await c1.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=False),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as c2:
                r2 = await c2.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert r1.json()["error"]["message"] == r2.json()["error"]["message"]

    async def test_no_db_writes_on_unknown_email(self, test_app: FastAPI) -> None:
        """No token or audit log written when email is not found."""
        repo = AsyncMock()
        repo.get_user_by_email.return_value = None

        with patch("apps.api.routers.auth.AuthRepository", return_value=repo):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        repo.update_last_login.assert_not_awaited()
        repo.create_refresh_token.assert_not_awaited()
        repo.create_audit_log.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — inactive account (401)
# ---------------------------------------------------------------------------


class TestLoginInactiveAccount:
    async def test_inactive_account_returns_401(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_user: MagicMock
    ) -> None:
        mock_user.is_active = False

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 401

    async def test_inactive_account_error_code(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_user: MagicMock
    ) -> None:
        mock_user.is_active = False

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_inactive_account_no_tokens_issued(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_user: MagicMock
    ) -> None:
        mock_user.is_active = False

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        mock_repo.create_refresh_token.assert_not_awaited()
        mock_repo.create_audit_log.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — no active membership (401)
# ---------------------------------------------------------------------------


class TestLoginNoMembership:
    async def test_no_membership_returns_401(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        mock_repo.get_active_membership.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        assert resp.status_code == 401

    async def test_no_membership_no_tokens_issued(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        mock_repo.get_active_membership.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.verify_password", return_value=True),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/login", json=_VALID_PAYLOAD)

        mock_repo.create_refresh_token.assert_not_awaited()
        mock_repo.create_audit_log.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — input validation (422)
# ---------------------------------------------------------------------------


class TestLoginInputValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("email", "not-an-email"),
            ("email", ""),
            ("password", ""),
        ],
    )
    async def test_invalid_input_returns_422(
        self, test_app: FastAPI, field: str, value: str
    ) -> None:
        payload = {**_VALID_PAYLOAD, field: value}
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/login", json=payload)
        assert resp.status_code == 422

    async def test_missing_email_returns_422(self, test_app: FastAPI) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/login", json={"password": _VALID_PASSWORD})
        assert resp.status_code == 422

    async def test_missing_password_returns_422(self, test_app: FastAPI) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/login", json={"email": _VALID_EMAIL})
        assert resp.status_code == 422
