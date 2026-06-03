"""
Redis-backed Sliding Window Rate Limiter Middleware — M1-Step16.

Engineering Specification references:
  Part 3, Section 11.2, Decision 2 — Rate Limiting:
    Unauthenticated : 20  req/min per IP
    Free tier       : 60  req/min per user_id
    Pro tier        : 300 req/min per user_id
    Enterprise      : 1,000 req/min per tenant_id
    Algorithm       : Redis sliding window counter
    Headers         : X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset

Architecture — position 3 in the middleware stack (innermost):

    JWTAuthMiddleware   — outermost; sets request.state.auth_context + request_id
    AuditMiddleware     — middle; writes audit record after response
    RateLimitMiddleware — innermost; checks Redis BEFORE forwarding to route

``add_middleware()`` prepends, so registration in main.py must be:
    app.add_middleware(RateLimitMiddleware)   # registered last → runs last (innermost)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(JWTAuthMiddleware)     # registered first → runs first (outermost)

Sliding window algorithm
------------------------
An atomic Lua script executes four Redis commands in one round-trip:
  1. ZREMRANGEBYSCORE — prune timestamps older than (now − window)
  2. ZCARD           — count requests still in the window
  3. ZADD            — record this request (score=now_ms, member=unique id)
  4. PEXPIRE         — set TTL = window + 1s (prevents leaked keys)

The script returns (remaining, reset_ms) on success, (-1, reset_ms) when the
limit is exceeded.  Because it runs as a single Lua transaction on the Redis
server, there are no race conditions between the count check and the ZADD.

Rate limit key format
---------------------
  Authenticated   : ``ratelimit:user:{user_id}``
  Unauthenticated : ``ratelimit:ip:{ip_address}``

Both use a 60-second rolling window.  The key is shared across the window
period rather than being bucketed per minute, so a burst at the boundary
cannot double the effective limit.

Tier resolution (M1)
--------------------
All authenticated users in M1 receive the ``free_tier`` limit (60 req/min).
The hook ``_resolve_limit()`` is structured to accept the full ``AuthRequestContext``
so that plan-based tier upgrades (Pro, Enterprise) can be added in a later
milestone by inspecting ``ctx.payload.role`` or a tenant plan lookup — without
changes to the middleware dispatch logic.

Failure mode
------------
If Redis is unreachable, the middleware **fails open**: the request is forwarded
without rate limit enforcement, and the failure is logged at ERROR.  This
prevents a Redis outage from taking down the API, at the cost of temporarily
unenforced limits.

Milestone: M1-Step16 — Rate limiter middleware
Status:    COMPLETE
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from apps.api.core.config import Settings, get_settings
from apps.api.middleware.auth import AuthRequestContext

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Skip list — identical to AuditMiddleware (operational, non-business paths)
# ---------------------------------------------------------------------------

_SKIP_EXACT: frozenset[str] = frozenset(
    {
        "/health",
        "/health/ready",
        "/health/detailed",
    }
)

_SKIP_PREFIXES: tuple[str, ...] = (
    "/api/v1/docs",
    "/api/v1/redoc",
    "/api/v1/openapi.json",
)


def _should_skip(path: str) -> bool:
    """Return True for paths that are never rate-limited."""
    if path in _SKIP_EXACT:
        return True
    return any(path.startswith(p) for p in _SKIP_PREFIXES)


# ---------------------------------------------------------------------------
# IP extraction (duplicated from audit middleware — shared util M2 refactor)
# ---------------------------------------------------------------------------


def _extract_ip(request: Request) -> str | None:
    """Return the real client IP, preferring X-Forwarded-For."""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Atomic Lua script — sliding window counter
# ---------------------------------------------------------------------------
# Accepts 4 ARGV arguments:
#   ARGV[1]  now_ms    — current epoch time in milliseconds
#   ARGV[2]  window_ms — window duration in milliseconds (default 60 000)
#   ARGV[3]  limit     — maximum requests permitted in the window
#   ARGV[4]  member    — unique string for this request (prevents ZADD collision)
#
# Returns a two-element list:
#   [remaining, reset_ms]
#     remaining >= 0  → request allowed; remaining = slots left after this one
#     remaining = -1  → request BLOCKED (limit reached)
#   reset_ms          → millisecond epoch when the oldest in-window entry expires

_SLIDING_WINDOW_LUA = """
local key        = KEYS[1]
local now_ms     = tonumber(ARGV[1])
local window_ms  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local member     = ARGV[4]

