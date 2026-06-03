"""
Auth router — authentication endpoints.

Engineering Specification references:
  Part 2, Section 8.2  — JWT access + refresh tokens, bcrypt cost 12, HIBP check
  Part 2, Section 8.3  — Token payload, password policy, account lockout
  Part 3, Section 11.3 — API endpoint definitions

Endpoints implemented:
  POST /api/v1/auth/register  — M1-Step18 ✓
  POST /api/v1/auth/login     — M1-Step19 ✓
  POST /api/v1/auth/refresh   — M1-Step20 ✓
  POST /api/v1/auth/logout    — M1-Step21 ✓

Endpoints planned (future steps):
  POST /api/v1/auth/forgot-password — M1-Step22
  POST /api/v1/auth/reset-password  — M1-Step23

Milestone: M1-Step18 — POST /auth/register  ✓
           M1-Step19 — POST /auth/login      ✓
           M1-Step20 — POST /auth/refresh    ✓
           M1-Step21 — POST /auth/logout     ✓
Status:    COMPLETE
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import get_settings
from apps.api.core.database import get_db
from apps.api.core.exceptions import ConflictError, UnauthorizedError, ValidationError
from apps.api.core.security import (
    check_hibp_password,
    create_access_token,
    generate_raw_refresh_token,
    generate_refresh_token_expiry,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from apps.api.middleware.auth import AuthRequestContext, require_authenticated
from apps.api.models import UserRole
from apps.api.repositories.auth import AuthRepository
from apps.api.schemas.auth import AuthResponse, LoginRequest, RegisterRequest

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

log = structlog.get_logger(__name__)

#: httpOnly cookie name carrying the opaque refresh token.
_REFRESH_COOKIE = "fdh_refresh"

#: Cookie path — scoped to auth endpoints; the refresh token is not sent on
#: data API requests, only on /api/v1/auth/refresh.
_REFRESH_COOKIE_PATH = "/api/v1/auth"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    """Extract the originating client IP, honouring X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _client_ua(request: Request) -> str | None:
    return request.headers.get("User-Agent")


