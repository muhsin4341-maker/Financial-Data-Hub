"""
Unit multiplier normaliser — M5.4.

Parses free-form financial table header strings and cell labels to extract
the numeric scaling factor implied by the unit context.

Covers:
  Western scales  — thousands (×1 000), millions (×1 000 000),
                    billions (×1 000 000 000), trillions (×1 000 000 000 000)
  Indian scales   — lakhs / lakh (×100 000), crores / crore (×10 000 000)
                    Required by the IND_AS framework (Amendment V1.2 §1).

Primary entry point
───────────────────
  extract_unit_multiplier(text: str) -> Decimal

  Parses *text* and returns the first recognised scale as an exact-integer
  Decimal.  Returns ``Decimal("1")`` (no scaling) if the text is empty,
  ``None``, or contains no recognisable scale keyword.

Design decisions
────────────────
Regex-based matching:
  A single compiled pattern covers all variants in one pass.  Each named
  group captures one scale family; the longest/most-specific alternatives
  within a group are listed first so the regex engine never matches a prefix
  of a longer keyword (e.g. "million" must not greedily consume "millions").

Normalisation before matching:
  The input is lower-cased and Unicode-NFKC-normalised before matching so
  that full-width digits, ligature characters, and mixed-case headers are
  handled without extra branches.  Currency symbols (₹ $ € £ ¥), commas,
  and parentheses are deliberately left in the string — they do not interfere
  with keyword matching and removing them could destroy context.

Decimal precision:
  All returned values are exact-integer Decimal literals constructed from
  string representations, not floats.  This guarantees that downstream
  multiplication (``parsed_value × multiplier``) remains within NUMERIC(38, 10)
  without floating-point error.  See extractor.py line 945 for the
  multiplication site.

No exceptions:
  ``extract_unit_multiplier`` is unconditionally safe — it returns
  ``Decimal("1")`` for any input that does not contain a recognised keyword,
  including empty strings, ``None``, and strings containing only noise.

Extensibility:
  Add a new scale family by:
    1. Appending a new ``(?P<family>term1|term2...)`` group to ``_UNIT_PATTERN``.
    2. Adding a corresponding ``"family": Decimal("...")`` entry to ``_SCALE_MAP``.
  No other changes are required.

Milestone: M5.4 — Unit Multiplier Normaliser
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Final

# ---------------------------------------------------------------------------
# Scale constants — exact-integer Decimal literals
# ---------------------------------------------------------------------------

_ONE: Final[Decimal]           = Decimal("1")
_THOUSAND: Final[Decimal]      = Decimal("1000")
_HUNDRED_THOUSAND: Final[Decimal] = Decimal("100000")      # 1 lakh
_MILLION: Final[Decimal]       = Decimal("1000000")
_TEN_MILLION: Final[Decimal]   = Decimal("10000000")       # 1 crore
_BILLION: Final[Decimal]       = Decimal("1000000000")
_TRILLION: Final[Decimal]      = Decimal("1000000000000")

# ---------------------------------------------------------------------------
# Compiled pattern and scale map
# ---------------------------------------------------------------------------
# Named groups follow the _SCALE_MAP keys exactly — the regex engine sets the
# group to a non-None string when it matches, giving us an O(1) dict lookup
# to retrieve the Decimal multiplier.
#
# Within each group, longer/more-specific alternatives precede shorter ones
# to prevent premature partial matches (e.g. "hundred thousands" before
# "hundred thousand" before "thousands" before "thousand").
#
# The pattern anchors each alternative with word-boundary assertions (\b) to
# avoid matching "millionaire" as "million" or "thousands-strong" partially.

_UNIT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""
    (?P<trillion>
        trillions?                          # "trillion" / "trillions"
        | \btr\b                            # abbreviation "tr"
    )
    |
    (?P<billion>
        billions?                           # "billion" / "billions"
        | \bbn\b                            # abbreviation "bn"
        | \bb\b                             # single letter "b" (isolated)
        | \(in\ billions?\)                 # "(in billion)" / "(in billions)"
        | amounts?\ in\ billions?
        | in\ billions?
    )
    |
    (?P<million>
        millions?                           # "million" / "millions"
        | \bmm\b                            # "MM" (institutional shorthand)
        | \bm\b                             # single letter "m" (isolated)
        | \(in\ millions?\)                 # "(in million)" / "(in millions)"
        | amounts?\ in\ millions?
        | in\ millions?
        | mn                                # "mn" shorthand
    )
    |
    (?P<crore>
        crores?                             # "crore" / "crores"
        | \bcr\b                            # abbreviation "cr"
        | in\ crores?
        | \(in\ crores?\)
        | amounts?\ in\ crores?
        | rs\.?\ in\ crores?               # "Rs. in crores" / "rs in crores"
        | ₹\ in\ crores?              # "₹ in crores"
    )
    |
    (?P<lakh>
        lakhs?                              # "lakh" / "lakhs"
        | lacs?                             # "lac" / "lacs" (alternate spelling)
        | \blk\b                            # abbreviation "lk"
        | in\ lakhs?
        | in\ lacs?
        | \(in\ lakhs?\)
        | \(in\ lacs?\)
        | amounts?\ in\ lakhs?
        | rs\.?\ in\ lakhs?                # "Rs. in lakhs"
        | ₹\ in\ lakhs?               # "₹ in lakhs"
    )
    |
    (?P<thousand>
        thousands?                          # "thousand" / "thousands"
        | \bk\b                             # single letter "k" (isolated)
        | \(in\ thousands?\)               # "(in thousand)" / "(in thousands)"
        | amounts?\ in\ thousands?
        | in\ thousands?
        | \bthsd\b                          # abbreviation "thsd"
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Maps regex group name → Decimal multiplier.
# Must stay in sync with the named groups in _UNIT_PATTERN.
_SCALE_MAP: Final[dict[str, Decimal]] = {
    "trillion": _TRILLION,
    "billion":  _BILLION,
    "million":  _MILLION,
    "crore":    _TEN_MILLION,
    "lakh":     _HUNDRED_THOUSAND,
    "thousand": _THOUSAND,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_unit_multiplier(text: str | None) -> Decimal:
    """
    Parse *text* and return the numeric scaling factor implied by the first
    recognised unit keyword.

    Common input forms handled:

    +---------------------------------------+-----------------------------+
    | Input string                          | Return value                |
    +=======================================+=============================+
    | ``"(In Millions of USD)"``            | ``Decimal("1000000")``      |
    | ``"₹ in Crores"``                     | ``Decimal("10000000")``     |
    | ``"Amounts in thousands"``            | ``Decimal("1000")``         |
    | ``"$ in Billions"``                   | ``Decimal("1000000000")``   |
    | ``"Rs. in Lakhs"``                    | ``Decimal("100000")``       |
    | ``"Revenue (MM)"``                    | ``Decimal("1000000")``      |
    | ``"Net Income (BN)"``                 | ``Decimal("1000000000")``   |
    | ``"All figures in K"``                | ``Decimal("1000")``         |
    | ``"Operating Profit"``                | ``Decimal("1")``            |
    | ``""``  /  ``None``                   | ``Decimal("1")``            |
    +---------------------------------------+-----------------------------+

    This function NEVER raises an exception.  Any input that cannot be
    matched returns the identity multiplier ``Decimal("1")``.

    Args:
        text: Free-form string from a table header, column label, or document
              note.  May include currency symbols, parentheses, and any
              Unicode characters.

    Returns:
        Decimal multiplier — one of:
          ``Decimal("1")``               — no scale / unknown
          ``Decimal("1000")``            — thousands
          ``Decimal("100000")``          — lakhs
          ``Decimal("1000000")``         — millions
          ``Decimal("10000000")``        — crores
          ``Decimal("1000000000")``      — billions
          ``Decimal("1000000000000")``   — trillions
    """
    if not text:
        return _ONE

    # Normalise: NFKC (handles full-width chars, ligatures) then lower-case.
    normalised = unicodedata.normalize("NFKC", text).lower()

    match = _UNIT_PATTERN.search(normalised)
    if match is None:
        return _ONE

    # Identify which named group matched and retrieve its multiplier.
    group_name = match.lastgroup
    if group_name is None:
        return _ONE  # defensive: pattern matched but no named group (shouldn't happen)

    return _SCALE_MAP.get(group_name, _ONE)


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def extract_unit_multipliers(texts: list[str | None]) -> list[Decimal]:
    """
    Apply ``extract_unit_multiplier`` to every element of *texts*.

    Convenience wrapper for callers that need to normalise a list of column
    headers in one call (e.g. a table with multiple currency-unit columns).

    Args:
        texts: List of raw header or label strings (may contain ``None``).

    Returns:
        List of Decimal multipliers, same length and order as *texts*.
    """
    return [extract_unit_multiplier(t) for t in texts]


# ---------------------------------------------------------------------------
# Introspection helpers (useful for tests and admin tooling)
# ---------------------------------------------------------------------------


def known_scales() -> dict[str, Decimal]:
    """
    Return a copy of the scale name → Decimal multiplier mapping.

    Useful for test assertions and documentation generation.

    Returns:
        Dict mapping scale group names to their multipliers, e.g.:
        ``{"trillion": Decimal("1000000000000"), "billion": Decimal("1000000000"), ...}``
    """
    return dict(_SCALE_MAP)
