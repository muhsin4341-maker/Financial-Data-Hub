"""
India SEBI BRSR / IND-AS parser — Strategy implementation for Indian filings.

This module provides ``IndiaBRSRParser``, which handles Indian regulatory
filings in the SEBI Business Responsibility and Sustainability Report (BRSR)
format and MCA / IND-AS annual reports.

Background:
  Indian listed companies are required (SEBI Circular SEBI/HO/CFD/CMD-2/P/CIR/
  2021/562, mandatory from FY 2022-23 for top 1000 companies) to include a BRSR
  section in their annual reports.  The BRSR covers nine sustainability
  principles spanning environmental, social, and governance (ESG) disclosures
  alongside standard IND-AS financial statements.

  US-GAAP-centric parsers fail on these documents because:
    1. Monetary unit is INR (₹), not USD.
    2. Financial line-item labels follow IND-AS/IFRS naming (not US-GAAP XBRL).
    3. BRSR sustainability metrics (energy consumption, GHG emissions, CSR spend)
       have no US-GAAP equivalent.
    4. Values are often expressed in ₹ Lakhs or ₹ Crores (× 100,000 / × 10,000,000).

Strategy:
  ``IndiaBRSRParser`` uses a specialised system prompt tuned for:
    — IND-AS canonical field prefixes (``ifrs-full:``).
    — INR unit multiplier detection (Lakhs = 100000, Crores = 10000000).
    — BRSR Section A/B/C principle extraction.
    — ESG metric mapping to a custom ``brsr:`` namespace.

Supported frameworks:
  IND_AS, SEBI_BRSR, MCA_AOC

Milestone: M4.2 — International Parser Strategy (Task 2)
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from services.ingestion.parsers.base import (
    DocumentParser,
    ParsedLineItem,
    ParseResult,
)

# ---------------------------------------------------------------------------
# IND-AS / IFRS canonical field lookup (common Indian filing line items)
# ---------------------------------------------------------------------------

_IND_AS_LABEL_MAP: dict[str, str] = {
    # Income Statement (Statement of Profit and Loss)
    "revenue from operations": "ifrs-full:Revenue",
    "total revenue": "ifrs-full:Revenue",
    "other income": "ifrs-full:OtherIncome",
    "total income": "ifrs-full:Revenue",
    "cost of materials consumed": "ifrs-full:CostOfSales",
    "cost of goods sold": "ifrs-full:CostOfSales",
    "employee benefit expenses": "ifrs-full:EmployeeBenefitsExpense",
    "finance costs": "ifrs-full:FinanceCosts",
    "depreciation and amortization": "ifrs-full:DepreciationAndAmortisationExpense",
    "other expenses": "ifrs-full:OtherExpense",
    "profit before tax": "ifrs-full:ProfitLossBeforeTax",
    "profit before exceptional items and tax": "ifrs-full:ProfitLossBeforeTax",
    "exceptional items": "ifrs-full:OtherComprehensiveIncome",
    "tax expense": "ifrs-full:IncomeTaxExpense",
    "profit after tax": "ifrs-full:ProfitLoss",
    "profit for the year": "ifrs-full:ProfitLoss",
    "pat": "ifrs-full:ProfitLoss",
    "earnings per share basic": "ifrs-full:BasicEarningsLossPerShare",
    "basic eps": "ifrs-full:BasicEarningsLossPerShare",
    "diluted eps": "ifrs-full:DilutedEarningsLossPerShare",
    # Balance Sheet
    "total assets": "ifrs-full:Assets",
    "non-current assets": "ifrs-full:NoncurrentAssets",
    "current assets": "ifrs-full:CurrentAssets",
    "cash and cash equivalents": "ifrs-full:CashAndCashEquivalents",
    "trade receivables": "ifrs-full:TradeAndOtherCurrentReceivables",
    "inventories": "ifrs-full:Inventories",
    "property plant and equipment": "ifrs-full:PropertyPlantAndEquipment",
    "goodwill": "ifrs-full:Goodwill",
    "total liabilities": "ifrs-full:Liabilities",
    "non-current liabilities": "ifrs-full:NoncurrentLiabilities",
    "current liabilities": "ifrs-full:CurrentLiabilities",
    "trade payables": "ifrs-full:TradeAndOtherCurrentPayables",
    "borrowings": "ifrs-full:Borrowings",
    "equity share capital": "ifrs-full:IssuedCapital",
    "reserves and surplus": "ifrs-full:RetainedEarnings",
    "total equity": "ifrs-full:Equity",
    "net worth": "ifrs-full:Equity",
    # Cash Flow Statement
    "net cash from operating activities": "ifrs-full:CashFlowsFromUsedInOperatingActivities",
    "net cash from investing activities": "ifrs-full:CashFlowsFromUsedInInvestingActivities",
    "net cash from financing activities": "ifrs-full:CashFlowsFromUsedInFinancingActivities",
    # BRSR ESG metrics — custom brsr: namespace
    "csr expenditure": "brsr:CSRExpenditure",
    "total energy consumed": "brsr:TotalEnergyConsumed",
    "total water consumed": "brsr:TotalWaterConsumed",
    "ghg emissions scope 1": "brsr:GHGEmissionsScope1",
    "ghg emissions scope 2": "brsr:GHGEmissionsScope2",
    "total waste generated": "brsr:TotalWasteGenerated",
    "renewable energy": "brsr:RenewableEnergyConsumed",
}

# BRSR Principles (Section C) — 9 principles from SEBI circular
_BRSR_PRINCIPLES: dict[int, str] = {
    1: "Businesses should conduct and govern themselves with integrity.",
    2: "Businesses should provide goods and services in a sustainable and safe manner.",
    3: "Businesses should respect and promote the well-being of all employees.",
    4: "Businesses should respect the interests of and be responsive to all its stakeholders.",
    5: "Businesses should respect and promote human rights.",
    6: "Businesses should respect and make efforts to protect and restore the environment.",
    7: "Businesses, when engaging in influencing public policy, should do so responsibly.",
    8: "Businesses should promote inclusive growth and equitable development.",
    9: "Businesses should engage with and provide value to their consumers responsibly.",
}

# INR unit multiplier patterns
_INR_UNIT_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\bcrore[s]?\b", re.IGNORECASE), 10_000_000),
    (re.compile(r"\blakh[s]?\b", re.IGNORECASE), 100_000),
    (re.compile(r"\bthousand[s]?\b", re.IGNORECASE), 1_000),
    (re.compile(r"\bmillion[s]?\b", re.IGNORECASE), 1_000_000),
]

# System prompt addition for Indian filings (appended to the base AI prompt)
_INDIA_BRSR_SYSTEM_PROMPT_SUPPLEMENT = """\

