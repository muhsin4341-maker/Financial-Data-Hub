"""
Currency Translation Engine — M5 Step 1: Dual-Pass Global FX Multipliers.

This module is the production-grade implementation of the Amendment V1.2
Section 1.3 / Section 3 currency translation requirements.  It provides:

  1. HistoricalFXRateProvider — concrete FXRateProvider (see protocol in
     services/extraction/normaliser/currency.py) with:
       - 5-day look-back fallback for weekends / bank holidays / data gaps.
       - Arithmetic weighted period-average computation for IS and CF items.
       - All coefficients held and returned as Python Decimal at NUMERIC(38,10).

  2. CurrencyTranslationEngine — the single user-facing entry point that
     wires HistoricalFXRateProvider into CurrencyNormaliser and exposes a
     clean `translate()` call.

Architecture (Amendment V1.2 §1.3 / §3):

  The dual-pass requirement prohibits applying a single flat rate to all
  statement types (BANNED: value_usd = value_reported / flat_rate).

  Pass 1 — Balance Sheet (statement_type = 'BS'):
    Apply the closing SPOT rate on ``period_end_date``.
    Economic rationale: balance sheet items are stock values as-of a point
    in time.  The closing rate on that date is the legally required measure
    under ASC 830-30-45, IAS 21.23(a), and Ind AS 21.23(a).

  Pass 2 — Income Statement / Cash Flow Statement (statement_type = 'IS' | 'CF'):
    Apply the ARITHMETIC WEIGHTED PERIOD AVERAGE rate.
    The average is computed as the simple arithmetic mean of the resolved
    closing rate for every calendar day in [period_start_date, period_end_date].
    Economic rationale: IS/CF items represent flows accumulated over the
    period; the average rate is the correct temporal equivalent under
    ASC 830-30-45, IAS 21.22, and Ind AS 21.22.

  The engine is deliberately stateless beyond its injected FXRateRepository.
  No module-level caches are held; caching (Redis / in-process LRU) is the
  responsibility of the FXRateRepository implementation.

Precision contract (Amendment V1.2 §1.1):
  All intermediate calculations use unbounded Python Decimal arithmetic.
  Final fx_rate_used values are quantised to NUMERIC(38,10) via
  Decimal.quantize with ROUND_HALF_EVEN at the point of return.
  Final value_usd values are quantised to NUMERIC(26,2).
  No float() operations are used anywhere in this module.

Look-back algorithm (Amendment V1.2 §1.3 fallback specification):
  For every calendar date d on which a rate is required:
    1. Look up d in the fetched rate map.
    2. If absent, try d−1, d−2, ..., d−5 (five calendar-day look-back).
    3. If still absent → raise MissingFXRateException with the problematic
       date, currency, and context (spot / weighted-average).
  A single DB range query fetching [target−5, target] (spot) or
  [period_start−5, period_end] (weighted average) is used so the look-back
  requires no additional DB round-trips.

Relationship to existing skeleton:
  services/extraction/normaliser/currency.py — defines:
    FXRateProvider (Protocol), FXRateLookupError, TranslationResult,
    CurrencyNormaliser (routing logic).
  This module — provides:
    MissingFXRateException, DailyRate, FXRateRepository (Protocol),
    HistoricalFXRateProvider (concrete), CurrencyTranslationEngine (facade).

Milestone: M5 Step 1 — Dual-Pass Global FX Multipliers
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Protocol, runtime_checkable

import structlog

# Import protocol + routing layer from the existing normaliser skeleton.
# These types are NOT redefined here; callers may import from either module.
from services.extraction.normaliser.currency import (
    CurrencyNormaliser,
    FXRateLookupError,
    TranslationResult,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Precision constants (Amendment V1.2 §1.1)
# ---------------------------------------------------------------------------

# NUMERIC(38,10) — FX coefficients, spot rates, period averages.
_FX_RATE_SCALE: Decimal = Decimal("0.0000000001")

# NUMERIC(26,2) — translated monetary value stored in value_usd.
_MONETARY_SCALE: Decimal = Decimal("0.01")

# Maximum calendar-day look-back for missing rates (Amendment V1.2 §1.3).
_MAX_LOOKBACK_DAYS: int = 5

# Statement type constants (must match FinancialLineItem.statement_type).
_BS = "BS"
_IS = "IS"
_CF = "CF"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MissingFXRateException(FXRateLookupError):
    """
    Raised when no historical FX rate can be found within the maximum
    look-back window (Amendment V1.2 §1.3 fallback specification).

    This exception is distinct from the generic FXRateLookupError (network /
    provider failure) — it specifically indicates a structural gap in the
    historical rate data that exceeds the tolerated look-back range.

    Attributes:
        currency:    ISO 4217 currency code for which the rate is missing.
        target_date: The calendar date that could not be resolved.
        context:     Human-readable context string ('spot' or 'weighted_average').
        lookback_days: The number of days searched before giving up.
    """

    def __init__(
        self,
        currency: str,
        target_date: date,
        context: str,
        lookback_days: int = _MAX_LOOKBACK_DAYS,
    ) -> None:
        self.currency = currency
        self.target_date = target_date
        self.context = context
        self.lookback_days = lookback_days
        super().__init__(
            f"No historical FX rate for {currency!r} on {target_date} "
            f"(context={context!r}). "
            f"Looked back {lookback_days} calendar days — no market-active "
            f"closing rate found. "
            f"Possible causes: extended market closure, data provider gap, "
            f"or currency not covered by the rate store."
        )


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyRate:
    """
    A single historical daily closing exchange rate record.

    Attributes:
        currency:  ISO 4217 currency code (e.g. 'EUR', 'INR', 'JPY').
        on_date:   The calendar date this closing rate applies to.
        rate:      Foreign currency units per 1 USD.
                   Precision: NUMERIC(38,10) — stored as Decimal.
                   Conversion: value_usd = value_reported / rate.
        source:    Rate data source identifier (e.g. 'ecb', 'fed', 'rbi',
                   'bis') for audit purposes (Amendment V1.2 §4.2).
    """

    currency: str
    on_date: date
    rate: Decimal
    source: str = "unknown"


# ---------------------------------------------------------------------------
# FX rate repository protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FXRateRepository(Protocol):
    """
    Protocol for raw historical rate storage access.

    Implementations may be backed by:
      - A local PostgreSQL fx_daily_rates table (primary).
      - An external FX data API (ECB, Fed, RBI, BIS) with circuit breaker.
      - A Redis or in-process LRU cache layered over either of the above.

    Rate convention (consistent with FXRateProvider):
      rate = foreign_currency_units_per_1_USD
      value_usd = value_reported / rate

    All Decimal values returned must be non-zero positive numbers.  Zero
    rates are treated as data errors by the consumer (HistoricalFXRateProvider).
    """

    async def get_rates_in_range(
        self,
        currency: str,
        start: date,
        end: date,
    ) -> list[DailyRate]:
        """
        Fetch all available daily closing rates for ``currency`` in
        [``start``, ``end``] (both endpoints inclusive).

        Only days for which the market was active and a rate record exists
        are returned — the caller is responsible for resolving gaps via the
        look-back algorithm.

        Args:
            currency: ISO 4217 currency code.
            start:    Inclusive start date of the query window.
            end:      Inclusive end date of the query window.

        Returns:
            List of DailyRate objects, ordered by on_date ascending.
            May be empty if no rates exist for the window.

        Raises:
            FXRateLookupError: On unrecoverable storage / network failure.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Concrete FX rate provider — full look-back + weighted average logic
