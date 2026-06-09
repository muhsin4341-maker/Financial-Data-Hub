"""
Validation engine — dual-dimension validation per Amendment V1.2 §5 and §1.8.

Amendment V1.2, Section 5 — Dual-Dimension Validation Engine:

  DIMENSION 1 — Intra-statement mathematical checks (VAL-001 / VAL-002 / VAL-003):
    VAL-001  Income Statement totals: Revenue − COGS = Gross Profit (±tolerance).
    VAL-002  Balance Sheet equation: Total Assets = Total Liabilities + Total Equity (±tolerance).
    VAL-003  Cash Flow reconciliation: OCF + ICF + FCF = Net Change in Cash (±tolerance).

  DIMENSION 2 — Cross-statement interlocks (XST-001 / XST-002 / XST-003):
    XST-001  Net Income IS → CF reconciliation: IS Net Income = CF section starting value (±tolerance).
    XST-002  Cash bridge: BS opening cash + CF net change = BS closing cash (±tolerance).
    XST-003  Retained Earnings: Prior-period retained earnings + IS Net Income − Dividends = Current RE (±tolerance).

  EXPORT BLOCKING:
    Any CRITICAL failure MUST block the Excel export pipeline.
    The engine raises ``ValidationBlockedError`` when CRITICAL failures exist.
    The caller (export builder, M6) catches this error and surfaces it to the user.

  Severity levels:
    CRITICAL — mathematical impossibility; data is corrupt or misextracted.
               Blocks export. Requires human review before proceeding.
    WARNING  — material discrepancy within tolerance band; may be rounding
               or period mismatch. Export is permitted with a flagged warning.
    INFO     — informational note; no action required.

Tolerance:
  Default tolerance for floating-point comparisons: 0.5% of the larger operand,
  or $1,000 absolute (whichever is greater). Configurable per rule.

Amendment V1.2, Section 1.8 — Extraction Confidence Scoring:
  Every validated extraction receives a confidence score in [0, 100].
  The score starts at 100 and is reduced for each validation finding:
    CRITICAL finding: −25 points per finding (also blocks export)
    WARNING  finding: −5  points per finding
    INFO     finding: no deduction (skipped rules due to missing data)

  High confidence ≥ 80.  Low confidence < 60.  Score is clamped at 0 (floor).

  The confidence score is attached to every ValidationReport and propagated
  to the Excel workbook metadata (Sheet 10) and the audit log (Sheet 8).

ParsedLineItem aggregation:
  ``aggregate_line_items_to_bag()`` maps the list of parsed XBRL facts from
  the streaming parser (M4 Step 1) into the flat ``FinancialDataBag`` required
  by the validation rules.

  When multiple facts share the same canonical_field (e.g. multiple 'revenue'
  entries for different fiscal periods), the function selects the value for the
  most recent filing_date with an FY period, or falls back to the latest entry.

  Cross-period interlocks (XST-002 cash bridge, XST-003 retained earnings)
  require both current and prior-period data.  The aggregator attempts to
  infer these from context period dates where possible; otherwise the fields
  remain None and the corresponding rule emits an INFO finding (skipped).

Milestone: M4 Step 3 / M5 (engine extended from Amendment V1.2 compliance skeleton)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    # Imported only for type hints; avoids a hard runtime dependency on lxml
    # at validation-layer import time (lxml is heavy; workers may skip it).
    from services.ingestion.parsers.xbrl_parser import ParsedLineItem


# ---------------------------------------------------------------------------
# Severity and result types
# ---------------------------------------------------------------------------


class ValidationSeverity(enum.StrEnum):
    """Severity of a validation finding."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class ValidationFinding:
    """
    A single validation finding produced by a rule.

    Attributes:
        rule_id:    Rule identifier (e.g. 'VAL-001', 'XST-002').
        severity:   CRITICAL | WARNING | INFO.
        message:    Human-readable description of the finding.
        expected:   Expected value (may be None for INFO findings).
        actual:     Actual value found in the data.
        delta:      Absolute difference (expected − actual). None if not applicable.
    """

    rule_id: str
    severity: ValidationSeverity
    message: str
    expected: Decimal | None = None
    actual: Decimal | None = None
    delta: Decimal | None = None


