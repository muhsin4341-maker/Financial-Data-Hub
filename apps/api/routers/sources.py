"""
Source Registry router — CRUD + enable/disable endpoints for source config management.

Engineering Specification references:
  M3 Execution Plan, M3.1          — Source Registry milestone
  M3 Execution Plan, Section 10.1  — Acquisition API endpoints

Endpoints:
  POST   /api/v1/sources                — Create source config (role >= admin)
  GET    /api/v1/sources                — List source configs (any auth)
  GET    /api/v1/sources/{id}           — Get source config by ID (any auth)
  PATCH  /api/v1/sources/{id}           — Update source config (role >= admin)
  DELETE /api/v1/sources/{id}           — Hard-delete source config (role >= admin)
  POST   /api/v1/sources/{id}/enable    — Enable a disabled source (role >= admin)
  POST   /api/v1/sources/{id}/disable   — Disable an active source (role >= admin)

Authorization:
  - POST / PATCH / DELETE / enable / disable : require_admin  (ADMIN or OWNER)
  - GET (list and detail)                    : require_authenticated (any valid JWT)

Source configs are platform-wide system records (no tenant_id).  Admin-level
access is required for mutations because incorrect source configuration could
disrupt the acquisition pipeline for all tenants.

Tenant isolation:
  Source configs are NOT per-tenant — they are global system configuration.
  Auth context (ctx) is still required to identify the acting user for audit
  logs, but no tenant filtering is applied.

Error codes:
  404 SOURCECONFIG_NOT_FOUND  — source does not exist
  409 CONFLICT                — code already exists
  422 VALIDATION_ERROR        — request body fails Pydantic validation
  401 UNAUTHORIZED            — missing or invalid JWT
  403 FORBIDDEN               — authenticated but insufficient role

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.database import get_db
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_admin,
    require_authenticated,
)
from apps.api.schemas.sources import (
    SourceConfigCreate,
    SourceConfigListResponse,
    SourceConfigResponse,
    SourceConfigUpdate,
)
from apps.api.services.sources import SourceRegistryService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


# ---------------------------------------------------------------------------
# POST /api/v1/sources
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=SourceConfigResponse,
    status_code=201,
    summary="Create a source config",
    description=(
        "Register a new data acquisition source (e.g. SEC_EDGAR, NSE, BSE).  "
        "Requires ADMIN role or above.  "
        "The ``code`` field is uppercased and must be globally unique."
    ),
)
async def create_source(
    payload: SourceConfigCreate,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SourceConfigResponse:
    """
    Register a new source config in the platform registry.

    Steps:
      1. Validate the request body (Pydantic — code uppercased, provider_type validated).
      2. Delegate to SourceRegistryService.create, which catches IntegrityError
         for duplicate codes and raises ConflictError → 409 Conflict.
      3. Return 201 with SourceConfigResponse.
    """
    service = SourceRegistryService(db)
    source = await service.create(payload)
    log.info(
        "source.created",
        source_id=str(source.id),
        code=source.code,
        actor_user_id=str(ctx.user_id),
    )
    return source


# ---------------------------------------------------------------------------
# GET /api/v1/sources
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=SourceConfigListResponse,
    status_code=200,
    summary="List source configs",
    description=(
        "Return a paginated list of all registered data acquisition sources.  "
        "Supports filtering by provider type, country code, and active status."
    ),
)
async def list_sources(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        20, ge=1, le=100, description="Items per page (max 100)."
    ),
    provider_type: str | None = Query(
        None,
        description=(
            "Filter by provider type. "
            "Values: regulatory | exchange | manual | broker."
        ),
    ),
    country_code: str | None = Query(
        None,
        description="Filter by ISO 3166-1 alpha-2 country code (e.g. 'US', 'IN').",
    ),
    is_active: bool | None = Query(
        None,
        description=(
            "true = enabled sources only; "
            "false = disabled only; "
            "omit = all."
        ),
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> SourceConfigListResponse:
    service = SourceRegistryService(db)
    return await service.list(
        page=page,
        page_size=page_size,
        provider_type=provider_type,
        country_code=country_code,
        is_active=is_active,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/sources/{source_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{source_id}",
    response_model=SourceConfigResponse,
    status_code=200,
    summary="Get a source config by ID",
    description=(
        "Return a single source config by its UUID.  "
        "Returns 404 if the source does not exist."
    ),
)
async def get_source(
    source_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> SourceConfigResponse:
    service = SourceRegistryService(db)
    return await service.get_by_id(source_id)


# ---------------------------------------------------------------------------
# PATCH /api/v1/sources/{source_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/{source_id}",
    response_model=SourceConfigResponse,
    status_code=200,
    summary="Update a source config",
    description=(
        "Partially update a source config.  Only fields present in the request "
        "body are modified.  Requires ADMIN role or above.  "
        "The ``code`` field is immutable and cannot be changed via PATCH."
    ),
)
async def update_source(
    source_id: uuid.UUID,
    payload: SourceConfigUpdate,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SourceConfigResponse:
    """
    Partial update via SourceRegistryService.update.

    The service uses schema.model_fields_set to determine which fields were
    explicitly provided.  Fields absent from the PATCH body are left unchanged.
    ``code`` cannot appear in SourceConfigUpdate and is therefore never modified.
    """
    service = SourceRegistryService(db)
    source = await service.update(source_id, payload)
    log.info(
        "source.updated",
        source_id=str(source_id),
        actor_user_id=str(ctx.user_id),
        fields=sorted(payload.model_fields_set),
    )
    return source


# ---------------------------------------------------------------------------
# DELETE /api/v1/sources/{source_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{source_id}",
    status_code=204,
    summary="Delete a source config",
    description=(
        "Permanently remove a source config from the registry.  "
        "Requires ADMIN role or above.  Returns 204 with no body on success.  "
        "**Prefer POST /{id}/disable for active sources** — disabling preserves "
        "referential integrity once filing_records references this source."
    ),
)
async def delete_source(
    source_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    service = SourceRegistryService(db)
    await service.delete(source_id)
    log.info(
        "source.deleted",
        source_id=str(source_id),
        actor_user_id=str(ctx.user_id),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/v1/sources/{source_id}/enable
# ---------------------------------------------------------------------------


@router.post(
    "/{source_id}/enable",
    response_model=SourceConfigResponse,
    status_code=200,
    summary="Enable a source",
    description=(
        "Set ``is_active = True`` on a disabled source config.  "
        "Requires ADMIN role or above.  "
        "Idempotent: enabling an already-active source returns 200 unchanged."
    ),
)
async def enable_source(
    source_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SourceConfigResponse:
    service = SourceRegistryService(db)
    source = await service.enable(source_id)
    log.info(
        "source.enabled",
        source_id=str(source_id),
        actor_user_id=str(ctx.user_id),
    )
    return source


# ---------------------------------------------------------------------------
# POST /api/v1/sources/{source_id}/disable
# ---------------------------------------------------------------------------


@router.post(
    "/{source_id}/disable",
    response_model=SourceConfigResponse,
    status_code=200,
    summary="Disable a source",
    description=(
        "Set ``is_active = False`` on an active source config.  "
        "Requires ADMIN role or above.  The acquisition pipeline skips "
        "disabled sources during job execution.  "
        "Idempotent: disabling an already-disabled source returns 200 unchanged.  "
        "Prefer this over DELETE to preserve audit history."
    ),
)
async def disable_source(
    source_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SourceConfigResponse:
    service = SourceRegistryService(db)
    source = await service.disable(source_id)
    log.info(
        "source.disabled",
        source_id=str(source_id),
        actor_user_id=str(ctx.user_id),
    )
    return source