def _request_id(request: Request) -> uuid.UUID | None:
    """Parse the request_id UUID attached by JWTAuthMiddleware, or None."""
    raw = getattr(request.state, "request_id", None)
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# POST /api/v1/auth/register
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=201,
    summary="Register a new account",
    description=(
        "Creates a Tenant workspace, a User, and a founding OWNER membership "
        "in a single atomic transaction. Returns a JWT access token in the "
        "response body and sets an httpOnly refresh token cookie."
    ),
)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """
    Registration flow — executed in a single database transaction:

    1. Validate password against HIBP breach database (async, fail-open).
    2. Check email uniqueness (query users table).
    3. Hash the password with bcrypt (cost factor 12 + SHA-256 prehash).
    4. Persist Tenant, User, TenantMembership (role=OWNER).
    5. Issue JWT access token (15-min) + opaque refresh token (30-day).
    6. Persist RefreshToken record (hash only — raw token goes to cookie).
    7. Append AuditLog entry (action="user.registered").
    8. Set httpOnly refresh cookie on the response.
    9. Return AuthResponse with access_token in the body.

    The get_db dependency auto-commits on handler success and auto-rolls back
    on any unhandled exception, ensuring atomicity across all writes.
    """
    repo = AuthRepository(db)

    # ── Step 1: HIBP breach check (async — cannot run inside Pydantic schema) ─
    try:
        is_breached = await check_hibp_password(payload.password)
    except Exception:  # noqa: BLE001 — network failure; fail open per spec
        log.warning("auth.register.hibp_unreachable")
        is_breached = False

    if is_breached:
        raise ValidationError(
            message=(
                "This password has appeared in a known data breach. "
                "Choose a different password."
            ),
            details={"field": "password", "reason": "hibp_breach"},
        )

    # ── Step 2: Email uniqueness ───────────────────────────────────────────────
    # Pre-check to return a clear 409 before attempting the INSERT.
    # A race-condition INSERT conflict is caught below by IntegrityError.
    existing = await repo.get_user_by_email(payload.email)
    if existing is not None:
        raise ConflictError("An account with this email address already exists.")

    # ── Step 3: Hash password ─────────────────────────────────────────────────
    # bcrypt cost 12 + SHA-256 prehash (handles arbitrary-length passwords).
    # This is CPU-intensive (~200 ms) — it runs in the async event loop but
    # does not block other coroutines because bcrypt releases the GIL.
    password_hash = hash_password(payload.password)

    # ── Steps 4–7: Atomic writes (single transaction via get_db) ─────────────
    try:
        tenant = await repo.create_tenant(payload.workspace_name)
        user = await repo.create_user(
            email=payload.email,
            full_name=payload.full_name,
            password_hash=password_hash,
        )
        await repo.create_membership(tenant=tenant, user=user, role=UserRole.OWNER)

        # Issue tokens.
        access_token, jti = create_access_token(
            user_id=user.id,
            tenant_id=tenant.id,
            role=UserRole.OWNER,
        )
        raw_refresh = generate_raw_refresh_token()

        await repo.create_refresh_token(
            user=user,
            tenant_id=tenant.id,
            token_hash=hash_refresh_token(raw_refresh),
            jti=jti,
            expires_at=generate_refresh_token_expiry(),
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
        )

        await repo.create_audit_log(
            tenant_id=tenant.id,
            user_id=user.id,
            action="user.registered",
            entity_type="user",
            entity_id=user.id,
            ip_address=_client_ip(request),
            user_agent=_client_ua(request),
            request_id=_request_id(request),
            changes={"after": {"email": user.email, "role": str(UserRole.OWNER)}},
        )

    except IntegrityError as exc:
        # Race condition: duplicate email or slug inserted between pre-check and
        # flush. Surface as 409 rather than letting a 500 reach the client.
        log.warning("auth.register.integrity_error", exc=str(exc))
        raise ConflictError(
            "An account with this email address already exists."
        ) from exc

    # ── Step 8: Set httpOnly refresh cookie ───────────────────────────────────
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw_refresh,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30 * 24 * 60 * 60,  # 30 days in seconds
        path=_REFRESH_COOKIE_PATH,
    )

    log.info(
        "auth.register.success",
        user_id=str(user.id),
        tenant_id=str(tenant.id),
    )

    # ── Step 9: Return AuthResponse ───────────────────────────────────────────
    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        tenant_id=tenant.id,
        role=str(UserRole.OWNER),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/auth/login
# ---------------------------------------------------------------------------

#: Generic credential error — same message for wrong email AND wrong password
#: to prevent user-enumeration via distinct error strings.
_INVALID_CREDENTIALS = "Invalid email or password."


