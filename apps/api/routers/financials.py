"""
Financials Router — M5.7: Financial Line-Item Data Ledger.

Replaces the empty stub.  Provides a paginated, filterable read endpoint
for extracted FinancialLineItem records scoped to a specific company.

Endpoint:
  GET /api/v1/companies/{company_id}/financials

Query parameters:
  limit            int   (1–500, default 50)  — page size
  offset           int   (≥0, default 0)      — zero-based row offset
  fiscal_year      int?                        — filter to a specific year
  fiscal_period    str?  Q1|Q2|Q3|Q4|FY       — filter to a period code
  statement_type   str?  IS|BS|CF              — filter to a statement class
  include_restated bool  (default false)       — include restatement rows

Error codes:
  COMPANY_NOT_FOUND (404) — company_id not found in the system.

Design notes:
  - The router prefix is /api/v1/companies, matching the RESTful
    sub-resource convention used throughout this codebase (cf. export,
    company_filings).  The full path is therefore:
      /api/v1/companies/{company_id}/financials
  - FinancialLineItem has no tenant_id column (global pipeline output).
    Access is scoped by company_id; the Company row is verified to exist
    before any data is returned.
  - Rows are ordered newest-year-first, then by period ascending, then
    by canonical_field — a natural reading order for a ledger view.
  - Two queries are executed per request: a COUNT(*) for pagination
    metadata, then the sliced SELECT.  Both share the same WHERE clause
    built from the filter conditions list to avoid divergence.

Milestone: M5.7 — Financial Line-Item Data Ledger & UI Viewer
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError
from apps.api.middleware.auth import AuthRequestContext, require_authenticated
from apps.api.schemas.financial_line_items import FinancialLineItemRead

log = structlog.get_logger(__name__)

# Router scoped under /api/v1/companies so the full path becomes
# /api/v1/companies/{company_id}/financials — consistent RESTful nesting.
router = APIRouter(prefix="/api/v1/companies", tags=["financials"])


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class FinancialsListResponse(BaseModel):
    """
    Paginated list of financial line items for a company.

    ``total`` reflects the count *after* all active filters are applied,
    allowing the frontend to compute page counts without a separate request.
    """

    items: list[FinancialLineItemRead] = Field(
        description="Slice of matching FinancialLineItem records."
    )
    total: int = Field(
        description="Total number of records matching the applied filters."
    )
    offset: int = Field(
        description="Zero-based row offset used for this page."
    )
    limit: int = Field(
        description="Page size used for this request."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "items": [],
                "total": 0,
                "offset": 0,
                "limit": 50,
            }
        }
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/{company_id}/financials",
    response_model=FinancialsListResponse,
    summary="List financial line items for a company",
    description="""
Return a paginated, filterable list of all extracted **FinancialLineItem**
records for the specified company.

Each record represents one data point from a processed financial document:
a single XBRL concept (e.g. `us-gaap:NetIncomeLoss`) for a specific
fiscal period, carrying both the originally reported value and the
FX-translated USD equivalent produced by the M5 translation pipeline.

### Filters

| Parameter | Type | Description |
|-----------|------|-------------|
| `fiscal_year` | integer | Restrict to a 4-digit fiscal year (e.g. 2024) |
| `fiscal_period` | Q1 \| Q2 \| Q3 \| Q4 \| FY | Restrict to a period code |
| `statement_type` | IS \| BS \| CF | Restrict to a statement class |
| `include_restated` | bool | Include restatement rows (default **false**) |

### Ordering

