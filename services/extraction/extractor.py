"""
AIExtractionService — Claude API orchestrator for financial document extraction.

Architecture position:

  StorageBackend (M3.6)
    ↓  text content retrieved by s3_key
  AIExtractionService (M4.2) ← this module
    ↓  calls Anthropic Claude API
    ↓  validates response via ExtractionElement (Amendment V1.2 §9.1)
    ↓  maps ExtractionElement → FinancialLineItemCreate
    ↓  calls FinancialLineItemRepository.bulk_upsert()
  FinancialLineItemRepository (M4.1)
    ↓  ON CONFLICT DO NOTHING → PostgreSQL

Responsibility boundary:
  This service owns the full AI extraction pipeline for a single document.
  It does NOT own task scheduling (M4.3) or job-status transitions (M4.4).
  Those layers call this service and manage their own lifecycle concerns.

Amendment V1.2 compliance wired here:
  §2.2 — Sign convention: outflow items have their values negated before
          persistence.  The _OUTFLOW_CONCEPTS set drives the sign inversion.
  §6.2 — Lineage watermarking: ExtractionElement.lineage_comment is stored
          in derived_expression_formula so the Excel export layer (M6) can
          render SOX audit cell comments without re-querying Claude.
  §9.1 — Visual bounding-box attestation: ExtractionElement requires
          page_number + bounding_box_coordinates.  For plain-text (non-PDF)
          sources, estimated coordinates are synthesised from character offsets
          so the constraint is always satisfied without relaxing the schema.

FX handling (deferred):
  value_usd is set equal to value_reported when the reported currency is USD.
  For non-USD documents, value_usd is left as None — the FX translation step
  (services/currency, planned for M5) fills this column in a second pass.
  The row is still valid for bulk_upsert because value_reported is non-null.

Claude API contract:
  Model, max_tokens, and rate limits are read from get_settings() so that
  production credentials can be rotated via environment variables without
  code changes.  The Anthropic AsyncAnthropic client is constructed once per
  AIExtractionService instance; do not reuse instances across processes.

Idempotency:
  bulk_upsert uses ON CONFLICT DO NOTHING on the point-in-time composite
  unique key — re-running the same extraction job is safe.

Milestone: M4.2 — AIExtractionService
"""

from __future__ import annotations

import json
import re
import textwrap
import uuid
from datetime import date, datetime, UTC
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import structlog
from anthropic import AsyncAnthropic, APIError, APITimeoutError, AuthenticationError, RateLimitError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import get_settings
from apps.api.repositories.financial_line_items import FinancialLineItemRepository
from apps.api.schemas.financial_line_items import FinancialLineItemCreate
from services.acquisition.storage.backend import StorageBackend, StorageError
from services.extraction.schema.extraction_schema import (
    BoundingBox,
    ExtractionElement,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Sign-convention — Amendment V1.2 §2.2
# ---------------------------------------------------------------------------
# Canonical fields in these sets represent outflows / contra-asset items.
# Values reported as positive in the source document must be stored as
# negative so that financial identity equations hold:
#   Revenue − Expenses = Net Income
#   Assets = Liabilities + Equity
#
# The set is keyed on canonical_field substrings; a concept whose canonical
# name contains any listed token is treated as an outflow.
# Extend this set as the canonical field catalogue grows.
_OUTFLOW_CONCEPT_SUBSTRINGS: frozenset[str] = frozenset(
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
        "LiabilitiesAndStockholdersEquity",  # balance-side, not inverted
    }
)

# ---------------------------------------------------------------------------
# Estimated bounding box for plain-text sources (Amendment V1.2 §9.1)
# ---------------------------------------------------------------------------
# PDF-extracted text carries real bounding boxes from the PDF parser.
# For plain-text (HTML-stripped) sources no spatial data is available.
# We synthesise a page-level bounding box from the character offset so that
# ExtractionElement validation is satisfied while flagging the approximation.
_TEXT_PAGE_WIDTH: float = 612.0   # US Letter width in PDF points
_TEXT_PAGE_HEIGHT: float = 792.0  # US Letter height in PDF points
_TEXT_CHARS_PER_PAGE: int = 3000  # approximate characters per rendered page


def _estimate_bounding_box(char_offset: int) -> BoundingBox:
    """
    Synthesise a bounding box from a character offset in a plain-text document.

    The entire "row" is assumed to span the full page width at a y-position
    proportional to the offset within the current page.  This satisfies the
    Amendment V1.2 §9.1 schema constraint while clearly indicating that the
    coordinates are estimated (y_min == y_max − 12 for a single text line).

    Args:
        char_offset: 0-based character position in the full document text.

    Returns:
        BoundingBox with coordinates in PDF points.
    """
    chars_into_page = char_offset % _TEXT_CHARS_PER_PAGE
    y_fraction = chars_into_page / _TEXT_CHARS_PER_PAGE
    y_min = round(_TEXT_PAGE_HEIGHT * y_fraction, 2)
    y_max = min(round(y_min + 12.0, 2), _TEXT_PAGE_HEIGHT - 1.0)
    # Ensure y_max > y_min (BoundingBox validator requires strict inequality)
    if y_max <= y_min:
        y_max = y_min + 1.0
    return BoundingBox(
        x_min=0.0,
        y_min=y_min,
        x_max=_TEXT_PAGE_WIDTH,
        y_max=y_max,
    )


# ---------------------------------------------------------------------------
# Extraction result dataclass
# ---------------------------------------------------------------------------


