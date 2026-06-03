"""
Unit tests — POST /api/v1/auth/register.

Strategy
--------
All database calls are replaced by AsyncMock so tests run without a live
PostgreSQL instance.  The test application is a minimal FastAPI instance
containing only the auth router and the APIError exception handler.

The ``get_db`` dependency is overridden to yield an AsyncMock session;
``AuthRepository`` is patched so the mock session is never actually used.

What is mocked
--------------
- ``AuthRepository``          — repo methods return pre-built MagicMock objects
- ``check_hibp_password``     — async call to HIBP API; returns False by default
- ``hash_password``           — returns a fixed hash string to avoid bcrypt overhead
- ``create_access_token``     — returns a fixed (token, jti) pair

What is NOT mocked (runs real code)
------------------------------------
- ``RegisterRequest`` schema validation  (Pydantic v2 + field_validator)
- ``validate_password_complexity``       (regex rules)
- ``AuthResponse`` serialisation
- Cookie setting on the response
- ConflictError / ValidationError exception handling
- Structlog calls (silently no-ops in tests)

Milestone: M1-Step18
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

_VALID_PAYLOAD: dict[str, Any] = {
    "email": "alice@example.com",
    "password": "Str0ng!Pass#99",
    "full_name": "Alice Smith",
    "workspace_name": "Acme Capital",
}

_FIXED_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.token"
_FIXED_JTI = str(uuid.uuid4())
_FIXED_USER_ID = uuid.uuid4()
_FIXED_TENANT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_tenant() -> MagicMock:
    t = MagicMock()
    t.id = _FIXED_TENANT_ID
    t.name = "Acme Capital"
    t.slug = "acme-capital-a3f7b2"
    return t


@pytest.fixture()
def mock_user() -> MagicMock:
    u = MagicMock()
    u.id = _FIXED_USER_ID
    u.email = "alice@example.com"
    u.full_name = "Alice Smith"
    return u


@pytest.fixture()
def mock_repo(mock_tenant: MagicMock, mock_user: MagicMock) -> AsyncMock:
    """Pre-configured AuthRepository mock for the happy path."""
    repo = AsyncMock()
    repo.get_user_by_email.return_value = None  # email not taken
    repo.create_tenant.return_value = mock_tenant
    repo.create_user.return_value = mock_user
    repo.create_membership.return_value = AsyncMock()
    repo.create_refresh_token.return_value = AsyncMock()
    repo.create_audit_log.return_value = None
    return repo


@pytest.fixture()
def test_app() -> FastAPI:
    """Minimal FastAPI app with only the auth router registered."""
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _override_get_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _patch_register_deps(mock_repo: AsyncMock) -> list[Any]:
    """
    Return a list of patch() context managers that together mock all
    external dependencies of the register endpoint.

    Applied via ``with ExitStack() as stack: [stack.enter_context(p) for p in ...]``
    or combined with the helper below.
    """
    return [
        patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
        patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
        patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
        patch(
            "apps.api.routers.auth.create_access_token",
            return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
        ),
    ]


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestRegisterSuccess:
    async def test_returns_201(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        assert resp.status_code == 201

    async def test_response_body_shape(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        body = resp.json()
        assert body["access_token"] == _FIXED_ACCESS_TOKEN
        assert body["token_type"] == "bearer"
        assert body["role"] == "owner"
        assert uuid.UUID(body["user_id"]) == _FIXED_USER_ID
        assert uuid.UUID(body["tenant_id"]) == _FIXED_TENANT_ID

    async def test_refresh_cookie_set(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        assert "fdh_refresh" in resp.cookies or "set-cookie" in resp.headers

    async def test_email_normalised_to_lowercase(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """Email stored in lowercase regardless of input casing."""
        payload = {**_VALID_PAYLOAD, "email": "ALICE@EXAMPLE.COM"}
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=payload)

        assert resp.status_code == 201
        # Repo received lowercase email
        call_args = mock_repo.get_user_by_email.call_args
        assert call_args.args[0] == "alice@example.com"

    async def test_repository_methods_called_in_order(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        mock_repo.get_user_by_email.assert_awaited_once_with("alice@example.com")
        mock_repo.create_tenant.assert_awaited_once()
        mock_repo.create_user.assert_awaited_once()
        mock_repo.create_membership.assert_awaited_once()
        mock_repo.create_refresh_token.assert_awaited_once()
        mock_repo.create_audit_log.assert_awaited_once()

    async def test_audit_log_action_is_user_registered(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        audit_call = mock_repo.create_audit_log.call_args
        assert audit_call.kwargs["action"] == "user.registered"
        assert audit_call.kwargs["entity_type"] == "user"

    async def test_membership_role_is_owner(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        from apps.api.models import UserRole

        membership_call = mock_repo.create_membership.call_args
        assert membership_call.kwargs["role"] == UserRole.OWNER


# ---------------------------------------------------------------------------
# Tests — duplicate email (409 Conflict)
# ---------------------------------------------------------------------------


class TestRegisterDuplicateEmail:
    async def test_returns_409_when_email_exists(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_user: MagicMock
    ) -> None:
        mock_repo.get_user_by_email.return_value = mock_user  # email already taken

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        assert resp.status_code == 409

    async def test_409_error_code(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_user: MagicMock
    ) -> None:
        mock_repo.get_user_by_email.return_value = mock_user

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        assert resp.json()["error"]["code"] == "CONFLICT"

    async def test_no_db_writes_on_duplicate_email(
        self, test_app: FastAPI, mock_repo: AsyncMock, mock_user: MagicMock
    ) -> None:
        """No tenant, user, or token records created when email already exists."""
        mock_repo.get_user_by_email.return_value = mock_user

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        mock_repo.create_tenant.assert_not_awaited()
        mock_repo.create_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — password policy violations (422 Unprocessable Entity)
# ---------------------------------------------------------------------------


class TestRegisterPasswordPolicy:
    @pytest.mark.parametrize(
        "password",
        [
            "short1A!",  # < 12 chars
            "nouppercase1!",  # no uppercase
            "NOLOWERCASE1!",  # no lowercase
            "NoDigitHere!!",  # no digit
            "NoSpecial1234",  # no special character
        ],
    )
    async def test_weak_password_returns_422(
        self, test_app: FastAPI, password: str
    ) -> None:
        payload = {**_VALID_PAYLOAD, "password": password}
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code == 422

    async def test_valid_password_accepted(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        """Boundary: exactly-valid password should not trigger 422."""
        payload = {**_VALID_PAYLOAD, "password": "Str0ng!Pass#12"}
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=payload)

        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Tests — HIBP breach detection (422 Validation Error)
# ---------------------------------------------------------------------------


class TestRegisterHIBP:
    async def test_breached_password_returns_422(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch(
                "apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=True)
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        assert resp.status_code == 422

    async def test_hibp_unreachable_does_not_block_registration(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """HIBP network failure → fail open → registration proceeds."""

        async def _hibp_raises(*_: object, **__: object) -> bool:
            raise OSError("network unreachable")

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=_hibp_raises),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_FIXED_ACCESS_TOKEN, _FIXED_JTI),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        assert resp.status_code == 201

    async def test_breached_password_no_db_writes(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """No DB writes when HIBP blocks the request."""
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch(
                "apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=True)
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        mock_repo.create_tenant.assert_not_awaited()
        mock_repo.create_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — input schema validation (422 Unprocessable Entity)
# ---------------------------------------------------------------------------


class TestRegisterInputValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("email", "not-an-email"),
            ("email", ""),
            ("password", ""),
            ("full_name", ""),
            ("workspace_name", ""),
        ],
    )
    async def test_missing_or_invalid_fields_return_422(
        self, test_app: FastAPI, field: str, value: str
    ) -> None:
        payload = {**_VALID_PAYLOAD, field: value}
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code == 422

    async def test_missing_required_field_returns_422(self, test_app: FastAPI) -> None:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "workspace_name"}
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests — transaction rollback on IntegrityError
# ---------------------------------------------------------------------------


class TestRegisterIntegrityError:
    async def test_integrity_error_returns_409(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """
        Race condition: email uniqueness check passed but DB INSERT failed
        (another request registered the same email in the interim).
        """
        from sqlalchemy.exc import IntegrityError

        mock_repo.create_tenant.side_effect = IntegrityError(
            statement="INSERT ...", params={}, orig=Exception("unique violation")
        )

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.check_hibp_password", new=AsyncMock(return_value=False)),
            patch("apps.api.routers.auth.hash_password", return_value="$2b$12$hashed"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/auth/register", json=_VALID_PAYLOAD)

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"
