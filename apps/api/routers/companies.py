"""
Companies router — CRUD + resolver endpoints for company management.

Engineering Specification references:
  M2 Execution Plan, Section 2.2   — companies API endpoints
  M2 Execution Plan, Section 6.1   — endpoint table (POST, GET, PATCH, DELETE)
  M2 Execution Plan, Section 9.2   — tenant isolation at repository layer
  M2 Execution Plan, Section 9.5   — role-based access control (partial M2 enforcement)

Endpoints:
  POST   /api/v1/companies               — Create company (role >= analyst)
  GET    /api/v1/companies               — List companies with pagination + search (any auth)
  GET    /api/v1/companies/resolve       — Resolve ticker/CIK to canonical info (any auth) [M3.2]
  GET    /api/v1/companies/{id}          — Get company by ID (any auth)
  PATCH  /api/v1/companies/{id}          — Update company (role >= analyst)
  DELETE /api/v1/companies/{id}          — Soft-delete company (role >= admin)

Authorization (Section 9.5):
  - POST / PATCH : require_analyst  (ANALYST, ADMIN, or OWNER)
  - DELETE       : require_admin    (ADMIN or OWNER)
  - GET (all)    : require_authenticated (any valid JWT)

Tenant isolation:
  The tenant_id is derived exclusively from the JWT payload (ctx.tenant_id)
  and passed directly to the repository layer.  It is never taken from the
  request body.  The repository enforces the isolation on every query.

Error codes:
  404 COMPANY_NOT_FOUND       — company does not exist or belongs to another tenant
  404 COMPANY_INFO_NOT_FOUND  — ticker/CIK not found in resolver (M3.2)
  409 CONFLICT                — ticker already exists in this tenant workspace
  422 VALIDATION_ERROR        — request body fails Pydantic validation
  401 UNAUTHORIZED            — missing or invalid JWT
  403 FORBIDDEN               — authenticated but insufficient role

Milestones:
  M2-Step 6  — CRUD endpoints
  M3.2       — GET /companies/resolve
"""

from __future__ import annotations

import math
import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import get_settings
from apps.api.core.database import get_db
from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_admin,
    require_analyst,
    require_authenticated,
)
from apps.api.repositories.companies import CompanyRepository
from apps.api.schemas.companies import (
    CompanyCreate,
    CompanyListResponse,
    CompanyResolveResponse,
    CompanyResponse,
    CompanyUpdate,
)
from services.acquisition.company_resolver.resolver import CompanyResolverService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/companies", tags=["companies"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_response(company: object) -> CompanyResponse:
    """Convert a Company ORM instance to its Pydantic response schema."""
    return CompanyResponse.model_validate(company)


