"""
Fiscal year and period normaliser — M5.5.

Parses free-form filing period labels into a strongly typed ``NormalizedPeriod``
value object whose tokens align directly with the inputs consumed by
``bulk_processor._derive_period_dates(fiscal_year, fiscal_period,
reporting_standard)``.

Supported input formats (representative, not exhaustive)
─────────────────────────────────────────────────────────
  Annual / Full Year:
    "FY 2025"  "FY25"  "FY2025"  "Annual 2024"  "Year Ended December 31, 2024"
    "Twelve Months Ended March 31, 2025"  "Full Year 2023"

  Quarterly (explicit):
    "Q3 2026"  "Q3FY26"  "FY2026 Q3"
    "1Q25"  "3Q 2025"            ← Wall Street "NQ YY[YY]" format
    "First Quarter 2025"  "Third Quarter Ended September 30, 2024"
    "Three Months Ended March 31, 2025"   ← end-month → calendar Q

  Half-year:
    "H1 2025"  "H2FY24"  "First Half 2024"  "Second Half FY2025"
    "Six Months Ended June 30, 2024"       ← end-month → H1
    "Six Months Ended December 31, 2024"   ← end-month → H2

  Indian formats:
    "Q1 FY25"  "Q1FY2025"  "H1 FY26"  "FY 2025-26"
    "Three Months Ended June 30, 2024"  (IND_AS Q1 — Apr→Jun)

  Nine-month YTD:
    "Nine Months Ended December 31, 2024"  → period_type = PeriodType.Q3
    (nine-month filings are Q3 YTD; the caller may override via period_overrides)

Integration contract with bulk_processor._derive_period_dates()
───────────────────────────────────────────────────────────────
  ``_derive_period_dates`` accepts fiscal_period in:
    "FY" | "Q1" | "Q2" | "Q3" | "Q4"  (case-insensitive after .upper().strip())

  ``H1`` and ``H2`` are defined in ``PeriodType`` for future extension but are
  NOT currently accepted by ``_derive_period_dates``; callers routing half-year
  items must provide ``period_overrides`` to ``BulkCurrencyTranslator``.

Year resolution rules
─────────────────────
  Four-digit year (19xx / 20xx):  used directly.
  Two-digit year (00–99):         mapped to 2000 + YY.
  Indian "FY 2025-26" notation:   the first year (2025) is extracted as the
                                  fiscal_year; the April-year convention is
                                  preserved in ``_derive_period_dates``.
  No year found:                  fiscal_year = None.

Fallback safety
───────────────
  ``extract_fiscal_period`` NEVER raises.  Any input that cannot be parsed
  returns ``NormalizedPeriod(fiscal_year=None, period_type=None,
  raw_label=<input>)``.

Milestone: M5.5 — Fiscal Year and Period Normaliser
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Final


# ---------------------------------------------------------------------------
# PeriodType enum
# ---------------------------------------------------------------------------

class PeriodType(str, Enum):
    """
    Canonical period type tokens aligned with bulk_processor._derive_period_dates().

    String inheritance (``str, Enum``) allows instances to be used directly
    wherever a plain string ``fiscal_period`` argument is expected — e.g.
    ``_derive_period_dates(year, PeriodType.Q1, "US_GAAP")`` works without
    explicit ``.value`` unwrapping.

    ``H1`` and ``H2`` are valid period types stored in FinancialLineItem rows
    for half-year filers, but ``_derive_period_dates`` does not yet handle
    them; callers must supply ``period_overrides`` for those items.
    """

    FY = "FY"   # Full / Annual year
    Q1 = "Q1"   # First quarter
    Q2 = "Q2"   # Second quarter
    Q3 = "Q3"   # Third quarter
    Q4 = "Q4"   # Fourth quarter
    H1 = "H1"   # First half-year (six months)
    H2 = "H2"   # Second half-year (six months)

    def is_quarterly(self) -> bool:
        """True for Q1–Q4."""
        return self in (PeriodType.Q1, PeriodType.Q2, PeriodType.Q3, PeriodType.Q4)

    def is_half_year(self) -> bool:
        """True for H1 / H2."""
        return self in (PeriodType.H1, PeriodType.H2)

    def is_annual(self) -> bool:
        """True for FY."""
        return self is PeriodType.FY

    def to_bulk_processor_token(self) -> str:
        """
        Return the string token accepted by ``bulk_processor._derive_period_dates``.

        H1 / H2 raise ``NotImplementedError`` because the bulk processor does
        not yet handle half-year periods — callers should provide
        ``period_overrides`` for those items instead.
        """
        if self in (PeriodType.H1, PeriodType.H2):
            raise NotImplementedError(
                f"{self.value!r} is not handled by _derive_period_dates; "
                "supply period_overrides to BulkCurrencyTranslator instead."
            )
        return self.value  # "FY" | "Q1" | "Q2" | "Q3" | "Q4"


# ---------------------------------------------------------------------------
# NormalizedPeriod value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizedPeriod:
    """
    Immutable value object carrying the normalised fiscal period tokens.

    Attributes:
        fiscal_year:  Integer fiscal year (e.g. 2025), or ``None`` if no
                      year could be detected.
        period_type:  :class:`PeriodType` member, or ``None`` if the period
                      type could not be inferred.
        raw_label:    The original input string, preserved verbatim for audit
                      logging and debugging.

    Integration example::

        period = extract_fiscal_period("Three Months Ended March 31, 2025")
        # period.fiscal_year  == 2025
        # period.period_type  == PeriodType.Q1
        # period.raw_label    == "Three Months Ended March 31, 2025"

        if period.fiscal_year and period.period_type:
            start, end = _derive_period_dates(
                period.fiscal_year,
                period.period_type,   # str-enum works as-is
                "US_GAAP",
            )
    """

    fiscal_year: int | None
    period_type: PeriodType | None
    raw_label: str

    @property
    def is_fully_resolved(self) -> bool:
        """True when both year and period type are present (no None fields)."""
        return self.fiscal_year is not None and self.period_type is not None

    def __str__(self) -> str:
        year = str(self.fiscal_year) if self.fiscal_year is not None else "?"
        ptype = self.period_type.value if self.period_type is not None else "?"
        return f"NormalizedPeriod({ptype} {year}, raw={self.raw_label!r})"


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Month name → calendar-year quarter (end-of-period mapping).
# "Three months ended March 31" → Q1 under calendar convention.
# IND_AS callers should apply period_overrides or map Q→IND_AS quarter externally.
_MONTH_TO_CALENDAR_Q: Final[dict[str, PeriodType]] = {
    "jan": PeriodType.Q1, "january":   PeriodType.Q1,
    "feb": PeriodType.Q1, "february":  PeriodType.Q1,
    "mar": PeriodType.Q1, "march":     PeriodType.Q1,
    "apr": PeriodType.Q2, "april":     PeriodType.Q2,
    "may": PeriodType.Q2,
    "jun": PeriodType.Q2, "june":      PeriodType.Q2,
    "jul": PeriodType.Q3, "july":      PeriodType.Q3,
    "aug": PeriodType.Q3, "august":    PeriodType.Q3,
    "sep": PeriodType.Q3, "september": PeriodType.Q3,
    "oct": PeriodType.Q4, "october":   PeriodType.Q4,
    "nov": PeriodType.Q4, "november":  PeriodType.Q4,
    "dec": PeriodType.Q4, "december":  PeriodType.Q4,
}

# Month name → half-year (end-of-period mapping).
# "Six months ended June 30"     → H1   (Jan–Jun)
# "Six months ended December 31" → H2   (Jul–Dec)
# "Six months ended September 30"→ H1 for IND_AS (Apr–Sep); H2 not inferrable
#   without framework — default to H1 (caller may override).
_MONTH_TO_HALF: Final[dict[str, PeriodType]] = {
    "jan": PeriodType.H1, "january":   PeriodType.H1,
    "feb": PeriodType.H1, "february":  PeriodType.H1,
    "mar": PeriodType.H1, "march":     PeriodType.H1,
    "apr": PeriodType.H1, "april":     PeriodType.H1,
    "may": PeriodType.H1,
    "jun": PeriodType.H1, "june":      PeriodType.H1,
    "jul": PeriodType.H2, "july":      PeriodType.H2,
    "aug": PeriodType.H2, "august":    PeriodType.H2,
    "sep": PeriodType.H2, "september": PeriodType.H2,
    "oct": PeriodType.H2, "october":   PeriodType.H2,
    "nov": PeriodType.H2, "november":  PeriodType.H2,
    "dec": PeriodType.H2, "december":  PeriodType.H2,
}

# Ordered month name alternation for use inside regex patterns.
_MONTH_ALT: Final[str] = (
    r"january|february|march|april|may|june|"
    r"july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
)

# ---------------------------------------------------------------------------
# Compiled patterns — ordered by specificity (most specific first)
# ---------------------------------------------------------------------------

# ── 1. Explicit quarter token ─────────────────────────────────────────────────
# Matches: "Q3", "Q1", "q4", "Q3FY26", "FY2026Q3"
_RE_EXPLICIT_Q: Final[re.Pattern[str]] = re.compile(
    r"\bq([1-4])\b",
    re.IGNORECASE,
)

# ── 2. Wall Street format: "1Q25", "3Q 2024" ─────────────────────────────────
_RE_WALL_STREET_Q: Final[re.Pattern[str]] = re.compile(
    r"\b([1-4])\s*q\b",
    re.IGNORECASE,
)

# ── 3. Explicit half-year token ───────────────────────────────────────────────
# Matches: "H1", "H2", "H1FY25"
_RE_EXPLICIT_H: Final[re.Pattern[str]] = re.compile(
    r"\bh([12])\b",
    re.IGNORECASE,
)

# ── 4. Ordinal quarter name ───────────────────────────────────────────────────
# Matches: "first quarter", "second quarter", "third quarter", "fourth quarter"
_RE_ORDINAL_Q: Final[re.Pattern[str]] = re.compile(
    r"\b(first|second|third|fourth)\s+quarter\b",
    re.IGNORECASE,
)
_ORDINAL_TO_Q: Final[dict[str, PeriodType]] = {
    "first":  PeriodType.Q1,
    "second": PeriodType.Q2,
    "third":  PeriodType.Q3,
    "fourth": PeriodType.Q4,
}

# ── 5. Ordinal half-year name ─────────────────────────────────────────────────
# Matches: "first half", "second half"
_RE_ORDINAL_H: Final[re.Pattern[str]] = re.compile(
    r"\b(first|second)\s+half\b",
    re.IGNORECASE,
)
_ORDINAL_TO_H: Final[dict[str, PeriodType]] = {
    "first":  PeriodType.H1,
    "second": PeriodType.H2,
}

# ── 6. "N months ended [month]" ───────────────────────────────────────────────
# Matches: "three months ended March 31", "six months ended June 30, 2024"
#          "nine months ended December 31, 2023"
# Named groups: months_word (three/six/nine/twelve), month_name
_RE_N_MONTHS_ENDED: Final[re.Pattern[str]] = re.compile(
    rf"\b(?P<months_word>three|six|nine|twelve)\s+months?\s+"
    rf"ended\s+(?P<month_name>{_MONTH_ALT})",
    re.IGNORECASE,
)
_MONTHS_WORD_TO_DURATION: Final[dict[str, int]] = {
    "three": 3, "six": 6, "nine": 9, "twelve": 12,
}

# ── 7. Annual / full-year markers ─────────────────────────────────────────────
# Matches: "annual", "full year", "year ended", "twelve months", "fy" (bare)
_RE_ANNUAL: Final[re.Pattern[str]] = re.compile(
    r"\b(?:annual(?:\s+report)?|full[- ]?year|year\s+ended|"
    r"twelve\s+months?\s+ended|twelve\s+months?|"
    r"fiscal\s+year|for\s+the\s+year)\b",
    re.IGNORECASE,
)

# ── 8. Bare "FY" token (only if no other period indicator found) ──────────────
_RE_BARE_FY: Final[re.Pattern[str]] = re.compile(
    r"\bfy\b",
    re.IGNORECASE,
)

# ── 9. Year extraction ────────────────────────────────────────────────────────
# Priority 1: 4-digit year (2000–2099 or 1900–1999)
_RE_YEAR_4: Final[re.Pattern[str]] = re.compile(
    r"\b((?:19|20)\d{2})\b",
)

# Priority 2: Indian "YYYY-YY" range (e.g. "2025-26") — take first year
_RE_YEAR_INDIA_RANGE: Final[re.Pattern[str]] = re.compile(
    r"\b((?:19|20)\d{2})-\d{2}\b",
)

# Priority 3: 2-digit year anchored after Q/H/FY prefix (e.g. "Q1FY25", "H2'24")
_RE_YEAR_2_ANCHORED: Final[re.Pattern[str]] = re.compile(
    r"\b(?:fy|q[1-4]|h[12])['’]?(\d{2})\b",
    re.IGNORECASE,
)

# Priority 4: standalone 2-digit year NOT preceded by day digits
# e.g. "1Q25" → year 25, but NOT "March 31" where 31 is a day
_RE_YEAR_2_STANDALONE: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9/])(?<!\d)\b(\d{2})\b(?![0-9/])",
)
# Disambiguation: 2-digit years 00–30 → 20xx; 31–99 → 19xx
# (Sane for modern financial filings. 1931 reports are unlikely inputs.)
_TWO_DIGIT_CUTOFF: Final[int] = 30


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_text(text: str) -> str:
    """
    NFKC-normalise and lower-case *text* for pattern matching.

    Does NOT strip punctuation — date separators ("March 31, 2025") and
    range separators ("2025-26") are meaningful for parsing.
    """
    return unicodedata.normalize("NFKC", text).lower().strip()


def _extract_year(normalised: str) -> int | None:
    """
    Extract the fiscal year integer from *normalised* text.

    Precedence:
      1. Indian range "YYYY-YY" — first year.
      2. 4-digit year.
      3. 2-digit year anchored to a period prefix (e.g. "fy25", "q1'24").
      4. Standalone 2-digit year (fallback, last resort).
    """
    # 1. Indian range e.g. "2025-26"
    m = _RE_YEAR_INDIA_RANGE.search(normalised)
    if m:
        return int(m.group(1))

    # 2. 4-digit year
    m = _RE_YEAR_4.search(normalised)
    if m:
        return int(m.group(1))

    # 3. Anchored 2-digit year (e.g. "q3fy24" → 2024)
    m = _RE_YEAR_2_ANCHORED.search(normalised)
    if m:
        yy = int(m.group(1))
        return 2000 + yy if yy <= _TWO_DIGIT_CUTOFF else 1900 + yy

    # 4. Standalone 2-digit year — lowest confidence
    #    Filter out obvious day numbers: anything ≤ 31 that appears near a
    #    month name is likely a day; apply a simple heuristic by checking
    #    that no month name sits within 10 characters of the digit.
    for m in _RE_YEAR_2_STANDALONE.finditer(normalised):
        raw_yy = int(m.group(1))
        if raw_yy < 1:           # "0" or leading zeros — not a year
            continue
        start = max(0, m.start() - 10)
        context_before = normalised[start: m.start()]
        # If a month name precedes this number, treat it as a day.
        month_pattern = re.compile(_MONTH_ALT, re.IGNORECASE)
        if month_pattern.search(context_before):
            continue
        return 2000 + raw_yy if raw_yy <= _TWO_DIGIT_CUTOFF else 1900 + raw_yy

    return None


def _extract_period_type(normalised: str) -> PeriodType | None:
    """
    Extract the period type from *normalised* text.

    Precedence (most specific → least specific):
      1. Explicit Q token: "q3", "q1", …
      2. Wall Street Q token: "1q", "3q", …
      3. Explicit H token: "h1", "h2"
      4. Ordinal quarter name: "third quarter"
      5. Ordinal half-year name: "second half"
      6. "N months ended [month]" — infer from duration + end month
      7. Annual keyword: "annual", "year ended", "twelve months ended", …
      8. Bare "fy" token (only if no other indicator found)
    """
    # 1. Explicit Q token
    m = _RE_EXPLICIT_Q.search(normalised)
    if m:
        return PeriodType(f"Q{m.group(1)}")

    # 2. Wall Street format "1Q" / "3Q"
    m = _RE_WALL_STREET_Q.search(normalised)
    if m:
        return PeriodType(f"Q{m.group(1)}")

    # 3. Explicit H token
    m = _RE_EXPLICIT_H.search(normalised)
    if m:
        return PeriodType(f"H{m.group(1)}")

    # 4. Ordinal quarter name
    m = _RE_ORDINAL_Q.search(normalised)
    if m:
        return _ORDINAL_TO_Q[m.group(1).lower()]

    # 5. Ordinal half-year name
    m = _RE_ORDINAL_H.search(normalised)
    if m:
        return _ORDINAL_TO_H[m.group(1).lower()]

    # 6. "N months ended [month]"
    m = _RE_N_MONTHS_ENDED.search(normalised)
    if m:
        duration = _MONTHS_WORD_TO_DURATION[m.group("months_word").lower()]
        month_name = m.group("month_name").lower()
        if duration == 12:
            return PeriodType.FY
        if duration == 3:
            return _MONTH_TO_CALENDAR_Q.get(month_name)
        if duration == 6:
            return _MONTH_TO_HALF.get(month_name)
        if duration == 9:
            # Nine months = YTD through Q3 — map to Q3 (closest scalar period).
            return PeriodType.Q3

    # 7. Annual keyword
    if _RE_ANNUAL.search(normalised):
        return PeriodType.FY

    # 8. Bare "fy" as last resort
    if _RE_BARE_FY.search(normalised):
        return PeriodType.FY

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_fiscal_period(text: str | None) -> NormalizedPeriod:
    """
    Parse *text* and return a :class:`NormalizedPeriod` with the fiscal year
    and period type tokens.

    This function NEVER raises.  Unrecognised or empty input produces a
    ``NormalizedPeriod`` with ``fiscal_year=None`` and ``period_type=None``.

    Integration with ``bulk_processor._derive_period_dates``::

        period = extract_fiscal_period("Three Months Ended March 31, 2025")
        if period.is_fully_resolved and period.period_type != PeriodType.H1
                and period.period_type != PeriodType.H2:
            start, end = _derive_period_dates(
                period.fiscal_year,
                period.period_type,   # PeriodType is a str-Enum; works directly
                "US_GAAP",
            )

    Args:
        text: Raw period label from a filing document, table header, or job
              metadata field.  May be ``None`` or empty.

    Returns:
        :class:`NormalizedPeriod` with resolved tokens (or ``None`` values
        for any token that could not be determined).

    Examples:
        >>> extract_fiscal_period("FY 2025")
        NormalizedPeriod(FY 2025, raw='FY 2025')

        >>> extract_fiscal_period("Three Months Ended March 31, 2025")
        NormalizedPeriod(Q1 2025, raw='Three Months Ended March 31, 2025')

        >>> extract_fiscal_period("1Q25")
        NormalizedPeriod(Q1 2025, raw='1Q25')

        >>> extract_fiscal_period("₹ in Crores Q3 FY2024-25")
        NormalizedPeriod(Q3 2024, raw='₹ in Crores Q3 FY2024-25')

        >>> extract_fiscal_period("Six Months Ended September 30, 2024")
        NormalizedPeriod(H2 2024, raw='Six Months Ended September 30, 2024')

        >>> extract_fiscal_period("Some Ambiguous Header")
        NormalizedPeriod(? ?, raw='Some Ambiguous Header')
    """
    raw_label = text or ""

    if not raw_label.strip():
        return NormalizedPeriod(
            fiscal_year=None,
            period_type=None,
            raw_label=raw_label,
        )

    normalised = _normalise_text(raw_label)

    fiscal_year = _extract_year(normalised)
    period_type = _extract_period_type(normalised)

    return NormalizedPeriod(
        fiscal_year=fiscal_year,
        period_type=period_type,
        raw_label=raw_label,
    )


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------

def extract_fiscal_periods(texts: list[str | None]) -> list[NormalizedPeriod]:
    """
    Apply :func:`extract_fiscal_period` to every element of *texts*.

    Convenience wrapper for callers processing a list of header strings.

    Args:
        texts: List of raw period label strings (may contain ``None``).

    Returns:
        List of :class:`NormalizedPeriod` instances, same length and order.
    """
    return [extract_fiscal_period(t) for t in texts]