Results are sorted: **newest fiscal year first**, then by period (Q1→Q4→FY),
then by `canonical_field` alphabetically, then by `filing_date` descending.
This ordering surfaces the most recently filed data for each concept at the
top of the response.
    """.strip(),
    responses={
        200: {"description": "Paginated financial line items returned."},
        404: {"description": "Company not found (`COMPANY_NOT_FOUND`)."},
    },
)
async def list_company_financials(
    company_id: uuid.UUID,
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Number of records per page (max 500).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Zero-based row offset for pagination.",
    ),
    fiscal_year: int | None = Query(
        default=None,
        ge=1900,
        le=2100,
        description="Filter to a specific 4-digit fiscal year.",
        examples=[2024],
    ),
    fiscal_period: Literal["Q1", "Q2", "Q3", "Q4", "FY"] | None = Query(
        default=None,
        description="Filter to a specific period code: Q1 | Q2 | Q3 | Q4 | FY.",
    ),
    statement_type: Literal["IS", "BS", "CF"] | None = Query(
        default=None,
        description="Filter to a statement type: IS (Income) | BS (Balance) | CF (Cash Flow).",
    ),
    include_restated: bool = Query(
        default=False,
        description=(
            "Include restatement rows alongside current values.  "
            "Defaults to false — only the current (non-restated) values are returned."
        ),
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),
    session: AsyncSession = Depends(get_db),
) -> FinancialsListResponse:
    """
    List paginated, filtered FinancialLineItem records for a company.

    Requires a valid Bearer token.  Returns 404 if the company is not found.
    Returns an empty ``items`` list (with ``total=0``) when the company exists
    but has no extracted data matching the applied filters — this is not an error.
    """
    # Deferred import: follows the established codebase pattern for avoiding
    # potential circular imports at module load time.
    from apps.api.models import Company, FinancialLineItem  # noqa: PLC0415

    bound_log = log.bind(
        company_id=str(company_id),
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        statement_type=statement_type,
        include_restated=include_restated,
        limit=limit,
        offset=offset,
        tenant_id=str(ctx.tenant_id),
        user_id=str(ctx.user_id),
    )
    bound_log.debug("financials.list.called")

    # ── 1. Company existence check ─────────────────────────────────────────
    company = await session.get(Company, company_id)
    if company is None:
        raise APIError(
            code="COMPANY_NOT_FOUND",
            message=f"Company {company_id} not found.",
            status_code=404,
        )

    # ── 2. Build shared filter conditions ─────────────────────────────────
    # Both the COUNT and the SELECT use the same list so they can never
    # diverge and produce inconsistent pagination metadata.
    conditions: list = [FinancialLineItem.company_id == company_id]

    if not include_restated:
        # Exclude restatement rows by default.  SQLAlchemy requires == False;
        # `is_(False)` is also acceptable but less idiomatic here.
        conditions.append(FinancialLineItem.is_restated == False)  # noqa: E712

    if fiscal_year is not None:
        conditions.append(FinancialLineItem.fiscal_year == fiscal_year)

    if fiscal_period is not None:
        conditions.append(FinancialLineItem.fiscal_period == fiscal_period)

    if statement_type is not None:
        conditions.append(FinancialLineItem.statement_type == statement_type)

    # ── 3. COUNT total matching rows (for pagination metadata) ────────────
    count_stmt = (
        select(func.count())
        .select_from(FinancialLineItem)
        .where(*conditions)
    )
    total: int = (await session.execute(count_stmt)).scalar_one()

    # Short-circuit: no rows, nothing to fetch.
    if total == 0:
        bound_log.debug("financials.list.empty")
        return FinancialsListResponse(
            items=[],
            total=0,
            offset=offset,
            limit=limit,
        )

    # ── 4. Fetch the requested page ────────────────────────────────────────
    items_stmt = (
        select(FinancialLineItem)
        .where(*conditions)
        .order_by(
            FinancialLineItem.fiscal_year.desc(),
            FinancialLineItem.fiscal_period.asc(),
            FinancialLineItem.canonical_field.asc(),
            FinancialLineItem.filing_date.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    rows = list((await session.execute(items_stmt)).scalars().all())

    # Validate each ORM row through the Pydantic read schema.
    # model_validate() respects from_attributes=True on FinancialLineItemRead.
    items = [FinancialLineItemRead.model_validate(row) for row in rows]

    bound_log.info(
        "financials.list.ok",
        total=total,
        returned=len(items),
    )

    return FinancialsListResponse(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
    )
