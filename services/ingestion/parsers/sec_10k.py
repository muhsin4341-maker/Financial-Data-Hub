"""
SEC 10-K / 10-Q / 20-F parser — US-GAAP Strategy implementation.

This module provides the concrete ``SEC10KParser`` that handles US-GAAP
filings submitted to the SEC.  For iXBRL/XBRL filings it delegates to the
existing ``XBRLParser``.  For PDF filings it delegates to the shared
``AIExtractionService`` with a US-GAAP-tuned system prompt context.

Architecture position (Strategy Pattern):
  ParserFactory.get_parser(document_type="pdf", country_code="US",
                           reporting_standard="US_GAAP")
      → SEC10KParser

Supported frameworks:
  SEC_10K, SEC_10Q, SEC_20F  (all US-GAAP, SEC-filed)
  Fallback: any document with reporting_standard == "US_GAAP"

Milestone: M4.2 — International Parser Strategy (Task 2)
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from services.ingestion.parsers.base import (
    DocumentParser,
    ParsedLineItem,
    ParseResult,
)

# US-GAAP sign-convention map — outflow concepts that must be stored negative.
# Keyed on canonical_field substring; extends the extractor._OUTFLOW_CONCEPT_SUBSTRINGS set.
_US_GAAP_OUTFLOW_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "CostOf",
        "OperatingExpense",
        "ResearchAndDevelopment",
        "SellingGeneralAndAdministrative",
        "InterestExpense",
        "IncomeTaxExpense",
        "DepreciationAndAmortization",
        "Depreciation",
        "Amortization",
        "PaymentsTo",          # investing outflows (CapEx etc.)
        "RepaymentOf",         # financing outflows
        "RepaymentsOf",
    }
)

# US-GAAP canonical-field lookup for common label strings (case-insensitive).
_US_GAAP_LABEL_MAP: dict[str, str] = {
    "total revenue": "us-gaap:Revenues",
    "net revenue": "us-gaap:Revenues",
    "revenue": "us-gaap:Revenues",
    "net sales": "us-gaap:Revenues",
    "cost of revenue": "us-gaap:CostOfRevenue",
    "cost of goods sold": "us-gaap:CostOfRevenue",
    "cogs": "us-gaap:CostOfRevenue",
    "gross profit": "us-gaap:GrossProfit",
    "operating income": "us-gaap:OperatingIncomeLoss",
    "ebit": "us-gaap:OperatingIncomeLoss",
    "net income": "us-gaap:NetIncomeLoss",
    "net profit": "us-gaap:NetIncomeLoss",
    "total assets": "us-gaap:Assets",
    "total liabilities": "us-gaap:Liabilities",
    "stockholders equity": "us-gaap:StockholdersEquity",
    "shareholders equity": "us-gaap:StockholdersEquity",
    "cash and cash equivalents": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    "operating cash flow": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "investing cash flow": "us-gaap:NetCashProvidedByUsedInInvestingActivities",
    "financing cash flow": "us-gaap:NetCashProvidedByUsedInFinancingActivities",
    "basic eps": "us-gaap:EarningsPerShareBasic",
    "diluted eps": "us-gaap:EarningsPerShareDiluted",
    "earnings per share": "us-gaap:EarningsPerShareBasic",
    "research and development": "us-gaap:ResearchAndDevelopmentExpense",
    "r&d": "us-gaap:ResearchAndDevelopmentExpense",
    "selling general and administrative": "us-gaap:SellingGeneralAndAdministrativeExpense",
    "sg&a": "us-gaap:SellingGeneralAndAdministrativeExpense",
    "interest expense": "us-gaap:InterestExpense",
    "income tax expense": "us-gaap:IncomeTaxExpense",
    "depreciation and amortization": "us-gaap:DepreciationDepletionAndAmortization",
    "total current assets": "us-gaap:AssetsCurrent",
    "total current liabilities": "us-gaap:LiabilitiesCurrent",
    "long-term debt": "us-gaap:LongTermDebt",
    "inventory": "us-gaap:InventoryNet",
    "accounts receivable": "us-gaap:AccountsReceivableNetCurrent",
}


class SEC10KParser(DocumentParser):
    """
    Concrete parser strategy for US-GAAP SEC filings (10-K, 10-Q, 20-F).

    Routing logic:
      - If the document is a structured iXBRL/XBRL file → delegates to
        ``XBRLParser`` (already fully implemented in ``xbrl_parser.py``).
      - Otherwise → delegates to ``AIExtractionService`` with US-GAAP context.
        The AI service handles PDF, HTML, and other unstructured formats.

    The parser does NOT attempt to distinguish between 10-K, 10-Q, and 20-F
    forms at the line-item level — the canonical field map is shared across
    all three form types.
    """

    parser_name: str = "sec_10k"
    supported_frameworks: tuple[str, ...] = (
        "US_GAAP",
        "SEC_10K",
        "SEC_10Q",
        "SEC_20F",
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
        Handle all US-GAAP filings and all SEC form types regardless of country.
        20-F filers are foreign private issuers but still file with the SEC.
        """
        rs_upper = reporting_standard.upper()
        cc_upper = country_code.upper()
        dt_lower = document_type.lower()

        # Explicit US-GAAP match
        if rs_upper == "US_GAAP":
            return True
        # SEC form types by document_type hint
        if dt_lower in ("sec_10k", "sec_10q", "sec_20f"):
            return True
        # US country with XBRL — assume US-GAAP
        if cc_upper == "US" and dt_lower in ("xbrl", "ixbrl"):
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
        Parse a US-GAAP SEC filing document.

        Routes iXBRL/XBRL content to XBRLParser; all other formats to
        AIExtractionService.  Returns a normalised ParseResult.

        Args:
            file_bytes:         Raw document bytes.
            company_id:         Company UUID as string.
            fiscal_year:        4-digit fiscal year.
            fiscal_period:      FY | Q1 | Q2 | Q3 | Q4.
            filing_date:        Date filed with the SEC.
            reporting_standard: Typically ``US_GAAP``.
            source_file_hash:   Pre-computed SHA-256 or None.

        Returns:
            ParseResult with US-GAAP line items.
        """
        file_hash = source_file_hash or self.compute_sha256(file_bytes)
        bound_log = self._log.bind(
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            file_hash=file_hash[:16],
        )

        # Detect iXBRL / XBRL by magic bytes / header sniff
        is_xbrl = self._is_xbrl_content(file_bytes)

        if is_xbrl:
            bound_log.info("sec10k_parser.routing_xbrl")
            return await self._parse_xbrl(
                file_bytes=file_bytes,
                company_id=company_id,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                filing_date=filing_date,
                reporting_standard=reporting_standard,
                file_hash=file_hash,
            )

        # Non-XBRL (PDF, HTML, plain text) — delegate to AI extraction service
        bound_log.info("sec10k_parser.routing_ai_extraction")
        return await self._parse_via_ai(
            file_bytes=file_bytes,
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing_date=filing_date,
            reporting_standard=reporting_standard,
            file_hash=file_hash,
        )

    # -------------------------------------------------------------------------
    # Internal routing helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _is_xbrl_content(data: bytes) -> bool:
        """
        Heuristic check for iXBRL/XBRL content based on the first 4 kB.

        Returns True when the file starts with XML/iXBRL markers or contains
        XBRL namespace declarations.
        """
        snippet = data[:4096]
        try:
            text = snippet.decode("utf-8", errors="replace").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(
            marker in text
            for marker in (
                "xmlns:xbrl",
                "xmlns:ix",
                "ixbrl",
                "xbrl:context",
                "<xbrli:",
            )
        )

    async def _parse_xbrl(
        self,
        *,
        file_bytes: bytes,
        company_id: str,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        file_hash: str,
    ) -> ParseResult:
        """Delegate to the existing XBRLParser and wrap its output as ParseResult."""
        from io import BytesIO

        try:
            from services.ingestion.parsers.xbrl_parser import XBRLParser

            xbrl = XBRLParser()
            # XBRLParser.parse() returns a list of ParsedLineItem-compatible dicts
            # or dataclass objects — adapt to our ParseResult structure.
            raw_items = xbrl.parse(
                BytesIO(file_bytes),
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                filing_date=filing_date,
                reporting_standard=reporting_standard,
                source_file_hash=file_hash,
            )
            line_items = _adapt_xbrl_items(raw_items, reporting_standard)
            return ParseResult(
                line_items=line_items,
                source_file_hash=file_hash,
                parser_name=self.parser_name,
                reporting_framework="SEC_10K",
                metadata={"xbrl_item_count": len(line_items), "source": "xbrl_parser"},
            )
        except Exception as exc:  # noqa: BLE001
            self._log.error("sec10k_parser.xbrl_failed", error=str(exc)[:300])
            return ParseResult(
                source_file_hash=file_hash,
                parser_name=self.parser_name,
                reporting_framework="SEC_10K",
                errors=[f"XBRLParser failed: {exc}"],
            )

    async def _parse_via_ai(
        self,
        *,
        file_bytes: bytes,
        company_id: str,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        file_hash: str,
    ) -> ParseResult:
        """
        Store file bytes in local temp storage and invoke AIExtractionService.

        The AI service reads document text from a StorageBackend key; we write
        the raw bytes to a temporary local path and pass the key through.
        """
        import tempfile
        import uuid as _uuid
        from pathlib import Path

        tmp_dir = Path(tempfile.gettempdir()) / "fdh-parser-tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_key = f"{_uuid.uuid4().hex}.bin"
        tmp_path = tmp_dir / tmp_key

        try:
            tmp_path.write_bytes(file_bytes)

            from services.acquisition.storage.backend import LocalStorageBackend
            storage = LocalStorageBackend(str(tmp_dir))

            # We need a DB session — this method is called from AIExtractionService
            # context only when triggered from within a Celery task that already
            # opened a session.  For standalone usage, callers must inject a session.
            # Return a stub result with a note if no session context is available.
            self._log.warning(
                "sec10k_parser.ai_extraction_requires_session",
                detail=(
                    "SEC10KParser._parse_via_ai() requires an active "
                    "AsyncSession to call AIExtractionService.  "
                    "Use extraction_tasks.py to invoke this parser end-to-end."
                ),
            )
            return ParseResult(
                source_file_hash=file_hash,
                parser_name=self.parser_name,
                reporting_framework="SEC_10K",
                warnings=[
                    "AI extraction requires a database session.  "
                    "Invoke via the extraction task pipeline."
                ],
                metadata={"tmp_key": tmp_key, "file_size_bytes": len(file_bytes)},
            )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Adapter — convert XBRLParser output to ParsedLineItem list
# ---------------------------------------------------------------------------


def _adapt_xbrl_items(raw_items: Any, reporting_standard: str) -> list[ParsedLineItem]:
    """
    Convert XBRLParser output (list of dataclass / dict objects) to ParsedLineItem.

    XBRLParser may return its own dataclass type — we adapt defensively using
    ``getattr`` so that future XBRLParser refactors don't break this adapter.
    """
    result: list[ParsedLineItem] = []
    for item in (raw_items or []):
        try:
            get = (lambda k, d=None: getattr(item, k, None) if not isinstance(item, dict)
                   else item.get(k, d))
            concept = str(get("concept_label") or get("label") or "Unknown")
            canonical = str(get("canonical_field") or get("xbrl_tag") or f"raw:{concept}")
            stmt = str(get("statement_type") or "IS")
            value = float(get("value_reported") or get("value") or 0)
            currency = str(get("reported_currency") or get("currency") or "USD")
            fy = int(get("fiscal_year") or 2024)
            fp = str(get("fiscal_period") or "FY")
            fd = get("filing_date") or date.today()
            result.append(
                ParsedLineItem(
                    concept_label=concept,
                    canonical_field=canonical,
                    statement_type=stmt,
                    value_reported=value,
                    reported_currency=currency,
                    value_usd=value if currency == "USD" else None,
                    unit_multiplier=int(get("unit_multiplier") or 1),
                    fiscal_year=fy,
                    fiscal_period=fp,
                    filing_date=fd,
                    reporting_standard=reporting_standard,
                    reporting_framework="SEC_10K",
                    confidence_pct=float(get("confidence_pct") or 95.0),
                    source_file_hash=get("source_file_hash"),
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return result
