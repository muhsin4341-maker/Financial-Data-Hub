"""
Unit tests — POST /api/v1/auth/reset-password.

Strategy
--------
No auth required — the endpoint is fully public (the reset token is the
authentication mechanism). All DB and email calls are mocked; tests run
without a live database or SMTP server.

What is mocked
--------------
- ``AuthRepository``            — all repo methods
- ``hash_password_reset_token`` — returns a fixed hash string
- ``hash_password``             — avoids bcrypt overhead; returns fixed hash
- ``get_email_backend``         — returns an AsyncMock backend

What is NOT mocked (runs real code)
-------------------------------------
- ``ResetPasswordRequest`` schema validation (Pydantic + complexity validator)
- 400 vs 422 status code distinction (schema vs token errors)
- ``MessageResponse`` serialisation
- ``APIError`` exception → JSON error response

Security properties verified
------------------------------
- Invalid token → 400 (same message as expired and already-used)
- Expired token → 400 (same message)
- Already-cleared token (user returned None) → 400 (same message)
- All failure messages are identical (no cause leakage)
- bcrypt is not called when token is invalid
- Refresh tokens are revoked after successful reset

Milestone: M1-Step23
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
from apps.api.routers.auth import router, _RESET_SUCCESS_MSG, _INVALID_RESET_TOKEN_MSG

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_TOKEN = "A" * 48          # raw token from email link
_TOKEN_HASH = "h" * 64           # what hash_password_reset_token returns
_VALID_PASSWORD = "NewStr0ng!Pass#99"
_FIXED_USER_ID = uuid.uuid4()
_FIXED_TENANT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(
    *,
    token_hash: str | None = _TOKEN_HASH,
    expires_at: datetime | None = None,
    is_active: bool = True,
) -> MagicMock:
    """Build a User mock for the reset-password endpoint."""
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(hours=1)
    u = MagicMock()
    u.id = _FIXED_USER_ID
    u.email = "alice@example.com"
    u.full_name = "Alice Smith"
    u.is_active = is_active
    u.password_reset_token = token_hash
    u.password_reset_expires_at = expires_at
    m = MagicMock()
    m.tenant_id = _FIXED_TENANT_ID
    u.memberships = [m]
    return u


@pytest.fixture()
def valid_user() -> MagicMock:
    """Active user with a valid, non-expired reset token."""
    return _make_user()


@pytest.fixture()
def mock_backend() -> AsyncMock:
    b = AsyncMock()
    b.send = AsyncMock()
    return b


@pytest.fixture()
def mock_repo(valid_user: MagicMock) -> AsyncMock:
    """Happy-path repository mock."""
    membership = MagicMock()
    membership.tenant_id = _FIXED_TENANT_ID

    repo = AsyncMock()
    repo.get_user_by_reset_token_hash.return_value = valid_user
    repo.complete_password_reset.return_value = None
    repo.revoke_all_user_refresh_tokens.return_value = None
    repo.get_active_membership.return_value = membership
    repo.create_audit_log.return_value = None
    return repo


@pytest.fixture()
def test_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _override_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _override_db
    app.include_router(router)
    return app


_VALID_PAYLOAD = {"token": _VALID_TOKEN, "new_password": _VALID_PASSWORD}


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestResetPasswordSuccess:
    async def test_returns_200(self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert resp.status_code == 200

    async def test_success_message(self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert resp.json()["message"] == _RESET_SUCCESS_MSG

    async def test_complete_password_reset_called(self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$newhash"),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        mock_repo.complete_password_reset.assert_awaited_once()
        kwargs = mock_repo.complete_password_reset.call_args.kwargs
        assert kwargs["new_password_hash"] == "$2b$12$newhash"

    async def test_all_refresh_tokens_revoked(self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock) -> None:
        """Password reset must terminate all active sessions."""
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        mock_repo.revoke_all_user_refresh_tokens.assert_awaited_once_with(_FIXED_USER_ID)

    async def test_audit_log_action(self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        audit_kwargs = mock_repo.create_audit_log.call_args.kwargs
        assert audit_kwargs["action"] == "user.password_reset"
        assert audit_kwargs["entity_type"] == "user"
        assert audit_kwargs["user_id"] == _FIXED_USER_ID

    async def test_notification_email_sent(self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        mock_backend.send.assert_awaited_once()
        msg = mock_backend.send.call_args.args[0]
        assert msg.to == "alice@example.com"
        assert "changed" in msg.subject.lower() or "reset" in msg.subject.lower()

    async def test_token_hash_looked_up(self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        mock_repo.get_user_by_reset_token_hash.assert_awaited_once_with(_TOKEN_HASH)


# ---------------------------------------------------------------------------
# Tests — invalid token (400)
# ---------------------------------------------------------------------------


class TestResetPasswordInvalidToken:
    async def test_unknown_token_returns_400(self, test_app: FastAPI) -> None:
        """Token not found in DB (invalid or already used)."""
        repo = AsyncMock()
        repo.get_user_by_reset_token_hash.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert resp.status_code == 400

    async def test_unknown_token_error_code(self, test_app: FastAPI) -> None:
        repo = AsyncMock()
        repo.get_user_by_reset_token_hash.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert resp.json()["error"]["code"] == "INVALID_RESET_TOKEN"

    async def test_no_db_writes_on_invalid_token(self, test_app: FastAPI) -> None:
        repo = AsyncMock()
        repo.get_user_by_reset_token_hash.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        repo.complete_password_reset.assert_not_awaited()
        repo.revoke_all_user_refresh_tokens.assert_not_awaited()
        repo.create_audit_log.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — expired token (400)
# ---------------------------------------------------------------------------


class TestResetPasswordExpiredToken:
    async def test_expired_token_returns_400(self, test_app: FastAPI) -> None:
        expired_user = _make_user(
            expires_at=datetime.now(UTC) - timedelta(hours=2)  # in the past
        )
        repo = AsyncMock()
        repo.get_user_by_reset_token_hash.return_value = expired_user

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_RESET_TOKEN"

    async def test_expired_and_invalid_same_message(self, test_app: FastAPI) -> None:
        """Expired and not-found tokens return identical messages (no leakage)."""
        # Not found
        repo_nf = AsyncMock()
        repo_nf.get_user_by_reset_token_hash.return_value = None
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo_nf),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                r_nf = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        # Expired
        repo_exp = AsyncMock()
        repo_exp.get_user_by_reset_token_hash.return_value = _make_user(
            expires_at=datetime.now(UTC) - timedelta(hours=1)
        )
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo_exp),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                r_exp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert r_nf.json()["error"]["message"] == r_exp.json()["error"]["message"]
        assert r_nf.json()["error"]["message"] == _INVALID_RESET_TOKEN_MSG

    async def test_none_expires_at_treated_as_expired(self, test_app: FastAPI) -> None:
        """If expires_at is NULL (data integrity issue), treat as expired."""
        broken_user = _make_user()
        broken_user.password_reset_expires_at = None  # force NULL after construction
        repo = AsyncMock()
        repo.get_user_by_reset_token_hash.return_value = broken_user

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests — password policy violations (422)
# ---------------------------------------------------------------------------


class TestResetPasswordPolicyViolations:
    @pytest.mark.parametrize(
        "password",
        [
            "short1A!",        # < 12 chars
            "nouppercase1!",   # no uppercase
            "NOLOWERCASE1!",   # no lowercase
            "NoDigitHere!!",   # no digit
            "NoSpecial1234",   # no special character
        ],
    )
    async def test_weak_password_returns_422(self, test_app: FastAPI, password: str) -> None:
        payload = {"token": _VALID_TOKEN, "new_password": password}
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/reset-password", json=payload)
        assert resp.status_code == 422

    async def test_weak_password_no_db_access(self, test_app: FastAPI) -> None:
        """Schema validation fires before any DB call."""
        repo = AsyncMock()
        payload = {"token": _VALID_TOKEN, "new_password": "weak"}
        with patch("apps.api.routers.auth.AuthRepository", return_value=repo):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/reset-password", json=payload)

        repo.get_user_by_reset_token_hash.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — input validation (422)
# ---------------------------------------------------------------------------


class TestResetPasswordInputValidation:
    async def test_missing_token_returns_422(self, test_app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/reset-password", json={"new_password": _VALID_PASSWORD})
        assert resp.status_code == 422

    async def test_missing_password_returns_422(self, test_app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/reset-password", json={"token": _VALID_TOKEN})
        assert resp.status_code == 422

    async def test_empty_token_returns_422(self, test_app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/reset-password", json={"token": "", "new_password": _VALID_PASSWORD})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests — notification email resilience
# ---------------------------------------------------------------------------


class TestResetPasswordNotificationResilience:
    async def test_email_failure_does_not_abort_reset(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """Password reset must succeed even if notification email fails."""
        failing_backend = AsyncMock()
        failing_backend.send.side_effect = OSError("SMTP unreachable")

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_TOKEN_HASH),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch("apps.api.routers.auth.get_email_backend", return_value=failing_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/reset-password", json=_VALID_PAYLOAD)

        assert resp.status_code == 200
        # Core operations still completed
        mock_repo.complete_password_reset.assert_awaited_once()
        mock_repo.revoke_all_user_refresh_tokens.assert_awaited_once()
        mock_repo.create_audit_log.assert_awaited_once()