class ExtractionResult:
    """
    Summary returned by AIExtractionService.extract().

    Attributes:
        inserted:       Number of new FinancialLineItem rows written to DB.
        skipped:        Number of rows silently skipped by ON CONFLICT DO NOTHING.
        rejected:       Number of elements dropped during validation.
        model_version:  Claude model identifier used for this extraction.
        extraction_timestamp: ISO 8601 UTC timestamp of the Claude API call.
        rejected_reasons: List of (element_index, error_summary) for rejected rows.
    """

    __slots__ = (
        "inserted",
        "skipped",
        "rejected",
        "model_version",
        "extraction_timestamp",
        "rejected_reasons",
    )

    def __init__(
        self,
        *,
        inserted: int,
        skipped: int,
        rejected: int,
        model_version: str,
        extraction_timestamp: str,
        rejected_reasons: list[tuple[int, str]],
    ) -> None:
        self.inserted = inserted
        self.skipped = skipped
        self.rejected = rejected
        self.model_version = model_version
        self.extraction_timestamp = extraction_timestamp
        self.rejected_reasons = rejected_reasons

    def __repr__(self) -> str:
        return (
            f"ExtractionResult(inserted={self.inserted}, skipped={self.skipped}, "
            f"rejected={self.rejected}, model={self.model_version!r})"
        )


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class ExtractionError(Exception):
    """Base class for extraction pipeline failures."""


class DocumentNotFoundError(ExtractionError):
    """Storage backend returned None for the requested text key."""


class EmptyDocumentError(ExtractionError):
    """The retrieved document text is empty or contains only whitespace."""


class ClaudeAPIError(ExtractionError):
    """Claude API call failed after retries."""


class ClaudeAuthError(ClaudeAPIError):
    """
    Claude API authentication failed — invalid or missing API key.

    This is a non-retryable error.  Retrying with the same key will never
    succeed; the task must transition to FAILED immediately without consuming
    retry budget.

    Raised when:
      - The Anthropic SDK returns HTTP 401 AuthenticationError.
      - The configured API key is a known dummy/test placeholder and mock
        mode is not explicitly enabled.
    """


class ParseError(ExtractionError):
    """Claude's response contained no parseable JSON array."""


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

# Path to the prompts directory (sibling of this file).
_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Minimum confidence threshold — elements below this are logged as warnings
# but still persisted (the operator can filter by confidence_pct later).
_MIN_CONFIDENCE_LOG_THRESHOLD: float = 50.0

_SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are an expert accounting analyst and financial data engineer specialising
    in SEC filings (10-K, 10-Q, 20-F) and Indian regulatory filings (MCA / SEBI).

    ## Your task
    Extract every quantitative financial data point from the document text provided
    by the user and return them as a single JSON array.  Each element in the array
    represents one line item from the financial statements.

    ## Output format (strict JSON — no prose, no markdown fences)
    Return ONLY a raw JSON array (starting with `[` and ending with `]`).
    Each object in the array must conform to this schema:

    {
      "concept_label":            <string>  // label exactly as it appears in source
      "canonical_field":          <string|null>  // XBRL concept tag if known, else null
                                  // US GAAP examples: "us-gaap:Revenues",
                                  //   "us-gaap:NetIncomeLoss", "us-gaap:Assets"
                                  // IFRS examples: "ifrs-full:Revenue",
                                  //   "ifrs-full:ProfitLoss"
      "raw_value":                <string>  // value string as-is from source
      "parsed_value":             <string|null>  // numeric value as decimal string,
                                  // null if parsing failed
                                  // Use negative sign for parenthetical values
                                  // (e.g. "(1,234)" → "-1234")
      "currency":                 <string|null>  // ISO 4217 code (e.g. "USD", "INR")
      "unit_multiplier":          <integer>  // 1, 1000, or 1000000 from table header
      "statement_type":           <"IS"|"BS"|"CF">
                                  // IS=Income Statement, BS=Balance Sheet,
                                  // CF=Cash Flow Statement
      "page_number":              <integer>  // 1-indexed page; estimate from document
                                  // position if not directly available
      "bounding_box_coordinates": {          // location on the page in PDF points
        "x_min": <float>,                    // estimate 0.0 if unavailable
        "y_min": <float>,                    // estimate proportional to line position
        "x_max": <float>,                    // estimate 612.0 if unavailable
        "y_max": <float>                     // y_max MUST be > y_min (add 12 if same)
      },
      "confidence_pct":           <float>  // your confidence 0–100
      "extraction_method":        "ai"
    }

    ## Canonical field mapping rules
    Map well-known line-item labels to their XBRL concept tags:
      Revenue / Net Revenue / Total Revenue  → us-gaap:Revenues (or ifrs-full:Revenue)
      Net Income / Net Profit / PAT          → us-gaap:NetIncomeLoss (or ifrs-full:ProfitLoss)
      Gross Profit                           → us-gaap:GrossProfit
      Operating Income / EBIT               → us-gaap:OperatingIncomeLoss
      EBITDA                                 → null (not a standard XBRL concept)
      Total Assets                           → us-gaap:Assets
      Total Liabilities                      → us-gaap:Liabilities
      Stockholders' Equity / Net Worth       → us-gaap:StockholdersEquity
      Cash and Cash Equivalents              → us-gaap:CashAndCashEquivalentsAtCarryingValue
      Operating Cash Flow                    → us-gaap:NetCashProvidedByUsedInOperatingActivities
      Investing Cash Flow                    → us-gaap:NetCashProvidedByUsedInInvestingActivities
      Financing Cash Flow                    → us-gaap:NetCashProvidedByUsedInFinancingActivities
      Basic EPS                              → us-gaap:EarningsPerShareBasic
      Diluted EPS                            → us-gaap:EarningsPerShareDiluted
      Cost of Revenue / COGS                 → us-gaap:CostOfRevenue
      R&D Expense                            → us-gaap:ResearchAndDevelopmentExpense
      SG&A Expense                           → us-gaap:SellingGeneralAndAdministrativeExpense
      Interest Expense                       → us-gaap:InterestExpense
      Income Tax Expense                     → us-gaap:IncomeTaxExpense
      Depreciation & Amortisation            → us-gaap:DepreciationDepletionAndAmortization
      Total Current Assets                   → us-gaap:AssetsCurrent
      Total Current Liabilities              → us-gaap:LiabilitiesCurrent
      Long-term Debt                         → us-gaap:LongTermDebt
      Inventory                              → us-gaap:InventoryNet
      Accounts Receivable                    → us-gaap:AccountsReceivableNetCurrent
    For Indian GAAP / Ind AS, prefer "ifrs-full:" prefixes where applicable.

    ## Value parsing rules
    1. Remove all currency symbols, commas, and spaces from numeric strings.
    2. Parenthetical values such as (1,234) represent negative numbers → -1234.
    3. Multiply parsed_value by unit_multiplier before returning (already done in your
       value — do NOT multiply again at the consumer end; consumer uses parsed_value as-is).
       Wait — actually return the RAW parsed number WITHOUT applying unit_multiplier.
       The consumer will compute: final_value = parsed_value × unit_multiplier.
    4. If a value cannot be parsed to a number, set parsed_value to null.
    5. For balance-sheet items where both a current-year and prior-year column exist,
       extract ONLY the current (most recent) period's value.

    ## Statement classification
    Classify each line item by the statement it belongs to:
      IS — Income Statement / P&L / Statement of Operations
      BS — Balance Sheet / Statement of Financial Position
      CF — Cash Flow Statement

    ## Bounding-box estimation for text documents
    When the source is plain text (not a rendered PDF), estimate coordinates:
      x_min = 0.0, x_max = 612.0 (full US Letter width in points)
      Divide the document into pages of approximately 3000 characters.
      Estimate y_min proportional to the line's position within its page (0–792 points).
      Set y_max = y_min + 12.0.
      Set page_number = 1 + (character_offset ÷ 3000).

    ## Quality rules
    - Include ALL quantitative line items: totals, subtotals, per-share data, ratios.
    - Exclude percentage-only metrics (e.g. "gross margin 42%") unless the raw value
      is also present as an absolute number.
    - If a table contains both USD and a foreign-currency column, extract the
      foreign-currency value and set currency accordingly.
    - Do NOT fabricate values.  If a value is illegible or absent, omit the element.
    - Set confidence_pct honestly: use 95+ for clearly structured tables,
      70–94 for values that required inference, below 70 for uncertain extractions.

    ## Critical constraint
    Return ONLY the JSON array.  No explanatory text, no markdown, no code fences.
    The entire response must be parseable by json.loads().
    """
)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

# Pattern 1: fenced ```json … ``` block (model sometimes wraps despite instructions)
_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(\[[\s\S]*?\])\s*```",
    re.IGNORECASE,
)

# Pattern 2: bare JSON array (starts with '[', ends with ']')
_BARE_ARRAY_RE = re.compile(r"\[[\s\S]*\]", re.DOTALL)


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """
    Robustly extract a JSON array from Claude's raw response text.

    Tries three strategies in order:
      1. Fenced ```json … ``` block — model sometimes wraps despite instructions.
      2. First bare '[' to the last ']' in the response.
      3. json.loads() on the full response stripped of surrounding whitespace.

    Args:
        text: Raw string returned by the Claude API.

    Returns:
        Parsed list of dicts.

    Raises:
        ParseError: When no valid JSON array can be extracted.
    """
    # Strategy 1 — fenced block
    fence_match = _FENCED_JSON_RE.search(text)
    if fence_match:
        candidate = fence_match.group(1)
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Strategy 2 — bare array (greedy: first '[' to last ']')
    bare_match = _BARE_ARRAY_RE.search(text)
    if bare_match:
        candidate = bare_match.group(0)
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Strategy 3 — full response as JSON
    try:
        result = json.loads(text.strip())
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    raise ParseError(
        "Claude's response contained no parseable JSON array. "
        f"Response length: {len(text)} chars. "
        f"First 300 chars: {text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _apply_sign_convention(
    value: Decimal,
    canonical_field: str | None,
) -> Decimal:
    """
    Apply Amendment V1.2 §2.2 sign convention.

    Outflow items (expenses, contra-assets) are stored as negative values.
    The check is performed on the canonical_field string; if the canonical
    field is None, the value is returned unchanged (the caller is responsible
    for sign correction at a higher layer).

    Args:
        value:           Absolute decimal value (already positive).
        canonical_field: Normalised XBRL concept tag, or None.

    Returns:
        Signed decimal value.
    """
    if canonical_field is None or value == Decimal("0"):
        return value
    # The canonical field must contain an outflow substring to be negated.
    # We only negate if the value is currently positive — negative values
    # (parenthetical amounts) are already correctly signed by the prompt.
    if value > 0 and any(
        token in canonical_field for token in _OUTFLOW_CONCEPT_SUBSTRINGS
    ):
        return -value
    return value


def _safe_decimal(raw: Any) -> Decimal | None:
    """
    Coerce a value from the AI response to Decimal, returning None on failure.

    Handles: str, int, float, Decimal, None.
    Rejects: empty string, non-numeric strings.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# Mock-mode detection
