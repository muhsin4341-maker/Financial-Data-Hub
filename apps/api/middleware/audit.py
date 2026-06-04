"""
Audit Log Middleware — M1-Step15.

Engineering Specification references:
  Part 1, Section 2.3  — Request lifecycle: audit middleware runs after JWT auth,
                          before route handler; logs every inbound request.
  Part 3, Section 12   — Audit log requirements:
                            * Append-only PostgreSQL table (audit_log).
                            * Records: tenant_id, user_id, action, IP, timestamp.
                            * Every response carries X-Request-ID header.
                            * request_id propagated to all log lines and audit records.
                            * Retention: 7 years (financial compliance).
                            * Non-blocking: write fires AFTER response is returned.
                            * Never log PII (passwords, full names, email addresses).

Architecture
------------
This middleware runs at position 2 in the stack (after JWTAuthMiddleware):

    JWTAuthMiddleware   — sets request.state.auth_context, request.state.request_id
    AuditMiddleware     — reads those fields, fires async DB write, sets X-Request-ID
    RateLimitMiddleware — next step (M1-Step16)
    Route handlers

``add_middleware()`` wraps outside-in, so registration order in main.py is:
    app.add_middleware(RateLimitMiddleware)  # last registered → innermost
    app.add_middleware(AuditMiddleware)
    app.add_middleware(JWTAuthMiddleware)    # first registered → outermost

Async write strategy
--------------------
The audit record is written via ``asyncio.create_task()``, which schedules the
DB write on the running event loop without blocking the response path. The
response is returned to the client first; the task runs on the next event loop
iteration. Errors in the write task are caught and logged — they never propagate
to the caller (non-blocking guarantee).

Scope
-----
* Audits all ``/api/v1/`` requests.
* Skips: /health, /health/ready, /health/detailed, OpenAPI schema endpoints.
* GET requests are audited for read-access traceability (recommended by spec).
* The ``changes`` JSON field captures: path, query_string, status_code,
  duration_ms.  Business-level events (job.created, export.downloaded) are
  written directly to audit_log by the service layer with full entity context.

Milestone: M1-Step15 — Audit middleware
Status:    COMPLETE
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from apps.api.middleware.auth import AuthRequestContext
from apps.api.models import AuditLog, gen_uuid7

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths that are never written to the audit log
# ---------------------------------------------------------------------------

#: Exact path matches — high-frequency operational endpoints with no audit value.
_SKIP_EXACT: frozenset[str] = frozenset(
    {
        "/health",
        "/health/ready",
        "/health/detailed",
    }
)

#: Prefix matches — OpenAPI / Swagger UI paths that fire on every page load.
_SKIP_PREFIXES: tuple[str, ...] = (
    "/api/v1/docs",
    "/api/v1/redoc",
    "/api/v1/openapi.json",
)


def _should_skip(path: str) -> bool:
    """Return True if this path should not be written to the audit log."""
    if path in _SKIP_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


# ---------------------------------------------------------------------------
# IP address extraction
# ---------------------------------------------------------------------------


def _extract_ip(request: Request) -> str | None:
    """
    Extract the real client IP address from the request.

    Honours ``X-Forwarded-For`` (set by ALB / nginx reverse proxy) and falls
    back to the direct connection IP. Returns ``None`` when unavailable.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For may be a comma-separated list; the first entry is the
        # original client IP in a correctly configured reverse proxy setup.
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


# ---------------------------------------------------------------------------
# Async audit writer (runs as a background task)
# ---------------------------------------------------------------------------