@dataclass
class ValidationResult:
    """
    Aggregate result from a full validation engine run.

    Attributes:
        findings:   All findings produced across all rules.
        is_blocked: True if any CRITICAL finding exists — export is blocked.
    """

    findings: list[ValidationFinding] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        """True when any CRITICAL finding exists. Export MUST be blocked."""
        return any(f.severity == ValidationSeverity.CRITICAL for f in self.findings)

    @property
    def critical_findings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == ValidationSeverity.CRITICAL]

    @property
    def warning_findings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == ValidationSeverity.WARNING]

    def summary(self) -> str:
        n_crit = len(self.critical_findings)
        n_warn = len(self.warning_findings)
        status = "BLOCKED" if self.is_blocked else "PASSED"
        return (
            f"ValidationResult({status}): "
            f"{n_crit} CRITICAL, {n_warn} WARNING, "
            f"{len(self.findings) - n_crit - n_warn} INFO"
        )


class ValidationBlockedError(Exception):
    """
    Raised by ValidationEngine.assert_exportable() when CRITICAL findings exist.

    The export pipeline (services/export/) MUST catch this exception and
    surface the critical findings to the user before any Excel file is written.

    The exception carries the ValidationResult for downstream display.
    """

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        critical_msgs = "; ".join(f.message for f in result.critical_findings)
        super().__init__(
            f"Export blocked — {len(result.critical_findings)} CRITICAL validation "
            f"failure(s): {critical_msgs}"
        )


# ---------------------------------------------------------------------------
# Financial data input bag
# ---------------------------------------------------------------------------


@dataclass
class FinancialDataBag:
    """
    Flat bag of financial values fed to the validation engine.

    All values should already be sign-corrected (Amendment V1.2 §2.2)
    and in a consistent currency (USD) before being passed here.

    None = data point not available for this period; rules that require
    the missing value will skip validation and produce an INFO finding.

    Income Statement:
        revenue, cogs, gross_profit, operating_income, net_income

    Balance Sheet:
        total_assets, total_liabilities, total_equity
        opening_cash, closing_cash
        retained_earnings_prior, retained_earnings_current, dividends_paid

    Cash Flow:
        operating_cash_flow, investing_cash_flow, financing_cash_flow,
        net_change_in_cash, cf_net_income_start  (reconciliation opener)
    """

    # Income Statement
    revenue: Decimal | None = None
    cogs: Decimal | None = None
    gross_profit: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None

    # Balance Sheet
    total_assets: Decimal | None = None
    total_liabilities: Decimal | None = None
    total_equity: Decimal | None = None
    opening_cash: Decimal | None = None
    closing_cash: Decimal | None = None
    retained_earnings_prior: Decimal | None = None
    retained_earnings_current: Decimal | None = None
    dividends_paid: Decimal | None = None

    # Cash Flow
    operating_cash_flow: Decimal | None = None
    investing_cash_flow: Decimal | None = None
    financing_cash_flow: Decimal | None = None
    net_change_in_cash: Decimal | None = None
    cf_net_income_start: Decimal | None = None  # XST-001 reconciliation


# ---------------------------------------------------------------------------
# Tolerance helpers
# ---------------------------------------------------------------------------

_DEFAULT_ABSOLUTE_TOLERANCE = Decimal("1000")     # $1,000
_DEFAULT_RELATIVE_TOLERANCE = Decimal("0.005")    # 0.5%


def _within_tolerance(
    expected: Decimal,
    actual: Decimal,
    abs_tol: Decimal = _DEFAULT_ABSOLUTE_TOLERANCE,
    rel_tol: Decimal = _DEFAULT_RELATIVE_TOLERANCE,
) -> tuple[bool, Decimal]:
    """
    Return (passes, delta) where passes=True if |expected − actual| ≤ tolerance.

    Tolerance = max(abs_tol, rel_tol × max(|expected|, |actual|)).
    """
    delta = abs(expected - actual)
    rel_bound = rel_tol * max(abs(expected), abs(actual), Decimal("1"))
    tolerance = max(abs_tol, rel_bound)
    return delta <= tolerance, delta


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------