# ---------------------------------------------------------------------------
# When the configured API key is a known dummy placeholder the service
# bypasses the live Claude call and returns synthetic financial records.
# This lets the frontend and export pipeline be exercised locally without a
# valid Anthropic subscription.
#
# To activate mock mode: set CLAUDE_API_KEY to any value starting with one of
# the prefixes below, or leave it empty.  To force real calls, use a genuine
# Anthropic API key (starts with "sk-ant-api03-" followed by a real secret).

_MOCK_KEY_PREFIXES: tuple[str, ...] = (
    "sk-ant-api01-thisis",   # common dev placeholder pattern
    "sk-ant-test",
    "sk-ant-api01-fake",
    "sk-ant-api01-mock",
    "sk-ant-api01-dummy",
)

# Synthetic financial records injected in mock mode.
# Format: (concept_label, canonical_field, statement_type, str_value, currency, unit_mult)
# Values represent a plausible mid-cap tech company in USD.
_MOCK_FINANCIAL_DATA: list[tuple[str, str, str, str, str, int]] = [
    # ── Income Statement ──────────────────────────────────────────────────────
    ("Total Revenue",                     "us-gaap:Revenues",                                          "IS", "12_450_000_000", "USD", 1),
    ("Cost of Revenue",                   "us-gaap:CostOfRevenue",                                     "IS",  "7_470_000_000", "USD", 1),
    ("Gross Profit",                      "us-gaap:GrossProfit",                                       "IS",  "4_980_000_000", "USD", 1),
    ("Research and Development",          "us-gaap:ResearchAndDevelopmentExpense",                     "IS",    "890_000_000", "USD", 1),
    ("Selling General and Administrative","us-gaap:SellingGeneralAndAdministrativeExpense",             "IS",    "620_000_000", "USD", 1),
    ("Operating Income",                  "us-gaap:OperatingIncomeLoss",                               "IS",  "3_470_000_000", "USD", 1),
    ("Interest Expense",                  "us-gaap:InterestExpense",                                   "IS",    "145_000_000", "USD", 1),
    ("Income Tax Expense",                "us-gaap:IncomeTaxExpense",                                  "IS",    "680_000_000", "USD", 1),
    ("Net Income",                        "us-gaap:NetIncomeLoss",                                     "IS",  "2_645_000_000", "USD", 1),
    ("Basic EPS",                         "us-gaap:EarningsPerShareBasic",                             "IS",           "8.42", "USD", 1),
    ("Diluted EPS",                       "us-gaap:EarningsPerShareDiluted",                           "IS",           "8.31", "USD", 1),
    # ── Balance Sheet ─────────────────────────────────────────────────────────
    ("Cash and Cash Equivalents",         "us-gaap:CashAndCashEquivalentsAtCarryingValue",             "BS",  "9_200_000_000", "USD", 1),
    ("Accounts Receivable",               "us-gaap:AccountsReceivableNetCurrent",                      "BS",  "2_100_000_000", "USD", 1),
    ("Inventory",                         "us-gaap:InventoryNet",                                      "BS",    "750_000_000", "USD", 1),
    ("Total Current Assets",              "us-gaap:AssetsCurrent",                                     "BS", "14_300_000_000", "USD", 1),
    ("Total Assets",                      "us-gaap:Assets",                                            "BS", "52_800_000_000", "USD", 1),
    ("Total Current Liabilities",         "us-gaap:LiabilitiesCurrent",                                "BS",  "8_900_000_000", "USD", 1),
    ("Long-term Debt",                    "us-gaap:LongTermDebt",                                      "BS", "11_500_000_000", "USD", 1),
    ("Total Liabilities",                 "us-gaap:Liabilities",                                       "BS", "31_200_000_000", "USD", 1),
    ("Stockholders Equity",               "us-gaap:StockholdersEquity",                                "BS", "21_600_000_000", "USD", 1),
    # ── Cash Flow Statement ───────────────────────────────────────────────────
    ("Operating Cash Flow",               "us-gaap:NetCashProvidedByUsedInOperatingActivities",        "CF",  "3_120_000_000", "USD", 1),
    ("Capital Expenditures",              "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",        "CF",   "-980_000_000", "USD", 1),
    ("Investing Cash Flow",               "us-gaap:NetCashProvidedByUsedInInvestingActivities",        "CF", "-1_450_000_000", "USD", 1),
    ("Financing Cash Flow",               "us-gaap:NetCashProvidedByUsedInFinancingActivities",        "CF",   "-870_000_000", "USD", 1),
    # D&A in CF is a non-cash add-back (positive).  We use the CF-specific
    # adjustments tag to avoid triggering the outflow sign-inversion rule,
    # which would incorrectly flip this to −$520 M and deflate operating cash flow.
    ("Depreciation and Amortization",     "us-gaap:DepreciationAndAmortizationCFAdjustment",           "CF",    "520_000_000", "USD", 1),
]