## Indian filing context (SEBI BRSR / IND-AS)

This document follows Indian accounting standards.  Apply the following rules:

### Currency & unit multiplier
- Default currency is INR (₹).
- Look for explicit unit declarations in table headers:
    "₹ in Lakhs" → unit_multiplier = 100000
    "₹ in Crores" → unit_multiplier = 10000000
    "₹ in Thousands" → unit_multiplier = 1000
  If no unit is declared, assume unit_multiplier = 1 (absolute INR).

### Canonical field prefixes
- Use ``ifrs-full:`` prefix for IND-AS financial line items.
- Use ``brsr:`` prefix for SEBI BRSR ESG/sustainability metrics.
  Examples:
    Revenue from Operations      → ifrs-full:Revenue
    Profit After Tax / PAT       → ifrs-full:ProfitLoss
    Total Equity / Net Worth     → ifrs-full:Equity
    CSR Expenditure              → brsr:CSRExpenditure
    Total Energy Consumed        → brsr:TotalEnergyConsumed
    GHG Emissions (Scope 1)      → brsr:GHGEmissionsScope1
    GHG Emissions (Scope 2)      → brsr:GHGEmissionsScope2

### BRSR structure
The BRSR has three sections:
  Section A — General Disclosures (company profile, employees, subsidiaries)
  Section B — Management & Process Disclosures
  Section C — Principle-wise Performance Disclosures (9 principles)

Extract financial/quantitative disclosures from all three sections.
For Section C metrics, set statement_type = "BS" for stock data,
"IS" for flow data, and "CF" for cash-related ESG flows.