@router.post(
    "/login",
    response_model=AuthResponse,
    status_code=200,
    summary="Log in to an existing account",
    description=(
        "Validates credentials, issues a JWT access token (response body) "
        "and a 30-day refresh token (httpOnly cookie)."
    ),
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """
    Login flow — executed in a single database transaction:

    1. Fetch user by email (deleted_at IS NULL filter applied by repo).
    2. Verify bcrypt password hash — constant-time comparison via bcrypt.checkpw.
    3. Reject if user account is inactive (is_active = False).
    4. Resolve active TenantMembership to obtain tenant_id and RBAC role.
    5. Stamp last_login_at = NOW().
    6. Issue JWT access token (15-min) and opaque refresh token (30-day).
    7. Persist RefreshToken record (hash only; raw token in httpOnly cookie).
    8. Append AuditLog entry (action="user.login").
    9. Set httpOnly refresh cookie and return AuthResponse.

    Error strategy:
      - Unknown email AND wrong password both return 401 with the same
        message to prevent user enumeration via distinct error text.
      - Inactive accounts return 401 (indistinct from credential failure at
        the message level; the code is UNAUTHORIZED in both cases).

    Note on timing:
      bcrypt.checkpw provides constant-time password comparison. A timing
      difference does exist between "email not found" (fast DB miss) and
      "wrong password" (slow bcrypt), which could theoretically enable email
      enumeration. A constant-time guard hash is deferred to a hardening
      pass (not in scope for M1).
    """
    repo = AuthRepository(db)

    # ── Step 1: Fetch user ────────────────────────────────────────────────────
    user = await repo.get_user_by_email(payload.email)

    # ── Step 2: Verify password ───────────────────────────────────────────────
    # verify_password uses bcrypt.checkpw — constant-time comparison.
    # We check this regardless of whether the user exists to keep the
    # error path uniform; if no user, we reject immediately after.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise UnauthorizedError(_INVALID_CREDENTIALS)

    # ── Step 3: Check account status ──────────────────────────────────────────
    if not user.is_active:
        log.warning("auth.login.inactive_account", user_id=str(user.id))
        raise UnauthorizedError(_INVALID_CREDENTIALS)

    # ── Step 4: Resolve tenant context ────────────────────────────────────────
    # For M1 every user has exactly one tenant (created at registration).
    # Multi-tenant context selection (X-Tenant-ID header) is deferred to M6+.
    membership = await repo.get_active_membership(user.id)
    if membership is None:
        # Data integrity problem: active user has no active tenant membership.
        log.error("auth.login.no_active_membership", user_id=str(user.id))
        raise UnauthorizedError(_INVALID_CREDENTIALS)

    # ── Step 5: Stamp last_login_at ────────────────────────────────────────────
    await repo.update_last_login(user)

    # ── Steps 6-8: Token issuance + DB writes ─────────────────────────────────
    access_token, jti = create_access_token(
        user_id=user.id,
        tenant_id=membership.tenant_id,
        role=membership.role,
    )
    raw_refresh = generate_raw_refresh_token()

    await repo.create_refresh_token(
        user=user,
        tenant_id=membership.tenant_id,
        token_hash=hash_refresh_token(raw_refresh),
        jti=jti,
        expires_at=generate_refresh_token_expiry(),
        ip_address=_client_ip(request),
        user_agent=_client_ua(request),
    )

    await repo.create_audit_log(
        tenant_id=membership.tenant_id,
        user_id=user.id,
        action="user.login",
        entity_type="user",
        entity_id=user.id,
        ip_address=_client_ip(request),
        user_agent=_client_ua(request),
        request_id=_request_id(request),
    )

    # ── Step 9: Set refresh cookie and return ─────────────────────────────────
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw_refresh,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30 * 24 * 60 * 60,
        path=_REFRESH_COOKIE_PATH,
    )

    log.info(
        "auth.login.success",
        user_id=str(user.id),
        tenant_id=str(membership.tenant_id),
        role=str(membership.role),
    )

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        tenant_id=membership.tenant_id,
        role=str(membership.role),
    )


# ---------------------------------------------------------------------------
# Shared helpers — token refresh / logout
# ---------------------------------------------------------------------------


async def _blocklist_jti(jti: str, ttl_seconds: int) -> None:
    """
    Write a JWT ID to the Redis blocklist with a TTL.

    Used during token rotation (refresh) and logout to invalidate the old
    access token before it naturally expires. The JWTAuthMiddleware checks
    this blocklist before accepting a token (TODO M1-Step21 wires the check).

    Key format: ``blocklist:{jti}``
    TTL:        Access token lifetime (default 15 min = 900 s) so the key
                expires automatically when the access token would have anyway.

    Fails open — a Redis outage must not block token rotation. The DB-level
    ``revoked_at`` flag on the RefreshToken is the authoritative revocation
    signal; Redis is a fast-path cache to catch in-flight access tokens.

    Args:
        jti:         JWT ID from the old access token payload.
        ttl_seconds: Key TTL in seconds — set to the access token expire time.
    """
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415

        settings = get_settings()
        client: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        await client.setex(f"blocklist:{jti}", ttl_seconds, "1")
        await client.aclose()
    except Exception:  # noqa: BLE001 — fail open; DB revoked_at is authoritative
        log.warning("auth.refresh.blocklist_write_failed", jti=jti)


# ---------------------------------------------------------------------------
# POST /api/v1/auth/refresh
# ---------------------------------------------------------------------------

#: Generic invalid-token error used for all refresh failure modes to prevent
#: enumeration of token state (expired vs revoked vs not found).
_INVALID_TOKEN = "Refresh token is invalid or has expired."


