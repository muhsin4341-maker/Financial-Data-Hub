"""
Financial Data Hub — FastAPI Application Entry Point.

Milestone: M1 — Foundation Layer
Status: STUB — business logic not yet implemented.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# TODO M1-Step10: Add lifespan context manager (startup/shutdown)
# TODO M1-Step14: Add auth middleware
# TODO M1-Step15: Add audit middleware
# TODO M1-Step16: Add rate limiter middleware


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Financial Data Hub API",
        version="0.1.0",
        description="Production-grade financial data acquisition and Excel export.",
        docs_url="/api/v1/docs",  # development only — disable in production
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
    )

    # CORS — configured from settings in M1
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],  # TODO M1: load from Settings
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health endpoints (M1-Step10) ─────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health_liveness() -> dict:
        """Liveness probe — returns 200 if process is running."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["system"])
    async def health_readiness() -> dict:
        """Readiness probe — checks DB + Redis connectivity."""
        # TODO M1-Step10: Check database and Redis connections
        return {"status": "ok", "database": "unchecked", "redis": "unchecked"}

    # ── Routers (added per milestone) ────────────────────────────────────────
    # TODO M1: include auth router
    # TODO M2: include companies router
    # TODO M2: include jobs router
    # TODO M6: include exports router

    return app


app = create_app()