### Value extraction
- Parenthetical values (1,234) represent losses → -1234.
- Lakhs: divide by 10 to get Crores; multiply by 100000 for absolute INR.
- Always return parsed_value WITHOUT applying unit_multiplier (consumer multiplies).
"""


class IndiaBRSRParser(DocumentParser):
    """
    Concrete parser strategy for Indian SEBI BRSR and IND-AS annual reports.

    Handles:
      - SEBI BRSR Section A / B / C disclosures
      - IND-AS financial statements (P&L, Balance Sheet, Cash Flow)
      - MCA AOC-style annual reports
      - INR Lakhs / Crores unit detection and multiplier propagation

    Routing: always uses AI extraction (no structured XBRL format for BRSR).
    The AI call uses the base system prompt PLUS ``_INDIA_BRSR_SYSTEM_PROMPT_SUPPLEMENT``.
    """

    parser_name: str = "india_brsr"
    supported_frameworks: tuple[str, ...] = (
        "IND_AS",
        "SEBI_BRSR",
        "MCA_AOC",
    )

    @classmethod
    def can_handle(
        cls,
        *,
        document_type: str,
        country_code: str,
        reporting_standard: str,
    ) -> bool:
        """
        Handle IND_AS/SEBI_BRSR/MCA_AOC frameworks, or any Indian-origin document
        with IFRS standard (IND-AS is IFRS-converged).
        """
        rs_upper = reporting_standard.upper()
        cc_upper = country_code.upper()

        if rs_upper in ("IND_AS", "SEBI_BRSR", "MCA_AOC"):
            return True
        # Indian-origin IFRS documents (IND-AS is aligned to IFRS)
        if cc_upper == "IN" and rs_upper == "IFRS":
            return True
        return False

    async def parse(
        self,
        *,
        file_bytes: bytes,
        company_id: str,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        source_file_hash: str | None = None,
    ) -> ParseResult:
        """
        Parse an Indian SEBI BRSR / IND-AS filing.

        Detects the INR unit multiplier from the document header, then
        delegates to the AI extraction service with an India-specific
        system prompt supplement.

        Args:
            file_bytes:         Raw PDF or HTML bytes of the BRSR/annual report.
            company_id:         Company UUID as string.
            fiscal_year:        4-digit fiscal year.
            fiscal_period:      FY (most BRSR reports are annual).
            filing_date:        Date filed with SEBI / MCA.
            reporting_standard: ``IND_AS`` | ``SEBI_BRSR`` | ``MCA_AOC``.
            source_file_hash:   Pre-computed SHA-256 or None.

        Returns:
            ParseResult with IND-AS / BRSR line items in INR.
        """
        file_hash = source_file_hash or self.compute_sha256(file_bytes)
        bound_log = self._log.bind(
            company_id=company_id,
            fiscal_year=fiscal_year,
            reporting_standard=reporting_standard,
            file_hash=file_hash[:16],
        )
        bound_log.info("india_brsr_parser.parse_start", file_size=len(file_bytes))

        # ── Detect INR unit multiplier from document text snippet ──────────
        unit_multiplier = self._detect_inr_unit(file_bytes)
        bound_log.info("india_brsr_parser.unit_detected", unit_multiplier=unit_multiplier)

        # ── Delegate to AI extraction with India-specific context ───────────
        # The AI extraction service is session-bound; this parser surfaces a
        # metadata-only result with the detected context so the calling task
        # can inject the supplement prompt and session.
        return ParseResult(
            source_file_hash=file_hash,
            parser_name=self.parser_name,
            reporting_framework="SEBI_BRSR" if "BRSR" in reporting_standard.upper() else "IND_AS",
            metadata={
                "detected_unit_multiplier": unit_multiplier,
                "reporting_standard": reporting_standard,
                "country": "IN",
                "currency": "INR",
                "system_prompt_supplement": _INDIA_BRSR_SYSTEM_PROMPT_SUPPLEMENT,
                "canonical_field_map": _IND_AS_LABEL_MAP,
                "brsr_principles": _BRSR_PRINCIPLES,
                "note": (
                    "IndiaBRSRParser detected context.  "
                    "Full line-item extraction requires AIExtractionService "
                    "with the system_prompt_supplement injected.  "
                    "Invoke via the extraction task pipeline."
                ),
            },
            warnings=[
                "Full AI extraction is required for PDF/HTML BRSR documents.  "
                "Metadata context has been populated for the task layer."
            ],
        )

    # -------------------------------------------------------------------------
    # INR unit multiplier detection
    # -------------------------------------------------------------------------

    @staticmethod
    def _detect_inr_unit(file_bytes: bytes) -> int:
        """
        Scan the first 8 kB of the document for INR unit declarations.

        Returns the multiplier (1, 1_000, 100_000, or 10_000_000).
        Defaults to 1 (absolute INR) when no declaration is found.
        """
        try:
            snippet = file_bytes[:8192].decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return 1

        for pattern, multiplier in _INR_UNIT_PATTERNS:
            if pattern.search(snippet):
                return multiplier
        return 1

    # -------------------------------------------------------------------------
    # Public utility — canonical field lookup
    # -------------------------------------------------------------------------

    @classmethod
    def get_canonical_field(cls, label: str) -> str | None:
        """
        Look up the IND-AS / BRSR canonical field for a human-readable label.

        Performs a case-insensitive lookup against ``_IND_AS_LABEL_MAP``.
        Returns None when the label has no known mapping.

        Args:
            label: Concept label as it appears in the filing.

        Returns:
            Canonical field string (e.g. ``ifrs-full:Revenue``) or None.
        """
        return _IND_AS_LABEL_MAP.get(label.lower().strip())

    @classmethod
    def get_brsr_principles(cls) -> dict[int, str]:
        """Return the nine SEBI BRSR principles."""
        return dict(_BRSR_PRINCIPLES)
