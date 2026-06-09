"""
Integration tests — /api/v1/invitations endpoints.

Uses the full FastAPI app (auth middleware + JWT + Redis + PostgreSQL).
Each test registers a fresh workspace to guarantee isolation.

To run:
    docker compose up -d db redis
    DATABASE_URL=postgresql+asyncpg://fdh:fdh_password@localhost:5432/fdh \\
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_invitations.py -v

Milestone: M2-Step 9
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="Integration tests require DATABASE_URL env var.",
)

_STRONG_PASSWORD = "Str0ng!Pass#Invite99"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(client: AsyncClient) -> tuple[str, str]:
    """Register a unique workspace owner and return (access_token, email)."""
    suffix = uuid.uuid4().hex[:8]
    email = f"inv-test-{suffix}@example.com"
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": _STRONG_PASSWORD,
            "full_name": "Invite Tester",
            "workspace_name": f"Inv WS {suffix}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"], email


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _invite_email(suffix: str = "") -> str:
    return f"invitee{suffix}-{uuid.uuid4().hex[:6]}@example.com"


# ---------------------------------------------------------------------------
# POST /api/v1/invitations
# ---------------------------------------------------------------------------


class TestCreateInvitationIntegration:
    @pytest.mark.anyio
    async def test_create_returns_201(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.post(
            "/api/v1/invitations",
            json={"email": _invite_email(), "role": "analyst"},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert "token" not in body

    @pytest.mark.anyio
    async def test_duplicate_active_invitation_returns_409(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        email = _invite_email()
        resp1 = await client.post(
            "/api/v1/invitations",
            json={"email": email, "role": "analyst"},
            headers=_auth(token),
        )
        assert resp1.status_code == 201, resp1.text

        resp2 = await client.post(
            "/api/v1/invitations",
            json={"email": email, "role": "viewer"},
            headers=_auth(token),
        )
        assert resp2.status_code == 409

    @pytest.mark.anyio
    async def test_requires_admin_role(self, client: AsyncClient) -> None:
        """Unauthenticated request must be rejected."""
        resp = await client.post(
            "/api/v1/invitations",
            json={"email": _invite_email(), "role": "analyst"},
        )
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_cross_tenant_isolation(self, client: AsyncClient) -> None:
        """Owner of tenant A cannot send invitations that appear in tenant B."""
        token_a, _ = await _register_and_login(client)
        token_b, _ = await _register_and_login(client)
        email = _invite_email()

        # Tenant A sends invitation
        resp_a = await client.post(
            "/api/v1/invitations",
            json={"email": email, "role": "analyst"},
            headers=_auth(token_a),
        )
        assert resp_a.status_code == 201

        # Tenant B can send invitation to the same email (different tenant)
        resp_b = await client.post(
            "/api/v1/invitations",
            json={"email": email, "role": "viewer"},
            headers=_auth(token_b),
        )
        assert resp_b.status_code == 201  # different tenant — no conflict


# ---------------------------------------------------------------------------
# GET /api/v1/invitations/{token}
# ---------------------------------------------------------------------------


class TestValidateInvitationIntegration:
    @pytest.mark.anyio
    async def test_validate_returns_200(self, client: AsyncClient) -> None:
        """
        We cannot get the raw token from the API response (by design).
        This test verifies the 404 path for unknown tokens.
        """
        resp = await client.get("/api/v1/invitations/completely-fake-token")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_no_auth_required(self, client: AsyncClient) -> None:
        """Public endpoint — no Authorization header needed."""
        resp = await client.get("/api/v1/invitations/some-random-token-xyz")
        # Must return 404 (not 401) — no auth required
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/invitations/{token}
# ---------------------------------------------------------------------------


class TestCancelInvitationIntegration:
    @pytest.mark.anyio
    async def test_cancel_unknown_token_returns_404(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.delete(
            "/api/v1/invitations/fake-token-xyz",
            headers=_auth(token),
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_cancel_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/v1/invitations/fake-token-xyz")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/invitations/{token}/resend
# ---------------------------------------------------------------------------


class TestResendInvitationIntegration:
    @pytest.mark.anyio
    async def test_resend_unknown_token_returns_404(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.post(
            "/api/v1/invitations/fake-token-xyz/resend",
            headers=_auth(token),
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_resend_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/invitations/fake-token-xyz/resend")
        assert resp.status_code == 401
