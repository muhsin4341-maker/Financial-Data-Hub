"""
Multi-Framework Normalization & Aggregation Views — M5 Step 3.

Responsibility:
  Transform disparate financial data produced under three accounting frameworks
  (US_GAAP / IFRS / IND_AS) into a unified, comparable presentation profile
  suitable for cross-border consolidation and analyst-facing views.

This module operates AFTER the currency translation layer (M5 Steps 1 & 2)
and is consumed BY the Excel export builder (M6) and the REST API view layer.
It never writes to the database — it is a pure read/compute service.

Three core deliverables (per Amendment V1.2 M5 Step 3 spec):

  1. Canonical Normalization Mapping (§1.2):
       _FRAMEWORK_CONCEPT_MAP documents which XBRL concept names in each
       framework resolve to a unified internal canonical category.
       _CANONICAL_TO_PRESENTATION maps the parser-produced canonical_field
       value to a display-level PresentationCategory (an enum).

  2. High-Precision Delta Reconciliation (§1.1):
       compute_consolidation_delta() calculates:
           fx_rounding_variance = Sum(subsidiary.total_assets_usd)
                                  − consolidated_parent.total_assets_usd
       All arithmetic uses Python Decimal exclusively.  The variance is
       stored and returned as NUMERIC(26,2) (Decimal quantised to 2 dp).
       A materiality threshold (default USD 100 000) flags large variances.

  3. Point-in-Time Restatement Filtering (§2.2 / §7.2):
       All DB queries hard-filter is_restated = FALSE via the partial index
       ix_financial_line_items_current.  Superseded rows are never pulled.
       When multiple filing_date rows exist for the same canonical_field
       (rare edge case under the composite unique key), the most recent
       filing_date wins — consistent with the restatement writer's convention.

Amendment V1.2 precision contract:
  - All aggregated USD values: Decimal, quantised to NUMERIC(26,2) at output.
  - fx_rounding_variance: Decimal, NUMERIC(26,2).
  - No float() operations anywhere in this module.

Architecture position:
  BulkCurrencyTranslator (M5 Step 2) fills value_usd on every row
    ↓
  MultiFrameworkAggregationEngine (this module) reads value_usd,
    aggregates, normalises, and detects consolidation variances
    ↓
  Excel export builder (M6) consumes NormalizedProfile / ComparativeView

Session ownership:
  This service is read-only.  All session interactions are SELECT.
  No session.commit() or session.flush() calls appear here.

Milestone: M5 Step 3 — Multi-Framework Normalization & Aggregation Views
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Sequence

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import FinancialLineItem

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Precision constants (Amendment V1.2 §1.1)
# ---------------------------------------------------------------------------

_MONETARY_SCALE: Decimal = Decimal("0.01")   # NUMERIC(26,2) for value_usd / variance
_ZERO: Decimal = Decimal("0")

# Materiality threshold for fx_rounding_variance flag (USD 100 000).
# Variances below this are expected FX rounding artefacts.
# Variances above this may indicate a consolidation or data error.
_DEFAULT_MATERIALITY_USD: Decimal = Decimal("100000.00")


# ---------------------------------------------------------------------------
# Presentation category enum
# ---------------------------------------------------------------------------


class PresentationCategory(str, enum.Enum):
    """
    Unified display-level category labels for the comparative profile view.

    Each value maps to one or more internal canonical_field names produced
    by the XBRL parser (see _CANONICAL_TO_PRESENTATION below).

    String-valued so it serialises cleanly to JSON / Excel column headers.
    """

    # ── Income Statement ──────────────────────────────────────────────────────
    TOTAL_REVENUE                = "total_revenue"
    COST_OF_GOODS_SOLD           = "cost_of_goods_sold"
    GROSS_PROFIT                 = "gross_profit"
    RESEARCH_AND_DEVELOPMENT     = "research_and_development"
    SELLING_GENERAL_ADMIN        = "selling_general_admin"
    TOTAL_OPERATING_EXPENSES     = "total_operating_expenses"
    OPERATING_INCOME             = "operating_income"
    INTEREST_EXPENSE             = "interest_expense"
    INTEREST_INCOME              = "interest_income"
    OTHER_NONOPERATING_INCOME    = "other_nonoperating_income"
    INCOME_BEFORE_TAX            = "income_before_tax"
    INCOME_TAX_EXPENSE           = "income_tax_expense"
    NET_INCOME                   = "net_income"
    COMPREHENSIVE_INCOME         = "comprehensive_income"
    EPS_BASIC                    = "eps_basic"
    EPS_DILUTED                  = "eps_diluted"

    # ── Balance Sheet — Assets ───────────────────────────────────────────────
    CASH_AND_EQUIVALENTS         = "cash_and_equivalents"
    SHORT_TERM_INVESTMENTS       = "short_term_investments"
    ACCOUNTS_RECEIVABLE          = "accounts_receivable"
    INVENTORY                    = "inventory"
    TOTAL_CURRENT_ASSETS         = "total_current_assets"
    PROPERTY_PLANT_EQUIPMENT     = "property_plant_equipment"
    GOODWILL                     = "goodwill"
    INTANGIBLE_ASSETS            = "intangible_assets"
    TOTAL_NONCURRENT_ASSETS      = "total_noncurrent_assets"
    TOTAL_ASSETS                 = "total_assets"

    # ── Balance Sheet — Liabilities ───────────────────────────────────────────
    ACCOUNTS_PAYABLE             = "accounts_payable"
    SHORT_TERM_DEBT              = "short_term_debt"
    TOTAL_CURRENT_LIABILITIES    = "total_current_liabilities"
    LONG_TERM_DEBT               = "long_term_debt"
    TOTAL_NONCURRENT_LIABILITIES = "total_noncurrent_liabilities"
    TOTAL_LIABILITIES            = "total_liabilities"

    # ── Balance Sheet — Equity ────────────────────────────────────────────────
    RETAINED_EARNINGS            = "retained_earnings"
    TOTAL_EQUITY                 = "total_equity"
    NONCONTROLLING_INTEREST      = "noncontrolling_interest"
    TOTAL_LIABILITIES_AND_EQUITY = "total_liabilities_and_equity"

    # ── Cash Flow ─────────────────────────────────────────────────────────────
    OPERATING_CASH_FLOW          = "operating_cash_flow"
    CAPEX                        = "capex"
    INVESTING_CASH_FLOW          = "investing_cash_flow"
    DIVIDENDS_PAID               = "dividends_paid"
    FINANCING_CASH_FLOW          = "financing_cash_flow"
    NET_CHANGE_IN_CASH           = "net_change_in_cash"
    FX_EFFECT_ON_CASH            = "fx_effect_on_cash"


# ---------------------------------------------------------------------------
# Canonical-to-presentation mapping
# ---------------------------------------------------------------------------

# Maps parser-produced canonical_field → PresentationCategory.
# Canonical fields not listed here are passed through with their own name
# as a fallback (raw passthrough).
_CANONICAL_TO_PRESENTATION: dict[str, PresentationCategory] = {
    # Income Statement
    "revenue":                      PresentationCategory.TOTAL_REVENUE,
    "cogs":                         PresentationCategory.COST_OF_GOODS_SOLD,
    "gross_profit":                 PresentationCategory.GROSS_PROFIT,
    "research_and_development":     PresentationCategory.RESEARCH_AND_DEVELOPMENT,
    "selling_general_administrative": PresentationCategory.SELLING_GENERAL_ADMIN,
    "selling_expense":              PresentationCategory.SELLING_GENERAL_ADMIN,
    "general_and_administrative":   PresentationCategory.SELLING_GENERAL_ADMIN,
    "distribution_costs":           PresentationCategory.SELLING_GENERAL_ADMIN,
    "operating_expenses":           PresentationCategory.TOTAL_OPERATING_EXPENSES,
    "other_operating_expenses":     PresentationCategory.TOTAL_OPERATING_EXPENSES,
    "operating_income":             PresentationCategory.OPERATING_INCOME,
    "interest_expense":             PresentationCategory.INTEREST_EXPENSE,
    "interest_income":              PresentationCategory.INTEREST_INCOME,
    "other_nonoperating_income":    PresentationCategory.OTHER_NONOPERATING_INCOME,
    "other_operating_income":       PresentationCategory.OTHER_NONOPERATING_INCOME,
    "income_before_tax":            PresentationCategory.INCOME_BEFORE_TAX,
    "income_tax_expense":           PresentationCategory.INCOME_TAX_EXPENSE,
    "net_income":                   PresentationCategory.NET_INCOME,
    "comprehensive_income":         PresentationCategory.COMPREHENSIVE_INCOME,
    "other_comprehensive_income":   PresentationCategory.COMPREHENSIVE_INCOME,
    "eps_basic":                    PresentationCategory.EPS_BASIC,
    "eps_diluted":                  PresentationCategory.EPS_DILUTED,
    # Balance Sheet — Assets
    "cash_and_equivalents":         PresentationCategory.CASH_AND_EQUIVALENTS,
    "cash_and_short_term_investments": PresentationCategory.CASH_AND_EQUIVALENTS,
    "short_term_investments":       PresentationCategory.SHORT_TERM_INVESTMENTS,
    "accounts_receivable":          PresentationCategory.ACCOUNTS_RECEIVABLE,
    "inventory":                    PresentationCategory.INVENTORY,
    "prepaid_and_other_current":    PresentationCategory.TOTAL_CURRENT_ASSETS,
    "total_current_assets":         PresentationCategory.TOTAL_CURRENT_ASSETS,
    "property_plant_equipment":     PresentationCategory.PROPERTY_PLANT_EQUIPMENT,
    "goodwill":                     PresentationCategory.GOODWILL,
    "intangible_assets":            PresentationCategory.INTANGIBLE_ASSETS,
    "goodwill_and_intangibles":     PresentationCategory.INTANGIBLE_ASSETS,
    "long_term_investments":        PresentationCategory.TOTAL_NONCURRENT_ASSETS,
    "deferred_tax_asset":           PresentationCategory.TOTAL_NONCURRENT_ASSETS,
    "total_noncurrent_assets":      PresentationCategory.TOTAL_NONCURRENT_ASSETS,
    "total_assets":                 PresentationCategory.TOTAL_ASSETS,
    # Balance Sheet — Liabilities
    "accounts_payable":             PresentationCategory.ACCOUNTS_PAYABLE,
    "accrued_liabilities":          PresentationCategory.ACCOUNTS_PAYABLE,
    "deferred_revenue":             PresentationCategory.ACCOUNTS_PAYABLE,
    "short_term_debt":              PresentationCategory.SHORT_TERM_DEBT,
    "current_portion_long_term_debt": PresentationCategory.SHORT_TERM_DEBT,
    "total_current_liabilities":    PresentationCategory.TOTAL_CURRENT_LIABILITIES,
    "long_term_debt":               PresentationCategory.LONG_TERM_DEBT,
    "deferred_revenue_noncurrent":  PresentationCategory.TOTAL_NONCURRENT_LIABILITIES,
    "deferred_tax_liability":       PresentationCategory.TOTAL_NONCURRENT_LIABILITIES,
    "total_noncurrent_liabilities": PresentationCategory.TOTAL_NONCURRENT_LIABILITIES,
    "total_liabilities":            PresentationCategory.TOTAL_LIABILITIES,
    # Balance Sheet — Equity
    "common_stock":                 PresentationCategory.TOTAL_EQUITY,
    "additional_paid_in_capital":   PresentationCategory.TOTAL_EQUITY,
    "retained_earnings":            PresentationCategory.RETAINED_EARNINGS,
    "accumulated_oci":              PresentationCategory.TOTAL_EQUITY,
    "treasury_stock":               PresentationCategory.TOTAL_EQUITY,
    "total_equity":                 PresentationCategory.TOTAL_EQUITY,
    "noncontrolling_interest":      PresentationCategory.NONCONTROLLING_INTEREST,
    "total_liabilities_and_equity": PresentationCategory.TOTAL_LIABILITIES_AND_EQUITY,
    # Cash Flow
    "operating_cash_flow":          PresentationCategory.OPERATING_CASH_FLOW,
    "depreciation_amortization":    PresentationCategory.OPERATING_CASH_FLOW,
    "stock_based_compensation":     PresentationCategory.OPERATING_CASH_FLOW,
    "deferred_tax_cf":              PresentationCategory.OPERATING_CASH_FLOW,
    "changes_in_receivables":       PresentationCategory.OPERATING_CASH_FLOW,
    "changes_in_inventory":         PresentationCategory.OPERATING_CASH_FLOW,
    "changes_in_payables":          PresentationCategory.OPERATING_CASH_FLOW,
    "capex":                        PresentationCategory.CAPEX,
    "acquisitions":                 PresentationCategory.INVESTING_CASH_FLOW,
    "purchase_of_investments":      PresentationCategory.INVESTING_CASH_FLOW,
    "proceeds_from_investments":    PresentationCategory.INVESTING_CASH_FLOW,
    "proceeds_from_asset_sales":    PresentationCategory.INVESTING_CASH_FLOW,
    "investing_cash_flow":          PresentationCategory.INVESTING_CASH_FLOW,
    "dividends_paid":               PresentationCategory.DIVIDENDS_PAID,
    "debt_repayment":               PresentationCategory.FINANCING_CASH_FLOW,
    "equity_issuance_proceeds":     PresentationCategory.FINANCING_CASH_FLOW,
    "debt_issuance_proceeds":       PresentationCategory.FINANCING_CASH_FLOW,
    "share_buybacks":               PresentationCategory.FINANCING_CASH_FLOW,
    "financing_cash_flow":          PresentationCategory.FINANCING_CASH_FLOW,
    "net_change_in_cash":           PresentationCategory.NET_CHANGE_IN_CASH,
    "fx_effect_on_cash":            PresentationCategory.FX_EFFECT_ON_CASH,
}

# ---------------------------------------------------------------------------
# Cross-framework concept equivalency map (Amendment V1.2 §1.2)
# ---------------------------------------------------------------------------
#
# Documents which XBRL concept names from each accounting framework resolve
# to the same unified PresentationCategory.  Used by the API documentation
# layer, audit reports, and the Excel "Mapping Notes" sheet in M6.
#
# Key   = PresentationCategory value string
# Value = dict[framework_code, list[xbrl_concept_local_name]]
#
# This map is intentionally comprehensive for the six most important
# presentation categories that frequently diverge across frameworks.

_FRAMEWORK_CONCEPT_MAP: dict[str, dict[str, list[str]]] = {
    "total_revenue": {
        "US_GAAP": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "RevenueFromContractWithCustomer",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "NetRevenues",
            "TotalRevenues",
        ],
        "IFRS": [
            "Revenue",
            "RevenueFromContractsWithCustomers",
        ],
        "IND_AS": [
            "RevenueFromOperations",
            "Revenue",
            "RevenueFromContractsWithCustomers",
            "NetRevenue",
        ],
    },
    "net_income": {
        "US_GAAP": [
            "NetIncomeLoss",
            "NetIncome",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ],
        "IFRS": [
            "ProfitLoss",
            "ProfitLossAttributableToOwnersOfParent",
        ],
        "IND_AS": [
            "ProfitLoss",
            "ProfitForThePeriod",
            "ProfitLossAttributableToOwnersOfParent",
        ],
    },
    "total_assets": {
        "US_GAAP": ["Assets", "TotalAssets"],
        "IFRS":    ["Assets", "TotalAssets"],
        "IND_AS":  ["Assets", "TotalAssets", "TotalAssetsAbstract"],
    },
    "total_liabilities": {
        "US_GAAP": ["Liabilities", "TotalLiabilities"],
        "IFRS":    ["Liabilities", "TotalLiabilities"],
        "IND_AS":  ["Liabilities", "TotalLiabilities"],
    },
    "total_equity": {
        "US_GAAP": [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ],
        "IFRS": [
            "Equity",
            "EquityAttributableToOwnersOfParent",
        ],
        "IND_AS": [
            "Equity",
            "TotalEquity",
            "ShareholdersEquity",
        ],
    },
    "operating_income": {
        "US_GAAP": ["OperatingIncomeLoss"],
        "IFRS":    ["OperatingProfit", "ProfitLossFromOperatingActivities"],
        "IND_AS":  ["OperatingProfit", "ProfitFromOperations"],
    },
    "operating_cash_flow": {
        "US_GAAP": [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ],
        "IFRS":    ["CashFlowsFromUsedInOperatingActivities"],
        "IND_AS":  ["NetCashFromOperatingActivities", "CashGeneratedFromOperations"],
    },
    "cost_of_goods_sold": {
        "US_GAAP": [
            "CostOfGoodsSold",
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
        "IFRS":    ["CostOfSales", "CostOfGoodsSold"],
        "IND_AS":  ["CostOfMaterialsConsumed", "CostOfGoodsSold", "CostOfSales"],
    },
    "income_tax_expense": {
        "US_GAAP": ["IncomeTaxExpenseBenefit"],
        "IFRS":    ["IncomeTaxExpense"],
        "IND_AS":  ["IncomeTaxExpense", "TaxExpenseOfDiscontinuedOperations"],
    },
}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class NormalizedValue:
    """
    A single financial data point in the normalized presentation layer.

    Attributes:
        category:           PresentationCategory this value was mapped to.
        canonical_field:    Raw canonical_field from the DB row (audit trail).
        reporting_standard: Source accounting framework (US_GAAP | IFRS | IND_AS).
        value_usd:          Translated USD amount, NUMERIC(26,2). None if
                            the currency translator has not yet run.
        filing_date:        The filing date of the source row.  When multiple
                            filing dates exist for the same canonical_field,
                            the most recent is selected (§7.2 convention).
    """

    category: PresentationCategory | str   # str fallback for unmapped fields
    canonical_field: str
    reporting_standard: str
    value_usd: Decimal | None
    filing_date: date


@dataclass
class NormalizedProfile:
    """
    One entity's financial profile mapped to PresentationCategory keys.

    When a PresentationCategory has multiple contributing canonical_fields
    (e.g., TOTAL_CURRENT_ASSETS aggregates several sub-items), the
    ``values`` dict holds the single most representative row (the one with
    the highest-precision canonical_field that directly represents the
    category, e.g., ``total_current_assets`` preferred over sub-items).

    Attributes:
        company_id:         Entity UUID.
        fiscal_year:        Integer fiscal year.
        fiscal_period:      'FY' | 'Q1' | … .
        reporting_standard: Dominant framework for this entity's filings.
        values:             category_key → NormalizedValue (USD).
        source_row_count:   Total DB rows that contributed to this profile.
        untranslated_count: Rows with value_usd IS NULL (translation pending).
    """

    company_id: uuid.UUID
    fiscal_year: int
    fiscal_period: str
    reporting_standard: str
    values: dict[str, NormalizedValue] = field(default_factory=dict)
    source_row_count: int = 0
    untranslated_count: int = 0

    def get_usd(self, category: PresentationCategory | str) -> Decimal | None:
        """Return the quantised USD value for a category, or None if absent."""
        key = category.value if isinstance(category, PresentationCategory) else category
        nv = self.values.get(key)
        if nv is None or nv.value_usd is None:
            return None
        return nv.value_usd


@dataclass
class ConsolidationDelta:
    """
    Cross-border FX rounding variance between a parent's consolidated
    balance sheet and the sum of its translated subsidiary balances.

    Amendment V1.2 §1.1: fx_rounding_variance is NUMERIC(26,2).

    Attributes:
        parent_company_id:       UUID of the parent entity.
        subsidiary_company_ids:  UUIDs of the consolidated subsidiaries.
        consolidated_assets_usd: Parent's reported total_assets in USD.
        sum_subsidiary_assets_usd: Arithmetic sum of each subsidiary's
                                   total_assets_usd as translated by M5 Step 2.
        fx_rounding_variance:    consolidated_assets − sum_subsidiary_assets,
                                 NUMERIC(26,2).  Expected small negative or
                                 positive due to FX rounding and elimination
                                 entries.
        variance_is_material:    True when abs(variance) > materiality_threshold.
        materiality_threshold:   The USD threshold applied (default 100 000).
        subsidiary_asset_detail: Per-subsidiary breakdown for audit trail.
    """

    parent_company_id: uuid.UUID
    subsidiary_company_ids: list[uuid.UUID]
    consolidated_assets_usd: Decimal
    sum_subsidiary_assets_usd: Decimal
    fx_rounding_variance: Decimal             # NUMERIC(26,2)
    variance_is_material: bool
    materiality_threshold: Decimal
    subsidiary_asset_detail: dict[str, Decimal] = field(default_factory=dict)
    # str keys are str(company_id)


@dataclass
class ComparativeView:
    """
    Side-by-side NormalizedProfiles for N entities over the same period.

    Suitable for direct consumption by the M6 Excel export builder — each
    entity maps to one column in the comparison worksheet.

    Attributes:
        entities:           Ordered list of NormalizedProfile objects.
        fiscal_year:        Common fiscal year.
        fiscal_period:      Common fiscal period.
        ordered_categories: Presentation-ordered list of all category keys
                            present across at least one entity.
    """

    entities: list[NormalizedProfile]
    fiscal_year: int
    fiscal_period: str
    ordered_categories: list[str] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        return len(self.entities)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MultiFrameworkAggregationEngine:
    """
    Cross-framework aggregation and normalization engine.

    Reads translated FinancialLineItem rows (value_usd NOT NULL, is_restated=FALSE)
    and produces unified NormalizedProfile / ComparativeView outputs that abstract
    away US_GAAP / IFRS / IND_AS structural differences.

    The engine is stateless and read-only: it accepts an AsyncSession per call
    and issues SELECT queries only.  No mutations are made to the database.

    All aggregated monetary values are Python Decimal quantised to NUMERIC(26,2).
    No float() operations appear in this class.

    Usage::

        engine = MultiFrameworkAggregationEngine()

        # Single entity profile:
        profile = await engine.build_normalized_profile(
            session, company_id=apple_uuid, fiscal_year=2024, fiscal_period="FY"
        )

        # Multi-entity comparison:
        view = await engine.build_comparative_view(
            session,
            company_ids=[apple_uuid, msft_uuid, infosys_uuid],
            fiscal_year=2024,
            fiscal_period="FY",
        )

        # Parent + subsidiaries consolidation delta:
        parent   = await engine.build_normalized_profile(session, parent_uuid, 2024, "FY")
        sub_a    = await engine.build_normalized_profile(session, sub_a_uuid, 2024, "FY")
        sub_b    = await engine.build_normalized_profile(session, sub_b_uuid, 2024, "FY")
        delta    = engine.compute_consolidation_delta(parent, [sub_a, sub_b])
    """

    # ── Public entry points ────────────────────────────────────────────────────

    async def build_normalized_profile(
        self,
        session: AsyncSession,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
    ) -> NormalizedProfile:
        """
        Build a NormalizedProfile for one entity and period.

        Query: SELECT * FROM financial_line_items
               WHERE company_id = :company_id
                 AND fiscal_year = :fiscal_year
                 AND fiscal_period = :fiscal_period
                 AND is_restated = FALSE
               ORDER BY filing_date DESC

        When multiple non-restated rows share the same canonical_field
        (can occur when two different filings cover the same period), the
        most recent filing_date is used and the earlier one is silently
        superseded for profile purposes (§7.2 tie-break convention).

        Args:
            session:       Active AsyncSession.
            company_id:    Entity UUID.
            fiscal_year:   Integer fiscal year.
            fiscal_period: 'FY' | 'Q1' | 'Q2' | 'Q3' | 'Q4'.

        Returns:
            NormalizedProfile with values keyed by PresentationCategory.value.
        """
        rows = await self._fetch_rows(session, company_id, fiscal_year, fiscal_period)
        return self._build_profile_from_rows(rows, company_id, fiscal_year, fiscal_period)

    async def build_comparative_view(
        self,
        session: AsyncSession,
        company_ids: Sequence[uuid.UUID],
        fiscal_year: int,
        fiscal_period: str,
    ) -> ComparativeView:
        """
        Build a ComparativeView across N entities for the same period.

        Entities are processed sequentially so a missing-data condition for
        one entity does not abort the others — the profile is still returned
        (with value_usd = None for missing categories).

        The ``ordered_categories`` list on the returned view is sorted in the
        canonical presentation order defined by _PRESENTATION_ORDER.

        Args:
            session:       Active AsyncSession.
            company_ids:   Ordered sequence of entity UUIDs to compare.
            fiscal_year:   Common fiscal year.
            fiscal_period: Common fiscal period.

        Returns:
            ComparativeView with one NormalizedProfile per entity.
        """
        profiles: list[NormalizedProfile] = []
        all_category_keys: set[str] = set()

        for cid in company_ids:
            profile = await self.build_normalized_profile(
                session, cid, fiscal_year, fiscal_period
            )
            profiles.append(profile)
            all_category_keys.update(profile.values.keys())

        ordered = _sort_categories(all_category_keys)

        log.info(
            "aggregation.comparative_view_built",
            entity_count=len(profiles),
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            category_count=len(ordered),
        )

        return ComparativeView(
            entities=profiles,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            ordered_categories=ordered,
        )

    def compute_consolidation_delta(
        self,
        parent_profile: NormalizedProfile,
        subsidiary_profiles: list[NormalizedProfile],
        materiality_threshold: Decimal = _DEFAULT_MATERIALITY_USD,
    ) -> ConsolidationDelta:
        """
        Compute the cross-border FX rounding variance for a consolidation group.

        Formula (Amendment V1.2 §1.1):
            fx_rounding_variance =
                consolidated_parent_assets − Sum(subsidiary_translated_assets)

        A positive variance indicates the parent reports MORE consolidated assets
        than the simple sum of subsidiaries (e.g., due to intercompany eliminations
        that reduce gross assets at the subsidiary level).

        A negative variance indicates the subsidiaries' translated total exceeds
        the parent's reported consolidated total — unusual; may signal a data gap
        or FX rate inconsistency.

        Precision: all arithmetic uses unbounded Decimal.
        fx_rounding_variance is quantised to NUMERIC(26,2) at return time.

        Args:
            parent_profile:        NormalizedProfile for the parent entity.
            subsidiary_profiles:   NormalizedProfile list for each subsidiary.
            materiality_threshold: USD threshold above which variance is flagged
                                   as potentially material (default USD 100 000).

        Returns:
            ConsolidationDelta with variance details and per-subsidiary breakdown.
        """
        parent_assets = parent_profile.get_usd(PresentationCategory.TOTAL_ASSETS)
        if parent_assets is None:
            parent_assets = _ZERO
            log.warning(
                "aggregation.parent_assets_missing",
                company_id=str(parent_profile.company_id),
                fiscal_year=parent_profile.fiscal_year,
                fiscal_period=parent_profile.fiscal_period,
            )

        # Accumulate subsidiary assets with full Decimal precision.
        subsidiary_detail: dict[str, Decimal] = {}
        running_sum: Decimal = _ZERO

        for sub in subsidiary_profiles:
            sub_assets = sub.get_usd(PresentationCategory.TOTAL_ASSETS)
            sub_assets = sub_assets if sub_assets is not None else _ZERO
            subsidiary_detail[str(sub.company_id)] = sub_assets
            running_sum += sub_assets

        # Variance: parent consolidated minus sum of parts.
        # Expected to be small positive (intercompany eliminations) for healthy data.
        raw_variance: Decimal = parent_assets - running_sum
        variance_q: Decimal = _quantise(raw_variance)

        is_material = abs(variance_q) > materiality_threshold

        if is_material:
            log.warning(
                "aggregation.material_fx_variance",
                parent_company_id=str(parent_profile.company_id),
                fx_rounding_variance=str(variance_q),
                materiality_threshold=str(materiality_threshold),
                consolidated_assets=str(parent_assets),
                sum_subsidiary_assets=str(running_sum),
            )
        else:
            log.debug(
                "aggregation.immaterial_fx_variance",
                parent_company_id=str(parent_profile.company_id),
                fx_rounding_variance=str(variance_q),
            )

        return ConsolidationDelta(
            parent_company_id=parent_profile.company_id,
            subsidiary_company_ids=[p.company_id for p in subsidiary_profiles],
            consolidated_assets_usd=_quantise(parent_assets),
            sum_subsidiary_assets_usd=_quantise(running_sum),
            fx_rounding_variance=variance_q,
            variance_is_material=is_material,
            materiality_threshold=_quantise(materiality_threshold),
            subsidiary_asset_detail=subsidiary_detail,
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    async def _fetch_rows(
        session: AsyncSession,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
    ) -> list[FinancialLineItem]:
        """
        Fetch all non-restated line items for a given entity + period.

        Amendment V1.2 §7.2: is_restated = FALSE filter ensures superseded
        rows never pollute the profile.  SQLAlchemy routes this through the
        partial index ix_financial_line_items_current for performance.

        Rows are ordered by filing_date DESC so that the tie-break logic in
        _build_profile_from_rows naturally encounters the most recent row first.
        """
        stmt = (
            select(FinancialLineItem)
            .where(
                and_(
                    FinancialLineItem.company_id == company_id,
                    FinancialLineItem.fiscal_year == fiscal_year,
                    FinancialLineItem.fiscal_period == fiscal_period,
                    FinancialLineItem.is_restated.is_(False),
                )
            )
            .order_by(
                FinancialLineItem.filing_date.desc(),
                FinancialLineItem.canonical_field,
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def _build_profile_from_rows(
        rows: list[FinancialLineItem],
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
    ) -> NormalizedProfile:
        """
        Convert raw DB rows into a NormalizedProfile.

        Selection strategy per canonical_field:
          - Since rows are ordered filing_date DESC, the first row encountered
            for each canonical_field is the most recent (tie-break winner).
          - A row's value_usd is used verbatim (already quantised by M5 Step 2).

        Presentation category selection strategy:
          - When multiple canonical_fields share a PresentationCategory
            (e.g., 'revenue', 'total_revenues' both → TOTAL_REVENUE), the
            highest-priority canonical_field wins per _CATEGORY_PRIORITY.
          - A NormalizedProfile holds ONE NormalizedValue per category.

        Reporting standard is taken from the most common standard across rows
        (majority vote), so a profile built from rows with mixed standards
        (e.g., a restatement that changed GAAP) picks the dominant framework.
        """
        profile = NormalizedProfile(
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            reporting_standard="UNKNOWN",
        )

        if not rows:
            log.debug(
                "aggregation.no_rows",
                company_id=str(company_id),
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
            )
            return profile

        profile.source_row_count = len(rows)

        # Tally reporting standards for majority vote.
        std_counts: dict[str, int] = {}

        # Per-canonical_field deduplication: first row wins (filing_date DESC order).
        seen_canonical: set[str] = set()

        # Per-category: track which canonical_field currently occupies it.
        # Higher-priority canonical wins based on _CATEGORY_PRIORITY.
        category_occupant: dict[str, tuple[int, str]] = {}
        # key = category_key, value = (priority_rank, canonical_field)

        raw_values: dict[str, NormalizedValue] = {}

        for row in rows:
            canonical = row.canonical_field

            # Count standard occurrence for majority-vote.
            std_str = (
                row.reporting_standard.value
                if hasattr(row.reporting_standard, "value")
                else str(row.reporting_standard)
            )
            std_counts[std_str] = std_counts.get(std_str, 0) + 1

            # Track untranslated rows.
            if row.value_usd is None:
                profile.untranslated_count += 1

            # Skip duplicate canonical_fields (filing_date tie-break: keep first).
            if canonical in seen_canonical:
                continue
            seen_canonical.add(canonical)

            # Resolve presentation category.
            category = _CANONICAL_TO_PRESENTATION.get(canonical)
            if category is None:
                # Unmapped field: use raw canonical as a passthrough category.
                category_key = canonical
            else:
                category_key = category.value

            # Apply category-level priority so that a direct measure (e.g.,
            # 'total_assets') beats a sub-component (e.g., 'long_term_investments')
            # that maps to the same broad category.
            priority = _CATEGORY_PRIORITY.get(canonical, 999)
            existing = category_occupant.get(category_key)

            if existing is None or priority < existing[0]:
                category_occupant[category_key] = (priority, canonical)
                raw_values[category_key] = NormalizedValue(
                    category=category or category_key,
                    canonical_field=canonical,
                    reporting_standard=std_str,
                    value_usd=(
                        _quantise(row.value_usd) if row.value_usd is not None else None
                    ),
                    filing_date=row.filing_date,
                )

        profile.values = raw_values
        profile.reporting_standard = (
            max(std_counts, key=lambda k: std_counts[k]) if std_counts else "UNKNOWN"
        )

        log.debug(
            "aggregation.profile_built",
            company_id=str(company_id),
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            source_rows=profile.source_row_count,
            categories_resolved=len(raw_values),
            untranslated=profile.untranslated_count,
            reporting_standard=profile.reporting_standard,
        )

        return profile


# ---------------------------------------------------------------------------
# Category priority table
# ---------------------------------------------------------------------------

# Lower rank = higher priority within the same PresentationCategory.
# Direct summary lines beat sub-items so the profile shows the authoritative
# figure from the filing rather than a sub-component that shares a category.
_CATEGORY_PRIORITY: dict[str, int] = {
    # Income Statement — exact aggregates preferred
    "revenue":              1,
    "net_income":           1,
    "gross_profit":         1,
    "operating_income":     1,
    "income_before_tax":    1,
    "income_tax_expense":   1,
    "comprehensive_income": 1,
    # Balance Sheet — summary lines beat components
    "total_assets":                  1,
    "total_current_assets":          1,
    "total_noncurrent_assets":       1,
    "total_liabilities":             1,
    "total_current_liabilities":     1,
    "total_noncurrent_liabilities":  1,
    "total_equity":                  1,
    "total_liabilities_and_equity":  1,
    # Cash Flow — top-line aggregates beat line-item adjustments
    "operating_cash_flow":  1,
    "investing_cash_flow":  1,
    "financing_cash_flow":  1,
    "net_change_in_cash":   1,
    # Sub-components (lower priority — still shown if no aggregate present)
    "cash_and_equivalents": 2,
    "accounts_receivable":  2,
    "inventory":            2,
    "long_term_debt":       2,
    "retained_earnings":    2,
    "capex":                2,
}


# ---------------------------------------------------------------------------
# Presentation ordering
# ---------------------------------------------------------------------------

# Canonical display order for ComparativeView columns — mirrors the order
# in which sections appear in a standard financial statement package.
_PRESENTATION_ORDER: list[str] = [
    # Income Statement
    PresentationCategory.TOTAL_REVENUE.value,
    PresentationCategory.COST_OF_GOODS_SOLD.value,
    PresentationCategory.GROSS_PROFIT.value,
    PresentationCategory.RESEARCH_AND_DEVELOPMENT.value,
    PresentationCategory.SELLING_GENERAL_ADMIN.value,
    PresentationCategory.TOTAL_OPERATING_EXPENSES.value,
    PresentationCategory.OPERATING_INCOME.value,
    PresentationCategory.INTEREST_EXPENSE.value,
    PresentationCategory.INTEREST_INCOME.value,
    PresentationCategory.OTHER_NONOPERATING_INCOME.value,
    PresentationCategory.INCOME_BEFORE_TAX.value,
    PresentationCategory.INCOME_TAX_EXPENSE.value,
    PresentationCategory.NET_INCOME.value,
    PresentationCategory.COMPREHENSIVE_INCOME.value,
    PresentationCategory.EPS_BASIC.value,
    PresentationCategory.EPS_DILUTED.value,
    # Balance Sheet
    PresentationCategory.CASH_AND_EQUIVALENTS.value,
    PresentationCategory.SHORT_TERM_INVESTMENTS.value,
    PresentationCategory.ACCOUNTS_RECEIVABLE.value,
    PresentationCategory.INVENTORY.value,
    PresentationCategory.TOTAL_CURRENT_ASSETS.value,
    PresentationCategory.PROPERTY_PLANT_EQUIPMENT.value,
    PresentationCategory.GOODWILL.value,
    PresentationCategory.INTANGIBLE_ASSETS.value,
    PresentationCategory.TOTAL_NONCURRENT_ASSETS.value,
    PresentationCategory.TOTAL_ASSETS.value,
    PresentationCategory.ACCOUNTS_PAYABLE.value,
    PresentationCategory.SHORT_TERM_DEBT.value,
    PresentationCategory.TOTAL_CURRENT_LIABILITIES.value,
    PresentationCategory.LONG_TERM_DEBT.value,
    PresentationCategory.TOTAL_NONCURRENT_LIABILITIES.value,
    PresentationCategory.TOTAL_LIABILITIES.value,
    PresentationCategory.RETAINED_EARNINGS.value,
    PresentationCategory.NONCONTROLLING_INTEREST.value,
    PresentationCategory.TOTAL_EQUITY.value,
    PresentationCategory.TOTAL_LIABILITIES_AND_EQUITY.value,
    # Cash Flow
    PresentationCategory.OPERATING_CASH_FLOW.value,
    PresentationCategory.CAPEX.value,
    PresentationCategory.INVESTING_CASH_FLOW.value,
    PresentationCategory.DIVIDENDS_PAID.value,
    PresentationCategory.FINANCING_CASH_FLOW.value,
    PresentationCategory.NET_CHANGE_IN_CASH.value,
    PresentationCategory.FX_EFFECT_ON_CASH.value,
]

_PRESENTATION_ORDER_INDEX: dict[str, int] = {
    cat: i for i, cat in enumerate(_PRESENTATION_ORDER)
}


def _sort_categories(category_keys: set[str]) -> list[str]:
    """
    Return a presentation-ordered list of category keys.

    Known categories follow _PRESENTATION_ORDER.
    Unknown (passthrough) categories are appended alphabetically at the end.
    """
    known = [k for k in _PRESENTATION_ORDER if k in category_keys]
    unknown = sorted(k for k in category_keys if k not in _PRESENTATION_ORDER_INDEX)
    return known + unknown


# ---------------------------------------------------------------------------
# Precision helper
# ---------------------------------------------------------------------------


def _quantise(value: Decimal) -> Decimal:
    """Quantise a Decimal to NUMERIC(26,2) using ROUND_HALF_EVEN."""
    return value.quantize(_MONETARY_SCALE, rounding=ROUND_HALF_EVEN)