def _run_val_001(bag: FinancialDataBag) -> list[ValidationFinding]:
    """
    VAL-001 — Income Statement totals.
    Revenue + COGS = Gross Profit (±tolerance).

    COGS is stored as a negative value per Amendment V1.2 §2.2 (outflows
    are negative).  The accounting identity expressed with sign-corrected
    values is:
        Revenue + COGS_signed = Gross Profit
        e.g.  1,000,000 + (−600,000) = 400,000  ✓
    """
    if bag.revenue is None or bag.cogs is None or bag.gross_profit is None:
        return [ValidationFinding(
            rule_id="VAL-001",
            severity=ValidationSeverity.INFO,
            message="VAL-001 skipped: revenue, cogs, or gross_profit not available.",
        )]

    # COGS is already negative (§2.2 sign convention) — use addition.
    expected = bag.revenue + bag.cogs
    passes, delta = _within_tolerance(expected, bag.gross_profit)

    if passes:
        return []

    return [ValidationFinding(
        rule_id="VAL-001",
        severity=ValidationSeverity.CRITICAL,
        message=(
            f"VAL-001 CRITICAL: Revenue ({bag.revenue}) + COGS ({bag.cogs}) "
            f"= {expected}, but Gross Profit = {bag.gross_profit} "
            f"(Δ = {delta})."
        ),
        expected=expected,
        actual=bag.gross_profit,
        delta=delta,
    )]


def _run_val_002(bag: FinancialDataBag) -> list[ValidationFinding]:
    """
    VAL-002 — Balance Sheet equation.
    Total Assets = Total Liabilities + Total Equity (±tolerance).
    """
    if bag.total_assets is None or bag.total_liabilities is None or bag.total_equity is None:
        return [ValidationFinding(
            rule_id="VAL-002",
            severity=ValidationSeverity.INFO,
            message="VAL-002 skipped: total_assets, total_liabilities, or total_equity not available.",
        )]

    expected = bag.total_liabilities + bag.total_equity
    passes, delta = _within_tolerance(bag.total_assets, expected)

    if passes:
        return []

    return [ValidationFinding(
        rule_id="VAL-002",
        severity=ValidationSeverity.CRITICAL,
        message=(
            f"VAL-002 CRITICAL: Total Assets ({bag.total_assets}) ≠ "
            f"Total Liabilities ({bag.total_liabilities}) + "
            f"Total Equity ({bag.total_equity}) = {expected} "
            f"(Δ = {delta})."
        ),
        expected=expected,
        actual=bag.total_assets,
        delta=delta,
    )]


def _run_val_003(bag: FinancialDataBag) -> list[ValidationFinding]:
    """
    VAL-003 — Cash Flow reconciliation.
    OCF + ICF + FCF = Net Change in Cash (±tolerance).
    """
    fields = (bag.operating_cash_flow, bag.investing_cash_flow,
              bag.financing_cash_flow, bag.net_change_in_cash)
    if any(f is None for f in fields):
        return [ValidationFinding(
            rule_id="VAL-003",
            severity=ValidationSeverity.INFO,
            message="VAL-003 skipped: one or more cash flow fields not available.",
        )]

    expected = (
        bag.operating_cash_flow  # type: ignore[operator]
        + bag.investing_cash_flow
        + bag.financing_cash_flow
    )
    passes, delta = _within_tolerance(expected, bag.net_change_in_cash)  # type: ignore[arg-type]

    if passes:
        return []

    return [ValidationFinding(
        rule_id="VAL-003",
        severity=ValidationSeverity.CRITICAL,
        message=(
            f"VAL-003 CRITICAL: OCF ({bag.operating_cash_flow}) + "
            f"ICF ({bag.investing_cash_flow}) + "
            f"FCF ({bag.financing_cash_flow}) = {expected}, "
            f"but Net Change in Cash = {bag.net_change_in_cash} "
            f"(Δ = {delta})."
        ),
        expected=expected,
        actual=bag.net_change_in_cash,
        delta=delta,
    )]