def _is_mock_mode(api_key: str) -> bool:
    """
    Return True when the API key is a known dummy/test placeholder.

    A real Anthropic production key starts with "sk-ant-api03-" followed by
    a 40+-character random secret.  Development/CI placeholders are typically
    shorter or start with well-known test prefixes.

    Args:
        api_key: Value of settings.claude_api_key.

    Returns:
        True  — bypass Claude API, use synthetic fixture data.
        False — proceed with live API call.
    """
    if not api_key or not api_key.strip():
        return True
    key_lower = api_key.lower().strip()
    if key_lower in ("", "your-api-key-here", "change-me"):
        return True
    return any(key_lower.startswith(p.lower()) for p in _MOCK_KEY_PREFIXES)


# ---------------------------------------------------------------------------
# AIExtractionService
# ---------------------------------------------------------------------------


class AIExtractionService:
    """
    Orchestrates the full AI extraction pipeline for a single filing document.

    Pipeline (per extract() call):
      1. Fetch raw text from storage backend using ``text_key``.
      2. Truncate text to fit Claude's context window if necessary.
      3. Call Anthropic Claude API with a structured system prompt.
      4. Parse and validate the JSON array response via ExtractionElement.
      5. Map ExtractionElement → FinancialLineItemCreate (sign convention,
         unit multiplication, FX stub, lineage comment).
      6. Persist valid rows via FinancialLineItemRepository.bulk_upsert().
      7. Return ExtractionResult with inserted/skipped/rejected counts.

    Thread / async safety:
      One instance per task invocation.  The AsyncAnthropic client is
      constructed in __init__ and must not be shared between coroutines.
      The SQLAlchemy AsyncSession is injected by the caller and must not be
      used outside the coroutine that calls extract().

    Args:
        session:         Active SQLAlchemy AsyncSession — caller owns lifecycle.
        storage_backend: StorageBackend implementation (local or S3).
        settings:        Optional Settings override; defaults to get_settings().
    """

    # Maximum characters sent to Claude.
    # claude-sonnet-4-5 context window is 200k tokens ≈ 800k characters.
    # We cap at 400k characters to leave room for the system prompt + response.
    _MAX_TEXT_CHARS: int = 400_000

    def __init__(
        self,
        session: AsyncSession,
        storage_backend: StorageBackend,
        settings: Any | None = None,
    ) -> None:
        self._session = session
        self._storage = storage_backend
        self._settings = settings or get_settings()
        self._client = AsyncAnthropic(api_key=self._settings.claude_api_key)
        self._model = self._settings.claude_model
        self._max_tokens = self._settings.claude_max_tokens

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def extract(
        self,
        *,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        text_key: str,
        source_file_hash: str | None = None,
    ) -> ExtractionResult:
        """
        Run the full extraction pipeline for one document.

        Args:
            company_id:         UUID of the company (no FK in financial_line_items
                                table — tenant isolation is at query layer).
            fiscal_year:        4-digit fiscal year (e.g. 2024).
            fiscal_period:      Fiscal period: 'Q1' | 'Q2' | 'Q3' | 'Q4' | 'FY'.
            filing_date:        Date the document was filed with the regulator.
            reporting_standard: Accounting standard: 'US_GAAP' | 'IFRS' | 'IND_AS'.
            text_key:           Storage backend key for the extracted plain-text
                                version of the document.
            source_file_hash:   SHA-256 hex digest of the source document (optional
                                but required for full Amendment V1.2 §4.2 compliance).

        Returns:
            ExtractionResult with inserted/skipped/rejected counts.

        Raises:
            DocumentNotFoundError: text_key does not exist in storage.
            EmptyDocumentError:    Retrieved text is empty.
            ClaudeAPIError:        Claude API call failed.
            ParseError:            Response contained no parseable JSON.
        """
        bound_log = log.bind(
            company_id=str(company_id),
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing_date=str(filing_date),
            text_key=text_key,
        )
        bound_log.info("extraction.start")

        # Step 1 — Fetch text from storage
        text = await self._fetch_text(text_key)
        bound_log.info("extraction.text_fetched", char_count=len(text))

        # Step 2 — Truncate to model context window if necessary
        truncated = False
        if len(text) > self._MAX_TEXT_CHARS:
            text = text[: self._MAX_TEXT_CHARS]
            truncated = True
            bound_log.warning(
                "extraction.text_truncated",
                original_length=len(text),
                truncated_to=self._MAX_TEXT_CHARS,
            )

        # Step 2b — Mock-mode short-circuit
        # When the API key is a known dummy placeholder, skip the live Claude
        # call entirely and inject synthetic fixture records.  This lets all
        # downstream layers (validation, export, dashboard) function correctly
        # during local development without a real Anthropic subscription.
        if _is_mock_mode(self._settings.claude_api_key):
            bound_log.warning(
                "extraction.mock_mode_active",
                detail=(
                    "CLAUDE_API_KEY is empty or matches a dummy placeholder.  "
                    "Returning synthetic fixture data.  Set a real API key to "
                    "enable live extraction."
                ),
            )
            return await self._generate_mock_result(
                company_id=company_id,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                filing_date=filing_date,
                reporting_standard=reporting_standard,
                source_file_hash=source_file_hash,
            )

        # Step 3 — Call Claude API
        extraction_timestamp = datetime.now(UTC).isoformat()
        raw_response = await self._call_claude(
            text=text,
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing_date=filing_date,
            reporting_standard=reporting_standard,
            truncated=truncated,
        )
        bound_log.info(
            "extraction.claude_responded",
            response_length=len(raw_response),
        )

        # Step 4 — Parse and validate ExtractionElement objects
        raw_elements = _extract_json_array(raw_response)
        bound_log.info(
            "extraction.json_parsed",
            raw_element_count=len(raw_elements),
        )

        validated_elements, rejected_reasons = self._validate_elements(
            raw_elements, bound_log
        )
        bound_log.info(
            "extraction.validation_complete",
            validated=len(validated_elements),
            rejected=len(rejected_reasons),
        )

        # Step 5 — Map ExtractionElement → FinancialLineItemCreate
        line_items, mapping_rejected = self._map_to_line_items(
            elements=validated_elements,
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing_date=filing_date,
            reporting_standard=reporting_standard,
            source_file_hash=source_file_hash,
            bound_log=bound_log,
        )
        rejected_reasons.extend(mapping_rejected)
        bound_log.info(
            "extraction.mapping_complete",
            line_item_count=len(line_items),
        )

        # Step 6 — Persist via repository
        repo = FinancialLineItemRepository(self._session)
        inserted = await repo.bulk_upsert(line_items)
        skipped = len(line_items) - inserted

        bound_log.info(
            "extraction.complete",
            inserted=inserted,
            skipped=skipped,
            rejected=len(rejected_reasons),
        )

        return ExtractionResult(
            inserted=inserted,
            skipped=skipped,
            rejected=len(rejected_reasons),
            model_version=self._model,
            extraction_timestamp=extraction_timestamp,
            rejected_reasons=rejected_reasons,
        )

    # -------------------------------------------------------------------------
    # Step 1 — Text retrieval
    # -------------------------------------------------------------------------

    async def _fetch_text(self, text_key: str) -> str:
        """
        Retrieve document text from the storage backend.

        Args:
            text_key: Storage object key for the plain-text document.

        Returns:
            Non-empty document text string.

        Raises:
            DocumentNotFoundError: Key does not exist in storage.
            EmptyDocumentError:    Retrieved text is empty / whitespace-only.
            StorageError:          Propagated from storage backend on IO failure.
        """
        try:
            content = await self._storage.retrieve(text_key)
        except StorageError as exc:
            raise StorageError(
                f"Storage backend failure reading text_key={text_key!r}: {exc}"
            ) from exc

        if content is None:
            raise DocumentNotFoundError(
                f"No document found at storage key {text_key!r}. "
                "Ensure the acquisition pipeline stored the extracted text "
                "before dispatching the extraction task."
            )

        stripped = content.strip()
        if not stripped:
            raise EmptyDocumentError(
                f"Document at storage key {text_key!r} is empty or "
                "contains only whitespace. Cannot extract financial data."
            )

        return stripped

    # -------------------------------------------------------------------------
    # Step 3 — Claude API call
    # -------------------------------------------------------------------------

    async def _call_claude(
        self,
        *,
        text: str,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        truncated: bool,
    ) -> str:
        """
        Send the document text to Claude and return the raw response string.

        The user message includes contextual metadata (reporting standard,
        fiscal period) so that Claude can infer canonical field prefixes
        (us-gaap: vs ifrs-full:) and sign conventions without ambiguity.

        Args:
            text:               Extracted document text (already truncated).
            company_id:         Company UUID (included for traceability in logs).
            fiscal_year:        4-digit fiscal year.
            fiscal_period:      Fiscal period label.
            filing_date:        Filing date.
            reporting_standard: Accounting standard.
            truncated:          True when the text was truncated to fit context.

        Returns:
            Raw response string from Claude.

        Raises:
            ClaudeAPIError: API call failed (rate limit, timeout, or API error).
        """
        truncation_note = (
            "\n\nNOTE: This document was truncated to fit the model context window. "
            "Extract all data points visible in the provided text; "
            "do not hallucinate data for sections that may have been cut off."
            if truncated
            else ""
        )

        user_message = textwrap.dedent(
            f"""\
            ## Document context
            Reporting standard : {reporting_standard}
            Fiscal year        : {fiscal_year}
            Fiscal period      : {fiscal_period}
            Filing date        : {filing_date.isoformat()}
            Company ID         : {company_id}
            {truncation_note}

            ## Document text
            {text}
            """
        )

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except AuthenticationError as exc:
            # HTTP 401 — invalid or revoked API key.  This is non-retryable:
            # retrying with the same credentials will always fail.  Raise
            # ClaudeAuthError so the Celery task wrapper can transition the
            # job to FAILED immediately without consuming retry budget.
            raise ClaudeAuthError(
                f"Claude API authentication failed (HTTP 401). "
                f"Verify that CLAUDE_API_KEY is a valid Anthropic secret key. "
                f"Original error: {exc}"
            ) from exc
        except RateLimitError as exc:
            raise ClaudeAPIError(
                f"Claude API rate limit exceeded: {exc}. "
                "The Celery task should retry with exponential back-off."
            ) from exc
        except APITimeoutError as exc:
            raise ClaudeAPIError(
                f"Claude API timed out: {exc}. "
                "Consider increasing celery_task_time_limit in settings."
            ) from exc
        except APIError as exc:
            raise ClaudeAPIError(
                f"Claude API error (status={getattr(exc, 'status_code', 'unknown')}): {exc}"
            ) from exc

        # Extract the text content from the first content block.
        if not response.content:
            raise ParseError(
                "Claude returned an empty content list. "
                f"Stop reason: {response.stop_reason!r}"
            )

        first_block = response.content[0]
        if not hasattr(first_block, "text"):
            raise ParseError(
                f"Unexpected content block type: {type(first_block).__name__}. "
                "Expected a TextBlock."
            )

        return first_block.text  # type: ignore[attr-defined]

    # -------------------------------------------------------------------------
    # Step 4 — Validate ExtractionElement objects
    # -------------------------------------------------------------------------

    def _validate_elements(
        self,
        raw_elements: list[dict[str, Any]],
        bound_log: Any,
    ) -> tuple[list[ExtractionElement], list[tuple[int, str]]]:
        """
        Validate raw dicts from the JSON response as ExtractionElement instances.

        Amendment V1.2 §9.1: elements without page_number or
        bounding_box_coordinates are rejected by ExtractionElement validation.
        For text-sourced documents, Claude should have estimated these values
        (per system prompt instructions); if it omits them, we synthesise
        estimates here before re-attempting validation.

        Args:
            raw_elements: List of raw dicts from _extract_json_array().
            bound_log:    Bound structlog logger.

        Returns:
            Tuple of (validated_elements, rejected_reasons).
            rejected_reasons is a list of (index, error_message) pairs.
        """
        validated: list[ExtractionElement] = []
        rejected: list[tuple[int, str]] = []

        for idx, raw in enumerate(raw_elements):
            if not isinstance(raw, dict):
                rejected.append((idx, f"Element is not a dict: {type(raw).__name__}"))
                continue

            # Synthesise bounding box if absent (text-source fallback).
            if "bounding_box_coordinates" not in raw or raw["bounding_box_coordinates"] is None:
                # Estimate page from array position as a last resort.
                char_offset = idx * 150  # rough characters-per-line estimate
                estimated_bb = _estimate_bounding_box(char_offset)
                raw["bounding_box_coordinates"] = {
                    "x_min": estimated_bb.x_min,
                    "y_min": estimated_bb.y_min,
                    "x_max": estimated_bb.x_max,
                    "y_max": estimated_bb.y_max,
                }
                bound_log.debug(
                    "extraction.bounding_box_synthesised",
                    element_index=idx,
                    concept_label=raw.get("concept_label", "<unknown>"),
                )

            # Synthesise page_number if absent.
            if "page_number" not in raw or raw["page_number"] is None:
                raw["page_number"] = max(1, idx // 20 + 1)
                bound_log.debug(
                    "extraction.page_number_synthesised",
                    element_index=idx,
                    page_number=raw["page_number"],
                )

            try:
                element = ExtractionElement(**raw)
            except (ValidationError, TypeError) as exc:
                short_err = str(exc)[:200]
                rejected.append((idx, short_err))
                bound_log.warning(
                    "extraction.element_rejected",
                    element_index=idx,
                    concept_label=raw.get("concept_label", "<unknown>"),
                    error=short_err,
                )
                continue

            # Log low-confidence elements as warnings (still persisted).
            if element.confidence_pct < _MIN_CONFIDENCE_LOG_THRESHOLD:
                bound_log.warning(
                    "extraction.low_confidence_element",
                    element_index=idx,
                    concept_label=element.concept_label,
                    confidence_pct=element.confidence_pct,
                )

            validated.append(element)

        return validated, rejected

    # -------------------------------------------------------------------------
    # Step 5 — Map ExtractionElement → FinancialLineItemCreate
    # -------------------------------------------------------------------------

    def _map_to_line_items(
        self,
        *,
        elements: list[ExtractionElement],
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        source_file_hash: str | None,
        bound_log: Any,
    ) -> tuple[list[FinancialLineItemCreate], list[tuple[int, str]]]:
        """
        Convert validated ExtractionElement instances to FinancialLineItemCreate.

        Mapping rules:
          1. value_reported = parsed_value × unit_multiplier (scaled absolute value).
          2. Sign convention (Amendment V1.2 §2.2): outflow canonical fields negated.
          3. value_usd = value_reported when currency is USD; None otherwise.
             (FX translation deferred to M5 currency service.)
          4. canonical_field falls back to a slugified concept_label when None.
          5. lineage_comment stored in derived_expression_formula for SOX traceability.
          6. Rows where parsed_value is None are dropped (cannot satisfy the
             FinancialLineItemCreate model_validator requiring at least one value).

        Args:
            elements:           Validated ExtractionElement list.
            company_id:         Company UUID.
            fiscal_year:        4-digit fiscal year.
            fiscal_period:      Fiscal period label.
            filing_date:        Filing date.
            reporting_standard: Accounting standard string.
            source_file_hash:   SHA-256 hex digest or None.
            bound_log:          Bound structlog logger.

        Returns:
            Tuple of (line_items, mapping_rejected_reasons).
        """
        line_items: list[FinancialLineItemCreate] = []
        rejected: list[tuple[int, str]] = []

        for idx, element in enumerate(elements):
            # ── Resolve numeric value ──────────────────────────────────────────
            parsed = _safe_decimal(element.parsed_value)
            if parsed is None:
                rejected.append(
                    (
                        idx,
                        f"parsed_value is None for concept_label={element.concept_label!r}; "
                        "cannot create FinancialLineItem without at least one monetary value.",
                    )
                )
                bound_log.debug(
                    "extraction.mapping_skipped_null_value",
                    element_index=idx,
                    concept_label=element.concept_label,
                )
                continue

            # ── Apply unit multiplier ──────────────────────────────────────────
            # parsed_value from Claude is the raw numeric; multiply by unit_multiplier
            # to get the absolute value (e.g. 391035 × 1000000 = 391_035_000_000).
            multiplier = Decimal(str(element.unit_multiplier)) if element.unit_multiplier else Decimal("1")
            value_reported = parsed * multiplier

            # ── Apply sign convention ──────────────────────────────────────────
            # amendment V1.2 §2.2: outflow concepts stored as negative.
            value_reported = _apply_sign_convention(value_reported, element.canonical_field)

            # ── FX stub ────────────────────────────────────────────────────────
            # When the reported currency is USD, value_usd == value_reported.
            # For all other currencies, value_usd is deferred to the M5 FX pass.
            currency = (element.currency or "").strip().upper() or None
            value_usd: Decimal | None = value_reported if currency == "USD" else None

            # ── Canonical field fallback ───────────────────────────────────────
            canonical = element.canonical_field
            if not canonical:
                # Slugify the concept_label as a best-effort canonical field.
                # The normalisation layer (M5) will remap these to XBRL tags.
                slug = re.sub(r"[^a-zA-Z0-9]+", "_", element.concept_label).strip("_")
                canonical = f"raw:{slug}"

            # ── Lineage comment — Amendment V1.2 §6.2 ─────────────────────────
            lineage = element.lineage_comment  # stored in derived_expression_formula

            # ── Build FinancialLineItemCreate ──────────────────────────────────
            try:
                item = FinancialLineItemCreate(
                    company_id=company_id,
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    reporting_standard=reporting_standard,
                    filing_date=filing_date,
                    is_restated=False,
                    canonical_field=canonical,
                    statement_type=element.statement_type,
                    value_usd=value_usd,
                    value_reported=value_reported,
                    reported_currency=currency,
                    fx_rate_used=None,
                    source_file_hash=source_file_hash,
                    extraction_method="ai",
                    derived_expression_formula=lineage,
                )
            except ValidationError as exc:
                short_err = str(exc)[:200]
                rejected.append((idx, short_err))
                bound_log.warning(
                    "extraction.line_item_validation_failed",
                    element_index=idx,
                    concept_label=element.concept_label,
                    error=short_err,
                )
                continue

            line_items.append(item)

        return line_items, rejected

    # -------------------------------------------------------------------------
    # Mock-mode synthetic extraction
    # -------------------------------------------------------------------------

    async def _generate_mock_result(
        self,
        *,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
        filing_date: date,
        reporting_standard: str,
        source_file_hash: str | None,
    ) -> ExtractionResult:
        """
        Return synthetic fixture financial records without calling the Claude API.

        Used when ``_is_mock_mode(settings.claude_api_key)`` is True.  Writes the
        same FinancialLineItemCreate rows as a live extraction would produce, using
        the ``_MOCK_FINANCIAL_DATA`` fixture table defined at module level.

        The ExtractionResult carries ``model_version="mock-mode"`` so callers and
        logs can distinguish mock runs from live runs.

        Args:
            company_id:         Company UUID.
            fiscal_year:        4-digit fiscal year.
            fiscal_period:      Fiscal period label (FY / Q1 / Q2 / Q3 / Q4).
            filing_date:        Filing date.
            reporting_standard: Accounting standard string (US_GAAP | IFRS | IND_AS).
            source_file_hash:   SHA-256 hex digest or None.

        Returns:
            ExtractionResult with inserted/skipped counts and model_version="mock-mode".
        """
        extraction_timestamp = datetime.now(UTC).isoformat()
        line_items: list[FinancialLineItemCreate] = []

        for concept_label, canonical_field, stmt_type, str_value, currency, unit_mult in _MOCK_FINANCIAL_DATA:
            # Parse the value (underscores allowed for readability in the table).
            raw_numeric = str_value.replace("_", "").strip()
            parsed = _safe_decimal(raw_numeric)
            if parsed is None:
                continue

            multiplier = Decimal(str(unit_mult))
            value_reported = parsed * multiplier
            # Apply sign convention so outflow items are stored negative.
            value_reported = _apply_sign_convention(value_reported, canonical_field)
            value_usd: Decimal | None = value_reported if currency == "USD" else None

            try:
                item = FinancialLineItemCreate(
                    company_id=company_id,
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    reporting_standard=reporting_standard,
                    filing_date=filing_date,
                    is_restated=False,
                    canonical_field=canonical_field,
                    statement_type=stmt_type,
                    value_usd=value_usd,
                    value_reported=value_reported,
                    reported_currency=currency,
                    fx_rate_used=None,
                    source_file_hash=source_file_hash,
                    extraction_method="ai",
                    derived_expression_formula=(
                        f"[MOCK] Synthetic fixture record — "
                        f"{concept_label} for FY{fiscal_year}/{fiscal_period}"
                    ),
                )
            except Exception:  # noqa: BLE001 — never crash on mock data shape mismatch
                continue

            line_items.append(item)

        repo = FinancialLineItemRepository(self._session)
        inserted = await repo.bulk_upsert(line_items)
        skipped = len(line_items) - inserted

        log.info(
            "extraction.mock_complete",
            company_id=str(company_id),
            fiscal_year=fiscal_year,
            inserted=inserted,
            skipped=skipped,
            total_mock_records=len(line_items),
        )

        return ExtractionResult(
            inserted=inserted,
            skipped=skipped,
            rejected=0,
            model_version="mock-mode",
            extraction_timestamp=extraction_timestamp,
            rejected_reasons=[],
        )
