"""
Analytics Router — M7.1: Aggregated Financial Summary and Trend Endpoints.

Serves clean, chronological time-series data for a company's four headline
financial metrics, optimised for rendering interactive charts and KPI cards
in the frontend dashboard.

Endpoint:
  GET /api/v1/analytics/companies/{company_id}/trends

Architecture:
  - Queries FinancialLineItem directly across ALL historical years/periods.
  - Filters to non-restated rows only (is_restated = FALSE).
  - Pivots in Python: groups by (fiscal_year, fiscal_period) → up to four
    metric values per period (Revenue, Gross Profit, Net Income, Operating
    Cash Flow).  Where multiple canonical field aliases map to the same
    metric, the most-recently-filed row wins (latest filing_date).
  - Multi-currency resolution:
      1. target_currency == "USD"  → value_usd  (pre-calculated by M5 FX)
      2. reported_currency matches target → value_reported
      3. Fallback                  → value_usd  (USD, not the requested CCY)
  - Periods are returned chronologically (ascending year, then Q1 < Q2 <
    Q3 < Q4 < H1 < H2 < FY within each year).

Error handling:
  - 404 COMPANY_NOT_FOUND   — company_id not found for the authenticated tenant.
  - 404 ANALYTICS_NO_DATA   — company exists but holds no financial line items
                              matching the four headline metrics yet.

Milestone: M7.1 — Aggregated Financial Summary and Trend Endpoints.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError
from apps.api.middleware.auth import AuthRequestContext, require_authenticated

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Chronological sort order for fiscal period codes within a year.
# Matches the M6 excel_generator convention.
_PERIOD_SORT_ORDER: dict[str, int] = {
    "Q1": 1,
    "Q2": 2,
    "Q3": 3,
    "Q4": 4,
    "H1": 5,
    "H2": 6,
    "FY": 7,
}

# Canonical XBRL field tags accepted for each headline metric.
# When multiple tags are present for the same (fiscal_year, fiscal_period,
# metric), the one with the latest filing_date wins — the frozenset is
# order-independent; the ordering is applied in the SQL query.
_METRIC_ALIASES: dict[str, frozenset[str]] = {
    "revenue": frozenset(
        {
            "us-gaap:Revenues",
            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
            "us-gaap:SalesRevenueNet",
            "us-gaap:SalesRevenueGoodsNet",
            "us-gaap:RevenuesNetOfInterestExpense",
            "ifrs-full:Revenue",
        }
    ),
    "gross_profit": frozenset(
        {
            "us-gaap:GrossProfit",
            "ifrs-full:GrossProfit",
        }
    ),
    "net_income": frozenset(
        {
            "us-gaap:NetIncomeLoss",
            "us-gaap:ProfitLoss",
            "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
            "ifrs-full:ProfitLoss",
            "ifrs-full:ProfitLossAttributableToOwnersOfParent",
        }
    ),
    "operating_cash_flow": frozenset(
        {
            "us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "ifrs-full:CashFlowsFromUsedInOperatingActivities",
        }
    ),
}

# Reverse lookup: canonical_field → metric name.  Built once at module load.
_FIELD_TO_METRIC: dict[str, str] = {
    field: metric
    for metric, fields in _METRIC_ALIASES.items()
    for field in fields
}

# Union of all tracked canonical fields, used in the SQL WHERE … IN (…) clause.
_ALL_TRACKED_FIELDS: frozenset[str] = frozenset(_FIELD_TO_METRIC.keys())


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------


class TrendDataPoint(BaseModel):
    """
    One fiscal period's headline financial metrics for a company.

    All monetary values are expressed in ``currency``.  ``None`` means no
    data was extracted for that metric in this period — not zero.
    """

    period: str = Field(
        ...,
        description="Human-readable period label, e.g. 'FY 2024' or 'Q3 2023'.",
        examples=["FY 2024", "Q1 2024"],
    )
    fiscal_year: int = Field(..., description="4-digit fiscal year (e.g. 2024).")
    fiscal_period: str = Field(
        ...,
        description="Period code: Q1 | Q2 | Q3 | Q4 | H1 | H2 | FY.",
        examples=["FY", "Q3"],
    )
    currency: str = Field(
        ...,
        description=(
            "ISO 4217 currency code the monetary values are expressed in.  "
            "Usually matches target_currency; may be 'USD' when a fallback "
            "was applied for non-USD requests."
        ),
        examples=["USD", "EUR"],
    )
    revenue: float | None = Field(
        None,
        description="Total revenue / net sales (may be None if not extracted).",
    )
    gross_profit: float | None = Field(
        None,
        description="Gross profit (revenue minus COGS; may be None).",
    )
    net_income: float | None = Field(
        None,
        description="Net income attributable to the company (may be None).",
    )
    operating_cash_flow: float | None = Field(
        None,
        description="Net cash provided by / used in operating activities (may be None).",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "period": "FY 2024",
                "fiscal_year": 2024,
                "fiscal_period": "FY",
                "currency": "USD",
                "revenue": 394328000000.0,
                "gross_profit": 170782000000.0,
                "net_income": 96995000000.0,
                "operating_cash_flow": 118254000000.0,
            }
        }
    }


class CompanyTrendsResponse(BaseModel):
    """
    Aggregated time-series financial data for a single company.

    ``data`` is sorted chronologically (oldest period first).
    """

    company_id: str = Field(..., description="UUID of the company.")
    target_currency: str = Field(
        ...,
        description="Requested target currency (ISO 4217), upper-cased.",
        examples=["USD", "GBP"],
    )
    periods_covered: int = Field(
        ...,
        description="Number of distinct fiscal periods returned in ``data``.",
    )
    data: list[TrendDataPoint] = Field(
        ...,
        description="Chronologically ordered trend data points (oldest first).",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "company_id": "019ea126-e02d-7ebd-ac37-f25800864395",
                "target_currency": "USD",
                "periods_covered": 3,
                "data": [
                    {
                        "period": "FY 2022",
                        "fiscal_year": 2022,
                        "fiscal_period": "FY",
                        "currency": "USD",
                        "revenue": 365817000000.0,
                        "gross_profit": 152836000000.0,
                        "net_income": 99803000000.0,
                        "operating_cash_flow": 122151000000.0,
                    },
                    {
                        "period": "FY 2023",
                        "fiscal_year": 2023,
                        "fiscal_period": "FY",
                        "currency": "USD",
                        "revenue": 383285000000.0,
                        "gross_profit": 169148000000.0,
                        "net_income": 96995000000.0,
                        "operating_cash_flow": 113550000000.0,
                    },
                ],
            }
        }
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _period_sort_key(fiscal_year: int, fiscal_period: str) -> tuple[int, int]:
    """
    Return a tuple used to sort (fiscal_year, fiscal_period) pairs
    chronologically.  Unknown period codes sort after FY (order value 99).
    """
    return (fiscal_year, _PERIOD_SORT_ORDER.get(fiscal_period, 99))


def _period_label(fiscal_year: int, fiscal_period: str) -> str:
    """
    Build a human-readable period label matching the M6 excel_generator
    convention:  'FY 2024',  'Q3 2023',  'H1 2022', etc.
    """
    if fiscal_period == "FY":
        return f"FY {fiscal_year}"
    return f"{fiscal_period} {fiscal_year}"


def _resolve_value(
    row: Any,
    target_currency: str,
) -> tuple[Decimal | None, str]:
    """
    Determine which stored value to use given the requested target currency.

    Resolution order
    ----------------
    1. target_currency == "USD"  →  row.value_usd          (M5 FX-translated)
    2. row.reported_currency matches target  →  row.value_reported  (native)
    3. Fallback  →  row.value_usd  (USD, signals a currency mismatch)

    Returns
    -------
    (value, actual_currency_used)
        ``actual_currency_used`` reflects which currency the returned value is
        denominated in.  Callers should surface this in the response so clients
        know when a fallback was applied.
    """
    tc = target_currency.upper()

    if tc == "USD":
        return row.value_usd, "USD"

    if row.reported_currency and row.reported_currency.upper() == tc:
        return row.value_reported, tc

    # Cannot serve the requested non-USD currency; fall back gracefully.
    return row.value_usd, "USD"


# ---------------------------------------------------------------------------
# Aggregation service function
# ---------------------------------------------------------------------------


async def _aggregate_trends(
    *,
    company_id: uuid.UUID,
    target_currency: str,
    session: AsyncSession,
) -> list[TrendDataPoint]:
    """
    Core aggregation logic for the trends endpoint.

    Steps
    -----
    1. Verify the company exists (404 if not).
    2. Query all non-restated FinancialLineItem rows that belong to one of
       the four tracked headline metrics, ordered by filing_date DESC so the
       most-recently-filed row surfaces first per (period, canonical_field).
    3. Pivot in Python into a nested dict keyed by (fiscal_year, fiscal_period),
       keeping only the first row seen per metric (deduplicated by the ordering
       guaranteeing the latest filing_date wins).
    4. Apply currency resolution to each row.
    5. Sort periods chronologically and build TrendDataPoint instances.
    6. Raise 404 if no data points resulted after pivoting.

    Parameters
    ----------
    company_id:
        UUID of the target company.
    target_currency:
        Upper-cased ISO 4217 code (e.g. "USD", "GBP").
    session:
        Active async SQLAlchemy session (owned by the caller / FastAPI DI).

    Returns
    -------
    list[TrendDataPoint]
        Non-empty; raises APIError(404) if the list would be empty.

    Raises
    ------
    APIError (404 COMPANY_NOT_FOUND)
        company_id does not exist.
    APIError (404 ANALYTICS_NO_DATA)
        Company exists but has no matching financial line items.
    """
    # Deferred imports follow the established pattern in this codebase to
    # avoid potential circular imports at module load time.
    from apps.api.models import Company, FinancialLineItem  # noqa: PLC0415

    # ── 1. Company existence check ─────────────────────────────────────────
    company = await session.get(Company, company_id)
    if company is None:
        raise APIError(
            code="COMPANY_NOT_FOUND",
            message=f"Company {company_id} not found.",
            status_code=404,
        )

    # ── 2. Query all non-restated tracked rows ────────────────────────────
    # ORDER BY fiscal_year ASC (for readability in logs), then filing_date
    # DESC within each (year, period) so the first row we encounter per
    # (year, period, canonical_field) is always the most recently filed one.
    stmt = (
        select(FinancialLineItem)
        .where(
            FinancialLineItem.company_id == company_id,
            FinancialLineItem.is_restated == False,  # noqa: E712 — SQLAlchemy requires ==
            FinancialLineItem.canonical_field.in_(_ALL_TRACKED_FIELDS),
        )
        .order_by(
            FinancialLineItem.fiscal_year.asc(),
            FinancialLineItem.fiscal_period.asc(),
            FinancialLineItem.filing_date.desc(),
        )
    )

    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    log.debug(
        "analytics._aggregate_trends.rows_fetched",
        company_id=str(company_id),
        target_currency=target_currency,
        row_count=len(rows),
    )

    # ── 3 & 4. Pivot + currency resolution ────────────────────────────────
    # Outer key : (fiscal_year, fiscal_period)
    # Inner key : metric name ("revenue", "gross_profit", etc.)
    # Value     : Decimal monetary value (resolved to target currency)
    pivot: dict[tuple[int, str], dict[str, Decimal | None]] = {}

    # Track the effective currency per period.  Defaults to target_currency
    # but may be downgraded to "USD" if any metric row triggers the fallback.
    currency_by_period: dict[tuple[int, str], str] = {}

    # Deduplication: keep the first (= most recently filed) value seen for
    # each (year, period, metric) triple.
    seen: set[tuple[int, str, str]] = set()

    for row in rows:
        metric = _FIELD_TO_METRIC.get(row.canonical_field)
        if metric is None:
            continue  # defensive: shouldn't happen given the WHERE IN clause

        period_key: tuple[int, str] = (row.fiscal_year, row.fiscal_period)
        dedup_key: tuple[int, str, str] = (row.fiscal_year, row.fiscal_period, metric)

        if dedup_key in seen:
            continue  # already have the best (latest-filed) value for this slot
        seen.add(dedup_key)

        value, actual_currency = _resolve_value(row, target_currency)

        if period_key not in pivot:
            pivot[period_key] = {}
            currency_by_period[period_key] = actual_currency
        elif actual_currency == "USD" and currency_by_period[period_key] != "USD":
            # If any metric in this period fell back to USD, surface USD for
            # the whole period so clients don't mix currencies.
            currency_by_period[period_key] = "USD"

        pivot[period_key][metric] = value

    # ── 5. Sort and build response data points ────────────────────────────
    sorted_keys = sorted(pivot.keys(), key=lambda k: _period_sort_key(k[0], k[1]))

    def _to_float(v: Decimal | None) -> float | None:
        return float(v) if v is not None else None

    data_points: list[TrendDataPoint] = [
        TrendDataPoint(
            period=_period_label(year, period),
            fiscal_year=year,
            fiscal_period=period,
            currency=currency_by_period[(year, period)],
            revenue=_to_float(pivot[(year, period)].get("revenue")),
            gross_profit=_to_float(pivot[(year, period)].get("gross_profit")),
            net_income=_to_float(pivot[(year, period)].get("net_income")),
            operating_cash_flow=_to_float(
                pivot[(year, period)].get("operating_cash_flow")
            ),
        )
        for year, period in sorted_keys
    ]

    # ── 6. Guard: no data ─────────────────────────────────────────────────
    if not data_points:
        raise APIError(
            code="ANALYTICS_NO_DATA",
            message=(
                f"No financial data is available for company {company_id}.  "
                "Extraction jobs may still be running or no documents have "
                "been processed for this company yet."
            ),
            status_code=404,
        )

    return data_points


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get(
    "/companies/{company_id}/trends",
    response_model=CompanyTrendsResponse,
    summary="Get company financial trends",
    description="""