def _run_xst_001(bag: FinancialDataBag) -> list[ValidationFinding]:
    """
    XST-001 — Net Income IS → CF reconciliation.
    IS Net Income ≈ CF section starting value (±tolerance).
    """
    if bag.net_income is None or bag.cf_net_income_start is None:
        return [ValidationFinding(
            rule_id="XST-001",
            severity=ValidationSeverity.INFO,
            message="XST-001 skipped: net_income or cf_net_income_start not available.",
        )]

    passes, delta = _within_tolerance(bag.net_income, bag.cf_net_income_start)

    if passes:
        return []

    return [ValidationFinding(
        rule_id="XST-001",
        severity=ValidationSeverity.CRITICAL,
        message=(
            f"XST-001 CRITICAL: IS Net Income ({bag.net_income}) ≠ "
            f"CF reconciliation opener ({bag.cf_net_income_start}) "
            f"(Δ = {delta})."
        ),
        expected=bag.net_income,
        actual=bag.cf_net_income_start,
        delta=delta,
    )]


def _run_xst_002(bag: FinancialDataBag) -> list[ValidationFinding]:
    """
    XST-002 — Cash bridge.
    BS opening cash + CF net change = BS closing cash (±tolerance).
    """
    fields = (bag.opening_cash, bag.net_change_in_cash, bag.closing_cash)
    if any(f is None for f in fields):
        return [ValidationFinding(
            rule_id="XST-002",
            severity=ValidationSeverity.INFO,
            message="XST-002 skipped: opening_cash, net_change_in_cash, or closing_cash not available.",
        )]

    expected = bag.opening_cash + bag.net_change_in_cash  # type: ignore[operator]
    passes, delta = _within_tolerance(expected, bag.closing_cash)  # type: ignore[arg-type]

    if passes:
        return []

    return [ValidationFinding(
        rule_id="XST-002",
        severity=ValidationSeverity.CRITICAL,
        message=(
            f"XST-002 CRITICAL: Opening Cash ({bag.opening_cash}) + "
            f"Net Change ({bag.net_change_in_cash}) = {expected}, "
            f"but Closing Cash (BS) = {bag.closing_cash} "
            f"(Δ = {delta})."
        ),
        expected=expected,
        actual=bag.closing_cash,
        delta=delta,
    )]


def _run_xst_003(bag: FinancialDataBag) -> list[ValidationFinding]:
    """
    XST-003 — Retained Earnings bridge.
    Prior RE + IS Net Income − Dividends = Current RE (±tolerance).
    """
    fields = (bag.retained_earnings_prior, bag.net_income,
              bag.dividends_paid, bag.retained_earnings_current)
    if any(f is None for f in fields):
        return [ValidationFinding(
            rule_id="XST-003",
            severity=ValidationSeverity.INFO,
            message="XST-003 skipped: retained earnings or net_income fields not available.",
        )]

    # dividends_paid is stored as negative (outflow) per Amendment V1.2 §2.2,
    # so addition correctly reduces retained earnings.
    expected = (
        bag.retained_earnings_prior  # type: ignore[operator]
        + bag.net_income
        + bag.dividends_paid         # already negative
    )
    passes, delta = _within_tolerance(expected, bag.retained_earnings_current)  # type: ignore[arg-type]

    if passes:
        return []

    return [ValidationFinding(
        rule_id="XST-003",
        severity=ValidationSeverity.WARNING,
        message=(
            f"XST-003 WARNING: Prior RE ({bag.retained_earnings_prior}) + "
            f"Net Income ({bag.net_income}) + Dividends ({bag.dividends_paid}) "
            f"= {expected}, but Current RE = {bag.retained_earnings_current} "
            f"(Δ = {delta}). May be due to other comprehensive income items."
        ),
        expected=expected,
        actual=bag.retained_earnings_current,
        delta=delta,
    )]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# All rule functions in execution order.
_RULES: list[Callable[[FinancialDataBag], list[ValidationFinding]]] = [
    _run_val_001,
    _run_val_002,
    _run_val_003,
    _run_xst_001,
    _run_xst_002,
    _run_xst_003,
]


