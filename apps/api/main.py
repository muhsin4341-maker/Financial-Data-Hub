"""
Financial Data Hub — FastAPI Application Entry Point.

Milestone: M1 — Foundation Layer
Step 10:   Lifespan integration, middleware wiring, real health checks.
Status:    COMPLETE
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.core.config import get_settings
from apps.api.core.database import check_db, dispose_db, init_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.middleware.audit import AuditMiddleware
from apps.api.middleware.auth import JWTAuthMiddleware
from apps.api.middleware.rate_limit import RateLimitMiddleware
from apps.api.routers.acquisition import router as acquisition_router
from apps.api.routers.auth import router as auth_router
from apps.api.routers.companies import router as companies_router
from apps.api.routers.export import router as export_router
from apps.api.routers.filings import company_filings_router, filings_router
from apps.api.routers.invitations import router as invitations_router
from apps.api.routers.jobs import router as jobs_router
from apps.api.routers.admin import router as admin_router
from apps.api.routers.analytics import router as analytics_router
from apps.api.routers.financials import router as financials_router
from apps.api.routers.sources import router as sources_router

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Redis health check
# ---------------------------------------------------------------------------

async def _check_redis(redis_url: str) -> bool:
    """Ping Redis; returns True if reachable, False otherwise."""
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415

        client: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
            redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        await client.ping()
        await client.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """
    Application startup and shutdown lifecycle.

    Startup:
      1. Resolve settings (cached singleton).
      2. Initialise SQLAlchemy async engine + session factory.
      3. Log service readiness.

    Shutdown:
      1. Drain SQLAlchemy connection pool and close engine.
    """
    settings = get_settings()

    # ── Startup ──────────────────────────────────────────────────────────────
    log.info(
        "application.startup",
        environment=settings.environment,
        debug=settings.debug,
    )

    init_db(
        database_url=settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        echo=settings.debug,
    )
    log.info("database.pool.initialised", pool_size=settings.database_pool_size)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    log.info("application.shutdown")
    await dispose_db()
    log.info("database.pool.disposed")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """
    Build and return the configured FastAPI application.

    Middleware is registered in reverse execution order because
    ``add_middleware()`` prepends each layer (outermost last in code,
    first to execute at request time):

        Registered order        Execution order (request in → response out)
        ────────────────────    ──────────────────────────────────────────
        1. CORSMiddleware       4. CORSMiddleware   (outermost)
        2. RateLimitMiddleware  3. JWTAuthMiddleware
        3. AuditMiddleware      2. AuditMiddleware
        4. JWTAuthMiddleware    1. RateLimitMiddleware (innermost)
    """
    settings = get_settings()

    app = FastAPI(
        title="Financial Data Hub API",
        version="0.1.0",
        description="Production-grade financial data acquisition and Excel export.",
        docs_url="/api/v1/docs" if not settings.is_production else None,
        redoc_url="/api/v1/redoc" if not settings.is_production else None,
        openapi_url="/api/v1/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Exception handlers ───────────────────────────────────────────────────
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    # ── Middleware (registered in reverse execution order) ───────────────────
    # 1. CORS — outermost; must run before auth so pre-flight OPTIONS succeed
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 2. Rate limiter — innermost Starlette middleware; checks Redis before
    #    forwarding to route handlers.  Registered last → runs last (innermost).
    app.add_middleware(RateLimitMiddleware)

    # 3. Audit — middle layer; reads auth_context set by JWTAuthMiddleware,
    #    fires non-blocking DB write after response is returned to client.
    app.add_middleware(AuditMiddleware)

    # 4. JWT auth — outermost Starlette middleware; decodes Bearer token and
    #    attaches AuthRequestContext to request.state for downstream layers.
    #    Registered last in code → executes first on inbound requests.
    app.add_middleware(JWTAuthMiddleware)

    # ── Health endpoints ─────────────────────────────────────────────────────
    @app.get("/health", tags=["system"], include_in_schema=False)
    async def health_liveness() -> dict[str, str]:
        """Liveness probe — returns 200 if the process is running."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["system"], include_in_schema=False)
    async def health_readiness() -> dict[str, Any]:
        """
        Readiness probe — verifies DB and Redis connectivity.

        Returns HTTP 200 with per-service status.  Callers (container
        orchestrators, load balancers) should treat any value other than
        ``"ok"`` as a degraded signal.  A future milestone will return
        HTTP 503 when critical dependencies are unreachable.
        """
        db_ok = await check_db()
        redis_ok = await _check_redis(get_settings().redis_url)

        return {
            "status": "ok" if (db_ok and redis_ok) else "degraded",
            "database": "ok" if db_ok else "unreachable",
            "redis": "ok" if redis_ok else "unreachable",
        }

    # ── Routers (added per milestone) ────────────────────────────────────────
    app.include_router(auth_router)             # M1-Steps 18-23
    app.include_router(companies_router)        # M2-Step 6
    app.include_router(jobs_router)             # M2-Step 7
    app.include_router(invitations_router)      # M2-Step 9
    app.include_router(sources_router)          # M3.1 — Source Registry
    app.include_router(acquisition_router)      # M3.8 — Acquisition APIs
    app.include_router(filings_router)          # M3.8 — Filing APIs
    app.include_router(company_filings_router)  # M3.8 — Company filing APIs
    app.include_router(export_router)           # M6.4 — Excel export download
    app.include_router(financials_router)        # M5.7 — Financial line-item ledger
    app.include_router(analytics_router)        # M7.1 — Analytics & trends
    app.include_router(admin_router)            # B3   — Admin FX sync endpoint

    return app


app = create_app()