# ---------------------------------------------------------------------------


class HistoricalFXRateProvider:
    """
    Concrete implementation of the FXRateProvider protocol backed by a
    FXRateRepository (historical daily rate store).

    Implements Amendment V1.2 §1.3 look-back fallback and the arithmetic
    weighted period-average required for IS/CF statement translation.

    Look-back strategy:
      A single range query fetching [target−5, target] (for spot) or
      [period_start−5, period_end] (for weighted average) is issued so that
      all gap resolution is performed in-process without extra DB round-trips.

    Precision:
      All intermediate computations use unbounded Decimal arithmetic.
      Output values are quantised to NUMERIC(38,10) at the moment of return.

    Args:
        repo: FXRateRepository implementation (DB, API, or cache-layered).
    """

    def __init__(self, repo: FXRateRepository) -> None:
        self._repo = repo

    # ── Pass 1: Balance Sheet — spot rate on period_end_date ───────────────────

    async def get_spot_rate(self, currency: str, on_date: date) -> Decimal:
        """
        Return the spot closing rate for ``currency`` on ``on_date``.

        If no rate exists on ``on_date`` (weekend / bank holiday / data gap),
        look back up to _MAX_LOOKBACK_DAYS (5) calendar days for the nearest
        prior market-active closing rate.

        Amendment V1.2 §1.3: if the gap exceeds 5 days, raise
        MissingFXRateException.

        Args:
            currency: ISO 4217 currency code.
            on_date:  Target date for the spot rate (Balance Sheet period_end_date).

        Returns:
            Spot rate as Decimal, quantised to NUMERIC(38,10).

        Raises:
            MissingFXRateException: No rate found within the 5-day window.
            FXRateLookupError:      Storage / provider failure.
        """
        # Short-circuit: USD is the target currency — rate is exactly 1.
        if currency.upper() == "USD":
            return Decimal("1").quantize(_FX_RATE_SCALE, rounding=ROUND_HALF_EVEN)

        fetch_start = on_date - timedelta(days=_MAX_LOOKBACK_DAYS)
        daily_rates = await self._repo.get_rates_in_range(currency, fetch_start, on_date)
        rate_map = _build_rate_map(daily_rates)

        rate = _resolve_with_lookback(rate_map, on_date, _MAX_LOOKBACK_DAYS)
        if rate is None:
            raise MissingFXRateException(
                currency=currency,
                target_date=on_date,
                context="spot",
                lookback_days=_MAX_LOOKBACK_DAYS,
            )

        _assert_nonzero_rate(rate, currency, on_date, "spot")

        quantised = rate.quantize(_FX_RATE_SCALE, rounding=ROUND_HALF_EVEN)
        log.debug(
            "fx.spot_rate_resolved",
            currency=currency,
            on_date=str(on_date),
            rate=str(quantised),
        )
        return quantised

    # ── Pass 2: IS / CF — arithmetic weighted period average ──────────────────

    async def get_weighted_average_rate(
        self,
        currency: str,
        period_start: date,
        period_end: date,
    ) -> Decimal:
        """
        Compute the arithmetic weighted period average rate for ``currency``
        over [``period_start``, ``period_end``] (inclusive, both endpoints).

        Algorithm (Amendment V1.2 §1.3):
          1. Fetch all available daily closing rates in
             [period_start − 5, period_end] in a single DB query.
             The extra 5-day prefix guarantees that look-back resolution for
             the first day(s) of the period succeeds without a second query.
          2. For every calendar day d in [period_start, period_end]:
               a. Look up d in the rate map.
               b. If absent, try d−1, d−2, ..., d−5 (look-back).
               c. If still absent → raise MissingFXRateException(d).
          3. Compute the arithmetic mean:
               avg = sum(resolved_daily_rates) / number_of_calendar_days
          4. Quantise avg to NUMERIC(38,10) via ROUND_HALF_EVEN.

        Rationale for per-calendar-day resolution:
          Each calendar day is assigned the closing rate of the most recent
          prior trading day.  The resulting per-day weight is exactly
          proportional to the number of calendar days each trading day's rate
          is "in force".  This is the mathematically rigorous interpretation
          of the ASC 830 / IAS 21 "average rate for the period" requirement.

        Args:
            currency:      ISO 4217 currency code.
            period_start:  Inclusive start of the reporting period.
            period_end:    Inclusive end of the reporting period.

        Returns:
            Arithmetic weighted average rate as Decimal, NUMERIC(38,10).

        Raises:
            ValueError:             period_start > period_end.
            MissingFXRateException: A calendar day in the period has no rate
                                    within the 5-day look-back window.
            FXRateLookupError:      Storage / provider failure.
        """
        if period_start > period_end:
            raise ValueError(
                f"period_start ({period_start}) must not be after "
                f"period_end ({period_end}) for weighted average computation."
            )

        # Short-circuit: USD reporting currency — average is exactly 1.
        if currency.upper() == "USD":
            return Decimal("1").quantize(_FX_RATE_SCALE, rounding=ROUND_HALF_EVEN)

        # Fetch [period_start − 5, period_end] in one query so look-back for
        # the opening days of the period never requires a second DB round-trip.
        fetch_start = period_start - timedelta(days=_MAX_LOOKBACK_DAYS)
        daily_rates = await self._repo.get_rates_in_range(currency, fetch_start, period_end)
        rate_map = _build_rate_map(daily_rates)

        # Resolve one rate per calendar day in [period_start, period_end].
        resolved: list[Decimal] = []
        current = period_start
        while current <= period_end:
            rate = _resolve_with_lookback(rate_map, current, _MAX_LOOKBACK_DAYS)
            if rate is None:
                raise MissingFXRateException(
                    currency=currency,
                    target_date=current,
                    context="weighted_average",
                    lookback_days=_MAX_LOOKBACK_DAYS,
                )
            _assert_nonzero_rate(rate, currency, current, "weighted_average")
            resolved.append(rate)
            current += timedelta(days=1)

        # Arithmetic mean using unbounded Decimal precision.
        # Only quantise the final coefficient, not intermediate sums.
        n = len(resolved)
        total: Decimal = sum(resolved, Decimal(0))
        avg = total / Decimal(n)

        quantised = avg.quantize(_FX_RATE_SCALE, rounding=ROUND_HALF_EVEN)
        log.debug(
            "fx.weighted_average_resolved",
            currency=currency,
            period_start=str(period_start),
            period_end=str(period_end),
            calendar_days=n,
            trading_days_with_data=len(rate_map),
            average_rate=str(quantised),
        )
        return quantised