Retrieve a chronological time-series of four headline financial metrics for
a specific company:

- **Revenue** — total revenue / net sales
- **Gross Profit** — revenue minus cost of goods sold
- **Net Income** — bottom-line profit attributable to the company
- **Operating Cash Flow** — net cash provided by operating activities

Periods are returned sorted **oldest-to-newest**.  Each period label uses
the format `FY 2024`, `Q1 2024`, `H2 2023`, etc.

### Currency

The `target_currency` query parameter (default `"USD"`) controls which
currency the values are expressed in.  The default `"USD"` path uses
pre-calculated FX-translated values produced by the M5 translation
pipeline (`value_usd`).  For other currencies the endpoint uses the
natively reported value (`value_reported`) when the filing's
`reported_currency` matches; otherwise it falls back to USD.

### Missing values

A `null` field means no data was extracted for that metric in that
period — not zero.  The field is omitted only when the extraction
pipeline has not yet processed a document that covers it.
    """.strip(),
    responses={
        200: {"description": "Chronological trend data returned successfully."},
        404: {
            "description": (
                "Company not found (`COMPANY_NOT_FOUND`) or no financial "
                "data available yet (`ANALYTICS_NO_DATA`)."
            )
        },
    },
)
async def get_company_trends(
    company_id: uuid.UUID,
    target_currency: str = Query(
        default="USD",
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code for returned monetary values (e.g. 'USD', 'GBP', 'EUR').",
        examples=["USD"],
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),
    session: AsyncSession = Depends(get_db),
) -> CompanyTrendsResponse:
    """
    Return aggregated financial trend data for a company.

    Requires a valid Bearer token.  The company must exist in the system and
    have at least one processed financial document before data is returned.
    """
    bound_log = log.bind(
        company_id=str(company_id),
        target_currency=target_currency.upper(),
        tenant_id=str(ctx.tenant_id),
        user_id=str(ctx.user_id),
    )
    bound_log.info("analytics.get_company_trends.called")

    data = await _aggregate_trends(
        company_id=company_id,
        target_currency=target_currency.upper(),
        session=session,
    )

    bound_log.info(
        "analytics.get_company_trends.success",
        periods_returned=len(data),
    )

    return CompanyTrendsResponse(
        company_id=str(company_id),
        target_currency=target_currency.upper(),
        periods_covered=len(data),
        data=data,
    )