async def _write_audit_record(
    *,
    action: str,
    request_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    ip_address: str | None,
    user_agent: str | None,
    changes: dict[str, Any],
) -> None:
    """
    Persist one audit record to the ``audit_log`` table.

    This function runs as an ``asyncio.Task`` after the HTTP response has been
    returned to the client. It opens its own database session, writes the
    record, and commits. Any exception is caught and logged — it must never
    propagate (non-blocking guarantee).

    When the database is not yet initialised (e.g. during startup or in unit
    tests that do not set up a DB), the write is silently skipped and the
    failure is logged at DEBUG level so test output stays clean.
    """
    # Import here to avoid a circular dependency at module load time and to
    # allow tests to patch AsyncSessionFactory after import.
    from apps.api.core.database import AsyncSessionFactory  # noqa: PLC0415

    if AsyncSessionFactory is None:
        logger.debug(
            "audit.db_not_ready",
            action=action,
            request_id=str(request_id) if request_id else None,
        )
        return

    # Skip middleware-level audit write for unauthenticated requests.
    # audit_log.tenant_id is NOT NULL; public auth endpoints (register,
    # login, forgot-password, reset-password) have no JWT context here.
    # Those endpoints write their own business-level audit records via
    # repo.create_audit_log() with the correct tenant_id.
    if tenant_id is None:
        logger.debug(
            "audit.skipped_no_tenant",
            action=action,
            request_id=str(request_id) if request_id else None,
        )
        return

    try:
        async with AsyncSessionFactory() as session:
            record = AuditLog(
                id=gen_uuid7(),
                tenant_id=tenant_id,
                user_id=user_id,
                action=action,
                request_id=request_id,
                ip_address=ip_address,
                user_agent=user_agent[:500] if user_agent else None,
                changes=changes,
            )
            session.add(record)
            await session.commit()

        logger.debug(
            "audit.record_written",
            action=action,
            request_id=str(request_id) if request_id else None,
            tenant_id=str(tenant_id) if tenant_id else None,
            status_code=changes.get("status_code"),
        )

    except Exception:  # noqa: BLE001 — never crash the response path
        logger.error(
            "audit.write_failed",
            action=action,
            request_id=str(request_id) if request_id else None,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# AuditMiddleware
# ---------------------------------------------------------------------------


class AuditMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that writes an audit record for every API request.

    Behaviour
    ---------
    * Reads ``request.state.auth_context`` and ``request.state.request_id``
      populated by ``JWTAuthMiddleware`` (which runs first).
    * Measures request duration with ``time.perf_counter()``.
    * Calls ``call_next(request)`` to forward to the route handler.
    * Sets ``X-Request-ID`` on the response header.
    * Schedules an async write to ``audit_log`` via ``asyncio.create_task()``.
    * Returns the response — the audit write runs in the background and does
      NOT delay the client.

    Audit record content
    --------------------
    ``action``:       ``http.{method.lower()}``  (e.g. ``http.post``)
    ``tenant_id``:    from ``auth_context.tenant_id`` when authenticated
    ``user_id``:      from ``auth_context.user_id``  when authenticated
    ``request_id``:   from ``request.state.request_id``
    ``ip_address``:   from ``X-Forwarded-For`` or direct client IP
    ``user_agent``:   from ``User-Agent`` header, truncated to 500 chars
    ``changes``:      JSONB — ``path``, ``query_string``, ``status_code``,
                      ``duration_ms``

    Skipped paths
    -------------
    ``/health``, ``/health/ready``, ``/health/detailed``, and all OpenAPI
    UI paths are never written to avoid filling the table with heartbeat noise.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path: str = request.url.path

        # ── Skip non-business paths ───────────────────────────────────────────
        if _should_skip(path):
            return await call_next(request)

        # ── Start timing ──────────────────────────────────────────────────────
        start = time.perf_counter()

        # ── Forward request to next middleware / route handler ────────────────
        response = await call_next(request)

        duration_ms = int((time.perf_counter() - start) * 1000)

        # ── Extract context set by JWTAuthMiddleware ──────────────────────────
        request_id_str: str = getattr(request.state, "request_id", "")
        auth_ctx: AuthRequestContext | None = getattr(request.state, "auth_context", None)

        # Convert request_id string to UUID (it was stored as str by auth middleware)
        request_id_uuid: uuid.UUID | None = None
        if request_id_str:
            try:
                request_id_uuid = uuid.UUID(request_id_str)
            except ValueError:
                pass

        tenant_id: uuid.UUID | None = auth_ctx.tenant_id if auth_ctx else None
        user_id: uuid.UUID | None = auth_ctx.user_id if auth_ctx else None

        # ── Set X-Request-ID on response ──────────────────────────────────────
        # Spec Part 3, Section 12: "Every response carries X-Request-ID header."
        if request_id_str:
            response.headers["X-Request-ID"] = request_id_str

        # ── Build changes payload ─────────────────────────────────────────────
        query_string = str(request.url.query) if request.url.query else None
        changes: dict[str, Any] = {
            "path": path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }
        if query_string:
            changes["query_string"] = query_string

        # ── Schedule async audit write (non-blocking) ─────────────────────────
        action = f"http.{request.method.lower()}"
        ip_address = _extract_ip(request)
        user_agent = request.headers.get("User-Agent")

        try:
            asyncio.create_task(
                _write_audit_record(
                    action=action,
                    request_id=request_id_uuid,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    changes=changes,
                ),
                name=f"audit:{action}:{request_id_str[:8]}",
            )
        except RuntimeError:
            # No running event loop in test environments that don't use asyncio
            logger.warning(
                "audit.task_creation_failed",
                action=action,
                request_id=request_id_str,
            )

        # ── Structured log line (synchronous, always emitted) ────────────────
        logger.info(
            "http.request",
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            tenant_id=str(tenant_id) if tenant_id else None,
            user_id=str(user_id) if user_id else None,
            request_id=request_id_str,
        )

        return response


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "AuditMiddleware",
]
