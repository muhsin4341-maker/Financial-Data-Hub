"""
Company alias manager.

Maps well-known company name aliases and alternate ticker symbols to their
canonical ticker so that ``CompanyResolverService`` can resolve queries like
"Facebook" → "META" or "Google" → "GOOGL" without hitting the EDGAR API.

Design
------
The MVP implementation uses an in-memory alias map seeded from
``_BUILTIN_ALIASES`` below.  This covers the most common rebrandings and
alternate names that would otherwise produce a ``CompanyResolutionError``
despite the company being publicly traded under a different ticker.

Extension hook
--------------
``AliasManager`` accepts an optional ``extra_aliases`` dict at construction
time so callers can inject additional mappings from a database, config file,
or test fixture without subclassing.  A future migration can add a
``company_aliases`` table; the service layer would load rows at startup and
pass them as ``extra_aliases``.

Usage::

    manager = AliasManager()
    canonical = manager.resolve("Facebook")   # → "META"
    canonical = manager.resolve("GOOG")       # → "GOOGL"
    canonical = manager.resolve("AAPL")       # → "AAPL" (no alias needed)
    canonical = manager.resolve("unknown xyz") # → None

Alias lookup is case-insensitive and strips surrounding whitespace.

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Built-in alias table
# ---------------------------------------------------------------------------
# Keys   : lowercase alias (name or alternate ticker)
# Values : canonical uppercase ticker symbol recognised by SEC EDGAR
#
# Sources: historical SEC filings, Bloomberg, common investor shorthand.
# Keep entries conservative — only add when the alias causes real resolution
# failures in production.

_BUILTIN_ALIASES: dict[str, str] = {
    # ── Meta / Facebook ──────────────────────────────────────────────────────
    "facebook":              "META",
    "facebook inc":          "META",
    "meta platforms":        "META",
    "meta platforms inc":    "META",
    # ── Alphabet / Google ────────────────────────────────────────────────────
    "google":                "GOOGL",
    "google inc":            "GOOGL",
    "alphabet":              "GOOGL",
    "alphabet inc":          "GOOGL",
    "goog":                  "GOOGL",    # class-C shares → resolve to class-A
    # ── Twitter / X ──────────────────────────────────────────────────────────
    "twitter":               "TWTR",     # delisted 2022 — kept for historical queries
    "x corp":                "TWTR",
    # ── Berkshire Hathaway ───────────────────────────────────────────────────
    "berkshire":             "BRK.B",
    "berkshire hathaway":    "BRK.B",
    "brk":                   "BRK.B",
    "brk-a":                 "BRK.A",
    "brk-b":                 "BRK.B",
    # ── Standard aliases for common tickers ──────────────────────────────────
    "apple":                 "AAPL",
    "apple inc":             "AAPL",
    "microsoft":             "MSFT",
    "microsoft corp":        "MSFT",
    "amazon":                "AMZN",
    "amazon.com":            "AMZN",
    "amazon.com inc":        "AMZN",
    "tesla":                 "TSLA",
    "tesla inc":             "TSLA",
    "nvidia":                "NVDA",
    "nvidia corp":           "NVDA",
    "netflix":               "NFLX",
    "netflix inc":           "NFLX",
    "salesforce":            "CRM",
    "salesforce inc":        "CRM",
    # ── Financial sector ─────────────────────────────────────────────────────
    "jpmorgan":              "JPM",
    "jp morgan":             "JPM",
    "jpmorgan chase":        "JPM",
    "bank of america":       "BAC",
    "wells fargo":           "WFC",
    "goldman sachs":         "GS",
    "morgan stanley":        "MS",
    # ── Healthcare / Pharma ──────────────────────────────────────────────────
    "johnson & johnson":     "JNJ",
    "johnson and johnson":   "JNJ",
    "pfizer":                "PFE",
    "pfizer inc":            "PFE",
    "unitedhealth":          "UNH",
    "unitedhealth group":    "UNH",
    # ── Energy ───────────────────────────────────────────────────────────────
    "exxon":                 "XOM",
    "exxonmobil":            "XOM",
    "exxon mobil":           "XOM",
    "chevron":               "CVX",
    "chevron corp":          "CVX",
}


# ---------------------------------------------------------------------------
# AliasManager
# ---------------------------------------------------------------------------


class AliasManager:
    """
    Resolves company name aliases and alternate ticker symbols to canonical
    SEC EDGAR ticker symbols.

    Parameters
    ----------
    extra_aliases:
        Additional alias → ticker mappings to merge on top of the built-in
        table.  Caller-supplied entries take precedence over built-ins when
        there is a conflict, allowing corrections without modifying this file.
    """

    def __init__(
        self,
        extra_aliases: dict[str, str] | None = None,
    ) -> None:
        # Merge built-ins first, then extra (extra wins on conflict).
        self._aliases: dict[str, str] = {**_BUILTIN_ALIASES}
        if extra_aliases:
            # Normalise keys to lowercase for consistent lookup.
            for alias, canonical in extra_aliases.items():
                self._aliases[alias.strip().lower()] = canonical.strip().upper()

        log.debug(
            "alias_manager.initialised",
            builtin_count=len(_BUILTIN_ALIASES),
            extra_count=len(extra_aliases) if extra_aliases else 0,
            total_count=len(self._aliases),
        )

    def resolve(self, query: str) -> str | None:
        """
        Attempt to map ``query`` to a canonical ticker.

        Lookup is case-insensitive and strips surrounding whitespace.

        Args:
            query: Company name, alternate ticker, or display name.
                   Examples: "Facebook", "GOOG", "berkshire hathaway".

        Returns:
            Canonical uppercase ticker string on hit (e.g. ``"META"``).
            ``None`` when no alias is registered — the caller should then
            attempt normal SEC EDGAR resolution.
        """
        normalised = query.strip().lower()
        result = self._aliases.get(normalised)
        if result is not None:
            log.debug(
                "alias_manager.hit",
                query=query,
                canonical=result,
            )
        return result

    def register(self, alias: str, canonical: str) -> None:
        """
        Register a new alias at runtime.

        Useful for hot-loading new mappings from the database without
        restarting the service.

        Args:
            alias:     The alternate name or ticker to register (case-insensitive).
            canonical: The canonical SEC EDGAR ticker (stored uppercase).
        """
        key = alias.strip().lower()
        value = canonical.strip().upper()
        self._aliases[key] = value
        log.info(
            "alias_manager.registered",
            alias=alias,
            canonical=value,
        )

    def __len__(self) -> int:
        """Return total number of registered aliases."""
        return len(self._aliases)

    def __contains__(self, query: str) -> bool:
        """Support ``'facebook' in manager`` membership checks."""
        return query.strip().lower() in self._aliases
