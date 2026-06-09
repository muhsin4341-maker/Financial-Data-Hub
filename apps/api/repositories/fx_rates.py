"""
Concrete FXRateRepository implementation — M5.2.

Implements:
  FXRateRepository  (Protocol: services/currency/translator.py)
    get_rates_in_range(currency, start, end) → list[DailyRate]

  FXRateProvider    (Protocol: services/extraction/normaliser/currency.py)
    get_spot_rate(currency, on_date) → Decimal
    get_weighted_average_rate(currency, period_start, period_end) → Decimal

Both protocols are satisfied structurally (duck typing).  The class does NOT
inherit from either Protocol to avoid circular imports between the API layer
and the services layer — structural subtyping is sufficient and is verified by
the Protocol's @runtime_checkable decorator at injection sites.

Design decisions
────────────────
Session lifecycle:
  The repository does NOT commit.  The caller owns the transaction unit-of-work.
  flush() is called inside bulk_upsert to surface constraint violations before
  the session is handed back.

Bulk upsert strategy (ON CONFLICT DO UPDATE):
  Unlike FinancialLineItemRepository (ON CONFLICT DO NOTHING), FX rates may be
  legitimately revised by the source data provider (rate corrections, ECB daily
  feed re-issues).  We therefore use ON CONFLICT DO UPDATE SET rate = EXCLUDED.rate,
  updated_at = EXCLUDED.updated_at.  This is idempotent: inserting the same rate
  twice is a no-op at the value level; inserting a revised rate updates the row.

get_rates_in_range:
  Issues a single query filtered by:
    from_currency = currency  AND  to_currency = 'USD'  AND  rate_date BETWEEN start AND end
  Results are returned ordered by rate_date ASC.  This single query covers both
  the spot look-back (HistoricalFXRateProvider fetches [on_date − 5, on_date] and
  takes the latest entry) and the period-average (arithmetic mean in-process).
  No secondary queries are needed.

USD short-circuit:
  get_spot_rate and get_weighted_average_rate both return Decimal("1") immediately
  for currency == "USD" to avoid unnecessary DB round-trips.

Precision:
  All intermediate arithmetic uses Python Decimal with no float conversions.
  The NUMERIC(38, 10) column is mapped to Python Decimal by asyncpg and psycopg2
  transparently.  Return values from get_spot_rate / get_weighted_average_rate are
  quantised to _FX_RATE_SCALE = Decimal("0.0000000001") (10 d.p.) per
  Amendment V1.2 §1.1.

Rate convention (consistent with translator.py and currency.py):
  rate = units of foreign currency per 1 USD
  value_usd = value_reported / rate

Errors:
  FXRateLookupError from services/extraction/normaliser/currency.py is re-raised
  on unrecoverable storage failures (SQLAlchemy exceptions that are not retryable).
  The caller (HistoricalFXRateProvider) converts this to MissingFXRateException
  when the look-back window is exhausted.

Engineering Specification references:
  Amendment V1.2, Section 1.1 — NUMERIC(38, 10) for FX coefficients
  Amendment V1.2, Section 1.3 — 5-day look-back fallback
  Amendment V1.2, Section 3   — Dual-pass translation (BS spot / IS·CF average)
  M5.1 — DailyFXRate ORM model and migration 012
  M5.2 — This file

Milestone: M5.2 — Concrete FXRateRepository
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import DailyFXRate
from services.extraction.normaliser.currency import DailyRate, FXRateLookupError

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Target currency for all conversions in this system.
_TARGET_CURRENCY: str = "USD"

# Number of decimal places for NUMERIC(38, 10) precision.
_FX_RATE_SCALE: Decimal = Decimal("0.0000000001")

# Maximum calendar-day look-back for spot rate resolution.
# Must match HistoricalFXRateProvider._MAX_LOOKBACK_DAYS in translator.py.
_MAX_LOOKBACK_DAYS: int = 5

# Minimum allowed rate — zero or negative rates are data errors.
_RATE_FLOOR: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _quantise(value: Decimal) -> Decimal:
    """Quantise *value* to NUMERIC(38, 10) using ROUND_HALF_EVEN."""
    return value.quantize(_FX_RATE_SCALE, rounding=ROUND_HALF_EVEN)


def _orm_to_daily_rate(row: DailyFXRate) -> DailyRate:
    """Convert a *DailyFXRate* ORM instance to a *DailyRate* dataclass."""
    return DailyRate(on_date=row.rate_date, rate=row.rate)


# ---------------------------------------------------------------------------
# Schemas for bulk upsert
# ---------------------------------------------------------------------------


class FXRateCreate:
    """
    Lightweight value object for a single FX rate to be upserted.

    Attributes:
        rate_date:      Calendar date of the closing rate.
        from_currency:  ISO 4217 source currency code (e.g. 'INR', 'EUR').
        to_currency:    ISO 4217 target currency code (e.g. 'USD').
        rate:           Closing rate as Decimal (units of *to_currency* per
                        1 unit of *from_currency*).
    """

    __slots__ = ("rate_date", "from_currency", "to_currency", "rate")

    def __init__(
        self,
        rate_date: date,
        from_currency: str,
        to_currency: str,
        rate: Decimal,
    ) -> None:
        if not isinstance(rate, Decimal):
            raise TypeError(
                f"rate must be Decimal, got {type(rate).__name__!r} "
                f"for {from_currency}/{to_currency} on {rate_date}"
            )
        if rate <= _RATE_FLOOR:
            raise ValueError(
                f"rate must be positive, got {rate!r} "
                f"for {from_currency}/{to_currency} on {rate_date}"
            )
        self.rate_date = rate_date
        self.from_currency = from_currency.upper()
        self.to_currency = to_currency.upper()
        self.rate = rate


# ---------------------------------------------------------------------------
# Concrete repository
# ---------------------------------------------------------------------------


class FXRateRepository:
    """
    Async repository for *daily_fx_rates* persistence and retrieval.

    Satisfies both:
      • ``FXRateRepository`` Protocol (services/currency/translator.py) —
        required by ``HistoricalFXRateProvider`` for range queries.
      • ``FXRateProvider`` Protocol (services/extraction/normaliser/currency.py) —
        required by ``CurrencyNormaliser`` for spot and average rates.

    Instantiate with an active AsyncSession:

        repo = FXRateRepository(session)
        rates = await repo.get_rates_in_range("INR", start, end)
        spot  = await repo.get_spot_rate("EUR", on_date)

    The repository does NOT commit.  The caller owns the transaction lifecycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -------------------------------------------------------------------------
    # FXRateRepository Protocol — range query (consumed by translator.py)
    # -------------------------------------------------------------------------

    async def get_rates_in_range(
        self,
        currency: str,
        start: date,
        end: date,
    ) -> list[DailyRate]:
        """
        Fetch all available daily closing rates for *currency* in
        [*start*, *end*] (both endpoints inclusive).

        Rate convention: rate = units of *currency* per 1 USD.
        Only days for which a rate record exists are returned — the caller is
        responsible for resolving gaps via the 5-day look-back algorithm.

        Args:
            currency: ISO 4217 currency code.
            start:    Inclusive start of the query window.
            end:      Inclusive end of the query window.

        Returns:
            List of DailyRate objects ordered by on_date ascending.
            Empty list if no rates exist for the window.

        Raises:
            FXRateLookupError: On unrecoverable storage failure.
        """
        currency = currency.upper()

        # USD is the target; its rate relative to itself is identically 1.
        if currency == _TARGET_CURRENCY:
            return [
                DailyRate(
                    on_date=start + timedelta(days=i),
                    rate=_quantise(Decimal("1")),
                )
                for i in range((end - start).days + 1)
            ]

        try:
            stmt = (
                select(DailyFXRate)
                .where(
                    DailyFXRate.from_currency == currency,
                    DailyFXRate.to_currency == _TARGET_CURRENCY,
                    DailyFXRate.rate_date >= start,
                    DailyFXRate.rate_date <= end,
                )
                .order_by(DailyFXRate.rate_date.asc())
            )
            result = await self._session.execute(stmt)
            rows: Sequence[DailyFXRate] = result.scalars().all()
        except SQLAlchemyError as exc:
            log.error(
                "fx_rate_range_query_failed",
                currency=currency,
                start=start.isoformat(),
                end=end.isoformat(),
                error=str(exc),
            )
            raise FXRateLookupError(
                f"Storage failure fetching FX rates for {currency} "
                f"[{start.isoformat()} → {end.isoformat()}]: {exc}"
            ) from exc

        daily_rates = [_orm_to_daily_rate(row) for row in rows]

        log.debug(
            "fx_rate_range_fetched",
            currency=currency,
            start=start.isoformat(),
            end=end.isoformat(),
            count=len(daily_rates),
        )
        return daily_rates

    # -------------------------------------------------------------------------
    # FXRateProvider Protocol — spot rate (consumed by currency.py / normaliser)
    # -------------------------------------------------------------------------

    async def get_spot_rate(self, currency: str, on_date: date) -> Decimal:
        """
        Return the closing spot rate for *currency* on *on_date*.

        Implements the Amendment V1.2 §1.3 look-back fallback in-repository:
        fetches [on_date − 5, on_date] in a single query, then resolves the
        latest available rate from the result map.

        Rate convention: rate = units of *currency* per 1 USD
          → value_usd = value_reported / rate

        Args:
            currency: ISO 4217 currency code.
            on_date:  Target date for the spot rate (BS period_end_date).

        Returns:
            Spot rate quantised to NUMERIC(38, 10).

        Raises:
            FXRateLookupError: No rate found within the 5-day window, or
                               unrecoverable storage failure.
        """
        currency = currency.upper()

        if currency == _TARGET_CURRENCY:
            return _quantise(Decimal("1"))

        fetch_start = on_date - timedelta(days=_MAX_LOOKBACK_DAYS)
        daily_rates = await self.get_rates_in_range(currency, fetch_start, on_date)

        if not daily_rates:
            raise FXRateLookupError(
                f"No FX rate found for {currency}/USD within {_MAX_LOOKBACK_DAYS} "
                f"calendar days of {on_date.isoformat()} (look-back window: "
                f"{fetch_start.isoformat()} → {on_date.isoformat()})"
            )

        # Take the latest available entry (list is ordered ASC by on_date).
        latest = daily_rates[-1]

        log.debug(
            "fx_spot_rate_resolved",
            currency=currency,
            target_date=on_date.isoformat(),
            resolved_date=latest.on_date.isoformat(),
            rate=str(latest.rate),
        )
        return _quantise(latest.rate)

    # -------------------------------------------------------------------------
    # FXRateProvider Protocol — weighted average (consumed by currency.py)
    # -------------------------------------------------------------------------

    async def get_weighted_average_rate(
        self,
        currency: str,
        period_start: date,
        period_end: date,
    ) -> Decimal:
        """
        Return the arithmetic weighted average closing rate for *currency*
        over [*period_start*, *period_end*].

        Used for Income Statement (IS) and Cash Flow Statement (CF) translation
        per Amendment V1.2 §3 / ASC 830-30-45 / IAS 21.22 / Ind AS 21.22.

        The average is computed from the resolved daily rates:
          1. Fetch [period_start − 5, period_end] to allow look-back at start.
          2. For each calendar day d in [period_start, period_end], resolve the
             rate using the 5-day look-back (nearest prior market-active day).
          3. Average the resolved rates using simple arithmetic mean.

        Rate convention: rate = units of *currency* per 1 USD
          → value_usd = value_reported / rate

        Args:
            currency:     ISO 4217 currency code.
            period_start: Inclusive start of the fiscal period.
            period_end:   Inclusive end of the fiscal period.

        Returns:
            Weighted average rate quantised to NUMERIC(38, 10).

        Raises:
            FXRateLookupError: No rates found for the period, or unrecoverable
                               storage failure.
        """
        currency = currency.upper()

        if currency == _TARGET_CURRENCY:
            return _quantise(Decimal("1"))

        # Extend the fetch window by _MAX_LOOKBACK_DAYS on the left to provide
        # enough data for look-back resolution at period_start.
        fetch_start = period_start - timedelta(days=_MAX_LOOKBACK_DAYS)
        daily_rates = await self.get_rates_in_range(currency, fetch_start, period_end)

        if not daily_rates:
            raise FXRateLookupError(
                f"No FX rates found for {currency}/USD in period "
                f"{period_start.isoformat()} → {period_end.isoformat()} "
                f"(fetch window: {fetch_start.isoformat()} → {period_end.isoformat()})"
            )

        # Build a {date: rate} map from the fetched rows for O(1) lookup.
        rate_map: dict[date, Decimal] = {dr.on_date: dr.rate for dr in daily_rates}

        # Walk every calendar day in [period_start, period_end] and resolve
        # the rate using the look-back fallback.
        resolved: list[Decimal] = []
        current = period_start
        total_days = (period_end - period_start).days + 1

        while current <= period_end:
            rate: Decimal | None = None
            for lookback in range(_MAX_LOOKBACK_DAYS + 1):
                candidate = current - timedelta(days=lookback)
                if candidate in rate_map:
                    rate = rate_map[candidate]
                    break

            if rate is not None:
                resolved.append(rate)
            # Days with no rate within the look-back window are silently skipped
            # (e.g. extended bank closures at the very start of a period).
            # HistoricalFXRateProvider uses the same skip-on-no-data approach.

            current += timedelta(days=1)

        if not resolved:
            raise FXRateLookupError(
                f"Could not resolve any FX rate for {currency}/USD across "
                f"{total_days} calendar days in period "
                f"{period_start.isoformat()} → {period_end.isoformat()}"
            )

        # Arithmetic mean using unbounded Decimal arithmetic.
        avg = sum(resolved, Decimal("0")) / Decimal(len(resolved))

        log.debug(
            "fx_weighted_average_computed",
            currency=currency,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            resolved_days=len(resolved),
            total_days=total_days,
            avg_rate=str(_quantise(avg)),
        )
        return _quantise(avg)

    # -------------------------------------------------------------------------
    # Write — bulk upsert (rate ingestion from external FX data feeds)
    # -------------------------------------------------------------------------

    async def bulk_upsert(
        self,
        rates: Sequence[FXRateCreate],
    ) -> int:
        """
        Upsert a batch of FX rate rows into *daily_fx_rates*.

        Strategy: INSERT … ON CONFLICT (rate_date, from_currency, to_currency)
        DO UPDATE SET rate = EXCLUDED.rate, updated_at = EXCLUDED.updated_at.

        This is idempotent AND update-safe:
          - Inserting the same rate twice is a no-op (excluded.rate = existing rate).
          - Inserting a revised rate (correction from the source provider) updates
            the row.  This differs from FinancialLineItemRepository which uses
            ON CONFLICT DO NOTHING because financial line items are immutable once
            filed, whereas FX closing rates may be revised within T+1 business day.

        Performance:
          The INSERT is issued as a single statement with a VALUES list, not
          per-row.  asyncpg and psycopg2 both support this efficiently via
          executemany-equivalent semantics within SQLAlchemy Core dialect.

        Args:
            rates: Sequence of FXRateCreate objects to persist.

        Returns:
            Number of rows inserted or updated.  (Rows whose rate value did not
            change count as "updated" at the SQL level but have no net effect.)

        Raises:
            FXRateLookupError: On unrecoverable storage failure.
        """
        if not rates:
            return 0

        from datetime import datetime, timezone  # noqa: PLC0415

        now = datetime.now(tz=timezone.utc)

        values = [
            {
                "rate_date": r.rate_date,
                "from_currency": r.from_currency,
                "to_currency": r.to_currency,
                "rate": r.rate,
                "created_at": now,
                "updated_at": now,
            }
            for r in rates
        ]

        try:
            insert_stmt = pg_insert(DailyFXRate).values(values)
            stmt = insert_stmt.on_conflict_do_update(
                constraint="pk_daily_fx_rates",
                set_={
                    "rate": insert_stmt.excluded.rate,
                    "updated_at": insert_stmt.excluded.updated_at,
                },
            )
            result = await self._session.execute(stmt)
            await self._session.flush()
        except SQLAlchemyError as exc:
            log.error(
                "fx_rate_bulk_upsert_failed",
                batch_size=len(rates),
                error=str(exc),
            )
            raise FXRateLookupError(
                f"Storage failure during FX rate bulk upsert "
                f"({len(rates)} rows): {exc}"
            ) from exc

        row_count: int = result.rowcount if result.rowcount >= 0 else len(rates)

        log.info(
            "fx_rate_bulk_upsert_complete",
            batch_size=len(rates),
            affected_rows=row_count,
        )
        return row_count

    # -------------------------------------------------------------------------
    # Read helpers — single-rate convenience wrappers
    # -------------------------------------------------------------------------

    async def get_rate_on_date(
        self,
        currency: str,
        rate_date: date,
    ) -> DailyRate | None:
        """
        Return the single DailyRate row for *currency* on *rate_date*, or None.

        This is a direct point-lookup with no look-back fallback.  Use
        ``get_spot_rate`` for look-back-aware spot resolution.

        Args:
            currency:  ISO 4217 source currency code.
            rate_date: Exact calendar date.

        Returns:
            DailyRate if a row exists, None otherwise.

        Raises:
            FXRateLookupError: On unrecoverable storage failure.
        """
        currency = currency.upper()

        if currency == _TARGET_CURRENCY:
            return DailyRate(on_date=rate_date, rate=_quantise(Decimal("1")))

        try:
            stmt = select(DailyFXRate).where(
                DailyFXRate.from_currency == currency,
                DailyFXRate.to_currency == _TARGET_CURRENCY,
                DailyFXRate.rate_date == rate_date,
            )
            result = await self._session.execute(stmt)
            row: DailyFXRate | None = result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            log.error(
                "fx_rate_point_query_failed",
                currency=currency,
                rate_date=rate_date.isoformat(),
                error=str(exc),
            )
            raise FXRateLookupError(
                f"Storage failure fetching FX rate for {currency}/USD "
                f"on {rate_date.isoformat()}: {exc}"
            ) from exc

        return _orm_to_daily_rate(row) if row is not None else None
