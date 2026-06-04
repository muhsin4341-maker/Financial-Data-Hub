"""
Unit tests — POST /api/v1/auth/forgot-password.

Strategy
--------
The endpoint must behave identically for existing and non-existing email
addresses to prevent account enumeration. All tests verify that the response
is always HTTP 200 with the same message body, and that DB/email side-effects
only fire when an active user is found.

No auth required — the endpoint is fully public.

What is mocked
--------------
- ``AuthRepository``                  — all repo methods
- ``get_email_backend``               — returns an AsyncMock backend
- ``generate_password_reset_token``   — returns a fixed token string
- ``hash_password_reset_token``       — returns a fixed hash string
- ``render_email_template``           — returns fixed strings

What is NOT mocked (runs real code)
-------------------------------------
- ``ForgotPasswordRequest`` schema validation
- ``MessageResponse`` serialisation
- Enumeration-proof 200 response on unknown email
- ConsoleEmailBackend (tested in isolation in test_email_service.py)

Milestone: M1-Step22
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
from apps.api.routers.auth import router, _FORGOT_PASSWORD_RESPONSE

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_EMAIL = "alice@example.com"
_FIXED_RAW_TOKEN = "A" * 48
_FIXED_TOKEN_HASH = "h" * 64
_FIXED_USER_ID = uuid.uuid4()
_FIXED_TENANT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_user() -> MagicMock:
    """Active user returned by get_user_by_email."""
    u = MagicMock()
    u.id = _FIXED_USER_ID
    u.email = _VALID_EMAIL
    u.full_name = "Alice Smith"
    u.is_active = True
    u.deleted_at = None
    # memberships list with one entry so audit log can get tenant_id
    m = MagicMock()
    m.tenant_id = _FIXED_TENANT_ID
    u.memberships = [m]
    return u


@pytest.fixture()
def mock_backend() -> AsyncMock:
    b = AsyncMock()
    b.send = AsyncMock()
    return b


@pytest.fixture()
def mock_repo(mock_user: MagicMock) -> AsyncMock:
    repo = AsyncMock()
    repo.get_user_by_email.return_value = mock_user
    repo.update_password_reset_token.return_value = None
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_patches(mock_repo: AsyncMock, mock_backend: AsyncMock) -> list[Any]:
    return [
        patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
        patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        patch(
            "apps.api.routers.auth.generate_password_reset_token",
            return_value=_FIXED_RAW_TOKEN,
        ),
        patch(
            "apps.api.routers.auth.hash_password_reset_token",
            return_value=_FIXED_TOKEN_HASH,
        ),
        patch(
            "apps.api.routers.auth.render_email_template",
            return_value="email body text",
        ),
    ]


# ---------------------------------------------------------------------------
# Tests — response shape (always 200 with the same message)
# ---------------------------------------------------------------------------


class TestForgotPasswordResponse:
    async def test_returns_200_for_existing_email(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        assert resp.status_code == 200

    async def test_returns_200_for_unknown_email(
        self, test_app: FastAPI
    ) -> None:
        repo = AsyncMock()
        repo.get_user_by_email.return_value = None  # email not found

        with patch("apps.api.routers.auth.AuthRepository", return_value=repo):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/auth/forgot-password", json={"email": "nobody@nowhere.com"}
                )

        assert resp.status_code == 200

    async def test_same_message_for_existing_and_unknown_email(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        """Enumeration protection: response body is identical."""
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                r_existing = await c.post(
                    "/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL}
                )

        repo_miss = AsyncMock()
        repo_miss.get_user_by_email.return_value = None
        with patch("apps.api.routers.auth.AuthRepository", return_value=repo_miss):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                r_missing = await c.post(
                    "/api/v1/auth/forgot-password", json={"email": "ghost@example.com"}
                )

        assert r_existing.json() == r_missing.json()
        assert r_existing.status_code == r_missing.status_code == 200

    async def test_response_message_matches_constant(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        assert resp.json()["message"] == _FORGOT_PASSWORD_RESPONSE


# ---------------------------------------------------------------------------
# Tests — side-effects only fire for existing active user
# ---------------------------------------------------------------------------


class TestForgotPasswordSideEffects:
    async def test_token_persisted_for_existing_user(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        mock_repo.update_password_reset_token.assert_awaited_once()
        call_kwargs = mock_repo.update_password_reset_token.call_args.kwargs
        assert call_kwargs["token_hash"] == _FIXED_TOKEN_HASH

    async def test_token_expiry_is_one_hour_from_now(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        before = datetime.now(UTC)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        expires_at = mock_repo.update_password_reset_token.call_args.kwargs["expires_at"]
        after = datetime.now(UTC)
        expected_min = before + timedelta(hours=1)
        expected_max = after + timedelta(hours=1)
        assert expected_min <= expires_at <= expected_max

    async def test_email_sent_for_existing_user(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        mock_backend.send.assert_awaited_once()
        msg = mock_backend.send.call_args.args[0]
        assert msg.to == _VALID_EMAIL
        assert "reset" in msg.subject.lower()

    async def test_reset_link_contains_raw_token(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        captured_ctx: dict[str, Any] = {}

        def _capture_render(template_name: str, context: dict[str, Any]) -> str:
            captured_ctx.update(context)
            return "rendered"

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", side_effect=_capture_render),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        assert _FIXED_RAW_TOKEN in captured_ctx.get("reset_link", "")

    async def test_audit_log_action(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        audit_kwargs = mock_repo.create_audit_log.call_args.kwargs
        assert audit_kwargs["action"] == "user.password_reset_requested"
        assert audit_kwargs["entity_type"] == "user"

    async def test_no_side_effects_for_unknown_email(
        self, test_app: FastAPI, mock_backend: AsyncMock
    ) -> None:
        """For unknown email: no DB write, no email send, no audit log."""
        repo = AsyncMock()
        repo.get_user_by_email.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"})

        repo.update_password_reset_token.assert_not_awaited()
        mock_backend.send.assert_not_awaited()
        repo.create_audit_log.assert_not_awaited()

    async def test_no_side_effects_for_inactive_user(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock,
        mock_user: MagicMock
    ) -> None:
        """Deactivated accounts are treated the same as unknown — no token issued."""
        mock_user.is_active = False

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        assert resp.status_code == 200
        mock_repo.update_password_reset_token.assert_not_awaited()
        mock_backend.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — resilience (email / template failures)
# ---------------------------------------------------------------------------


class TestForgotPasswordResilience:
    async def test_email_failure_still_returns_200(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """Email send failure must not surface as an error to the caller."""
        failing_backend = AsyncMock()
        failing_backend.send.side_effect = OSError("SMTP unreachable")

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=failing_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        assert resp.status_code == 200
        # Token was still persisted even though email failed
        mock_repo.update_password_reset_token.assert_awaited_once()

    async def test_template_failure_falls_back_to_plain_text(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        """Template rendering failure should fall back, not abort the request."""

        def _template_fail(*_: object, **__: object) -> str:
            raise RuntimeError("Template not found")

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", side_effect=_template_fail),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                resp = await c.post("/api/v1/auth/forgot-password", json={"email": _VALID_EMAIL})

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests — input validation
# ---------------------------------------------------------------------------


class TestForgotPasswordInputValidation:
    @pytest.mark.parametrize(
        "email",
        ["not-an-email", "", "plaintext", "@nodomain"],
    )
    async def test_invalid_email_returns_422(self, test_app: FastAPI, email: str) -> None:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/forgot-password", json={"email": email})
        assert resp.status_code == 422

    async def test_missing_email_returns_422(self, test_app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            resp = await c.post("/api/v1/auth/forgot-password", json={})
        assert resp.status_code == 422

    async def test_email_normalised_to_lowercase(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_backend: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.get_email_backend", return_value=mock_backend),
            patch("apps.api.routers.auth.generate_password_reset_token", return_value=_FIXED_RAW_TOKEN),
            patch("apps.api.routers.auth.hash_password_reset_token", return_value=_FIXED_TOKEN_HASH),
            patch("apps.api.routers.auth.render_email_template", return_value="body"),
        ):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
                await c.post("/api/v1/auth/forgot-password", json={"email": "ALICE@EXAMPLE.COM"})

        mock_repo.get_user_by_email.assert_awaited_once_with("alice@example.com")