# ---------------------------------------------------------------------------
# Translation engine facade
# ---------------------------------------------------------------------------


class CurrencyTranslationEngine:
    """
    Production-grade dual-pass currency translation engine.

    Wires HistoricalFXRateProvider (look-back + weighted average) into
    CurrencyNormaliser (dual-pass routing) to form a single user-facing
    entry point.

    Usage::

        engine = CurrencyTranslationEngine(repo=my_fx_repo)
        result = await engine.translate(
            value_reported=Decimal("1000000"),
            currency="INR",
            statement_type="IS",
            period_start_date=date(2024, 4, 1),
            period_end_date=date(2025, 3, 31),
        )
        # result.value_usd  → NUMERIC(26,2)
        # result.fx_rate_used → NUMERIC(38,10)
        # result.rate_type  → 'weighted_average'

    Sign convention (Amendment V1.2 §2.2):
      Sign inversion (×−1) for outflow items MUST be applied BEFORE calling
      this engine.  The engine translates magnitudes; it does not alter signs.

    Args:
        repo: FXRateRepository implementation providing daily closing rates.
    """

    def __init__(self, repo: FXRateRepository) -> None:
        provider = HistoricalFXRateProvider(repo)
        self._normaliser = CurrencyNormaliser(provider)  # type: ignore[arg-type]

    async def translate(
        self,
        *,
        value_reported: Decimal,
        currency: str,
        statement_type: str,
        period_end_date: date,
        period_start_date: date | None = None,
    ) -> TranslationResult:
        """
        Translate ``value_reported`` to USD using the Amendment V1.2 §3
        dual-pass strategy.

        Pass 1 (BS): spot rate on ``period_end_date``.
        Pass 2 (IS / CF): arithmetic weighted average over
                          [``period_start_date``, ``period_end_date``].

        Args:
            value_reported:    Source value in the reported currency.
                               Must be a Python Decimal — float not accepted.
            currency:          ISO 4217 three-letter currency code.
            statement_type:    'BS' | 'IS' | 'CF' (case-insensitive).
            period_end_date:   Closing date of the reporting period.
            period_start_date: Opening date. Required for IS and CF;
                               ignored (and therefore optional) for BS.

        Returns:
            TranslationResult:
              value_usd      — NUMERIC(26,2) translated amount.
              fx_rate_used   — NUMERIC(38,10) coefficient applied.
              rate_type      — 'spot' | 'weighted_average' | 'identity'.
              currency       — ISO 4217 source currency.

        Raises:
            ValueError:             Unknown statement_type or missing
                                    period_start_date for IS/CF.
            MissingFXRateException: Rate gap exceeds 5-day look-back window.
            FXRateLookupError:      Storage / provider failure.
        """
        if not isinstance(value_reported, Decimal):
            raise TypeError(
                f"value_reported must be a Python Decimal, "
                f"got {type(value_reported).__name__!r}. "
                f"Float inputs are prohibited (Amendment V1.2 §1.1)."
            )

        return await self._normaliser.translate(
            value_reported=value_reported,
            currency=currency,
            statement_type=statement_type,
            period_end_date=period_end_date,
            period_start_date=period_start_date,
        )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _build_rate_map(daily_rates: list[DailyRate]) -> dict[date, Decimal]:
    """
    Convert a list of DailyRate records into a {date: rate} dict.

    When multiple records exist for the same date (should not happen in a
    well-maintained rate store), the record with the highest rate precision
    (last in list, assuming the caller orders by data-source priority) wins.
    """
    return {dr.on_date: dr.rate for dr in daily_rates}


