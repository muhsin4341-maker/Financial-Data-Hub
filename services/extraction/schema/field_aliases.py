"""
Canonical field alias table and resolver — M5.3.

Maps plain-text concept labels extracted by the AI into standardised regulatory
taxonomy keys for US GAAP (``us-gaap:…``), IFRS (``ifrs-full:…``), and Ind AS
(``ind-as:…``).

Architecture
────────────
The alias table is organised as a two-level mapping:

    _ALIAS_TABLE: dict[str, dict[str, str]]
      └─ framework key  (e.g. "US_GAAP")
           └─ normalised label  →  canonical XBRL tag

At query time, ``resolve_canonical_tag`` normalises the raw label (lower-case,
collapse whitespace, strip punctuation) and looks it up in the framework's sub-
dictionary.  If not found, it falls back to a ``raw:{slug}`` identifier that
preserves the original text for downstream analysis — consistent with the slug
logic already used in ``extractor.py`` (``f"raw:{slug}"``).

Extensibility rules
───────────────────
1. Add a new framework: add a new top-level key to ``_ALIAS_TABLE``.  No code
   changes needed elsewhere.
2. Add new aliases to an existing framework: add entries to that framework's
   ``dict[str, str]`` — normalise the key yourself or let ``_normalise`` do it
   at build time (the table is normalised once at module import).
3. Aliases must map *normalised* plain-text labels → XBRL CamelCase tags.
   All keys in ``_ALIAS_TABLE`` use the same normalisation applied at query
   time, so you may write keys in any case/spacing — they are normalised on
   first import.

Rate convention alignment
─────────────────────────
The alias table does NOT alter sign convention.  Sign direction is the
responsibility of ``_apply_sign_convention`` in ``extractor.py``.

XBRL tag prefixes
─────────────────
  us-gaap:   US Generally Accepted Accounting Principles (FASB XBRL taxonomy).
  ifrs-full: International Financial Reporting Standards (IASB taxonomy).
  ind-as:    Indian Accounting Standards (MCA Ind AS taxonomy, mirrors IFRS).

Milestone: M5.3 — Canonical Field Alias Table
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

# Regex to collapse whitespace and strip non-alphanumeric, non-space chars.
_NON_ALNUM_RE: Final = re.compile(r"[^a-z0-9 ]+")
_MULTI_SPACE_RE: Final = re.compile(r" {2,}")


def _normalise(label: str) -> str:
    """
    Normalise a raw label for alias table lookup.

    Steps:
      1. Unicode NFKC normalisation (converts ligatures, full-width chars, etc.)
      2. Lower-case.
      3. Strip leading/trailing whitespace.
      4. Remove all characters that are not ASCII alphanumeric or space.
      5. Collapse multiple spaces to a single space.
      6. Strip again after collapsing.

    The same function is applied both when the table is built (at module import)
    and at query time, guaranteeing that table keys and query keys are always
    in the same canonical form.

    Examples:
        "Total Revenue"         → "total revenue"
        "  Net  Income (Loss)"  → "net income loss"
        "Cost of Goods Sold"    → "cost of goods sold"
        "R&D Expense"           → "rd expense"
        "Turnover"              → "turnover"
    """
    label = unicodedata.normalize("NFKC", label)
    label = label.lower().strip()
    label = _NON_ALNUM_RE.sub(" ", label)
    label = _MULTI_SPACE_RE.sub(" ", label)
    return label.strip()


def _slug(label: str) -> str:
    """
    Convert *label* to a URL-safe slug for use in ``raw:`` fallback identifiers.

    Replaces all non-alphanumeric characters with underscores and strips
    leading/trailing underscores — exactly the same logic as in extractor.py
    so that ``field_aliases.py`` fallback IDs remain consistent with the AI
    extraction layer's fallback IDs.

    Example: "Total Revenue (USD)" → "Total_Revenue_USD"
    """
    return re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_")


# ---------------------------------------------------------------------------
# Alias table
# ---------------------------------------------------------------------------
# Raw structure — keys may be written in any casing/spacing.
# ``_build_alias_table`` normalises all keys at import time so query-time
# normalisation always finds a match without case sensitivity issues.
#
# Alias coverage targets the most frequent financial statement line items that
# appear in annual reports for the three supported frameworks.  The table is
# intentionally comprehensive for common concepts and relies on the ``raw:``
# fallback for long-tail items — adding entries here over time is the preferred
# extension mechanism.
#
# Column layout:  "<raw alias>"  :  "<canonical XBRL tag>"

_RAW_ALIAS_TABLE: Final[dict[str, dict[str, str]]] = {
    # =========================================================================
    # US GAAP  (FASB XBRL US GAAP Taxonomy)
    # =========================================================================
    "US_GAAP": {
        # ── Income Statement ─────────────────────────────────────────────────
        # Revenue / Top-line
        "Revenue":                                          "us-gaap:Revenues",
        "Revenues":                                         "us-gaap:Revenues",
        "Total Revenue":                                    "us-gaap:Revenues",
        "Total Revenues":                                   "us-gaap:Revenues",
        "Net Revenue":                                      "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Net Revenues":                                     "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Net Sales":                                        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Sales":                                            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Net Sales Revenue":                                "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Product Revenue":                                  "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Service Revenue":                                  "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Total Net Revenue":                                "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        # Cost of revenue
        "Cost of Revenue":                                  "us-gaap:CostOfRevenue",
        "Cost of Revenues":                                 "us-gaap:CostOfRevenue",
        "Cost of Goods Sold":                               "us-gaap:CostOfGoodsSold",
        "Cost of Sales":                                    "us-gaap:CostOfGoodsSold",
        "Cost of Products Sold":                            "us-gaap:CostOfGoodsSold",
        "Cost of Services":                                 "us-gaap:CostOfRevenue",
        # Gross profit
        "Gross Profit":                                     "us-gaap:GrossProfit",
        "Gross Margin":                                     "us-gaap:GrossProfit",
        "Gross Income":                                     "us-gaap:GrossProfit",
        # Operating expenses
        "Operating Expenses":                               "us-gaap:OperatingExpenses",
        "Total Operating Expenses":                         "us-gaap:OperatingExpenses",
        "Research and Development":                         "us-gaap:ResearchAndDevelopmentExpense",
        "Research and Development Expense":                 "us-gaap:ResearchAndDevelopmentExpense",
        "R&D Expense":                                      "us-gaap:ResearchAndDevelopmentExpense",
        "R&D":                                              "us-gaap:ResearchAndDevelopmentExpense",
        "Selling General and Administrative":               "us-gaap:SellingGeneralAndAdministrativeExpense",
        "Selling General and Administrative Expense":       "us-gaap:SellingGeneralAndAdministrativeExpense",
        "SG&A":                                             "us-gaap:SellingGeneralAndAdministrativeExpense",
        "SG&A Expense":                                     "us-gaap:SellingGeneralAndAdministrativeExpense",
        "General and Administrative Expense":               "us-gaap:GeneralAndAdministrativeExpense",
        "General and Administrative":                       "us-gaap:GeneralAndAdministrativeExpense",
        "Selling Expense":                                  "us-gaap:SellingExpense",
        "Marketing Expense":                                "us-gaap:MarketingExpense",
        "Depreciation and Amortization":                    "us-gaap:DepreciationDepletionAndAmortization",
        "Depreciation & Amortization":                      "us-gaap:DepreciationDepletionAndAmortization",
        "Depreciation":                                     "us-gaap:Depreciation",
        "Amortization":                                     "us-gaap:AmortizationOfIntangibleAssets",
        "Restructuring Charges":                            "us-gaap:RestructuringCharges",
        "Impairment Charges":                               "us-gaap:AssetImpairmentCharges",
        "Other Operating Expenses":                         "us-gaap:OtherOperatingIncomeExpenseNet",
        # Operating income
        "Operating Income":                                 "us-gaap:OperatingIncomeLoss",
        "Operating Loss":                                   "us-gaap:OperatingIncomeLoss",
        "Operating Income (Loss)":                          "us-gaap:OperatingIncomeLoss",
        "Income from Operations":                           "us-gaap:OperatingIncomeLoss",
        "Loss from Operations":                             "us-gaap:OperatingIncomeLoss",
        "EBIT":                                             "us-gaap:OperatingIncomeLoss",
        # Non-operating items
        "Interest Expense":                                 "us-gaap:InterestExpense",
        "Interest Income":                                  "us-gaap:InterestAndDividendIncomeOperating",
        "Interest and Other Income":                        "us-gaap:NonoperatingIncomeExpense",
        "Other Income (Expense)":                           "us-gaap:NonoperatingIncomeExpense",
        "Other Income":                                     "us-gaap:OtherNonoperatingIncome",
        "Other Expense":                                    "us-gaap:OtherNonoperatingExpense",
        "Non-operating Income (Expense)":                   "us-gaap:NonoperatingIncomeExpense",
        "Investment Income":                                "us-gaap:InvestmentIncomeNet",
        "Gain (Loss) on Investments":                       "us-gaap:GainLossOnInvestments",
        "Foreign Currency Gain (Loss)":                     "us-gaap:ForeignCurrencyTransactionGainLossBeforeTax",
        # Pre-tax income
        "Income Before Income Taxes":                       "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "Pretax Income":                                    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "Pre-tax Income":                                   "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "Income Before Taxes":                              "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "Earnings Before Tax":                              "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "EBT":                                              "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        # Tax
        "Income Tax Expense":                               "us-gaap:IncomeTaxExpenseBenefit",
        "Income Tax Benefit":                               "us-gaap:IncomeTaxExpenseBenefit",
        "Income Tax Expense (Benefit)":                     "us-gaap:IncomeTaxExpenseBenefit",
        "Provision for Income Taxes":                       "us-gaap:IncomeTaxExpenseBenefit",
        # Net income
        "Net Income":                                       "us-gaap:NetIncomeLoss",
        "Net Loss":                                         "us-gaap:NetIncomeLoss",
        "Net Income (Loss)":                                "us-gaap:NetIncomeLoss",
        "Net Income Attributable to Common Stockholders":   "us-gaap:NetIncomeLoss",
        "Profit for the Period":                            "us-gaap:NetIncomeLoss",
        "Net Earnings":                                     "us-gaap:NetIncomeLoss",
        # EPS
        "Basic EPS":                                        "us-gaap:EarningsPerShareBasic",
        "Diluted EPS":                                      "us-gaap:EarningsPerShareDiluted",
        "Earnings Per Share Basic":                         "us-gaap:EarningsPerShareBasic",
        "Earnings Per Share Diluted":                       "us-gaap:EarningsPerShareDiluted",
        "Basic Earnings Per Share":                         "us-gaap:EarningsPerShareBasic",
        "Diluted Earnings Per Share":                       "us-gaap:EarningsPerShareDiluted",
        # EBITDA / non-GAAP (mapped to nearest GAAP concept)
        "EBITDA":                                           "us-gaap:EarningsBeforeInterestTaxesDepreciationAndAmortization",
        "Adjusted EBITDA":                                  "us-gaap:EarningsBeforeInterestTaxesDepreciationAndAmortization",

        # ── Balance Sheet — Assets ────────────────────────────────────────────
        "Total Assets":                                     "us-gaap:Assets",
        "Assets":                                           "us-gaap:Assets",
        # Current assets
        "Current Assets":                                   "us-gaap:AssetsCurrent",
        "Total Current Assets":                             "us-gaap:AssetsCurrent",
        "Cash and Cash Equivalents":                        "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "Cash":                                             "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "Cash and Equivalents":                             "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "Short-term Investments":                           "us-gaap:ShortTermInvestments",
        "Marketable Securities Current":                    "us-gaap:MarketableSecuritiesCurrent",
        "Accounts Receivable":                              "us-gaap:AccountsReceivableNetCurrent",
        "Accounts Receivable Net":                          "us-gaap:AccountsReceivableNetCurrent",
        "Trade Receivables":                                "us-gaap:AccountsReceivableNetCurrent",
        "Net Receivables":                                  "us-gaap:ReceivablesNetCurrent",
        "Inventories":                                      "us-gaap:InventoryNet",
        "Inventory":                                        "us-gaap:InventoryNet",
        "Prepaid Expenses":                                 "us-gaap:PrepaidExpenseAndOtherAssetsCurrent",
        "Other Current Assets":                             "us-gaap:OtherAssetsCurrent",
        # Non-current assets
        "Non-current Assets":                               "us-gaap:AssetsNoncurrent",
        "Total Non-current Assets":                         "us-gaap:AssetsNoncurrent",
        "Long-term Investments":                            "us-gaap:LongTermInvestments",
        "Property Plant and Equipment":                     "us-gaap:PropertyPlantAndEquipmentNet",
        "Property Plant and Equipment Net":                 "us-gaap:PropertyPlantAndEquipmentNet",
        "PP&E":                                             "us-gaap:PropertyPlantAndEquipmentNet",
        "Goodwill":                                         "us-gaap:Goodwill",
        "Intangible Assets":                                "us-gaap:IntangibleAssetsNetExcludingGoodwill",
        "Intangible Assets Net":                            "us-gaap:IntangibleAssetsNetExcludingGoodwill",
        "Deferred Tax Assets":                              "us-gaap:DeferredIncomeTaxAssetsNet",
        "Other Non-current Assets":                         "us-gaap:OtherAssetsNoncurrent",
        "Other Assets":                                     "us-gaap:OtherAssetsNoncurrent",

        # ── Balance Sheet — Liabilities ───────────────────────────────────────
        "Total Liabilities":                                "us-gaap:Liabilities",
        "Liabilities":                                      "us-gaap:Liabilities",
        # Current liabilities
        "Current Liabilities":                              "us-gaap:LiabilitiesCurrent",
        "Total Current Liabilities":                        "us-gaap:LiabilitiesCurrent",
        "Accounts Payable":                                 "us-gaap:AccountsPayableCurrent",
        "Trade Payables":                                   "us-gaap:AccountsPayableCurrent",
        "Accrued Liabilities":                              "us-gaap:AccruedLiabilitiesCurrent",
        "Accrued Expenses":                                 "us-gaap:AccruedLiabilitiesCurrent",
        "Short-term Debt":                                  "us-gaap:ShortTermBorrowings",
        "Current Portion of Long-term Debt":                "us-gaap:LongTermDebtCurrent",
        "Deferred Revenue Current":                         "us-gaap:DeferredRevenueCurrent",
        "Other Current Liabilities":                        "us-gaap:OtherLiabilitiesCurrent",
        # Non-current liabilities
        "Non-current Liabilities":                          "us-gaap:LiabilitiesNoncurrent",
        "Total Non-current Liabilities":                    "us-gaap:LiabilitiesNoncurrent",
        "Long-term Debt":                                   "us-gaap:LongTermDebtNoncurrent",
        "Long-term Debt Net":                               "us-gaap:LongTermDebtNoncurrent",
        "Deferred Tax Liabilities":                         "us-gaap:DeferredIncomeTaxLiabilitiesNet",
        "Other Non-current Liabilities":                    "us-gaap:OtherLiabilitiesNoncurrent",

        # ── Balance Sheet — Equity ────────────────────────────────────────────
        "Total Stockholders Equity":                        "us-gaap:StockholdersEquity",
        "Stockholders Equity":                              "us-gaap:StockholdersEquity",
        "Shareholders Equity":                              "us-gaap:StockholdersEquity",
        "Total Equity":                                     "us-gaap:StockholdersEquity",
        "Common Stock":                                     "us-gaap:CommonStockValue",
        "Additional Paid-in Capital":                       "us-gaap:AdditionalPaidInCapital",
        "Retained Earnings":                                "us-gaap:RetainedEarningsAccumulatedDeficit",
        "Accumulated Deficit":                              "us-gaap:RetainedEarningsAccumulatedDeficit",
        "Accumulated Other Comprehensive Income (Loss)":    "us-gaap:AccumulatedOtherComprehensiveIncomeLossNetOfTax",
        "AOCI":                                             "us-gaap:AccumulatedOtherComprehensiveIncomeLossNetOfTax",
        "Treasury Stock":                                   "us-gaap:TreasuryStockValue",
        "Total Liabilities and Stockholders Equity":        "us-gaap:LiabilitiesAndStockholdersEquity",
        "Total Liabilities and Equity":                     "us-gaap:LiabilitiesAndStockholdersEquity",

        # ── Cash Flow Statement ───────────────────────────────────────────────
        "Net Cash from Operating Activities":               "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "Net Cash Provided by Operating Activities":        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "Cash from Operations":                             "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "Operating Cash Flow":                              "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "Net Cash from Investing Activities":               "us-gaap:NetCashProvidedByUsedInInvestingActivities",
        "Net Cash Used in Investing Activities":            "us-gaap:NetCashProvidedByUsedInInvestingActivities",
        "Cash from Investing":                              "us-gaap:NetCashProvidedByUsedInInvestingActivities",
        "Capital Expenditures":                             "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "CapEx":                                            "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "Purchases of Property and Equipment":              "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "Net Cash from Financing Activities":               "us-gaap:NetCashProvidedByUsedInFinancingActivities",
        "Net Cash Used in Financing Activities":            "us-gaap:NetCashProvidedByUsedInFinancingActivities",
        "Cash from Financing":                              "us-gaap:NetCashProvidedByUsedInFinancingActivities",
        "Dividends Paid":                                   "us-gaap:PaymentsOfDividends",
        "Repurchase of Common Stock":                       "us-gaap:PaymentsForRepurchaseOfCommonStock",
        "Proceeds from Issuance of Debt":                   "us-gaap:ProceedsFromIssuanceOfLongTermDebt",
        "Repayment of Debt":                                "us-gaap:RepaymentsOfLongTermDebt",
        "Free Cash Flow":                                   "us-gaap:NetCashProvidedByUsedInOperatingActivities",  # non-GAAP → nearest
        "Net Change in Cash":                               "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "Net Increase (Decrease) in Cash":                  "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
    },

    # =========================================================================
    # IFRS  (IASB IFRS Foundation taxonomy — ifrs-full: namespace)
    # =========================================================================
    "IFRS": {
        # ── Income Statement (Statement of Profit or Loss) ────────────────────
        "Revenue":                                          "ifrs-full:Revenue",
        "Revenues":                                         "ifrs-full:Revenue",
        "Total Revenue":                                    "ifrs-full:Revenue",
        "Turnover":                                         "ifrs-full:Revenue",
        "Net Revenue":                                      "ifrs-full:Revenue",
        "Net Sales":                                        "ifrs-full:Revenue",
        "Sales":                                            "ifrs-full:Revenue",
        "Revenue from Contracts with Customers":            "ifrs-full:RevenueFromContractsWithCustomers",
        "Cost of Sales":                                    "ifrs-full:CostOfSales",
        "Cost of Goods Sold":                               "ifrs-full:CostOfSales",
        "Cost of Revenue":                                  "ifrs-full:CostOfSales",
        "Gross Profit":                                     "ifrs-full:GrossProfit",
        "Gross Margin":                                     "ifrs-full:GrossProfit",
        "Other Income":                                     "ifrs-full:OtherIncome",
        "Distribution Costs":                               "ifrs-full:DistributionCosts",
        "Selling and Distribution Expenses":                "ifrs-full:DistributionCosts",
        "Administrative Expenses":                          "ifrs-full:AdministrativeExpense",
        "General and Administrative Expenses":              "ifrs-full:AdministrativeExpense",
        "Research and Development":                         "ifrs-full:ResearchAndDevelopmentExpense",
        "Research and Development Expense":                 "ifrs-full:ResearchAndDevelopmentExpense",
        "R&D":                                              "ifrs-full:ResearchAndDevelopmentExpense",
        "Other Expenses":                                   "ifrs-full:OtherExpense",
        "Operating Profit":                                 "ifrs-full:ProfitLossFromOperatingActivities",
        "Operating Income":                                 "ifrs-full:ProfitLossFromOperatingActivities",
        "Profit from Operations":                           "ifrs-full:ProfitLossFromOperatingActivities",
        "EBIT":                                             "ifrs-full:ProfitLossFromOperatingActivities",
        "Finance Costs":                                    "ifrs-full:FinanceCosts",
        "Finance Charges":                                  "ifrs-full:FinanceCosts",
        "Interest Expense":                                 "ifrs-full:FinanceCosts",
        "Finance Income":                                   "ifrs-full:FinanceIncome",
        "Interest Income":                                  "ifrs-full:FinanceIncome",
        "Share of Profit of Associates":                    "ifrs-full:ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
        "Profit Before Tax":                                "ifrs-full:ProfitLossBeforeTax",
        "Income Before Tax":                                "ifrs-full:ProfitLossBeforeTax",
        "Pre-tax Income":                                   "ifrs-full:ProfitLossBeforeTax",
        "Profit Before Income Tax":                         "ifrs-full:ProfitLossBeforeTax",
        "EBT":                                              "ifrs-full:ProfitLossBeforeTax",
        "Income Tax Expense":                               "ifrs-full:IncomeTaxExpenseContinuingOperations",
        "Tax Expense":                                      "ifrs-full:IncomeTaxExpenseContinuingOperations",
        "Profit for the Period":                            "ifrs-full:ProfitLoss",
        "Net Profit":                                       "ifrs-full:ProfitLoss",
        "Net Income":                                       "ifrs-full:ProfitLoss",
        "Net Loss":                                         "ifrs-full:ProfitLoss",
        "Net Income (Loss)":                                "ifrs-full:ProfitLoss",
        "Total Comprehensive Income":                       "ifrs-full:ComprehensiveIncome",
        "Other Comprehensive Income":                       "ifrs-full:OtherComprehensiveIncome",
        "Basic EPS":                                        "ifrs-full:BasicEarningsLossPerShare",
        "Diluted EPS":                                      "ifrs-full:DilutedEarningsLossPerShare",
        "Basic Earnings Per Share":                         "ifrs-full:BasicEarningsLossPerShare",
        "Diluted Earnings Per Share":                       "ifrs-full:DilutedEarningsLossPerShare",
        "EBITDA":                                           "ifrs-full:ProfitLossBeforeTax",  # no IFRS standard tag; nearest

        # ── Balance Sheet (Statement of Financial Position) ───────────────────
        "Total Assets":                                     "ifrs-full:Assets",
        "Assets":                                           "ifrs-full:Assets",
        "Current Assets":                                   "ifrs-full:CurrentAssets",
        "Total Current Assets":                             "ifrs-full:CurrentAssets",
        "Cash and Cash Equivalents":                        "ifrs-full:CashAndCashEquivalents",
        "Cash":                                             "ifrs-full:CashAndCashEquivalents",
        "Trade Receivables":                                "ifrs-full:TradeAndOtherCurrentReceivables",
        "Accounts Receivable":                              "ifrs-full:TradeAndOtherCurrentReceivables",
        "Inventories":                                      "ifrs-full:Inventories",
        "Inventory":                                        "ifrs-full:Inventories",
        "Other Current Assets":                             "ifrs-full:OtherCurrentAssets",
        "Non-current Assets":                               "ifrs-full:NoncurrentAssets",
        "Total Non-current Assets":                         "ifrs-full:NoncurrentAssets",
        "Property Plant and Equipment":                     "ifrs-full:PropertyPlantAndEquipment",
        "PP&E":                                             "ifrs-full:PropertyPlantAndEquipment",
        "Right-of-use Assets":                              "ifrs-full:RightofuseAssets",
        "Goodwill":                                         "ifrs-full:Goodwill",
        "Intangible Assets":                                "ifrs-full:IntangibleAssetsOtherThanGoodwill",
        "Deferred Tax Assets":                              "ifrs-full:DeferredTaxAssets",
        "Other Non-current Assets":                         "ifrs-full:OtherNoncurrentAssets",
        "Total Liabilities":                                "ifrs-full:Liabilities",
        "Liabilities":                                      "ifrs-full:Liabilities",
        "Current Liabilities":                              "ifrs-full:CurrentLiabilities",
        "Total Current Liabilities":                        "ifrs-full:CurrentLiabilities",
        "Trade Payables":                                   "ifrs-full:TradeAndOtherCurrentPayables",
        "Accounts Payable":                                 "ifrs-full:TradeAndOtherCurrentPayables",
        "Short-term Borrowings":                            "ifrs-full:ShorttermBorrowings",
        "Current Portion of Long-term Borrowings":          "ifrs-full:CurrentPortionOfNoncurrentBorrowings",
        "Other Current Liabilities":                        "ifrs-full:OtherCurrentLiabilities",
        "Non-current Liabilities":                          "ifrs-full:NoncurrentLiabilities",
        "Total Non-current Liabilities":                    "ifrs-full:NoncurrentLiabilities",
        "Long-term Borrowings":                             "ifrs-full:NoncurrentPortionOfNoncurrentBorrowings",
        "Deferred Tax Liabilities":                         "ifrs-full:DeferredTaxLiabilities",
        "Provisions":                                       "ifrs-full:NoncurrentProvisions",
        "Other Non-current Liabilities":                    "ifrs-full:OtherNoncurrentLiabilities",
        "Equity":                                           "ifrs-full:Equity",
        "Total Equity":                                     "ifrs-full:Equity",
        "Share Capital":                                    "ifrs-full:IssuedCapital",
        "Issued Capital":                                   "ifrs-full:IssuedCapital",
        "Share Premium":                                    "ifrs-full:SharePremium",
        "Additional Paid-in Capital":                       "ifrs-full:SharePremium",
        "Retained Earnings":                                "ifrs-full:RetainedEarnings",
        "Accumulated Deficit":                              "ifrs-full:RetainedEarnings",
        "Other Reserves":                                   "ifrs-full:OtherReserves",
        "Total Liabilities and Equity":                     "ifrs-full:EquityAndLiabilities",

        # ── Cash Flow Statement ───────────────────────────────────────────────
        "Cash from Operating Activities":                   "ifrs-full:CashFlowsFromUsedInOperatingActivities",
        "Net Cash from Operating Activities":               "ifrs-full:CashFlowsFromUsedInOperatingActivities",
        "Operating Cash Flow":                              "ifrs-full:CashFlowsFromUsedInOperatingActivities",
        "Cash from Investing Activities":                   "ifrs-full:CashFlowsFromUsedInInvestingActivities",
        "Net Cash from Investing Activities":               "ifrs-full:CashFlowsFromUsedInInvestingActivities",
        "Purchase of Property Plant and Equipment":         "ifrs-full:PurchaseOfPropertyPlantAndEquipment",
        "Capital Expenditures":                             "ifrs-full:PurchaseOfPropertyPlantAndEquipment",
        "CapEx":                                            "ifrs-full:PurchaseOfPropertyPlantAndEquipment",
        "Cash from Financing Activities":                   "ifrs-full:CashFlowsFromUsedInFinancingActivities",
        "Net Cash from Financing Activities":               "ifrs-full:CashFlowsFromUsedInFinancingActivities",
        "Dividends Paid":                                   "ifrs-full:DividendsPaidClassifiedAsFinancingActivities",
        "Net Change in Cash":                               "ifrs-full:IncreaseDecreaseInCashAndCashEquivalents",
        "Net Increase (Decrease) in Cash":                  "ifrs-full:IncreaseDecreaseInCashAndCashEquivalents",
    },

    # =========================================================================
    # IND_AS  (Indian Accounting Standards — mirrors IFRS with MCA ind-as: prefix)
    # =========================================================================
    "IND_AS": {
        # ── Income Statement ─────────────────────────────────────────────────
        "Revenue from Operations":                          "ind-as:RevenueFromOperations",
        "Revenue":                                          "ind-as:Revenue",
        "Revenues":                                         "ind-as:Revenue",
        "Total Revenue":                                    "ind-as:Revenue",
        "Turnover":                                         "ind-as:Revenue",
        "Net Revenue":                                      "ind-as:Revenue",
        "Sales":                                            "ind-as:Revenue",
        "Net Sales":                                        "ind-as:Revenue",
        "Other Income":                                     "ind-as:OtherIncome",
        "Total Income":                                     "ind-as:TotalIncome",
        "Cost of Materials Consumed":                       "ind-as:CostOfMaterialsConsumed",
        "Cost of Goods Sold":                               "ind-as:CostOfGoodsSold",
        "Purchases of Stock-in-trade":                      "ind-as:PurchasesOfStockInTrade",
        "Change in Inventories":                            "ind-as:ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
        "Employee Benefits Expense":                        "ind-as:EmployeeBenefitsExpense",
        "Employee Costs":                                   "ind-as:EmployeeBenefitsExpense",
        "Finance Costs":                                    "ind-as:FinanceCosts",
        "Interest Expense":                                 "ind-as:FinanceCosts",
        "Depreciation and Amortization":                    "ind-as:DepreciationDepletionAndAmortisation",
        "Depreciation":                                     "ind-as:DepreciationOfPropertyPlantAndEquipment",
        "Amortization":                                     "ind-as:AmortisationOfIntangibleAssets",
        "Other Expenses":                                   "ind-as:OtherExpenses",
        "Total Expenses":                                   "ind-as:Expenses",
        "Profit Before Tax":                                "ind-as:ProfitBeforeTax",
        "Profit Before Exceptional Items and Tax":          "ind-as:ProfitBeforeExceptionalItemsAndTax",
        "Exceptional Items":                                "ind-as:ExceptionalItems",
        "Tax Expense":                                      "ind-as:TaxExpense",
        "Current Tax":                                      "ind-as:CurrentTax",
        "Deferred Tax":                                     "ind-as:DeferredTax",
        "Profit for the Period":                            "ind-as:ProfitLoss",
        "Profit for the Year":                              "ind-as:ProfitLoss",
        "Net Profit":                                       "ind-as:ProfitLoss",
        "Net Income":                                       "ind-as:ProfitLoss",
        "Net Loss":                                         "ind-as:ProfitLoss",
        "Other Comprehensive Income":                       "ind-as:OtherComprehensiveIncome",
        "Total Comprehensive Income":                       "ind-as:TotalComprehensiveIncome",
        "Basic EPS":                                        "ind-as:BasicEarningsPerShare",
        "Diluted EPS":                                      "ind-as:DilutedEarningsPerShare",
        "Basic Earnings Per Share":                         "ind-as:BasicEarningsPerShare",
        "Diluted Earnings Per Share":                       "ind-as:DilutedEarningsPerShare",
        "EBITDA":                                           "ind-as:ProfitBeforeTax",  # no standard tag; nearest

        # ── Balance Sheet ─────────────────────────────────────────────────────
        "Total Assets":                                     "ind-as:Assets",
        "Assets":                                           "ind-as:Assets",
        "Non-current Assets":                               "ind-as:NoncurrentAssets",
        "Total Non-current Assets":                         "ind-as:NoncurrentAssets",
        "Property Plant and Equipment":                     "ind-as:PropertyPlantAndEquipment",
        "PP&E":                                             "ind-as:PropertyPlantAndEquipment",
        "Capital Work-in-Progress":                         "ind-as:CapitalWorkInProgress",
        "Right-of-use Assets":                              "ind-as:RightofuseAssets",
        "Goodwill":                                         "ind-as:Goodwill",
        "Intangible Assets":                                "ind-as:IntangibleAssets",
        "Financial Assets Non-current":                     "ind-as:NoncurrentFinancialAssets",
        "Deferred Tax Assets":                              "ind-as:DeferredTaxAssets",
        "Other Non-current Assets":                         "ind-as:OtherNoncurrentAssets",
        "Current Assets":                                   "ind-as:CurrentAssets",
        "Total Current Assets":                             "ind-as:CurrentAssets",
        "Inventories":                                      "ind-as:Inventories",
        "Inventory":                                        "ind-as:Inventories",
        "Trade Receivables":                                "ind-as:TradeReceivables",
        "Accounts Receivable":                              "ind-as:TradeReceivables",
        "Cash and Cash Equivalents":                        "ind-as:CashAndCashEquivalents",
        "Cash":                                             "ind-as:CashAndCashEquivalents",
        "Bank Balances":                                    "ind-as:BalancesWithBanks",
        "Other Current Assets":                             "ind-as:OtherCurrentAssets",
        "Total Liabilities":                                "ind-as:Liabilities",
        "Liabilities":                                      "ind-as:Liabilities",
        "Equity":                                           "ind-as:Equity",
        "Total Equity":                                     "ind-as:Equity",
        "Share Capital":                                    "ind-as:ShareCapital",
        "Other Equity":                                     "ind-as:OtherEquity",
        "Reserves and Surplus":                             "ind-as:ReservesAndSurplus",
        "Non-current Liabilities":                          "ind-as:NoncurrentLiabilities",
        "Total Non-current Liabilities":                    "ind-as:NoncurrentLiabilities",
        "Long-term Borrowings":                             "ind-as:NoncurrentBorrowings",
        "Deferred Tax Liabilities":                         "ind-as:DeferredTaxLiabilities",
        "Other Non-current Liabilities":                    "ind-as:OtherNoncurrentLiabilities",
        "Current Liabilities":                              "ind-as:CurrentLiabilities",
        "Total Current Liabilities":                        "ind-as:CurrentLiabilities",
        "Short-term Borrowings":                            "ind-as:CurrentBorrowings",
        "Trade Payables":                                   "ind-as:TradePayables",
        "Accounts Payable":                                 "ind-as:TradePayables",
        "Other Current Liabilities":                        "ind-as:OtherCurrentLiabilities",
        "Total Equity and Liabilities":                     "ind-as:EquityAndLiabilities",

        # ── Cash Flow Statement ───────────────────────────────────────────────
        "Net Cash from Operating Activities":               "ind-as:CashFlowsFromUsedInOperatingActivities",
        "Cash from Operating Activities":                   "ind-as:CashFlowsFromUsedInOperatingActivities",
        "Operating Cash Flow":                              "ind-as:CashFlowsFromUsedInOperatingActivities",
        "Net Cash from Investing Activities":               "ind-as:CashFlowsFromUsedInInvestingActivities",
        "Cash from Investing Activities":                   "ind-as:CashFlowsFromUsedInInvestingActivities",
        "Capital Expenditures":                             "ind-as:PurchaseOfPropertyPlantAndEquipment",
        "CapEx":                                            "ind-as:PurchaseOfPropertyPlantAndEquipment",
        "Net Cash from Financing Activities":               "ind-as:CashFlowsFromUsedInFinancingActivities",
        "Cash from Financing Activities":                   "ind-as:CashFlowsFromUsedInFinancingActivities",
        "Dividends Paid":                                   "ind-as:DividendsPaid",
        "Net Change in Cash":                               "ind-as:IncreaseDecreaseInCashAndCashEquivalents",
    },
}


# ---------------------------------------------------------------------------
# Build the normalised lookup table at module import time
# ---------------------------------------------------------------------------
# Pre-normalising all keys means _normalise() is called once per alias at
# startup rather than once per query.  This keeps query-time cost to a single
# dict lookup after a short string normalisation.

def _build_alias_table(
    raw: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """
    Normalise all keys in *raw* and return a two-level dict ready for O(1)
    lookup.

    Args:
        raw: Framework → { raw_label → canonical_tag } mapping.

    Returns:
        Framework → { normalised_label → canonical_tag } mapping.
        Framework keys are upper-cased; label keys are normalised.

    Raises:
        ValueError: If the same normalised key maps to two different tags
                    within the same framework (collision detection at startup).
    """
    normalised: dict[str, dict[str, str]] = {}
    for framework, aliases in raw.items():
        fw_key = framework.upper()
        fw_map: dict[str, str] = {}
        for raw_label, canonical_tag in aliases.items():
            norm_key = _normalise(raw_label)
            if norm_key in fw_map and fw_map[norm_key] != canonical_tag:
                raise ValueError(
                    f"Alias collision in framework {fw_key!r}: "
                    f"normalised key {norm_key!r} maps to both "
                    f"{fw_map[norm_key]!r} and {canonical_tag!r}.  "
                    f"De-duplicate the alias table."
                )
            fw_map[norm_key] = canonical_tag
        normalised[fw_key] = fw_map
    return normalised


# Module-level compiled table — built exactly once at import time.
_ALIAS_TABLE: Final[dict[str, dict[str, str]]] = _build_alias_table(_RAW_ALIAS_TABLE)


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------

def resolve_canonical_tag(framework: str, raw_label: str) -> str:
    """
    Resolve *raw_label* to a canonical XBRL taxonomy tag for *framework*.

    The resolver:
      1. Normalises both *framework* (upper-case) and *raw_label* (see
         ``_normalise``) so that casing and minor punctuation differences are
         silently ignored.
      2. Looks up the normalised label in the framework's alias sub-dictionary.
      3. If found, returns the canonical tag (e.g. ``"us-gaap:Revenues"``).
      4. If not found, returns the safe fallback ``"raw:{slug}"`` where *slug*
         is the original *raw_label* with non-alphanumeric characters replaced
         by underscores — consistent with the fallback identifier already
         produced by ``extractor.py`` for unmapped concepts.

    This function NEVER raises an exception.  An unknown label produces a
    ``raw:`` fallback that preserves the original text for downstream analysis
    and manual review.

    Args:
        framework: Reporting standard identifier — one of ``"US_GAAP"``,
                   ``"IFRS"``, or ``"IND_AS"``.  Case-insensitive.
        raw_label: Concept label as extracted from the source document or
                   provided by the AI extractor.

    Returns:
        Canonical XBRL tag string (e.g. ``"us-gaap:Revenues"``), or
        ``"raw:{slug}"`` if the label is not in the alias table.

    Examples:
        >>> resolve_canonical_tag("US_GAAP", "Total Revenue")
        'us-gaap:Revenues'
        >>> resolve_canonical_tag("IFRS", "Turnover")
        'ifrs-full:Revenue'
        >>> resolve_canonical_tag("IND_AS", "Revenue from Operations")
        'ind-as:RevenueFromOperations'
        >>> resolve_canonical_tag("US_GAAP", "Some Unknown Concept")
        'raw:Some_Unknown_Concept'
        >>> resolve_canonical_tag("US_GAAP", "  total  revenue  ")
        'us-gaap:Revenues'
    """
    fw_key = framework.upper()
    norm_label = _normalise(raw_label)

    fw_map = _ALIAS_TABLE.get(fw_key)
    if fw_map is not None:
        canonical = fw_map.get(norm_label)
        if canonical is not None:
            return canonical

    # Safe fallback: preserve original text as a raw identifier.
    return f"raw:{_slug(raw_label)}"


# ---------------------------------------------------------------------------
# Introspection helpers (useful for tests and admin tooling)
# ---------------------------------------------------------------------------

def supported_frameworks() -> list[str]:
    """
    Return the list of framework keys present in the alias table.

    Example:
        >>> supported_frameworks()
        ['US_GAAP', 'IFRS', 'IND_AS']
    """
    return sorted(_ALIAS_TABLE.keys())


def alias_count(framework: str) -> int:
    """
    Return the number of alias entries registered for *framework*.

    Returns 0 for unknown frameworks rather than raising.

    Example:
        >>> alias_count("US_GAAP")
        87
    """
    return len(_ALIAS_TABLE.get(framework.upper(), {}))
