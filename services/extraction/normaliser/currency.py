"""
Currency normalisation — dual-pass split translation per Amendment V1.2 §3.

Amendment V1.2, Section 3 — Split Currency Translation (ASC 830 / IAS 21 / Ind AS 21):

  A SINGLE flat exchange rate applied to ALL financial data is PROHIBITED.

  REQUIRED — two separate translation passes:

  1. Balance Sheet (statement_type = 'BS'):
       Translate at the SPOT RATE on ``period_end_date``.
       Rationale: balance sheet items represent point-in-time stock values.

  2. Income Statement (statement_type = 'IS') and
     Cash Flow Statement (statement_type = 'CF'):
       Translate at the WEIGHTED AVERAGE RATE over the period
       [``period_start_date``, ``period_end_date``].
       Rationale: IS and CF items represent flows accumulated over the
       period; an average rate gives the economically correct translation.

  PROHIBITED::

      value_usd = value_reported / flat_rate   # ← BANNED (single rate for all)

  REQUIRED::

      if statement_type == "BS":
          rate = get_spot_rate(currency, period_end_date)
      else:  # IS or CF
          rate = get_weighted_average_rate(currency, period_start_date, period_end_date)
      value_usd = value_reported / rate

Sign convention (Amendment V1.2 §2.2):
  Sign inversion (×−1) for outflow/expense items MUST be applied BEFORE
  this normaliser is called.  This normaliser translates values as-is;
  it does not classify or invert signs.

Milestone: M4
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

# Statement type codes (must match FinancialLineItem.statement_type).
_BS = "BS"  # Balance Sheet  → spot rate on period_end_date
_IS = "IS"  # Income Statement → weighted average rate
_CF = "CF"  # Cash Flow        → weighted average rate

_BALANCE_SHEET_STATEMENTS = frozenset({_BS})
_FLOW_STATEMENTS = frozenset({_IS, _CF})

# Rounding scale for NUMERIC(26,2) target columns.
_MONETARY_SCALE = Decimal("0.01")

# Rounding scale for NUMERIC(38,10) fx_rate_used column.
_FX_RATE_SCALE = Decimal("0.0000000001")


# ---------------------------------------------------------------------------
# FX rate provider protocol
# ---------------------------------------------------------------------------


class FXRateProvider(Protocol):
    """
    Protocol for FX rate lookup implementations.

    Implementations may source rates from:
      - A local historical rates database
      - An external FX API (with circuit breaker per Amendment V1.2 §9.2)
      - A cache (Redis or in-process)

    Both methods must return the number of foreign currency units per 1 USD,
    i.e. rate = foreign_amount / usd_amount. To convert:
        value_usd = value_reported / rate
    """

    async def get_spot_rate(
        self, currency: str, on_date: date
    ) -> Decimal:
        """
        Return the spot exchange rate for ``currency`` on ``on_date``.

        Args:
            currency:  ISO 4217 three-letter currency code (e.g. 'EUR', 'INR').
            on_date:   The date for which the spot rate is required.

        Returns:
            Rate as a Decimal. For USD→USD, returns Decimal('1').

        Raises:
            FXRateLookupError: If the rate cannot be obtained.
        """
        ...  # pragma: no cover

    async def get_weighted_average_rate(
        self, currency: str, period_start: date, period_end: date
    ) -> Decimal:
        """
        Return the weighted average exchange rate for ``currency`` over
        [``period_start``, ``period_end``] (inclusive).

        Args:
            currency:      ISO 4217 currency code.
            period_start:  First day of the reporting period.
            period_end:    Last day of the reporting period.

        Returns:
            Weighted average rate as a Decimal.

        Raises:
            FXRateLookupError: If the rate cannot be obtained.
        """
        ...  # pragma: no cover


class FXRateLookupError(Exception):
    """Raised when an FX rate cannot be retrieved for the requested parameters."""


# ---------------------------------------------------------------------------
# Translation result
# ---------------------------------------------------------------------------


@dataclass
class TranslationResult:
    """
    Result of a single currency translation operation.

    Attributes:
        value_usd:      Translated value in USD. NUMERIC(26,2).
        fx_rate_used:   The rate applied. NUMERIC(38,10). Stored in
                        FinancialLineItem.fx_rate_used for audit.
        rate_type:      'spot' (BS) or 'weighted_average' (IS/CF).
        currency:       Original currency code.
    """

    value_usd: Decimal
    fx_rate_used: Decimal
    rate_type: str
    currency: str


# ---------------------------------------------------------------------------
# Normaliser
# ---------------------------------------------------------------------------


class CurrencyNormaliser:
    """
    Translates reported financial values to USD using the dual-pass strategy.

    Amendment V1.2, Section 3: Balance Sheet items use spot rate on
    period_end_date; Income Statement and Cash Flow items use the weighted
    average rate over [period_start_date, period_end_date].

    Args:
        fx_provider:  Implementation of the FXRateProvider protocol.
    """

    def __init__(self, fx_provider: FXRateProvider) -> None:
        self._fx = fx_provider

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
        Translate ``value_reported`` to USD using the correct rate for the
        statement type (Amendment V1.2 §3 dual-pass split translation).

        Args:
            value_reported:    Value in the reported currency.
            currency:          ISO 4217 source currency code.
            statement_type:    'BS' | 'IS' | 'CF'.
            period_end_date:   End of the reporting period.
            period_start_date: Start of the reporting period. Required for
                               IS and CF translation; ignored for BS.

        Returns:
            TranslationResult with value_usd, fx_rate_used, and rate_type.

        Raises:
            ValueError:         If statement_type is unrecognised, or if
                                period_start_date is missing for IS/CF.
            FXRateLookupError:  If the FX provider cannot return a rate.
        """
        # Short-circuit: USD values require no translation.
        if currency.upper() == "USD":
            rate = Decimal("1")
            return TranslationResult(
                value_usd=value_reported.quantize(_MONETARY_SCALE, ROUND_HALF_UP),
                fx_rate_used=rate,
                rate_type="identity",
                currency="USD",
            )

        st = statement_type.upper()

        if st in _BALANCE_SHEET_STATEMENTS:
            # Amendment V1.2 §3: Balance Sheet → closing spot rate.
            rate = await self._fx.get_spot_rate(currency, period_end_date)
            rate_type = "spot"
            log.debug(
                "currency.translate.spot",
                currency=currency,
                on_date=str(period_end_date),
                rate=str(rate),
            )

        elif st in _FLOW_STATEMENTS:
            # Amendment V1.2 §3: IS / CF → weighted average rate over period.
            if period_start_date is None:
                raise ValueError(
                    f"period_start_date is required for {st} translation "
                    f"(Amendment V1.2 §3 weighted average rate)."
                )
            rate = await self._fx.get_weighted_average_rate(
                currency, period_start_date, period_end_date
            )
            rate_type = "weighted_average"
            log.debug(
                "currency.translate.weighted_average",
                currency=currency,
                period_start=str(period_start_date),
                period_end=str(period_end_date),
                rate=str(rate),
            )

        else:
            raise ValueError(
                f"Unknown statement_type {st!r}. "
                f"Expected one of: {sorted(_BALANCE_SHEET_STATEMENTS | _FLOW_STATEMENTS)}"
            )

        if rate == 0:
            raise FXRateLookupError(
                f"FX rate for {currency!r} returned zero — cannot translate."
            )

        value_usd = (value_reported / rate).quantize(_MONETARY_SCALE, ROUND_HALF_UP)
        rate_stored = rate.quantize(_FX_RATE_SCALE, ROUND_HALF_UP)

        return TranslationResult(
            value_usd=value_usd,
            fx_rate_used=rate_stored,
            rate_type=rate_type,
            currency=currency,
        )