def _resolve_with_lookback(
    rate_map: dict[date, Decimal],
    target: date,
    max_lookback: int,
) -> Decimal | None:
    """
    Find the most recent closing rate on or before ``target`` within
    ``max_lookback`` calendar days.

    Iterates delta = 0, 1, 2, ..., max_lookback and returns the first
    (most recent) rate found.  Returns None if no rate exists in the window.

    The check for delta=0 means an exact-date match is always preferred over
    a look-back result, which is the correct behaviour for spot rates.

    Args:
        rate_map:     Dict of {date: rate} from _build_rate_map.
        target:       The calendar date for which a rate is needed.
        max_lookback: Maximum number of calendar days to search backwards.

    Returns:
        The most recent available rate, or None if the window is empty.
    """
    for delta in range(max_lookback + 1):  # 0 … max_lookback inclusive
        candidate = target - timedelta(days=delta)
        if candidate in rate_map:
            return rate_map[candidate]
    return None


def _assert_nonzero_rate(
    rate: Decimal,
    currency: str,
    on_date: date,
    context: str,
) -> None:
    """
    Guard against zero or negative rates that would invert or nullify values.

    A zero rate returned by the repository is a data integrity error, not a
    legitimate market condition.  A negative rate is physically meaningless.

    Raises:
        FXRateLookupError: If rate is ≤ 0.
    """
    if rate <= Decimal(0):
        raise FXRateLookupError(
            f"FX rate for {currency!r} on {on_date} (context={context!r}) "
            f"is {rate} — non-positive rates are data errors and cannot be "
            f"used for translation."
        )