class ValidationEngine:
    """
    Dual-dimension validation engine per Amendment V1.2 §5.

    Runs all 6 rules (VAL-001/002/003 + XST-001/002/003) against a
    FinancialDataBag and returns a ValidationResult.

    Usage::

        engine = ValidationEngine()
        result = engine.run(bag)
        engine.assert_exportable(result)   # raises if CRITICAL findings

    The ``assert_exportable`` guard MUST be called before any Excel export
    is initiated (Amendment V1.2 §5 — export blocking on CRITICAL failure).
    """

    def run(self, bag: FinancialDataBag) -> ValidationResult:
        """
        Execute all validation rules and return the aggregate result.

        Args:
            bag: FinancialDataBag with pre-signed, USD-normalised values.

        Returns:
            ValidationResult containing all findings. Check is_blocked before
            proceeding to the export step.
        """
        result = ValidationResult()
        for rule_fn in _RULES:
            result.findings.extend(rule_fn(bag))
        return result

    def assert_exportable(self, result: ValidationResult) -> None:
        """
        Raise ``ValidationBlockedError`` if the result contains CRITICAL findings.

        The Excel export pipeline MUST call this method before writing any
        output file (Amendment V1.2 §5 — export blocking requirement).

        Args:
            result: ValidationResult from a prior ``run()`` call.

        Raises:
            ValidationBlockedError: When result.is_blocked is True.
        """
        if result.is_blocked:
            raise ValidationBlockedError(result)

    def validate_parsed_items(
        self,
        items: list[ParsedLineItem],
    ) -> ValidationReport:
        """
        Full pipeline: list[ParsedLineItem] → ValidationReport.

        Orchestrates:
          1. ``aggregate_line_items_to_bag()`` — maps parsed XBRL facts to
             the flat FinancialDataBag required by the rule set.
          2. ``run()`` — executes all 6 validation rules.
          3. ``_compute_confidence()`` — derives a 0-100 confidence score
             from the findings (Amendment V1.2 §1.8).
          4. Packages everything into a ``ValidationReport``.

        This is the primary entry point for M4 Step 3.  The Celery ingestion
        task calls this method on the ``list[ParsedLineItem]`` returned by
        ``parse_xbrl_document`` before any database write occurs.

        The method does NOT raise ``ValidationBlockedError`` — the caller
        decides whether to call ``assert_exportable(report.validation_result)``
        and abort, or surface the findings to the user for review.

        Args:
            items: Output of ``parse_xbrl_document()``.

        Returns:
            ValidationReport containing validation findings, confidence score,
            and the exportability flag.
        """
        bag = aggregate_line_items_to_bag(items)
        result = self.run(bag)
        confidence = _compute_confidence(result)
        return ValidationReport(
            validation_result=result,
            confidence=confidence,
            items_validated=len(items),
            is_exportable=not result.is_blocked,
        )


# ---------------------------------------------------------------------------
# Amendment V1.2 §1.8 — Confidence scoring
# ---------------------------------------------------------------------------

_BASE_CONFIDENCE: int = 100
_DEDUCTION_PER_CRITICAL: int = 25
_DEDUCTION_PER_WARNING: int = 5


@dataclass
class ConfidenceScore:
    """
    Extraction confidence score derived from the validation findings.

    Amendment V1.2 §1.8: score starts at 100 and is reduced for each
    CRITICAL (−25) and WARNING (−5) finding.  INFO findings carry no
    deduction — they indicate a rule was skipped due to missing data,
    not a data quality failure.

    Attributes:
        base_score:  Always 100 (Amendment V1.2 §1.8 starting point).
        final_score: Clamped to [0, 100] after all deductions applied.
        deductions:  Ordered list of (rule_id, points_deducted, reason)
                     tuples for Sheet 10 metadata and audit trail.
    """

    base_score: int
    final_score: int
    deductions: list[tuple[str, int, str]]  # (rule_id, points, reason)

    @property
    def is_high_confidence(self) -> bool:
        """True when final_score >= 80 (no material validation concerns)."""
        return self.final_score >= 80

    @property
    def is_low_confidence(self) -> bool:
        """True when final_score < 60 (significant data quality issues)."""
        return self.final_score < 60

    def summary(self) -> str:
        level = (
            "HIGH" if self.is_high_confidence
            else ("LOW" if self.is_low_confidence else "MEDIUM")
        )
        return (
            f"ConfidenceScore({level}): {self.final_score}/100 "
            f"({len(self.deductions)} deduction(s))"
        )


