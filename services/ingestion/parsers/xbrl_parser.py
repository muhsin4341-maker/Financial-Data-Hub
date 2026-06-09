"""
iXBRL/XBRL parser — M4 Step 1: full streaming ingestion pipeline.

Amendment V1.2, Section 7.1 — Streamed Event-Driven XML/XBRL Ingestion:

  MANDATORY: All XBRL/iXBRL documents MUST be parsed using
  ``lxml.etree.iterparse`` in streaming mode.  After processing each
  element, ``elem.clear()`` MUST be called to release memory.

  PROHIBITED: ``file.read()`` and any full-DOM parsing strategy
  (``lxml.etree.parse``, ``xml.etree.ElementTree.parse``, BeautifulSoup
  with full document load, etc.) are BANNED.  Loading a complete 10-K
  iXBRL document into memory at once consumes 200-800 MB per filing and
  will OOM multi-worker Celery deployments.

Amendment V1.2, Section 2.2 — Sign Convention:
  Outflow / expense concepts are multiplied by -1 before packaging.
  The taxonomy map carries an ``is_outflow`` flag for every concept.
  The iXBRL ``sign`` attribute is also respected for document-level
  negation indicators.

  Inflows/assets: stored positive.
  Expenses/outflows: stored negative (applied at parse time).

Amendment V1.2, Section 4.2 — SHA-256 Audit Trail:
  SHA-256 of the raw document bytes is computed once at entry.
  Every ``ParsedLineItem`` carries the same hash so the downstream
  DB insert can populate ``financial_line_items.source_file_hash``.

Parsing architecture (two passes over the same BytesIO):
  Pass 1 — ``_collect_context_map``: streams xbrli:context elements only,
            builds a dict mapping context_id → ContextInfo (period dates).
  Pass 2 — ``stream_xbrl_facts``: streams ix:nonFraction elements, resolves
            each fact's period via the context map, applies taxonomy mapping
            and sign convention, yields ``XBRLFact`` objects.

  ``parse_xbrl_document`` orchestrates both passes and returns a list of
  ``ParsedLineItem`` objects ready for bulk insert into financial_line_items.

Supported taxonomies:
  US_GAAP (FASB us-gaap/*), IFRS (XBRL ifrs-full/*), IND_AS (in-gaap/*).

Milestone: M4-Step38 (full implementation)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Iterator

# lxml is the ONLY permitted XML parser for XBRL (Amendment V1.2 §7.1).
import lxml.etree as ET  # type: ignore[import]

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# XBRL namespace constants
# ---------------------------------------------------------------------------

_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_IXBRL = "http://www.xbrl.org/2013/inlineXBRL"

# Clark-notation tags used in both passes.
_TAG_CONTEXT  = f"{{{_NS_XBRLI}}}context"
_TAG_INSTANT  = f"{{{_NS_XBRLI}}}instant"
_TAG_START    = f"{{{_NS_XBRLI}}}startDate"
_TAG_END      = f"{{{_NS_XBRLI}}}endDate"
_TAG_IX_NF    = f"{{{_NS_IXBRL}}}nonFraction"
_TAG_IX_NN    = f"{{{_NS_IXBRL}}}nonNumeric"

# Decimal scales corresponding to iXBRL ``scale`` attribute values.
_SCALE_MAP: dict[str, Decimal] = {
    "0":  Decimal("1"),
    "1":  Decimal("10"),
    "2":  Decimal("100"),
    "3":  Decimal("1000"),
    "4":  Decimal("10000"),
    "5":  Decimal("100000"),
    "6":  Decimal("1000000"),
    "7":  Decimal("10000000"),
    "8":  Decimal("100000000"),
    "9":  Decimal("1000000000"),
    "-3": Decimal("0.001"),
    "-6": Decimal("0.000001"),
}

# Concept-name prefixes that identify each reporting standard.
_PREFIX_TO_STANDARD: dict[str, str] = {
    "us-gaap":   "US_GAAP",
    "gaap":      "US_GAAP",
    "ifrs-full": "IFRS",
    "ifrs":      "IFRS",
    "in-gaap":   "IND_AS",
    "ind-as":    "IND_AS",
}

# ---------------------------------------------------------------------------
# Taxonomy map: local concept name → (canonical_field, statement_type, is_outflow)
#
# canonical_field  — internal normalised name stored in financial_line_items.
# statement_type   — IS | BS | CF.
# is_outflow       — True applies ×-1 per Amendment V1.2 §2.2.
#
# Covers US_GAAP (FASB), IFRS-full (IASB), and IND_AS equivalents.
# Local names shared across standards appear once; standard-specific
# synonyms are listed separately so every concept resolves.
# ---------------------------------------------------------------------------

_TAXONOMY: dict[str, tuple[str, str, bool]] = {
    # ── Income Statement — Revenue (inflows) ─────────────────────────────────
    "Revenues":                                                        ("revenue",                      "IS", False),
    "RevenueFromContractWithCustomerExcludingAssessedTax":             ("revenue",                      "IS", False),
    "RevenueFromContractWithCustomerIncludingAssessedTax":             ("revenue",                      "IS", False),
    "RevenueFromContractWithCustomer":                                 ("revenue",                      "IS", False),
    "SalesRevenueNet":                                                 ("revenue",                      "IS", False),
    "SalesRevenueGoodsNet":                                            ("revenue",                      "IS", False),
    "NetRevenues":                                                     ("revenue",                      "IS", False),
    "Revenue":                                                         ("revenue",                      "IS", False),
    "RevenueFromContractsWithCustomers":                               ("revenue",                      "IS", False),
    "TotalRevenues":                                                   ("revenue",                      "IS", False),

    # ── Income Statement — Cost of Sales (outflows → ×-1) ────────────────────
    "CostOfGoodsSold":                                                 ("cogs",                         "IS", True),
    "CostOfRevenue":                                                   ("cogs",                         "IS", True),
    "CostOfGoodsAndServicesSold":                                      ("cogs",                         "IS", True),
    "CostOfSales":                                                     ("cogs",                         "IS", True),
    "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization":    ("cogs",                         "IS", True),

    # ── Income Statement — Gross Profit ──────────────────────────────────────
    "GrossProfit":                                                     ("gross_profit",                 "IS", False),

    # ── Income Statement — Operating Expenses (outflows → ×-1) ───────────────
    "OperatingExpenses":                                               ("operating_expenses",           "IS", True),
    "ResearchAndDevelopmentExpense":                                   ("research_and_development",     "IS", True),
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost":     ("research_and_development",     "IS", True),
    "SellingGeneralAndAdministrativeExpense":                          ("selling_general_administrative","IS", True),
    "SellingAndMarketingExpense":                                      ("selling_expense",               "IS", True),
    "GeneralAndAdministrativeExpense":                                 ("general_and_administrative",   "IS", True),
    "DistributionCosts":                                               ("distribution_costs",           "IS", True),
    "OtherOperatingExpenses":                                          ("other_operating_expenses",     "IS", True),
    "OtherOperatingIncomeExpenseNet":                                  ("other_operating_income",       "IS", False),
    "DepreciationAndAmortizationNotIncludedElsewhere":                 ("depreciation_amortization",    "IS", True),

    # ── Income Statement — Operating Income ──────────────────────────────────
    "OperatingIncomeLoss":                                             ("operating_income",             "IS", False),
    "OperatingProfit":                                                 ("operating_income",             "IS", False),

    # ── Income Statement — Below-the-line ────────────────────────────────────
    "InterestExpense":                                                 ("interest_expense",             "IS", True),
    "InterestExpenseNet":                                              ("interest_expense",             "IS", True),
    "FinanceCosts":                                                    ("interest_expense",             "IS", True),
    "FinanceExpenses":                                                 ("interest_expense",             "IS", True),
    "InvestmentIncomeInterest":                                        ("interest_income",              "IS", False),
    "InterestAndDividendIncomeOperating":                              ("interest_income",              "IS", False),
    "FinanceIncome":                                                   ("interest_income",              "IS", False),
    "OtherNonoperatingIncomeExpense":                                  ("other_nonoperating_income",    "IS", False),
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                                                                       ("income_before_tax",            "IS", False),
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic":     ("income_before_tax",            "IS", False),
    "IncomeLossBeforeIncomeTaxExpenseBenefit":                         ("income_before_tax",            "IS", False),
    "ProfitLossBeforeTax":                                             ("income_before_tax",            "IS", False),
    "IncomeTaxExpenseBenefit":                                         ("income_tax_expense",           "IS", True),
    "IncomeTaxExpense":                                                ("income_tax_expense",           "IS", True),

    # ── Income Statement — Net Income ─────────────────────────────────────────
    "NetIncomeLoss":                                                   ("net_income",                   "IS", False),
    "NetIncome":                                                       ("net_income",                   "IS", False),
    "ProfitLoss":                                                      ("net_income",                   "IS", False),
    "NetIncomeLossAvailableToCommonStockholdersBasic":                 ("net_income",                   "IS", False),
    "ProfitLossAttributableToOwnersOfParent":                          ("net_income",                   "IS", False),
    "ComprehensiveIncomeNetOfTax":                                     ("comprehensive_income",         "IS", False),
    "OtherComprehensiveIncomeLossNetOfTax":                            ("other_comprehensive_income",   "IS", False),

    # ── Income Statement — Per-share metrics (NUMERIC(38,10)) ────────────────
    "EarningsPerShareBasic":                                           ("eps_basic",                    "IS", False),
    "BasicEarningsLossPerShare":                                       ("eps_basic",                    "IS", False),
    "EarningsPerShareDiluted":                                         ("eps_diluted",                  "IS", False),
    "DilutedEarningsLossPerShare":                                     ("eps_diluted",                  "IS", False),
    "WeightedAverageNumberOfSharesOutstandingBasic":                   ("shares_basic",                 "IS", False),
    "WeightedAverageNumberOfDilutedSharesOutstanding":                 ("shares_diluted",               "IS", False),

    # ── Balance Sheet — Current Assets ───────────────────────────────────────
    "CashAndCashEquivalentsAtCarryingValue":                           ("cash_and_equivalents",         "BS", False),
    "CashAndCashEquivalents":                                          ("cash_and_equivalents",         "BS", False),
    "Cash":                                                            ("cash_and_equivalents",         "BS", False),
    "CashCashEquivalentsAndShortTermInvestments":                      ("cash_and_short_term_investments","BS", False),
    "ShortTermInvestments":                                            ("short_term_investments",        "BS", False),
    "AccountsReceivableNetCurrent":                                    ("accounts_receivable",          "BS", False),
    "TradeAndOtherReceivablesCurrent":                                 ("accounts_receivable",          "BS", False),
    "ReceivablesCurrent":                                              ("accounts_receivable",          "BS", False),
    "InventoryNet":                                                    ("inventory",                    "BS", False),
    "Inventories":                                                     ("inventory",                    "BS", False),
    "PrepaidExpenseAndOtherAssetsCurrent":                             ("prepaid_and_other_current",    "BS", False),
    "AssetsCurrent":                                                   ("total_current_assets",         "BS", False),
    "CurrentAssets":                                                   ("total_current_assets",         "BS", False),

    # ── Balance Sheet — Non-Current Assets ───────────────────────────────────
    "PropertyPlantAndEquipmentNet":                                    ("property_plant_equipment",     "BS", False),
    "PropertyPlantAndEquipment":                                       ("property_plant_equipment",     "BS", False),
    "Goodwill":                                                        ("goodwill",                     "BS", False),
    "IntangibleAssetsNetExcludingGoodwill":                            ("intangible_assets",            "BS", False),
    "IntangibleAssetsOtherThanGoodwill":                               ("intangible_assets",            "BS", False),
    "IntangibleAssets":                                                ("intangible_assets",            "BS", False),
    "GoodwillAndIntangibleAssetsNet":                                  ("goodwill_and_intangibles",     "BS", False),
    "LongTermInvestments":                                             ("long_term_investments",        "BS", False),
    "AssetsNoncurrent":                                                ("total_noncurrent_assets",      "BS", False),
    "NoncurrentAssets":                                                ("total_noncurrent_assets",      "BS", False),
    "DeferredTaxAssetsLiabilitiesNet":                                 ("deferred_tax_asset",           "BS", False),
    "DeferredIncomeTaxAssetsNet":                                      ("deferred_tax_asset",           "BS", False),

    # ── Balance Sheet — Total Assets ─────────────────────────────────────────
    "Assets":                                                          ("total_assets",                 "BS", False),
    "TotalAssets":                                                     ("total_assets",                 "BS", False),

    # ── Balance Sheet — Current Liabilities ──────────────────────────────────
    "AccountsPayableCurrent":                                          ("accounts_payable",             "BS", False),
    "TradeAndOtherPayablesCurrent":                                    ("accounts_payable",             "BS", False),
    "AccruedLiabilitiesCurrent":                                       ("accrued_liabilities",          "BS", False),
    "DeferredRevenueCurrent":                                          ("deferred_revenue",             "BS", False),
    "ContractWithCustomerLiabilityCurrent":                            ("deferred_revenue",             "BS", False),
    "ShortTermBorrowings":                                             ("short_term_debt",              "BS", False),
    "LongTermDebtCurrent":                                             ("current_portion_long_term_debt","BS", False),
    "LiabilitiesCurrent":                                              ("total_current_liabilities",    "BS", False),
    "CurrentLiabilities":                                              ("total_current_liabilities",    "BS", False),

    # ── Balance Sheet — Non-Current Liabilities ───────────────────────────────
    "LongTermDebt":                                                    ("long_term_debt",               "BS", False),
    "LongTermDebtNoncurrent":                                          ("long_term_debt",               "BS", False),
    "LongTermBorrowings":                                              ("long_term_debt",               "BS", False),
    "DeferredRevenueNoncurrent":                                       ("deferred_revenue_noncurrent",  "BS", False),
    "DeferredIncomeTaxLiabilitiesNet":                                 ("deferred_tax_liability",       "BS", False),
    "LiabilitiesNoncurrent":                                           ("total_noncurrent_liabilities", "BS", False),
    "NoncurrentLiabilities":                                           ("total_noncurrent_liabilities", "BS", False),

    # ── Balance Sheet — Total Liabilities ────────────────────────────────────
    "Liabilities":                                                     ("total_liabilities",            "BS", False),
    "TotalLiabilities":                                                ("total_liabilities",            "BS", False),

    # ── Balance Sheet — Equity ───────────────────────────────────────────────
    "CommonStockValue":                                                ("common_stock",                 "BS", False),
    "CommonStock":                                                     ("common_stock",                 "BS", False),
    "ShareCapital":                                                    ("common_stock",                 "BS", False),
    "AdditionalPaidInCapital":                                         ("additional_paid_in_capital",   "BS", False),
    "AdditionalPaidInCapitalCommonStock":                              ("additional_paid_in_capital",   "BS", False),
    "SharePremium":                                                    ("additional_paid_in_capital",   "BS", False),
    "RetainedEarningsAccumulatedDeficit":                              ("retained_earnings",            "BS", False),
    "RetainedEarnings":                                                ("retained_earnings",            "BS", False),
    "AccumulatedOtherComprehensiveIncomeLossNetOfTax":                 ("accumulated_oci",              "BS", False),
    "OtherReserves":                                                   ("accumulated_oci",              "BS", False),
    "TreasuryStockValue":                                              ("treasury_stock",               "BS", False),
    "StockholdersEquity":                                              ("total_equity",                 "BS", False),
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest":
                                                                       ("total_equity",                 "BS", False),
    "Equity":                                                          ("total_equity",                 "BS", False),
    "EquityAttributableToOwnersOfParent":                              ("total_equity",                 "BS", False),
    "MinorityInterest":                                                ("noncontrolling_interest",      "BS", False),
    "LiabilitiesAndStockholdersEquity":                                ("total_liabilities_and_equity", "BS", False),

    # ── Cash Flow — Operating ─────────────────────────────────────────────────
    "NetCashProvidedByUsedInOperatingActivities":                      ("operating_cash_flow",          "CF", False),
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations":  ("operating_cash_flow",          "CF", False),
    "CashFlowsFromUsedInOperatingActivities":                          ("operating_cash_flow",          "CF", False),
    "DepreciationDepletionAndAmortization":                            ("depreciation_amortization",    "CF", False),
    "DepreciationAmortisationAndImpairmentLoss":                       ("depreciation_amortization",    "CF", False),
    "DepreciationAndAmortization":                                     ("depreciation_amortization",    "CF", False),
    "ShareBasedCompensation":                                          ("stock_based_compensation",     "CF", False),
    "DeferredIncomeTaxExpenseBenefit":                                 ("deferred_tax_cf",              "CF", False),
    "IncreaseDecreaseInAccountsReceivable":                            ("changes_in_receivables",       "CF", False),
    "IncreaseDecreaseInInventories":                                   ("changes_in_inventory",         "CF", False),
    "IncreaseDecreaseInAccountsPayable":                               ("changes_in_payables",          "CF", False),

    # ── Cash Flow — Investing ─────────────────────────────────────────────────
    "NetCashProvidedByUsedInInvestingActivities":                      ("investing_cash_flow",          "CF", False),
    "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations":  ("investing_cash_flow",          "CF", False),
    "CashFlowsFromUsedInInvestingActivities":                          ("investing_cash_flow",          "CF", False),
    "PaymentsToAcquirePropertyPlantAndEquipment":                      ("capex",                        "CF", True),
    "PurchaseOfPropertyPlantAndEquipment":                             ("capex",                        "CF", True),
    "PurchasesOfPropertyPlantAndEquipment":                            ("capex",                        "CF", True),
    "PaymentsToAcquireBusinessesNetOfCashAcquired":                    ("acquisitions",                 "CF", True),
    "AcquisitionsNetOfCashAcquiredAndPurchasesOfBusinesses":           ("acquisitions",                 "CF", True),
    "PaymentsToAcquireInvestments":                                    ("purchase_of_investments",      "CF", True),
    "ProceedsFromSaleOfInvestments":                                   ("proceeds_from_investments",    "CF", False),
    "ProceedsFromSaleOfPropertyPlantAndEquipment":                     ("proceeds_from_asset_sales",    "CF", False),

    # ── Cash Flow — Financing ─────────────────────────────────────────────────
    "NetCashProvidedByUsedInFinancingActivities":                      ("financing_cash_flow",          "CF", False),
    "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations":  ("financing_cash_flow",          "CF", False),
    "CashFlowsFromUsedInFinancingActivities":                          ("financing_cash_flow",          "CF", False),
    "PaymentsOfDividends":                                             ("dividends_paid",               "CF", True),
    "PaymentsOfDividendsCommonStock":                                  ("dividends_paid",               "CF", True),
    "DividendsPaid":                                                   ("dividends_paid",               "CF", True),
    "RepaymentsOfLongTermDebt":                                        ("debt_repayment",               "CF", True),
    "ProceedsFromIssuanceOfCommonStock":                               ("equity_issuance_proceeds",     "CF", False),
    "ProceedsFromIssuanceOfLongTermDebt":                              ("debt_issuance_proceeds",       "CF", False),
    "PaymentsForRepurchaseOfCommonStock":                              ("share_buybacks",               "CF", True),

    # ── Cash Flow — Net Change ────────────────────────────────────────────────
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect":
                                                                       ("net_change_in_cash",           "CF", False),
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect":
                                                                       ("net_change_in_cash",           "CF", False),
    "IncreaseDecreaseInCashAndCashEquivalents":                        ("net_change_in_cash",           "CF", False),
    "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents":
                                                                       ("fx_effect_on_cash",            "CF", False),
}

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class ContextInfo:
    """
    Period information decoded from an xbrli:context element.

    For balance-sheet (instant) contexts, ``period_start`` is None.
    For flow (duration) contexts, both dates are populated.
    """

    context_id: str
    period_start: date | None   # None for instant contexts (BS)
    period_end: date            # instant date OR duration end date
    entity_cik: str = ""        # SEC CIK extracted from identifier element


@dataclass
class XBRLFact:
    """
    A single raw fact extracted from an ix:nonFraction element before
    taxonomy resolution. Kept as an intermediate representation.

    sign_factor is -1 when the taxonomy marks the concept as an outflow
    OR when the iXBRL ``sign`` attribute indicates document-level negation.
    The two factors are multiplied together: an outflow concept with
    sign="-" (double negation) would yield +1 (back to positive).
    """

    concept_raw: str            # fully prefixed name: e.g. 'us-gaap:Revenues'
    context_ref: str
    value_raw: str              # cleaned numeric string (commas stripped)
    unit_ref: str | None
    decimals: str | None
    sign_factor: int            # 1 or -1
    scale: Decimal              # iXBRL scale multiplier (default 1)
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedLineItem:
    """
    A fully resolved financial data point ready for insertion into
    ``financial_line_items``.  Maps 1:1 to the FinancialLineItem ORM model.

    All monetary values are in the reported currency at this stage;
    USD translation is deferred to the CurrencyNormaliser
    (services/extraction/normaliser/currency.py).
    """

    # FinancialLineItem fields
    company_id: str
    fiscal_year: int
    fiscal_period: str          # Q1 | Q2 | Q3 | Q4 | FY
    reporting_standard: str     # US_GAAP | IFRS | IND_AS
    filing_date: date           # date the filing was submitted to EDGAR
    canonical_field: str        # normalised internal name (e.g. 'revenue')
    concept_raw: str            # original XBRL concept (audit trail)
    statement_type: str         # IS | BS | CF
    value_reported: Decimal     # sign-corrected value in reported currency
    reported_currency: str | None
    source_file_hash: str       # SHA-256 of the raw document bytes (§4.2)
    extraction_method: str = "xbrl"
    derived_expression_formula: str | None = None

    # Period dates — needed by CurrencyNormaliser for split translation (§3)
    period_end_date: date | None = None
    period_start_date: date | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_reporting_standard(concept_raw: str) -> str:
    """
    Determine the reporting standard from the prefixed concept name.

    'us-gaap:Revenues'   → 'US_GAAP'
    'ifrs-full:Revenue'  → 'IFRS'
    'in-gaap:Revenue'    → 'IND_AS'
    'unknown:Concept'    → 'US_GAAP' (default for SEC filings)
    """
    if ":" in concept_raw:
        prefix = concept_raw.split(":")[0].lower()
        return _PREFIX_TO_STANDARD.get(prefix, "US_GAAP")
    return "US_GAAP"


_PAREN_RE = re.compile(r"^\s*\(([0-9,.\s]+)\)\s*$")
_NUM_CLEAN_RE = re.compile(r"[,\s]")


def _parse_numeric(raw: str, scale: Decimal, sign_factor: int) -> Decimal | None:
    """
    Parse an iXBRL numeric value string to a sign-corrected Decimal.

    Handles:
    - Comma-separated thousands: "1,234,567" → 1234567
    - Parenthetical negatives: "(1,234)" → -1234
    - Scale multiplier from iXBRL ``scale`` attribute
    - External sign_factor from taxonomy + iXBRL sign attribute

    Returns None if the string cannot be parsed as a number (e.g. empty,
    "N/A", text labels left inside nonFraction by malformed XBRL).
    """
    if not raw:
        return None

    # Parenthetical format encodes a negative value in source documents.
    paren_match = _PAREN_RE.match(raw)
    if paren_match:
        raw = paren_match.group(1)
        sign_factor = sign_factor * -1

    cleaned = _NUM_CLEAN_RE.sub("", raw)
    if not cleaned or cleaned in ("-", "+", "."):
        return None

    try:
        value = Decimal(cleaned) * scale * Decimal(sign_factor)
    except InvalidOperation:
        return None

    return value


def _infer_fiscal_period(
    period_start: date | None,
    period_end: date,
) -> tuple[int, str]:
    """
    Derive (fiscal_year, fiscal_period) from XBRL context period dates.

    Duration mapping (approximate day count):
      < 100 days  → single quarter (QX based on period_end month)
      100-200 days → H1 or Q2 YTD → 'Q2'
      200-300 days → 9-month YTD  → 'Q3'
      ≥ 300 days  → annual        → 'FY'

    Instant (period_start is None) → quarter based on end month.

    Quarter mapping by end month:
      Mar (3) → Q1, Jun (6) → Q2, Sep (9) → Q3, Dec (12) → Q4
      Others  → FY (full year or non-standard reporting)

    Fiscal year is always period_end.year.
    """
    fiscal_year = period_end.year

    if period_start is None:
        # Instant context — balance sheet point in time.
        return fiscal_year, _month_to_quarter(period_end.month)

    duration_days = (period_end - period_start).days

    if duration_days < 100:
        return fiscal_year, _month_to_quarter(period_end.month)
    elif duration_days < 200:
        return fiscal_year, "Q2"
    elif duration_days < 300:
        return fiscal_year, "Q3"
    else:
        return fiscal_year, "FY"


def _month_to_quarter(month: int) -> str:
    if month <= 3:
        return "Q1"
    elif month <= 6:
        return "Q2"
    elif month <= 9:
        return "Q3"
    else:
        return "Q4"


def _parse_date(text: str | None) -> date | None:
    """Parse YYYY-MM-DD date string; return None on failure."""
    if not text:
        return None
    text = text.strip()
    try:
        parts = text.split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Pass 1 — collect xbrli:context map
# ---------------------------------------------------------------------------


def _collect_context_map(content_bytes: bytes) -> dict[str, ContextInfo]:
    """
    Stream the document once and collect all xbrli:context elements.

    Memory cost: O(number of contexts) — typically 20-200 per filing.
    The context elements are small; only facts (thousands) need streaming.

    Args:
        content_bytes: Raw iXBRL/XBRL document bytes.

    Returns:
        Dict mapping context_id → ContextInfo.
    """
    ctx_map: dict[str, ContextInfo] = {}
    source = BytesIO(content_bytes)

    try:
        for _event, elem in ET.iterparse(source, events=("end",), recover=True):
            if elem.tag != _TAG_CONTEXT:
                # Clear non-context elements immediately; we only care about contexts here.
                elem.clear()
                continue

            ctx_id = elem.get("id", "")
            if not ctx_id:
                elem.clear()
                continue

            # Locate period child — can be under any namespace variant.
            instant_text: str | None = None
            start_text: str | None = None
            end_text: str | None = None
            entity_cik: str = ""

            for child in elem.iter():
                ctag = child.tag
                if ctag == _TAG_INSTANT:
                    instant_text = child.text
                elif ctag == _TAG_START:
                    start_text = child.text
                elif ctag == _TAG_END:
                    end_text = child.text
                elif ctag.endswith("}identifier"):
                    entity_cik = (child.text or "").strip()

            if instant_text:
                d = _parse_date(instant_text)
                if d:
                    ctx_map[ctx_id] = ContextInfo(
                        context_id=ctx_id,
                        period_start=None,
                        period_end=d,
                        entity_cik=entity_cik,
                    )
            elif end_text:
                d_end = _parse_date(end_text)
                d_start = _parse_date(start_text)
                if d_end:
                    ctx_map[ctx_id] = ContextInfo(
                        context_id=ctx_id,
                        period_start=d_start,
                        period_end=d_end,
                        entity_cik=entity_cik,
                    )

            # Amendment V1.2 §7.1 — mandatory memory release.
            elem.clear()
            parent = elem.getparent()
            if parent is not None:
                while len(parent) > 0 and parent[0] is not elem:
                    del parent[0]

    except ET.XMLSyntaxError as exc:
        log.warning("xbrl_parser.context_pass_syntax_error", error=str(exc))

    return ctx_map


# ---------------------------------------------------------------------------
# Pass 2 — stream ix:nonFraction facts
# ---------------------------------------------------------------------------


def stream_xbrl_facts(
    content_bytes: bytes,
    context_map: dict[str, ContextInfo],
    *,
    filing_accession: str = "",
) -> Iterator[XBRLFact]:
    """
    Stream ix:nonFraction elements and yield resolved XBRLFact objects.

    Sign resolution (Amendment V1.2 §2.2):
      1. Look up local concept name in _TAXONOMY.
      2. If is_outflow=True, taxonomy_sign = -1, else +1.
      3. Read iXBRL ``sign`` attribute: if sign="-", ixbrl_sign = -1, else +1.
      4. Effective sign_factor = taxonomy_sign × ixbrl_sign.
         (Double negation on an already-negative outflow → correct positive.)

    Args:
        content_bytes:   Raw iXBRL/XBRL document bytes.
        context_map:     Dict from _collect_context_map().
        filing_accession: Accession number for log context.

    Yields:
        XBRLFact for each resolved numeric concept.
    """
    source = BytesIO(content_bytes)
    fact_count = 0
    skipped_unmapped = 0
    parse_errors = 0

    try:
        for _event, elem in ET.iterparse(source, events=("end",), recover=True):
            if elem.tag != _TAG_IX_NF:
                elem.clear()
                continue

            try:
                concept_raw = elem.get("name", "")
                context_ref = elem.get("contextRef", "")

                if not concept_raw or not context_ref:
                    continue

                # Resolve local name (strip prefix) for taxonomy lookup.
                local_name = concept_raw.split(":")[-1] if ":" in concept_raw else concept_raw
                taxonomy_entry = _TAXONOMY.get(local_name)

                if taxonomy_entry is None:
                    skipped_unmapped += 1
                    continue

                _canonical, _stmt_type, is_outflow = taxonomy_entry

                # iXBRL sign attribute: "-" means the displayed value is inverted.
                ixbrl_sign_attr = elem.get("sign", "")
                ixbrl_sign = -1 if ixbrl_sign_attr == "-" else 1

                # taxonomy sign
                taxonomy_sign = -1 if is_outflow else 1

                # Combined sign factor: outflow × ixbrl_sign
                sign_factor = taxonomy_sign * ixbrl_sign

                # iXBRL scale attribute (power of 10 multiplier).
                scale_attr = elem.get("scale", "0")
                scale = _SCALE_MAP.get(scale_attr, Decimal("1"))

                # Raw text — the formatted numeric string inside the element.
                raw_text = (elem.text or "").strip()

                # Preserve attributes that don't appear as separate columns.
                extra = {
                    k: v for k, v in elem.attrib.items()
                    if k not in ("name", "contextRef", "unitRef", "decimals", "sign", "scale")
                }

                fact_count += 1
                yield XBRLFact(
                    concept_raw=concept_raw,
                    context_ref=context_ref,
                    value_raw=raw_text,
                    unit_ref=elem.get("unitRef"),
                    decimals=elem.get("decimals"),
                    sign_factor=sign_factor,
                    scale=scale,
                    extra=extra,
                )

            except Exception as exc:  # noqa: BLE001
                parse_errors += 1
                log.debug(
                    "xbrl_parser.fact_error",
                    accession=filing_accession,
                    error=str(exc),
                )
            finally:
                # Amendment V1.2 §7.1 — MANDATORY memory release.
                elem.clear()
                parent = elem.getparent()
                if parent is not None:
                    while len(parent) > 0 and parent[0] is not elem:
                        del parent[0]

    finally:
        log.debug(
            "xbrl_parser.stream_complete",
            accession=filing_accession,
            facts_yielded=fact_count,
            skipped_unmapped=skipped_unmapped,
            parse_errors=parse_errors,
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_xbrl_document(
    content_bytes: bytes,
    *,
    company_id: str,
    filing_date: date,
    filing_accession: str = "",
    source_url: str = "",
) -> list[ParsedLineItem]:
    """
    Parse a raw iXBRL/XBRL document and return structured financial line items.

    Orchestrates both passes:
      1. Collect context map (period dates per context_ref).
      2. Stream facts, resolve taxonomy, apply sign convention.
      3. Package each valid fact as a ParsedLineItem.

    Amendment V1.2 §4.2: SHA-256 is computed once from ``content_bytes`` and
    stamped on every output item for downstream SOX audit trail linkage.

    Amendment V1.2 §2.2: Sign inversion for outflow/expense concepts is
    applied inside stream_xbrl_facts() via the taxonomy ``is_outflow`` flag.

    Amendment V1.2 §7.1: Neither pass calls file.read() or loads a full DOM.

    Args:
        content_bytes:    Raw bytes of the iXBRL/XBRL document.
        company_id:       UUID string of the company record.
        filing_date:      Date the filing was submitted to SEC EDGAR.
        filing_accession: SEC accession number (for logging).
        source_url:       URL of the source document (for audit log).

    Returns:
        List of ParsedLineItem, one per resolved and parseable fact.
        Facts that cannot be parsed (non-numeric, missing context) are
        silently skipped; a debug log entry is emitted for each skip.
    """
    # Amendment V1.2 §4.2 — SHA-256 computed once at document entry.
    source_file_hash = hashlib.sha256(content_bytes).hexdigest()

    log.info(
        "xbrl_parser.parse_start",
        accession=filing_accession,
        content_bytes=len(content_bytes),
        source_file_hash=source_file_hash[:16] + "...",
    )

    # Pass 1 — build context map.
    context_map = _collect_context_map(content_bytes)
    log.debug(
        "xbrl_parser.context_map_built",
        accession=filing_accession,
        context_count=len(context_map),
    )

    # Pass 2 — stream facts and package output.
    items: list[ParsedLineItem] = []
    skipped_no_context = 0
    skipped_no_value = 0

    for fact in stream_xbrl_facts(content_bytes, context_map, filing_accession=filing_accession):
        # Resolve the context to get period dates.
        ctx = context_map.get(fact.context_ref)
        if ctx is None:
            skipped_no_context += 1
            log.debug(
                "xbrl_parser.no_context",
                accession=filing_accession,
                context_ref=fact.context_ref,
                concept=fact.concept_raw,
            )
            continue

        # Parse the numeric value with scale and sign.
        value = _parse_numeric(fact.value_raw, fact.scale, fact.sign_factor)
        if value is None:
            skipped_no_value += 1
            log.debug(
                "xbrl_parser.unparseable_value",
                accession=filing_accession,
                concept=fact.concept_raw,
                raw=fact.value_raw,
            )
            continue

        # Resolve taxonomy entry (guaranteed to exist because stream_xbrl_facts
        # already filtered unmapped concepts; look up again for field/type).
        local_name = fact.concept_raw.split(":")[-1] if ":" in fact.concept_raw else fact.concept_raw
        taxonomy_entry = _TAXONOMY[local_name]
        canonical_field, statement_type, _is_outflow = taxonomy_entry

        # Infer fiscal year and period from context period dates.
        fiscal_year, fiscal_period = _infer_fiscal_period(
            ctx.period_start, ctx.period_end
        )

        # Determine reporting standard from concept prefix.
        reporting_standard = _classify_reporting_standard(fact.concept_raw)

        items.append(ParsedLineItem(
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            reporting_standard=reporting_standard,
            filing_date=filing_date,
            canonical_field=canonical_field,
            concept_raw=fact.concept_raw,
            statement_type=statement_type,
            value_reported=value,
            reported_currency=_normalise_currency(fact.unit_ref),
            source_file_hash=source_file_hash,
            extraction_method="xbrl",
            period_end_date=ctx.period_end,
            period_start_date=ctx.period_start,
        ))

    log.info(
        "xbrl_parser.parse_complete",
        accession=filing_accession,
        items_produced=len(items),
        skipped_no_context=skipped_no_context,
        skipped_no_value=skipped_no_value,
    )
    return items


def _normalise_currency(unit_ref: str | None) -> str | None:
    """
    Convert an XBRL unit reference to an ISO 4217 currency code.

    SEC EDGAR XBRL unit references use the pattern 'iso4217:USD' or
    plain 'USD'. Share-count units ('shares') are returned as None
    since they are not monetary values.
    """
    if not unit_ref:
        return None
    # Strip iso4217: prefix if present.
    code = unit_ref.split(":")[-1].upper()
    # 3-letter alphabetic codes only; 'SHARES', 'PURE', etc. are not currencies.
    if len(code) == 3 and code.isalpha():
        return code
    return None
