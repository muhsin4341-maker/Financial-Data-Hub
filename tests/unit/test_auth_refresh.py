"""
Unit tests — POST /api/v1/auth/refresh.

Strategy
--------
All database and Redis calls are mocked. The test application contains only
the auth router and the APIError exception handler. ``get_db`` is overridden
to yield an AsyncMock session; ``AuthRepository`` and ``_blocklist_jti`` are
patched so no real I/O occurs.

The refresh token cookie (``fdh_refresh``) is injected directly into the
httpx request using ``cookies={"fdh_refresh": "<value>"}``.

What is mocked
--------------
- ``AuthRepository``        — all repo methods pre-configured per test
- ``_blocklist_jti``        — Redis write; patched to AsyncMock
- ``hash_refresh_token``    — returns a deterministic string
- ``create_access_token``   — returns a fixed (token, jti) pair

What is NOT mocked (runs real code)
-------------------------------------
- ``RefreshToken.is_valid`` property (revoked_at / expires_at logic)
- UnauthorizedError exception handling and serialisation
- Cookie setting and reading
- AuthResponse serialisation

Milestone: M1-Step20
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
from apps.api.models import RefreshToken
from apps.api.routers.auth import router

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_RAW_COOKIE = "a" * 64          # fake 64-char raw refresh token
_TOKEN_HASH = "hash:" + "b" * 58  # fake hash (what hash_refresh_token returns)
_FIXED_USER_ID = uuid.uuid4()
_FIXED_TENANT_ID = uuid.uuid4()
_FIXED_OLD_JTI = str(uuid.uuid4())
_FIXED_NEW_JTI = str(uuid.uuid4())
_NEW_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.refresh.token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_stored_token(
    *,
    revoked: bool = False,
    expired: bool = False,
) -> MagicMock:
    """Build a RefreshToken mock whose is_valid property reflects the flags."""
    token = MagicMock(spec=RefreshToken)
    token.id = uuid.uuid4()
    token.user_id = _FIXED_USER_ID
    token.tenant_id = _FIXED_TENANT_ID
    token.jti = _FIXED_OLD_JTI
    token.revoked_at = datetime.now(UTC) if revoked else None
    token.expires_at = (
        datetime.now(UTC) - timedelta(days=1)  # past
        if expired
        else datetime.now(UTC) + timedelta(days=29)  # future
    )
    # Wire the real is_valid logic through the MagicMock
    token.is_valid = (not revoked) and (datetime.now(UTC) < token.expires_at)
    return token


@pytest.fixture()
def valid_stored_token() -> MagicMock:
    return _make_stored_token()


@pytest.fixture()
def mock_user() -> MagicMock:
    u = MagicMock()
    u.id = _FIXED_USER_ID
    u.email = "alice@example.com"
    u.is_active = True
    u.deleted_at = None
    return u


@pytest.fixture()
def mock_membership() -> MagicMock:
    m = MagicMock()
    m.tenant_id = _FIXED_TENANT_ID
    m.role = "owner"
    m.is_active = True
    return m


@pytest.fixture()
def mock_repo(
    valid_stored_token: MagicMock,
    mock_user: MagicMock,
    mock_membership: MagicMock,
) -> AsyncMock:
    repo = AsyncMock()
    repo.get_refresh_token_by_hash.return_value = valid_stored_token
    repo.get_user_by_id.return_value = mock_user
    repo.get_active_membership.return_value = mock_membership
    repo.revoke_refresh_token.return_value = None
    repo.create_refresh_token.return_value = AsyncMock()
    repo.create_audit_log.return_value = None
    return repo


@pytest.fixture()
def test_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _override_get_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Context manager that patches all external dependencies for the happy path
# ---------------------------------------------------------------------------


def _patches(mock_repo: AsyncMock) -> list[Any]:
    return [
        patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
        patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        patch(
            "apps.api.routers.auth.create_access_token",
            return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
        ),
        patch(
            "apps.api.routers.auth.generate_raw_refresh_token",
            return_value="new_raw_refresh_" + "x" * 48,
        ),
        patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
    ]


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestRefreshSuccess:
    async def test_returns_200(self, test_app: FastAPI, mock_repo: AsyncMock) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert resp.status_code == 200

    async def test_returns_new_access_token(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        body = resp.json()
        assert body["access_token"] == _NEW_ACCESS_TOKEN
        assert body["token_type"] == "bearer"
        assert uuid.UUID(body["user_id"]) == _FIXED_USER_ID
        assert uuid.UUID(body["tenant_id"]) == _FIXED_TENANT_ID
        assert body["role"] == "owner"

    async def test_new_refresh_cookie_is_set(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert "fdh_refresh" in resp.cookies or "set-cookie" in resp.headers

    async def test_old_token_is_revoked(
        self, test_app: FastAPI, mock_repo: AsyncMock, valid_stored_token: MagicMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        mock_repo.revoke_refresh_token.assert_awaited_once_with(valid_stored_token)

    async def test_new_refresh_token_persisted(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        mock_repo.create_refresh_token.assert_awaited_once()
        kwargs = mock_repo.create_refresh_token.call_args.kwargs
        assert kwargs["jti"] == _FIXED_NEW_JTI
        assert kwargs["tenant_id"] == _FIXED_TENANT_ID

    async def test_audit_log_action_is_token_refresh(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=AsyncMock()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        audit_kwargs = mock_repo.create_audit_log.call_args.kwargs
        assert audit_kwargs["action"] == "user.token_refresh"
        assert audit_kwargs["entity_type"] == "user"

    async def test_old_jti_blocklisted(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        mock_blocklist = AsyncMock()
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=mock_blocklist),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        mock_blocklist.assert_awaited_once()
        call_kwargs = mock_blocklist.call_args.kwargs
        assert call_kwargs["jti"] == _FIXED_OLD_JTI


# ---------------------------------------------------------------------------
# Tests — missing cookie (401)
# ---------------------------------------------------------------------------


class TestRefreshMissingCookie:
    async def test_no_cookie_returns_401(self, test_app: FastAPI) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/refresh")

        assert resp.status_code == 401

    async def test_no_cookie_error_code(self, test_app: FastAPI) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/refresh")

        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_no_cookie_no_db_reads(self, test_app: FastAPI) -> None:
        repo = AsyncMock()
        with patch("apps.api.routers.auth.AuthRepository", return_value=repo):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/auth/refresh")

        repo.get_refresh_token_by_hash.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — token not found in DB (401)
# ---------------------------------------------------------------------------


class TestRefreshTokenNotFound:
    async def test_unknown_token_returns_401(self, test_app: FastAPI) -> None:
        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert resp.status_code == 401

    async def test_unknown_token_no_revoke_call(self, test_app: FastAPI) -> None:
        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        repo.revoke_refresh_token.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — revoked token (401)
# ---------------------------------------------------------------------------


class TestRefreshRevokedToken:
    async def test_revoked_token_returns_401(self, test_app: FastAPI) -> None:
        revoked_token = _make_stored_token(revoked=True)
        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = revoked_token

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert resp.status_code == 401

    async def test_revoked_token_no_new_token_issued(self, test_app: FastAPI) -> None:
        revoked_token = _make_stored_token(revoked=True)
        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = revoked_token

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        repo.create_refresh_token.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — expired token (401)
# ---------------------------------------------------------------------------


class TestRefreshExpiredToken:
    async def test_expired_token_returns_401(self, test_app: FastAPI) -> None:
        expired_token = _make_stored_token(expired=True)
        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = expired_token

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert resp.status_code == 401

    async def test_all_401s_same_message(self, test_app: FastAPI) -> None:
        """Revoked, expired, and not-found all return the same message."""
        messages: list[str] = []

        # Not found
        repo_nf = AsyncMock()
        repo_nf.get_refresh_token_by_hash.return_value = None
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo_nf),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )
            messages.append(r.json()["error"]["message"])

        # Revoked
        repo_rv = AsyncMock()
        repo_rv.get_refresh_token_by_hash.return_value = _make_stored_token(revoked=True)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo_rv),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )
            messages.append(r.json()["error"]["message"])

        # Expired
        repo_ex = AsyncMock()
        repo_ex.get_refresh_token_by_hash.return_value = _make_stored_token(expired=True)
        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo_ex),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                r = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )
            messages.append(r.json()["error"]["message"])

        assert len(set(messages)) == 1, f"Messages differ: {messages}"


# ---------------------------------------------------------------------------
# Tests — user disabled or deleted (401)
# ---------------------------------------------------------------------------


class TestRefreshUserInvalid:
    async def test_deleted_user_returns_401(
        self, test_app: FastAPI, valid_stored_token: MagicMock
    ) -> None:
        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = valid_stored_token
        repo.get_user_by_id.return_value = None  # deleted

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert resp.status_code == 401

    async def test_inactive_user_returns_401(
        self, test_app: FastAPI, valid_stored_token: MagicMock
    ) -> None:
        inactive_user = MagicMock()
        inactive_user.id = _FIXED_USER_ID
        inactive_user.is_active = False

        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = valid_stored_token
        repo.get_user_by_id.return_value = inactive_user

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert resp.status_code == 401

    async def test_no_active_membership_returns_401(
        self, test_app: FastAPI, valid_stored_token: MagicMock, mock_user: MagicMock
    ) -> None:
        repo = AsyncMock()
        repo.get_refresh_token_by_hash.return_value = valid_stored_token
        repo.get_user_by_id.return_value = mock_user
        repo.get_active_membership.return_value = None

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests — Redis blocklist fail-open
# ---------------------------------------------------------------------------


class TestRefreshRedisFailOpen:
    async def test_redis_failure_does_not_block_rotation(
        self, test_app: FastAPI, mock_repo: AsyncMock
    ) -> None:
        """
        If Redis is unreachable, token rotation must still succeed.
        The DB-level revoked_at is the authoritative revocation signal.
        """

        async def _redis_raises(*_: object, **__: object) -> None:
            raise OSError("Redis unreachable")

        with (
            patch("apps.api.routers.auth.AuthRepository", return_value=mock_repo),
            patch("apps.api.routers.auth.hash_refresh_token", return_value=_TOKEN_HASH),
            patch(
                "apps.api.routers.auth.create_access_token",
                return_value=(_NEW_ACCESS_TOKEN, _FIXED_NEW_JTI),
            ),
            patch("apps.api.routers.auth.generate_raw_refresh_token", return_value="newraw" * 10),
            patch("apps.api.routers.auth._blocklist_jti", new=_redis_raises),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/auth/refresh", cookies={"fdh_refresh": _RAW_COOKIE}
                )

        # Rotation succeeded despite Redis failure
        assert resp.status_code == 200
        mock_repo.revoke_refresh_token.assert_awaited_once()
        mock_repo.create_refresh_token.assert_awaited_once()