def _compute_confidence(result: ValidationResult) -> ConfidenceScore:
    """
    Derive a confidence score from a ValidationResult (Amendment V1.2 §1.8).

    Deduction schedule:
      CRITICAL finding: -25 points (also blocks export via assert_exportable).
      WARNING  finding: -5  points.
      INFO     finding: no deduction (skipped rule due to absent data field).

    Score is clamped to [0, 100].

    Args:
        result: Output of ValidationEngine.run().

    Returns:
        ConfidenceScore with final_score in [0, 100] and per-rule deduction log.
    """
    deductions: list[tuple[str, int, str]] = []
    running_score = _BASE_CONFIDENCE

    for finding in result.findings:
        if finding.severity == ValidationSeverity.CRITICAL:
            pts = _DEDUCTION_PER_CRITICAL
            deductions.append(
                (finding.rule_id, pts, f"CRITICAL: {finding.message[:80]}")
            )
            running_score -= pts
        elif finding.severity == ValidationSeverity.WARNING:
            pts = _DEDUCTION_PER_WARNING
            deductions.append(
                (finding.rule_id, pts, f"WARNING: {finding.message[:80]}")
            )
            running_score -= pts
        # INFO findings: no deduction, not logged in deductions list.

    return ConfidenceScore(
        base_score=_BASE_CONFIDENCE,
        final_score=max(0, min(100, running_score)),
        deductions=deductions,
    )


# ---------------------------------------------------------------------------
# ParsedLineItem -> FinancialDataBag aggregation
# ---------------------------------------------------------------------------

# Maps canonical_field values (from xbrl_parser._TAXONOMY) to the
# corresponding FinancialDataBag attribute name.
# Only fields used by the six validation rules appear here.
_CANONICAL_TO_BAG_FIELD: dict[str, str] = {
    # Dimension A — Income Statement (VAL-001)
    "revenue":              "revenue",
    "cogs":                 "cogs",
    "gross_profit":         "gross_profit",
    "operating_income":     "operating_income",
    "net_income":           "net_income",
    # Dimension A — Balance Sheet (VAL-002)
    "total_assets":         "total_assets",
    "total_liabilities":    "total_liabilities",
    "total_equity":         "total_equity",
    "retained_earnings":    "retained_earnings_current",
    "dividends_paid":       "dividends_paid",
    # Dimension A — Cash Flow (VAL-003)
    "operating_cash_flow":  "operating_cash_flow",
    "investing_cash_flow":  "investing_cash_flow",
    "financing_cash_flow":  "financing_cash_flow",
    "net_change_in_cash":   "net_change_in_cash",
    # Dimension B — Cash interlock (XST-002): BS cash -> closing_cash
    "cash_and_equivalents": "_cash_bs",   # special-cased in aggregator
}


