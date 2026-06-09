"""
Unit tests — /api/v1/invitations router.

Strategy
--------
Minimal FastAPI app per test with:
  - The invitations router
  - APIError exception handler
  - get_db overridden with AsyncMock session
  - Auth dependencies overridden to inject fixed AuthRequestContext

InvitationRepository is patched at the router's import path.

What is mocked
--------------
- InvitationRepository — all methods
- Auth dependencies    — inject fixed AuthRequestContext
- get_db               — yields AsyncMock session
- _dispatch_invitation_email — patched to avoid real email dispatch

What is NOT mocked (real code runs)
------------------------------------
- All Pydantic schema validation
- NotFoundError / ConflictError / ForbiddenError handling
- token hashing (generate_invitation_token / hash_invitation_token)
- HTTP status codes and response body structure

Milestone: M2-Step 9
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_admin,
    require_authenticated,
)
from apps.api.models import InvitationStatus
from apps.api.routers.invitations import router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_INVITATION_ID = uuid.uuid4()
_NOW = datetime.now(UTC)
_EXPIRES = _NOW + timedelta(hours=72)

_ADMIN_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="admin",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)
_VIEWER_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="viewer",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)
_AUTH_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="analyst",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)

_INVITATION_DATA: dict[str, Any] = {
    "id": _INVITATION_ID,
    "tenant_id": _TENANT_ID,
    "invitee_email": "bob@example.com",
    "role": "analyst",
    "status": InvitationStatus.PENDING.value,
    "expires_at": _EXPIRES,
    "accepted_at": None,
    "invited_by_id": _USER_ID,
    "accepted_by_id": None,
    "created_at": _NOW,
    "updated_at": _NOW,
    "is_expired": False,
    "is_usable": True,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(ctx: AuthRequestContext) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_admin] = lambda: ctx
    app.dependency_overrides[require_authenticated] = lambda: ctx
    app.include_router(router)
    return app


def _mock_invitation(**overrides: Any) -> MagicMock:
    inv = MagicMock()
    data = {**_INVITATION_DATA, **overrides}
    for k, v in data.items():
        setattr(inv, k, v)
    return inv


def _mock_user(email: str = "bob@example.com") -> MagicMock:
    u = MagicMock()
    u.id = _USER_ID
    u.email = email
    u.is_active = True
    return u


# ---------------------------------------------------------------------------
# POST /api/v1/invitations
# ---------------------------------------------------------------------------


class TestCreateInvitation:
    @pytest.mark.anyio
    async def test_returns_201_on_success(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_active_by_email.return_value = None
        mock_repo.create.return_value = _mock_invitation()

        with (
            patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo),
            patch("apps.api.routers.invitations._dispatch_invitation_email"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/invitations",
                    json={"email": "bob@example.com", "role": "analyst"},
                )

        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_response_body_shape(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_active_by_email.return_value = None
        mock_repo.create.return_value = _mock_invitation()

        with (
            patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo),
            patch("apps.api.routers.invitations._dispatch_invitation_email"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/invitations",
                    json={"email": "bob@example.com", "role": "analyst"},
                )

        body = resp.json()
        assert body["invitee_email"] == "bob@example.com"
        assert body["role"] == "analyst"
        assert body["status"] == "pending"
        assert "token" not in body  # raw token must NEVER appear in response

    @pytest.mark.anyio
    async def test_duplicate_active_invitation_returns_409(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_active_by_email.return_value = _mock_invitation()

        with (
            patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo),
            patch("apps.api.routers.invitations._dispatch_invitation_email"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/invitations",
                    json={"email": "bob@example.com", "role": "analyst"},
                )

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.anyio
    async def test_owner_role_rejected_422(self) -> None:
        app = _build_app(_ADMIN_CTX)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/invitations",
                json={"email": "bob@example.com", "role": "owner"},
            )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_email_normalised_to_lowercase(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_active_by_email.return_value = None
        captured: dict[str, Any] = {}

        async def _capture(tenant_id: Any, invited_by_id: Any, token_hash: Any, schema: Any) -> Any:
            captured["email"] = schema.email
            return _mock_invitation()

        mock_repo.create.side_effect = _capture

        with (
            patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo),
            patch("apps.api.routers.invitations._dispatch_invitation_email"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post(
                    "/api/v1/invitations",
                    json={"email": "BOB@EXAMPLE.COM", "role": "analyst"},
                )

        assert captured["email"] == "bob@example.com"

    @pytest.mark.anyio
    async def test_viewer_cannot_invite(self) -> None:
        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        app.dependency_overrides[get_db] = _mock_db
        app.dependency_overrides[_get_auth_context] = lambda: _VIEWER_CTX
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/invitations",
                json={"email": "bob@example.com", "role": "analyst"},
            )

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_tenant_id_from_jwt_not_body(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_active_by_email.return_value = None
        captured: dict[str, Any] = {}

        async def _capture(tenant_id: Any, invited_by_id: Any, token_hash: Any, schema: Any) -> Any:
            captured["tenant_id"] = tenant_id
            captured["invited_by_id"] = invited_by_id
            return _mock_invitation()

        mock_repo.create.side_effect = _capture

        with (
            patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo),
            patch("apps.api.routers.invitations._dispatch_invitation_email"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post(
                    "/api/v1/invitations",
                    json={"email": "bob@example.com", "role": "analyst"},
                )

        assert captured["tenant_id"] == _TENANT_ID
        assert captured["invited_by_id"] == _USER_ID


# ---------------------------------------------------------------------------
# GET /api/v1/invitations/{token}
# ---------------------------------------------------------------------------


class TestValidateInvitation:
    @pytest.mark.anyio
    async def test_returns_200_for_valid_token(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = _mock_invitation()

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/invitations/some-valid-token")

        assert resp.status_code == 200
        body = resp.json()
        assert body["invitee_email"] == "bob@example.com"
        assert body["is_usable"] is True

    @pytest.mark.anyio
    async def test_returns_404_for_unknown_token(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = None

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/invitations/bad-token")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_no_auth_required(self) -> None:
        """GET /invitations/{token} is public — no JWT needed."""
        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        app.dependency_overrides[get_db] = _mock_db
        app.include_router(router)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = _mock_invitation()

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/invitations/some-token")

        # Should reach the handler (not 401/403)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/invitations/{token}/accept
# ---------------------------------------------------------------------------


class TestAcceptInvitation:
    @pytest.mark.anyio
    async def test_returns_200_on_success(self) -> None:
        app = _build_app(_AUTH_CTX)
        mock_repo = AsyncMock()
        inv = _mock_invitation()
        mock_repo.get_by_token_hash.return_value = inv
        mock_repo.get_user_by_email.return_value = _mock_user()
        mock_repo.get_membership.return_value = None
        mock_repo.accept.return_value = _mock_invitation(
            status=InvitationStatus.ACCEPTED.value,
            accepted_at=_NOW,
        )
        mock_repo.create_membership.return_value = MagicMock()

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/valid-token/accept")

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_unknown_token_returns_404(self) -> None:
        app = _build_app(_AUTH_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = None

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/bad-token/accept")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_expired_invitation_returns_404(self) -> None:
        app = _build_app(_AUTH_CTX)
        mock_repo = AsyncMock()
        expired = _mock_invitation(
            is_usable=False,
            expires_at=_NOW - timedelta(hours=1),
        )
        mock_repo.get_by_token_hash.return_value = expired

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/expired-token/accept")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_wrong_user_email_returns_403(self) -> None:
        app = _build_app(_AUTH_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = _mock_invitation()
        # Return a user with a different ID
        wrong_user = _mock_user()
        wrong_user.id = uuid.uuid4()  # different from _USER_ID
        mock_repo.get_user_by_email.return_value = wrong_user

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/valid-token/accept")

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_already_member_returns_409(self) -> None:
        app = _build_app(_AUTH_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = _mock_invitation()
        mock_repo.get_user_by_email.return_value = _mock_user()
        mock_repo.get_membership.return_value = MagicMock()  # already a member

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/valid-token/accept")

        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/v1/invitations/{token}/resend
# ---------------------------------------------------------------------------


class TestResendInvitation:
    @pytest.mark.anyio
    async def test_returns_200_with_fresh_invitation(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        inv = _mock_invitation()
        mock_repo.get_by_token_hash.return_value = inv
        mock_repo.refresh_token.return_value = _mock_invitation()

        with (
            patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo),
            patch("apps.api.routers.invitations._dispatch_invitation_email"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/valid-token/resend")

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_unknown_token_returns_404(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = None

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/bad-token/resend")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_wrong_tenant_returns_404(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        # Invitation belongs to a different tenant
        inv = _mock_invitation(tenant_id=uuid.uuid4())
        mock_repo.get_by_token_hash.return_value = inv

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/valid-token/resend")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_accepted_invitation_returns_409(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        inv = _mock_invitation(status=InvitationStatus.ACCEPTED.value, tenant_id=_TENANT_ID)
        mock_repo.get_by_token_hash.return_value = inv

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/invitations/valid-token/resend")

        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/v1/invitations/{token}
# ---------------------------------------------------------------------------


class TestCancelInvitation:
    @pytest.mark.anyio
    async def test_returns_204_on_success(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        inv = _mock_invitation()
        mock_repo.get_by_token_hash.return_value = inv
        mock_repo.cancel.return_value = _mock_invitation(
            status=InvitationStatus.CANCELLED.value
        )

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.delete("/api/v1/invitations/valid-token")

        assert resp.status_code == 204
        assert resp.content == b""

    @pytest.mark.anyio
    async def test_unknown_token_returns_404(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_token_hash.return_value = None

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.delete("/api/v1/invitations/bad-token")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_wrong_tenant_returns_404(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        inv = _mock_invitation(tenant_id=uuid.uuid4())
        mock_repo.get_by_token_hash.return_value = inv

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.delete("/api/v1/invitations/valid-token")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_accepted_invitation_returns_409(self) -> None:
        app = _build_app(_ADMIN_CTX)
        mock_repo = AsyncMock()
        inv = _mock_invitation(
            status=InvitationStatus.ACCEPTED.value, tenant_id=_TENANT_ID
        )
        mock_repo.get_by_token_hash.return_value = inv

        with patch("apps.api.routers.invitations.InvitationRepository", return_value=mock_repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.delete("/api/v1/invitations/valid-token")

        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_viewer_cannot_cancel(self) -> None:
        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        app.dependency_overrides[get_db] = _mock_db
        app.dependency_overrides[_get_auth_context] = lambda: _VIEWER_CTX
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/api/v1/invitations/some-token")

        assert resp.status_code == 403