def _to_list_response(
    items: list,  # type: ignore[type-arg]
    total: int,
    page: int,
    page_size: int,
) -> CompanyListResponse:
    """Build a paginated list response from repository results."""
    pages = math.ceil(total / page_size) if page_size else 0
    return CompanyListResponse(
        items=[_to_response(c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/companies
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CompanyResponse,
    status_code=201,
    summary="Create a company",
    description=(
        "Add a new company to the authenticated tenant workspace.  "
        "Requires ANALYST role or above.  "
        "The ticker symbol is uppercased and must be unique within the workspace."
    ),
)
async def create_company(
    payload: CompanyCreate,
    ctx: AuthRequestContext = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
) -> CompanyResponse:
    """
    Create a new company in the tenant workspace.

    Steps:
      1. Validate the request body (Pydantic — ticker uppercased, CIK padded).
      2. Persist via CompanyRepository.create, injecting tenant_id from JWT.
      3. Catch IntegrityError for duplicate ticker → 409 Conflict.
      4. Return 201 with CompanyResponse.
    """
    repo = CompanyRepository(db)
    try:
        company = await repo.create(ctx.tenant_id, payload)
    except IntegrityError as exc:
        raise ConflictError(
            f"A company with ticker '{payload.ticker}' already exists "
            f"in this workspace."
        ) from exc

    log.info(
        "company.created",
        company_id=str(company.id),
        tenant_id=str(ctx.tenant_id),
        ticker=company.ticker,
    )
    return _to_response(company)


# ---------------------------------------------------------------------------
# GET /api/v1/companies
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=CompanyListResponse,
    status_code=200,
    summary="List companies",
    description=(
        "Return a paginated list of companies in the tenant workspace.  "
        "Supports case-insensitive name search and active/inactive filtering."
    ),
)
async def list_companies(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        20, ge=1, le=100, description="Items per page (max 100)."
    ),
    search: str | None = Query(
        None, description="Case-insensitive substring match on company name."
    ),
    is_active: bool | None = Query(
        None,
        description=(
            "true = active companies only; "
            "false = inactive only; "
            "omit = all."
        ),
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> CompanyListResponse:
    repo = CompanyRepository(db)
    items, total = await repo.list(
        ctx.tenant_id,
        page=page,
        page_size=page_size,
        search=search,
        is_active=is_active,
    )
    return _to_list_response(items, total, page, page_size)


# ---------------------------------------------------------------------------
# GET /api/v1/companies/resolve  (M3.2 — Company Resolver)
# ---------------------------------------------------------------------------

# NOTE: This route MUST be registered before /{company_id} so that FastAPI
# matches the literal path segment "resolve" before attempting UUID parsing.


async def _get_resolver() -> CompanyResolverService:
    """
    FastAPI dependency that provides a CompanyResolverService.

    Lazily creates a Redis client using the application settings. Redis is
    fail-open — if unreachable, resolution proceeds without cache. Override
    this dependency in tests by calling app.dependency_overrides.
    """
    settings = get_settings()
    redis_client = None
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415

        redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    except Exception:  # noqa: BLE001
        pass  # fail-open: proceed without cache

    return CompanyResolverService(
        redis_client=redis_client,
        user_agent=settings.edgar_user_agent,
    )


@router.get(
    "/resolve",
    response_model=CompanyResolveResponse,
    status_code=200,
    summary="Resolve a ticker or CIK to canonical company information",
    description=(
        "Look up a company by ticker symbol or SEC CIK.  "
        "Results are resolved via SEC EDGAR and cached in Redis (TTL 24 h).  "
        "Exactly one of ``ticker`` or ``cik`` must be provided.  "
        "Returns 404 if the identifier is not found in the resolver registry."
    ),
)
async def resolve_company(
    ticker: str | None = Query(
        default=None,
        description=(
            "Stock ticker symbol to resolve (e.g. 'AAPL'). "
            "Case-insensitive; normalised to uppercase."
        ),
        examples=["AAPL"],
    ),
    cik: str | None = Query(
        default=None,
        description=(
            "SEC Central Index Key to resolve (e.g. '0000320193' or '320193'). "
            "Zero-padded to 10 digits internally."
        ),
        examples=["0000320193"],
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    resolver: CompanyResolverService = Depends(_get_resolver),
) -> CompanyResolveResponse:
    """
    Resolve a ticker or CIK to canonical company information.

    Exactly one of ``ticker`` or ``cik`` must be provided. Supplying both
    or neither returns a 422 validation error.

    Steps:
      1. Validate that exactly one identifier is provided.
      2. Check Redis cache (company:ticker:{T} or company:cik:{C}).
      3. On cache miss, delegate to SECCompanyResolver → SEC EDGAR APIs.
      4. Write result to cache; return CompanyResolveResponse.
      5. Return 404 if the identifier cannot be resolved.
    """
    if ticker is None and cik is None:
        from apps.api.core.exceptions import ValidationError  # noqa: PLC0415
        raise ValidationError(
            "Provide exactly one of 'ticker' or 'cik' as a query parameter."
        )
    if ticker is not None and cik is not None:
        from apps.api.core.exceptions import ValidationError  # noqa: PLC0415
        raise ValidationError(
            "Provide exactly one of 'ticker' or 'cik', not both."
        )

    if ticker is not None:
        info = await resolver.resolve_by_ticker(ticker)
        identifier = ticker.upper()
    else:
        info = await resolver.resolve_by_cik(cik)  # type: ignore[arg-type]
        identifier = cik  # type: ignore[assignment]

    if info is None:
        raise NotFoundError("CompanyInfo", identifier)

    log.info(
        "company.resolved",
        ticker=info.ticker,
        cik=info.cik,
        company_name=info.company_name,
    )
    return CompanyResolveResponse(
        ticker=info.ticker,
        company_name=info.company_name,
        cik=info.cik,
        exchange=info.exchange,
        country=info.country,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/companies/{company_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{company_id}",
    response_model=CompanyResponse,
    status_code=200,
    summary="Get a company by ID",
    description=(
        "Return a single company.  Returns 404 if the company does not exist "
        "or belongs to a different tenant."
    ),
)
async def get_company(
    company_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> CompanyResponse:
    repo = CompanyRepository(db)
    company = await repo.get_by_id(ctx.tenant_id, company_id)
    if company is None:
        raise NotFoundError("Company", str(company_id))
    return _to_response(company)


# ---------------------------------------------------------------------------
# PATCH /api/v1/companies/{company_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/{company_id}",
    response_model=CompanyResponse,
    status_code=200,
    summary="Update a company",
    description=(
        "Partially update a company.  Only the fields present in the request "
        "body are modified.  Requires ANALYST role or above.  "
        "Setting a nullable field to null clears it."
    ),
)
async def update_company(
    company_id: uuid.UUID,
    payload: CompanyUpdate,
    ctx: AuthRequestContext = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
) -> CompanyResponse:
    """
    Partial update via CompanyRepository.update.

    The repository uses schema.model_fields_set to determine which fields
    were explicitly provided.  Fields absent from the PATCH body are left
    unchanged, even if their value in the schema object is None.
    """
    repo = CompanyRepository(db)
    try:
        company = await repo.update(ctx.tenant_id, company_id, payload)
    except IntegrityError as exc:
        # Ticker collision with another company in the same tenant.
        raise ConflictError(
            "A company with that ticker already exists in this workspace."
        ) from exc

    if company is None:
        raise NotFoundError("Company", str(company_id))

    log.info(
        "company.updated",
        company_id=str(company_id),
        tenant_id=str(ctx.tenant_id),
        fields=sorted(payload.model_fields_set),
    )
    return _to_response(company)


# ---------------------------------------------------------------------------
# DELETE /api/v1/companies/{company_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{company_id}",
    status_code=204,
    summary="Soft-delete a company",
    description=(
        "Mark a company as deleted.  The company is excluded from normal list "
        "and detail queries but its job history is retained.  "
        "Requires ADMIN role or above.  Returns 204 with no body on success."
    ),
)
async def delete_company(
    company_id: uuid.UUID,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    repo = CompanyRepository(db)
    deleted = await repo.soft_delete(ctx.tenant_id, company_id)
    if not deleted:
        raise NotFoundError("Company", str(company_id))

    log.info(
        "company.deleted",
        company_id=str(company_id),
        tenant_id=str(ctx.tenant_id),
    )
    return Response(status_code=204)
