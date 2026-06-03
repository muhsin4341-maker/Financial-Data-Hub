"""
JWT Authentication Middleware and FastAPI Dependency Injection.

Engineering Specification references:
  Part 1, Section 2.3  — Request lifecycle: auth middleware runs first,
                          attaches tenant_id, user_id, role to request.state
  Part 2, Section 8.2  — JWT access tokens (15-min) + refresh tokens (30-day)
  Part 2, Section 8.3  — JWT payload: { sub, tid, role, exp, jti }
                          Role hierarchy: OWNER > ADMIN > ANALYST > VIEWER
  Part 3, Table 4–8    — Auth = required on all /api/v1/* routes except
                          /auth/register, /auth/login, /auth/refresh,
                          /auth/forgot-password, /auth/reset-password

Architecture — two complementary layers:

1. JWTAuthMiddleware  (Starlette BaseHTTPMiddleware)
   Runs on every HTTP request. Passively extracts and validates the
   Authorization: Bearer token (if present). On success, attaches an
   AuthRequestContext to request.state.auth_context so that:
     - Audit middleware (M1-Step15) can log user/tenant without re-decoding.
     - Rate limiter middleware (M1-Step16) can branch by tier without re-decoding.
   Does NOT raise on missing or invalid tokens — enforcement is the
   Depends() layer's responsibility. This keeps public routes simple.

2. FastAPI Depends() functions
   Route handlers declare the exact auth level they require:
     @router.get("/jobs")
     async def list_jobs(ctx = Depends(require_analyst)):  # ANALYST or above
         ...
   Available guards (all re-export as module-level callables):
     require_authenticated  — any valid JWT
     require_viewer         — VIEWER or above (= any authenticated user)
     require_analyst        — ANALYST, ADMIN, or OWNER
     require_admin          — ADMIN or OWNER
     require_owner          — OWNER only
     get_current_user       — require_authenticated + DB lookup for User row

Milestone: M1-Step14 — Auth middleware
Status:    COMPLETE
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from apps.api.core.config import Settings, get_settings
from apps.api.core.database import get_db
from apps.api.core.exceptions import ForbiddenError, UnauthorizedError
from apps.api.core.security import TokenPayload, verify_access_token
from apps.api.models import User, UserRole

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Role hierarchy
# ---------------------------------------------------------------------------

#: Numeric rank for each role — higher = more privileged.
#: Spec Part 2, Section 8.2, Decision 3.
_ROLE_RANK: dict[str, int] = {
    UserRole.VIEWER: 0,
    UserRole.ANALYST: 1,
    UserRole.ADMIN: 2,
    UserRole.OWNER: 3,
}


def _role_rank(role: str) -> int:
    """
    Return the numeric rank of a role string.

    Returns -1 for any unrecognised value so that an invalid role in a JWT
    payload is always treated as less privileged than VIEWER.
    """
    return _ROLE_RANK.get(role, -1)


# ---------------------------------------------------------------------------
# AuthRequestContext — immutable per-request auth snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthRequestContext:
    """
    Immutable snapshot of a successfully authenticated request.

    Populated by ``JWTAuthMiddleware`` and stored as
    ``request.state.auth_context``. All downstream middleware and Depends()
    functions read from this object rather than re-decoding the token.

    Fields mirror the JWT payload exactly (Spec Part 2, Section 8.3):
      sub  → user_id     (UUID)
      tid  → tenant_id   (UUID)
      role → role        (str)
      jti  → jti         (str)
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    jti: str
    payload: TokenPayload


