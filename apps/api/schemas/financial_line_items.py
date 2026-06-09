"""
Pydantic schemas for FinancialLineItem — M4.1.

Schemas:
  FinancialLineItemCreate — write schema consumed by FinancialLineItemRepository.bulk_upsert()
                            and produced by AIExtractionService (M4.2).
  FinancialLineItemRead   — response schema returned by query methods and future API endpoints.

Design notes:
  - value_usd / value_reported use Decimal for exact NUMERIC(26,2) round-trips.
    String coercion is accepted on input so that JSON payloads from the Claude AI
    response (which serialises numbers as strings to preserve precision) validate
    without an extra conversion step at the caller.
  - fx_rate_used uses Decimal for NUMERIC(38,10) precision.
  - reporting_standard is validated against the ReportingStandard enum values.
  - statement_type is validated against the three known two-letter codes.
  - is_restated defaults to False — only restatement rows set it to True.
  - canonical_field is left as a plain string; the extraction layer is responsible
    for normalising to XBRL concept tags before calling bulk_upsert().

Amendment V1.2 compliance:
  §1.1 — NUMERIC(26,2) for absolute monetary values; NUMERIC(38,10) for FX coefficients.
  §1.2 — Point-in-time: filing_date + is_restated form part of the composite unique key.
  §2.1 — reporting_standard is mandatory on every row.
  §4.2 — source_file_hash links to stored_documents.content_hash for SOX / IT Act audit.

Milestone: M4.1 — FinancialLineItem Repository & Schemas
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.api.models import ReportingStandard

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Known reporting standard string values — used for validation.
_VALID_REPORTING_STANDARDS: frozenset[str] = frozenset(rs.value for rs in ReportingStandard)

#: Two-letter statement type codes recognised by the platform.
#: IS = Income Statement, BS = Balance Sheet, CF = Cash Flow.
_VALID_STATEMENT_TYPES: frozenset[str] = frozenset({"IS", "BS", "CF"})

#: Known extraction method labels.
_VALID_EXTRACTION_METHODS: frozenset[str] = frozenset({"xbrl", "pdf", "ocr", "ai"})


# ---------------------------------------------------------------------------
# FinancialLineItemCreate
# ---------------------------------------------------------------------------


class FinancialLineItemCreate(BaseModel):
    """
    Input schema for creating a single FinancialLineItem row.

    Used by:
      - AIExtractionService (M4.2) — one instance per extracted data point.
      - FinancialLineItemRepository.bulk_upsert() — accepts a list of these.
      - Unit tests — construct expected rows for assertion.

    All monetary fields (value_usd, value_reported) accept both Decimal and
    coercible string inputs so that AI extraction responses (which serialise
    numeric values as strings to preserve precision) validate directly without
    an extra conversion step.

    Amendment V1.2 §2.2 sign convention:
      Inflows (revenue, assets, cash inflows) are stored as positive values.
      Outflows (expenses, liabilities, cash outflows) are stored as negative.
      The extraction layer applies the sign inversion before constructing this
      schema — the repository writes the value as-is.
    """

    # ── Company and period identity ───────────────────────────────────────────
    company_id: uuid.UUID = Field(
        description="UUID of the company this data point belongs to.",
    )
    fiscal_year: int = Field(
        ge=1900,
        le=2100,
        description="4-digit fiscal year (e.g. 2024).",
        examples=[2024],
    )
    fiscal_period: str = Field(
        description="Fiscal period: 'Q1', 'Q2', 'Q3', 'Q4', or 'FY'.",
        examples=["FY", "Q3"],
    )

    # ── Reporting standard ────────────────────────────────────────────────────
    reporting_standard: str = Field(
        description=(
            "Accounting standard under which the value was reported. "
            f"Allowed values: {sorted(_VALID_REPORTING_STANDARDS)}."
        ),
        examples=["US_GAAP", "IFRS", "IND_AS"],
    )

    # ── Reporting framework (migration 014) ───────────────────────────────────
    reporting_framework: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "Regulatory filing framework (free-text).  "
            "Complements reporting_standard with the specific regime.  "
            "Examples: SEC_10K, SEC_10Q, SEBI_BRSR, MCA_AOC, IFRS_AR, EU_CSRD.  "
            "Nullable — omit when not applicable."
        ),
        examples=["SEC_10K", "SEBI_BRSR", None],
    )

    # ── Point-in-time fields ──────────────────────────────────────────────────
    filing_date: date = Field(
        description=(
            "Date the containing document was filed with the regulator. "
            "Restatements use a later filing_date; original rows are never overwritten."
        ),
        examples=["2024-02-02"],
    )
    is_restated: bool = Field(
        default=False,
        description=(
            "True when this row supersedes an earlier filing for the same period. "
            "Default False — only restatement rows should set this to True."
        ),
    )

    # ── Canonical field identifier ────────────────────────────────────────────
    canonical_field: str = Field(
        max_length=255,
        description=(
            "XBRL concept tag or normalised field name. "
            "Examples: 'us-gaap:Revenues', 'ifrs-full:Revenue', 'net_income'."
        ),
        examples=["us-gaap:Revenues", "us-gaap:NetIncomeLoss"],
    )

    # ── Statement classification ──────────────────────────────────────────────
    statement_type: str = Field(
        description=(
            "Financial statement type. "
            "Allowed values: 'IS' (Income Statement), 'BS' (Balance Sheet), "
            "'CF' (Cash Flow)."
        ),
        examples=["IS", "BS", "CF"],
    )

    # ── Monetary values — NUMERIC(26,2) ───────────────────────────────────────
    value_usd: Decimal | None = Field(
        default=None,
        description=(
            "Value translated to USD. NUMERIC(26,2). "
            "Sign convention: inflows positive, outflows negative."
        ),
        examples=["391035000000.00", None],
    )
    value_reported: Decimal | None = Field(
        default=None,
        description=(
            "Value in the original reported currency. NUMERIC(26,2). "
            "Equals value_usd when the reporting currency is USD."
        ),
        examples=["391035000000.00", None],
    )
    reported_currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description=(
            "ISO 4217 currency code of the reported value (e.g. 'USD', 'INR'). "
            "None when the value has no currency dimension (e.g. EPS share count)."
        ),
        examples=["USD", "INR", None],
    )

    # ── FX coefficient — NUMERIC(38,10) ───────────────────────────────────────
    fx_rate_used: Decimal | None = Field(
        default=None,
        description=(
            "FX translation coefficient (NUMERIC(38,10)). "
            "Balance Sheet: spot rate on period_end_date (ASC 830 / IAS 21). "
            "Income Statement & Cash Flow: weighted average rate over period."
        ),
        examples=["83.1234567890", None],
    )

    # ── Audit traceability ────────────────────────────────────────────────────
    source_file_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description=(
            "SHA-256 hex digest of the source document. "
            "Links to stored_documents.content_hash for SOX 404 / IT Act 2000 audit."
        ),
        examples=[None],
    )

    # ── Extraction provenance ─────────────────────────────────────────────────
    extraction_method: str | None = Field(
        default=None,
        description=(
            "How this value was extracted. "
            f"Allowed values: {sorted(_VALID_EXTRACTION_METHODS)}."
        ),
        examples=["ai", "xbrl", None],
    )

    # ── Derived / imputed formula ─────────────────────────────────────────────
    derived_expression_formula: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Algebraic derivation string for computed or imputed values "
            "(Amendment V1.2 §8.1). "
            "Example: 'us-gaap:GrossProfit - us-gaap:OperatingExpenses'."
        ),
        examples=[None],
    )

    # ── Field validators ──────────────────────────────────────────────────────

    @field_validator("fiscal_period")
    @classmethod
    def _validate_fiscal_period(cls, v: str) -> str:
        allowed = frozenset({"FY", "Q1", "Q2", "Q3", "Q4"})
        upper = v.strip().upper()
        if upper not in allowed:
            raise ValueError(
                f"fiscal_period must be one of {sorted(allowed)}. Received: {v!r}"
            )
        return upper

    @field_validator("reporting_standard")
    @classmethod
    def _validate_reporting_standard(cls, v: str) -> str:
        stripped = v.strip().upper()
        if stripped not in _VALID_REPORTING_STANDARDS:
            raise ValueError(
                f"reporting_standard must be one of "
                f"{sorted(_VALID_REPORTING_STANDARDS)}. Received: {v!r}"
            )
        return stripped

    @field_validator("statement_type")
    @classmethod
    def _validate_statement_type(cls, v: str) -> str:
        upper = v.strip().upper()
        if upper not in _VALID_STATEMENT_TYPES:
            raise ValueError(
                f"statement_type must be one of {sorted(_VALID_STATEMENT_TYPES)}. "
                f"Received: {v!r}"
            )
        return upper

    @field_validator("extraction_method")
    @classmethod
    def _validate_extraction_method(cls, v: str | None) -> str | None:
        if v is None:
            return None
        lower = v.strip().lower()
        if lower not in _VALID_EXTRACTION_METHODS:
            raise ValueError(
                f"extraction_method must be one of "
                f"{sorted(_VALID_EXTRACTION_METHODS)}. Received: {v!r}"
            )
        return lower

    @field_validator("reported_currency")
    @classmethod
    def _normalise_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        upper = v.strip().upper()
        return upper if upper else None

    @field_validator("value_usd", "value_reported", "fx_rate_used", mode="before")
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Decimal | None:
        """
        Accept numeric strings from AI/JSON responses as Decimal.

        The Claude API serialises large numbers as strings to avoid float
        precision loss (e.g. "391035000000.00").  This coercion allows
        callers to pass str, int, float, or Decimal interchangeably.
        """
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except InvalidOperation as exc:
            raise ValueError(
                f"Cannot coerce {v!r} to Decimal. "
                "Provide a numeric string, int, float, or Decimal."
            ) from exc

    @model_validator(mode="after")
    def _require_at_least_one_value(self) -> FinancialLineItemCreate:
        """
        Reject rows where both value_usd and value_reported are None.

        Every financial line item must carry at least one monetary value.
        Rows with no value are invalid and should not be persisted.
        """
        if self.value_usd is None and self.value_reported is None:
            raise ValueError(
                "At least one of value_usd or value_reported must be provided. "
                "A FinancialLineItem with no monetary value is meaningless."
            )
        return self


# ---------------------------------------------------------------------------
# FinancialLineItemRead
# ---------------------------------------------------------------------------


class FinancialLineItemRead(BaseModel):
    """
    Response schema for a single FinancialLineItem record.

    ``from_attributes=True`` allows instantiation directly from SQLAlchemy ORM
    instances (Pydantic v2 replacement for ``orm_mode=True``).

    Returned by FinancialLineItemRepository query methods and, in a later
    milestone, by the /api/v1/financials endpoints.

    Milestone: M4.1 — FinancialLineItem Repository & Schemas
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="UUID v7 primary key.")
    company_id: uuid.UUID = Field(description="Company this data point belongs to.")
    fiscal_year: int = Field(description="4-digit fiscal year.")
    fiscal_period: str = Field(description="Fiscal period: Q1 | Q2 | Q3 | Q4 | FY.")
    reporting_standard: str = Field(
        description="Accounting standard: US_GAAP | IFRS | IND_AS."
    )
    filing_date: date = Field(description="Date the source document was filed.")
    is_restated: bool = Field(
        description="True when this row supersedes an earlier filing for the same period."
    )
    canonical_field: str = Field(
        description="XBRL concept tag or normalised field name."
    )
    statement_type: str = Field(
        description="IS = Income Statement | BS = Balance Sheet | CF = Cash Flow."
    )
    value_usd: Decimal | None = Field(description="Value in USD. NUMERIC(26,2).")
    value_reported: Decimal | None = Field(
        description="Value in the original reported currency. NUMERIC(26,2)."
    )
    reported_currency: str | None = Field(description="ISO 4217 currency code.")
    fx_rate_used: Decimal | None = Field(
        description="FX translation coefficient. NUMERIC(38,10)."
    )
    source_file_hash: str | None = Field(
        description="SHA-256 hex digest of the source document."
    )
    extraction_method: str | None = Field(
        description="Extraction method: xbrl | pdf | ocr | ai."
    )
    derived_expression_formula: str | None = Field(
        description="Algebraic derivation string for computed values."
    )
    created_at: datetime = Field(description="ISO 8601 creation timestamp (UTC).")
    updated_at: datetime = Field(description="ISO 8601 last-update timestamp (UTC).")