-- 1. Prune requests older than the window
redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)

-- 2. Count requests currently in the window
local count = redis.call('ZCARD', key)

-- 3. Compute reset time (oldest entry + window, or now + window if empty)
local reset_ms = now_ms + window_ms
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
if #oldest >= 2 then
    reset_ms = tonumber(oldest[2]) + window_ms
end

-- 4. Reject if already at limit
if count >= limit then
    return {-1, reset_ms}
end

-- 5. Record this request and set TTL
redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms + 1000)

return {limit - count - 1, reset_ms}
"""

#: Window duration: 60 seconds expressed in milliseconds.
_WINDOW_MS: int = 60_000


# ---------------------------------------------------------------------------
# Limit resolution
# ---------------------------------------------------------------------------


def _resolve_limit(
    auth_ctx: AuthRequestContext | None,
    settings: Settings,
) -> tuple[int, str]:
    """
    Return *(limit, identifier)* for the request.

    The identifier becomes the Redis key suffix; the limit is the max
    requests per window for that identifier's tier.

    M1 tier mapping
    ---------------
    Anonymous        → unauthenticated limit per IP
    Authenticated    → free_tier limit per user_id
      (Pro / Enterprise tier upgrades plugged in here when Tenant.plan
       field is populated — no middleware changes required)
    """
    if auth_ctx is None:
        return settings.rate_limit_unauthenticated, "anon"
    # M1: all authenticated users → free tier (plan-based upgrade: future)
    return settings.rate_limit_free_tier, "auth"


# ---------------------------------------------------------------------------
# 429 response builder
# ---------------------------------------------------------------------------


def _build_429_response(
    *,
    limit: int,
    reset_ms: float,
    request_id: str,
) -> JSONResponse:
    """
    Build a standards-compliant 429 Too Many Requests response.

    Headers (per Spec Part 3, §11.2, Decision 2):
      X-RateLimit-Limit     — limit for this identifier's tier
      X-RateLimit-Remaining — always 0 when blocked
      X-RateLimit-Reset     — Unix timestamp (seconds) when the window resets
      Retry-After           — seconds until the client may retry

    Body follows the standard error envelope (Spec Part 1, §2.2, Decision 4).
    """
    now_ms = time.time() * 1000
    reset_s = int(reset_ms / 1000)
    retry_after = max(1, int((reset_ms - now_ms) / 1000))

    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Too many requests — please slow down and retry after the reset time.",
                "details": {
                    "limit": limit,
                    "reset_at": reset_s,
                    "retry_after_seconds": retry_after,
                },
                "request_id": request_id,
            }
        },
        headers={
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset_s),
            "Retry-After": str(retry_after),
            "X-Request-ID": request_id,
        },
    )


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter backed by Redis.

    Reads ``request.state.auth_context`` (set by ``JWTAuthMiddleware``) and
    ``request.state.request_id`` (set by ``JWTAuthMiddleware``).

    The Redis client is created lazily on the first request and reused for
    the lifetime of the process.  An optional ``redis_client`` constructor
    argument lets tests inject a mock without touching a real Redis server.

    Behaviour
    ---------
    1. Skip health / OpenAPI paths.
    2. Resolve the rate limit tier and identifier from auth_context.
    3. Execute the atomic Lua sliding window script.
    4. If **allowed**: forward to next layer, attach ``X-RateLimit-*`` headers.
    5. If **blocked**: return 429 directly without calling ``call_next``.
    6. If **Redis unavailable**: fail open — forward request, log ERROR.
    """

    def __init__(
        self,
        app: object,
        *,
        settings: Settings | None = None,
        redis_client: aioredis.Redis | None = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings
        self._redis: aioredis.Redis | None = redis_client
        self._redis_provided = redis_client is not None

    # ── Redis client management ───────────────────────────────────────────────

    async def _get_redis(self, settings: Settings) -> aioredis.Redis | None:
        """
        Return the Redis client, creating it lazily if needed.

        Returns ``None`` if the client cannot be created (e.g. Redis is
        unreachable at startup).
        """
        if self._redis_provided:
            return self._redis
        if self._redis is not None:
            return self._redis
        try:
            self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                settings.redis_url,
                encoding="utf-8",
                decode_responses=False,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
        except Exception:  # noqa: BLE001
            logger.error("rate_limit.redis_init_failed", redis_url=settings.redis_url)
            return None
        return self._redis

    # ── Sliding window check ──────────────────────────────────────────────────

    async def _check(
        self,
        redis: aioredis.Redis,
        key: str,
        limit: int,
    ) -> tuple[int, float]:
        """
        Execute the Lua sliding window script.

        Returns ``(remaining, reset_ms)`` where remaining is -1 if blocked.
        Any Redis error is re-raised to be caught by the caller.
        """
        now_ms = int(time.time() * 1000)
        member = str(uuid.uuid4())

        result: list[Any] = await redis.eval(  # type: ignore[misc]
            _SLIDING_WINDOW_LUA,
            1,
            key,
            str(now_ms),
            str(_WINDOW_MS),
            str(limit),
            member,
        )
        remaining: int = int(result[0])
        reset_ms: float = float(result[1])
        return remaining, reset_ms

    # ── Middleware dispatch ───────────────────────────────────────────────────

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path: str = request.url.path

        # ── 1. Skip non-business paths ─────────────────────────────────────
        if _should_skip(path):
            return await call_next(request)

        # ── 2. Read context set by JWTAuthMiddleware ───────────────────────
        request_id: str = getattr(request.state, "request_id", "")
        auth_ctx: AuthRequestContext | None = getattr(request.state, "auth_context", None)

        # ── 3. Resolve settings, limit, and identifier ────────────────────
        cfg = self._settings or get_settings()
        limit, tier = _resolve_limit(auth_ctx, cfg)

        if auth_ctx is not None:
            key = f"ratelimit:user:{auth_ctx.user_id}"
        else:
            ip = _extract_ip(request) or "unknown"
            key = f"ratelimit:ip:{ip}"

        # ── 4. Acquire Redis client ────────────────────────────────────────
        redis = await self._get_redis(cfg)

        if redis is None:
            # Redis unavailable — fail open, no rate limiting enforced
            logger.error(
                "rate_limit.redis_unavailable",
                path=path,
                tier=tier,
                request_id=request_id,
            )
            return await call_next(request)

        # ── 5. Execute sliding window check ───────────────────────────────
        try:
            remaining, reset_ms = await self._check(redis, key, limit)
        except Exception:  # noqa: BLE001 — Redis error, fail open
            logger.error(
                "rate_limit.check_failed",
                key=key,
                path=path,
                tier=tier,
                request_id=request_id,
                exc_info=True,
            )
            return await call_next(request)

        # ── 6. Blocked — return 429 without calling call_next ─────────────
        if remaining < 0:
            logger.warning(
                "rate_limit.exceeded",
                key=key,
                path=path,
                method=request.method,
                limit=limit,
                tier=tier,
                request_id=request_id,
            )
            return _build_429_response(
                limit=limit,
                reset_ms=reset_ms,
                request_id=request_id,
            )

        # ── 7. Allowed — forward request, attach rate limit headers ───────
        response = await call_next(request)

        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(reset_ms / 1000))

        logger.debug(
            "rate_limit.ok",
            key=key,
            path=path,
            remaining=remaining,
            limit=limit,
            tier=tier,
            request_id=request_id,
        )

        return response


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "RateLimitMiddleware",
]