# ---------------------------------------------------------------------------
# JWTAuthMiddleware — passive extraction layer
# ---------------------------------------------------------------------------


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that passively extracts the JWT on every request.

    Behaviour
    ---------
    * Generates a ``request_id`` (UUID4) and attaches it to ``request.state``
      so the error handler and audit middleware can correlate log entries.
    * Binds ``request_id`` to the structlog context-var store so every log
      line emitted during the request automatically includes it.
    * Tries to extract ``Authorization: Bearer <token>``.
      - No header present  → ``request.state.auth_context = None``, silent.
      - Token invalid/expired → ``request.state.auth_context = None``,
        logs WARNING. The request proceeds; the Depends() layer will raise
        401 for any protected route.
      - Token valid → populates ``request.state.auth_context`` with a frozen
        ``AuthRequestContext``. Binds ``user_id``, ``tenant_id``, ``role``
        to structlog context-vars.
    * Clears structlog context-vars after the response is sent.

    Configuration
    -------------
    ``settings`` may be injected at construction time for testing:
        app.add_middleware(JWTAuthMiddleware, settings=override)
    Defaults to ``get_settings()`` when omitted.
    """

    def __init__(
        self,
        app: object,
        *,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings  # None → resolved lazily per request

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # ── 1. Assign a unique request ID for end-to-end tracing ─────────────
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        request.state.auth_context = None

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # ── 2. Extract and validate the Bearer token ──────────────────────────
        auth_header: str = request.headers.get("Authorization", "")

        if auth_header.startswith("Bearer "):
            raw_token = auth_header[len("Bearer ") :]
            settings = self._settings or get_settings()

            try:
                payload = verify_access_token(raw_token, settings=settings)

                ctx = AuthRequestContext(
                    user_id=payload.sub,
                    tenant_id=payload.tid,
                    role=payload.role,
                    jti=payload.jti,
                    payload=payload,
                )
                request.state.auth_context = ctx

                structlog.contextvars.bind_contextvars(
                    user_id=str(payload.sub),
                    tenant_id=str(payload.tid),
                    role=payload.role,
                )
                logger.debug(
                    "auth.token_valid",
                    path=request.url.path,
                    method=request.method,
                )

            except UnauthorizedError as exc:
                # Invalid / expired token — log and continue with None context.
                # The Depends() layer will raise 401 for any protected route.
                logger.warning(
                    "auth.token_invalid",
                    path=request.url.path,
                    method=request.method,
                    reason=exc.message,
                )

        elif auth_header:
            # Malformed Authorization header (not "Bearer ...")
            logger.warning(
                "auth.malformed_header",
                path=request.url.path,
                method=request.method,
                header_prefix=auth_header[:20],
            )

        # ── 3. Forward to the next layer ──────────────────────────────────────
        response = await call_next(request)

        # ── 4. Clean up structlog context-vars to prevent leakage ─────────────
        structlog.contextvars.clear_contextvars()
        return response


# ---------------------------------------------------------------------------
# Internal helper — read auth context from request.state
# ---------------------------------------------------------------------------


def _get_auth_context(request: Request) -> AuthRequestContext | None:
    """
    Read the ``AuthRequestContext`` set by ``JWTAuthMiddleware``.

    Returns ``None`` when:
      - No ``Authorization`` header was present.
      - The token was present but invalid/expired.
      - ``JWTAuthMiddleware`` is not registered (e.g. raw unit test).

    This function is intentionally *not* exported as a public API —
    callers should use ``require_authenticated`` or a role guard instead.
    """
    return getattr(request.state, "auth_context", None)


# ---------------------------------------------------------------------------
# require_authenticated — enforce valid JWT (any role)
# ---------------------------------------------------------------------------


def require_authenticated(
    ctx: AuthRequestContext | None = Depends(_get_auth_context),
) -> AuthRequestContext:
    """
    FastAPI dependency — requires a valid JWT; rejects anonymous requests.

    Usage::

        @router.get("/me")
        async def get_me(ctx: AuthRequestContext = Depends(require_authenticated)):
            return {"user_id": str(ctx.user_id)}

    Raises:
        UnauthorizedError (HTTP 401): No token, invalid token, or expired token.
    """
    if ctx is None:
        raise UnauthorizedError("Authentication required")
    return ctx


# ---------------------------------------------------------------------------
# require_role — RBAC enforcement factory
# ---------------------------------------------------------------------------


def require_role(minimum_role: UserRole) -> Callable[..., AuthRequestContext]:
    """
    FastAPI dependency *factory* — returns a Depends()-compatible function
    that enforces a minimum role level.

    Role hierarchy (Spec Part 2, Section 8.2, Decision 3):
        OWNER (3) > ADMIN (2) > ANALYST (1) > VIEWER (0)

    ``require_role(UserRole.ANALYST)`` passes for ANALYST, ADMIN, and OWNER;
    rejects VIEWER and any unrecognised role string.

    Usage::

        @router.post("/jobs")
        async def create_job(ctx: AuthRequestContext = Depends(require_analyst)):
            ...

    Args:
        minimum_role: The lowest ``UserRole`` that may access the route.

    Returns:
        A FastAPI Depends()-compatible function. Assign it to a module-level
        name (e.g. ``require_analyst = require_role(UserRole.ANALYST)``)
        so FastAPI can hash it for dependency de-duplication.

    Raises:
        UnauthorizedError (HTTP 401): No valid JWT.
        ForbiddenError    (HTTP 403): Valid JWT but insufficient role.
    """
    minimum_rank = _role_rank(minimum_role)

    def _check_role(
        ctx: AuthRequestContext = Depends(require_authenticated),
    ) -> AuthRequestContext:
        user_rank = _role_rank(ctx.role)
        if user_rank < minimum_rank:
            logger.warning(
                "auth.forbidden",
                user_id=str(ctx.user_id),
                tenant_id=str(ctx.tenant_id),
                user_role=ctx.role,
                required_role=minimum_role.value,
            )
            raise ForbiddenError(
                f"This action requires {minimum_role.value} role or above. "
                f"Your current role is {ctx.role!r}."
            )
        return ctx

    # Give FastAPI a stable name so it can de-duplicate equal dependencies.
    _check_role.__name__ = f"require_{minimum_role.value}"
    return _check_role


# ---------------------------------------------------------------------------
# Role-specific guard shortcuts (pre-built; import and use directly)
# ---------------------------------------------------------------------------

#: Any authenticated user (VIEWER and above). Equivalent to require_authenticated.
require_viewer: Callable[..., AuthRequestContext] = require_role(UserRole.VIEWER)

#: ANALYST, ADMIN, or OWNER — for job creation and export.
require_analyst: Callable[..., AuthRequestContext] = require_role(UserRole.ANALYST)

#: ADMIN or OWNER — for user management endpoints.
require_admin: Callable[..., AuthRequestContext] = require_role(UserRole.ADMIN)

#: OWNER only — for billing, workspace deletion.
require_owner: Callable[..., AuthRequestContext] = require_role(UserRole.OWNER)


# ---------------------------------------------------------------------------
# get_current_user — authenticated identity + DB validation
# ---------------------------------------------------------------------------


async def get_current_user(
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency — resolves the authenticated user to a database row.

    Combines JWT validation (via ``require_authenticated``) with a database
    lookup that confirms the user:
      - Still exists in the ``users`` table.
      - Has not been soft-deleted (``deleted_at IS NULL``).
      - Is active (``is_active = TRUE``).

    Use this when a route needs full ``User`` model attributes (e.g. email,
    TOTP state). Use ``require_authenticated`` when only the JWT payload
    context is needed — it avoids the DB round-trip.

    TODO M1-Step16: Also check whether ``ctx.jti`` appears in the Redis
    token blocklist to catch tokens invalidated by logout before expiry.

    Args:
        ctx: Resolved by ``require_authenticated`` — raises 401 if absent.
        db:  Async database session from ``get_db``.

    Returns:
        The active, non-deleted ``User`` ORM instance.

    Raises:
        UnauthorizedError (HTTP 401): Valid JWT but user row not found,
            deactivated, or soft-deleted (account may have been purged after
            the token was issued).
    """
    result = await db.execute(
        select(User).where(
            User.id == ctx.user_id,
            User.deleted_at.is_(None),
            User.is_active.is_(True),
        )
    )
    user: User | None = result.scalar_one_or_none()

    if user is None:
        logger.warning(
            "auth.user_not_found",
            user_id=str(ctx.user_id),
            tenant_id=str(ctx.tenant_id),
        )
        raise UnauthorizedError("User account not found or has been deactivated")

    return user


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    # Types
    "AuthRequestContext",
    # Middleware
    "JWTAuthMiddleware",
    # Dependencies — require these in route handlers
    "require_authenticated",
    "require_viewer",
    "require_analyst",
    "require_admin",
    "require_owner",
    "require_role",
    "get_current_user",
]