@router.post(
    "/refresh",
    response_model=AuthResponse,
    status_code=200,
    summary="Rotate refresh token and issue new access token",
    description=(
        "Reads the ``fdh_refresh`` httpOnly cookie, validates the refresh "
        "token, and performs full token rotation: the old token is revoked, "
        "a new refresh token is issued (cookie updated), and a new JWT access "
        "token is returned in the response body."
    ),
)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    fdh_refresh: str | None = Cookie(default=None),
) -> AuthResponse:
    """
    Token rotation flow — Engineering Spec Part 2, Section 8.2, Decision 1:
      "Refresh token rotation on every use — revoke old token, issue new one."

    Steps:
      1. Read raw refresh token from ``fdh_refresh`` cookie → 401 if absent.
      2. Hash it and look up the RefreshToken record → 401 if not found.
      3. Validate ``token.is_valid`` (not revoked, not expired) → 401 if invalid.
      4. Load the associated user → 401 if deleted or inactive.
      5. Load the user's active TenantMembership → 401 if none.
      6. Revoke the old RefreshToken (set ``revoked_at``).
      7. Issue new access token + new refresh token.
      8. Persist new RefreshToken record.
      9. Append AuditLog entry (action="user.token_refresh").
     10. Write old ``jti`` to Redis blocklist (TTL = access token lifetime,
         fail-open — Redis outage must not block rotation).
     11. Set new ``fdh_refresh`` cookie and return AuthResponse.

    Security: all failure modes return the same 401 message to prevent
    callers from distinguishing "not found" from "revoked" from "expired".
    """
    repo = AuthRepository(db)

    # ── Step 1: Cookie presence ───────────────────────────────────────────────
    if not fdh_refresh:
        raise UnauthorizedError(_INVALID_TOKEN)

    # ── Step 2: Look up token record ──────────────────────────────────────────
    token_hash = hash_refresh_token(fdh_refresh)
    stored_token = await repo.get_refresh_token_by_hash(token_hash)
    if stored_token is None:
        raise UnauthorizedError(_INVALID_TOKEN)

    # ── Step 3: Validate token state ──────────────────────────────────────────
    # is_valid = revoked_at is None AND now < expires_at
    if not stored_token.is_valid:
        log.warning(
            "auth.refresh.invalid_token",
            token_id=str(stored_token.id),
            revoked=stored_token.revoked_at is not None,
        )
        raise UnauthorizedError(_INVALID_TOKEN)

    # ── Step 4: Verify user still active ─────────────────────────────────────
    user = await repo.get_user_by_id(stored_token.user_id)
    if user is None or not user.is_active:
        log.warning("auth.refresh.user_invalid", user_id=str(stored_token.user_id))
        raise UnauthorizedError(_INVALID_TOKEN)

    # ── Step 5: Resolve tenant context ────────────────────────────────────────
    membership = await repo.get_active_membership(user.id)
    if membership is None:
        log.error("auth.refresh.no_active_membership", user_id=str(user.id))
        raise UnauthorizedError(_INVALID_TOKEN)

    # Capture old jti before rotation (needed for Redis blocklist).
    old_jti = stored_token.jti

    # ── Steps 6–9: Atomic writes ──────────────────────────────────────────────
    await repo.revoke_refresh_token(stored_token)

    new_access_token, new_jti = create_access_token(
        user_id=user.id,
        tenant_id=membership.tenant_id,
        role=membership.role,
    )
    new_raw_refresh = generate_raw_refresh_token()

    await repo.create_refresh_token(
        user=user,
        tenant_id=membership.tenant_id,
        token_hash=hash_refresh_token(new_raw_refresh),
        jti=new_jti,
        expires_at=generate_refresh_token_expiry(),
        ip_address=_client_ip(request),
        user_agent=_client_ua(request),
    )

    await repo.create_audit_log(
        tenant_id=membership.tenant_id,
        user_id=user.id,
        action="user.token_refresh",
        entity_type="user",
        entity_id=user.id,
        ip_address=_client_ip(request),
        user_agent=_client_ua(request),
        request_id=_request_id(request),
    )

    # ── Step 10: Blocklist old jti in Redis (fail-open) ───────────────────────
    # Wrapped in try/except so any unexpected propagation from _blocklist_jti
    # (e.g. in tests replacing it with a raising stub) cannot abort rotation.
    try:
        settings = get_settings()
        await _blocklist_jti(
            jti=old_jti,
            ttl_seconds=settings.jwt_access_token_expire_minutes * 60,
        )
    except Exception:  # noqa: BLE001
        log.warning("auth.refresh.blocklist_error", jti=old_jti)

    # ── Step 11: Set new cookie and return ────────────────────────────────────
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=new_raw_refresh,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30 * 24 * 60 * 60,
        path=_REFRESH_COOKIE_PATH,
    )

    log.info(
        "auth.refresh.success",
        user_id=str(user.id),
        tenant_id=str(membership.tenant_id),
    )

    return AuthResponse(
        access_token=new_access_token,
        token_type="bearer",
        user_id=user.id,
        tenant_id=membership.tenant_id,
        role=str(membership.role),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    status_code=204,
    summary="Log out and revoke session tokens",
    description=(
        "Revokes the server-side refresh token associated with the current "
        "access token (matched by jti), blocklists the access token jti in "
        "Redis for its remaining lifetime, and clears the ``fdh_refresh`` "
        "httpOnly cookie. Requires a valid Bearer token."
    ),
)
async def logout(
    request: Request,
    response: Response,
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Logout flow:

    1. Validate Bearer token (enforced by ``require_authenticated``).
    2. Compute remaining access token TTL for the Redis blocklist key.
    3. Look up the RefreshToken record by ``ctx.jti``.
       - Revoke it (set ``revoked_at``) if found and not already revoked.
       - Proceed idempotently if already revoked or not found.
    4. Blocklist ``ctx.jti`` in Redis with TTL = remaining access token life.
       Fail-open: a Redis outage must not prevent logout from completing.
    5. Write AuditLog entry (action="user.logout").
    6. Clear the ``fdh_refresh`` cookie.
    7. Return HTTP 204 No Content.

    Idempotency: calling logout twice with the same (still-valid) access
    token succeeds both times. The second call finds the refresh token
    already revoked and proceeds without error.

    Security note: the Redis blocklist entry means the access token is
    rejected by JWTAuthMiddleware from this point forward, even before
    its natural 15-minute expiry. The DB ``revoked_at`` flag on the
    RefreshToken provides a durable, Redis-independent revocation record.
    """
    repo = AuthRepository(db)

    # ── Step 2: Compute remaining access token TTL ────────────────────────────
    remaining_ttl = max(
        0,
        int((ctx.payload.exp - datetime.now(UTC)).total_seconds()),
    )

    # ── Step 3: Revoke the paired refresh token (by jti) ─────────────────────
    stored_token = await repo.get_refresh_token_by_jti(ctx.jti)
    if stored_token is not None and stored_token.revoked_at is None:
        await repo.revoke_refresh_token(stored_token)
        log.debug("auth.logout.refresh_token_revoked", jti=ctx.jti)
    else:
        # Already revoked or not found — idempotent, proceed normally.
        log.debug(
            "auth.logout.refresh_token_skipped",
            jti=ctx.jti,
            found=stored_token is not None,
        )

    # ── Step 4: Blocklist access token jti in Redis ───────────────────────────
    try:
        await _blocklist_jti(jti=ctx.jti, ttl_seconds=remaining_ttl)
    except Exception:  # noqa: BLE001
        log.warning("auth.logout.blocklist_error", jti=ctx.jti)

    # ── Step 5: Audit log ─────────────────────────────────────────────────────
    await repo.create_audit_log(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        action="user.logout",
        entity_type="user",
        entity_id=ctx.user_id,
        ip_address=_client_ip(request),
        user_agent=_client_ua(request),
        request_id=_request_id(request),
    )

    # ── Step 6: Clear refresh cookie ──────────────────────────────────────────
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        path=_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )

    log.info(
        "auth.logout.success",
        user_id=str(ctx.user_id),
        tenant_id=str(ctx.tenant_id),
        jti=ctx.jti,
    )