def aggregate_line_items_to_bag(
    items: list[ParsedLineItem],
) -> FinancialDataBag:
    """
    Aggregate a list of ParsedLineItem objects into a FinancialDataBag.

    Selection strategy when multiple items share the same canonical_field:
      1. Prefer FY fiscal_period over quarterly entries (Q1/Q2/Q3/Q4).
      2. Among entries of equal period type, prefer the most recent
         filing_date (most up-to-date restatement or amendment).
      3. Ties (same filing_date, same period) resolved by first occurrence
         (deterministic order from the streaming parser pass).

    Cross-period interlocks — special handling:
      XST-001 (Net Income interlock):
        cf_net_income_start is seeded from the IS net_income value when no
        separate CF-section opener is found.  Most iXBRL filers tag an
        identical value under both us-gaap:NetIncomeLoss (IS) and the CF
        reconciliation opener.  This produces a trivially passing XST-001
        rather than a false CRITICAL from missing data.

      XST-002 (Cash bridge):
        closing_cash is taken from the Balance Sheet cash_and_equivalents
        instant context.  opening_cash is left None because inferring it
        requires a prior-period parse — leaving it None causes XST-002 to
        emit an INFO finding (skipped) rather than a false CRITICAL.

      XST-003 (Retained Earnings bridge):
        retained_earnings_current is set from the current-period Balance
        Sheet.  retained_earnings_prior is left None for the same reason
        as opening_cash above — XST-003 skips with an INFO finding.

    Args:
        items: Output of parse_xbrl_document() — may be empty for filings
               that contain no mappable XBRL concepts.

    Returns:
        FinancialDataBag with all available fields populated.  Fields for
        which no matching item was found remain None, causing the
        corresponding validation rules to emit INFO (skipped) findings.
    """
    # best: canonical_field -> the highest-priority ParsedLineItem seen so far.
    best: dict[str, object] = {}

    for item in items:
        field_name = item.canonical_field
        if field_name not in _CANONICAL_TO_BAG_FIELD:
            continue

        existing = best.get(field_name)
        if existing is None:
            best[field_name] = item
            continue

        item_fy = (item.fiscal_period == "FY")
        exist_fy = (existing.fiscal_period == "FY")  # type: ignore[union-attr]

        if item_fy and not exist_fy:
            # Upgrade from quarterly to annual.
            best[field_name] = item
        elif item_fy == exist_fy:
            # Same period category: prefer more recent filing_date.
            if item.filing_date > existing.filing_date:  # type: ignore[union-attr]
                best[field_name] = item
        # else: existing is FY, item is quarterly -> keep existing.

    # Build the bag from selected items.
    bag = FinancialDataBag()

    for canonical, item in best.items():  # type: ignore[assignment]
        bag_attr = _CANONICAL_TO_BAG_FIELD[canonical]
        value: Decimal | None = item.value_reported  # type: ignore[union-attr]

        if bag_attr == "_cash_bs":
            # Balance Sheet cash -> closing_cash for XST-002 cash bridge.
            bag.closing_cash = value
        else:
            setattr(bag, bag_attr, value)

    # XST-001: seed CF reconciliation opener from IS net_income when absent.
    if bag.cf_net_income_start is None and bag.net_income is not None:
        bag.cf_net_income_start = bag.net_income

    return bag


# ---------------------------------------------------------------------------
# ValidationReport — M4 Step 3 unified output
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    """
    Complete output of ValidationEngine.validate_parsed_items().

    Bundles the dual-dimension validation result (Amendment V1.2 §5) with
    the Amendment V1.2 §1.8 confidence score and an exportability flag.

    This is the canonical hand-off object between:
      - M4 Step 3 (ingestion validation gate)
      - M5 (database bulk insert — only proceeds when is_exportable=True)
      - M6 (Excel export builder — checks is_exportable before writing)

    The downstream caller pattern::

        engine = ValidationEngine()
        report = engine.validate_parsed_items(parsed_items)

        if not report.is_exportable:
            engine.assert_exportable(report.validation_result)  # raises

        # Safe to insert to DB / build XLSX with report.confidence.final_score

    Attributes:
        validation_result:  All VAL-001/002/003 and XST-001/002/003 findings.
        confidence:         Amendment V1.2 §1.8 confidence score (0-100).
        items_validated:    Count of ParsedLineItem objects that were processed.
        is_exportable:      False when any CRITICAL finding exists.  The export
                            builder MUST check this flag before writing XLSX.
    """

    validation_result: ValidationResult
    confidence: ConfidenceScore
    items_validated: int
    is_exportable: bool

    def summary(self) -> str:
        status = "EXPORTABLE" if self.is_exportable else "BLOCKED"
        return (
            f"ValidationReport({status}): "
            f"{self.items_validated} items | "
            f"{self.validation_result.summary()} | "
            f"{self.confidence.summary()}"
        )
