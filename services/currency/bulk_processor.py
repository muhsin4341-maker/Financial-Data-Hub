"""
Bulk Currency Translation Service — M5 Step 2: Batch Transactional Processing.

Responsibility:
  Iterate over a batch of FinancialLineItem rows whose ``value_usd`` column
  is NULL (not yet translated), invoke the dual-pass CurrencyTranslationEngine
  for each row, and write the translated amounts back to the database.

Architecture position:
  parse_xbrl_document  (M4 Step 1)
  ValidationEngine     (M4 Step 3)
  FinancialLineItemWriter (M4 Step 4)  ← writes value_usd = NULL
    ↓  financial_line_items rows with value_usd IS NULL
  BulkCurrencyTranslator (this module) ← fills value_usd / fx_rate_used
    ↓  UPDATE financial_line_items SET value_usd, fx_rate_used, updated_at
  (M6 Excel export builder)

Transactional integrity (Amendment V1.2 §1.3):
  The entire batch is wrapped in a single SQLAlchemy ``session.begin_nested()``
  savepoint.  If ``CurrencyTranslationEngine.translate()`` raises
  ``MissingFXRateException`` for ANY item in the batch:
    - The savepoint is rolled back atomically (no partial writes persist).
    - The gap details are logged at ERROR level.
    - If a ``job_id`` is provided, the parent FinancialJob status is updated
      to ``'failed_fx_data_gap'`` so the Celery orchestrator can halt
      downstream Excel export.
  The caller's outer transaction is NOT committed here; the caller owns the
  transaction boundary (consistent with FinancialLineItemWriter convention).

Period date derivation:
  ``FinancialLineItem`` stores ``fiscal_year`` and ``fiscal_period`` but NOT
  ``period_start_date`` / ``period_end_date``.  ``BulkCurrencyTranslator``
  derives these via ``_derive_period_dates()``:

    IND_AS (SEBI/MCA filers):
      FY  = Apr 1 (fiscal_year)  → Mar 31 (fiscal_year + 1)
      Q1  = Apr 1 → Jun 30
      Q2  = Jul 1 → Sep 30
      Q3  = Oct 1 → Dec 31
      Q4  = Jan 1 (fiscal_year + 1) → Mar 31 (fiscal_year + 1)

    US_GAAP / IFRS (calendar-year default):
      FY  = Jan 1 → Dec 31 (fiscal_year)
      Q1  = Jan 1 → Mar 31
      Q2  = Apr 1 → Jun 30
      Q3  = Jul 1 → Sep 30
      Q4  = Oct 1 → Dec 31

  For non-calendar-year US GAAP filers (e.g., September fiscal year end),
  the caller may pass an explicit ``period_overrides`` dict keyed by
  ``(fiscal_year, fiscal_period)`` to supply exact dates without modifying
  the derivation logic.

Precision contract (Amendment V1.2 §1.1):
  - ``value_usd``    → NUMERIC(26,2): quantised via ROUND_HALF_EVEN at write time.
  - ``fx_rate_used`` → NUMERIC(38,10): quantised by CurrencyTranslationEngine.
  - No float() operations anywhere in this module.

Milestone: M5 Step 2 — Bulk Transactional Processing
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import FinancialJob, FinancialLineItem
from services.currency.translator import CurrencyTranslationEngine, MissingFXRateException

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Precision constant (Amendment V1.2 §1.1)
# ---------------------------------------------------------------------------

# NUMERIC(26,2) — absolute monetary translated amount.
_MONETARY_SCALE: Decimal = Decimal("0.01")

# Job status written when a MissingFXRateException blocks the batch.
_STATUS_FX_DATA_GAP = "failed_fx_data_gap"

# Job statuses that are valid entry points for FX translation.
_TRANSLATABLE_STATUSES: frozenset[str] = frozenset({
    "pending", "queued", "running", "completed",
})

# ---------------------------------------------------------------------------
# Period override type alias
# ---------------------------------------------------------------------------

# Caller-supplied exact period dates keyed by (fiscal_year, fiscal_period).
PeriodOverrides = dict[tuple[int, str], tuple[date, date]]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BulkTranslationResult:
    """
    Summary of a completed BulkCurrencyTranslator run.

    Attributes:
        rows_translated:    Line items successfully translated (value_usd set).
        rows_skipped_usd:   Items whose reported_currency is already 'USD'
                            (value_usd = value_reported; rate = 1.0000000000).
        rows_skipped_null:  Items with NULL value_reported — skipped, not failed.
        rows_failed:        Items that triggered MissingFXRateException.
                            Non-zero only when the batch was partially processed
                            before the rollback; post-rollback this equals the
                            count of items attempted before the failure.
        job_status_updated_to: New job status string if updated, else None.
        missing_rate_details: Populated when MissingFXRateException fires.
            Keys: 'currency', 'target_date', 'context', 'lookback_days'.
    """

    rows_translated: int = 0
    rows_skipped_usd: int = 0
    rows_skipped_null: int = 0
    rows_failed: int = 0
    job_status_updated_to: str | None = None
    missing_rate_details: dict | None = None

    @property
    def success(self) -> bool:
        return self.rows_failed == 0

    @property
    def total_loaded(self) -> int:
        return (
            self.rows_translated
            + self.rows_skipped_usd
            + self.rows_skipped_null
            + self.rows_failed
        )

    def summary(self) -> str:
        return (
            f"BulkTranslationResult: {self.rows_translated} translated, "
            f"{self.rows_skipped_usd} skipped (USD), "
            f"{self.rows_skipped_null} skipped (null value), "
            f"{self.rows_failed} failed"
        )


# ---------------------------------------------------------------------------
# Bulk translator
# ---------------------------------------------------------------------------


class BulkCurrencyTranslator:
    """
    Translates a batch of FinancialLineItem rows to USD using the dual-pass
    CurrencyTranslationEngine and writes results back to the database.

    The entire batch is wrapped in a single savepoint so that a
    MissingFXRateException for any item triggers a complete rollback of all
    translations attempted in that batch (Amendment V1.2 §1.3).

    The session is NOT committed inside this class; the caller owns the
    transaction boundary.

    Usage (from a Celery task)::

        engine = CurrencyTranslationEngine(repo=fx_repo)
        translator = BulkCurrencyTranslator(engine=engine, session=session)

        # Translate all untranslated items for a job:
        result = await translator.translate_for_job(job_id=job_uuid)

        # Or translate a specific company/period block:
        result = await translator.translate_for_company_period(
            company_id=company_uuid,
            fiscal_year=2024,
            fiscal_period="FY",
        )

        if result.success:
            await session.commit()
        else:
            await session.rollback()

    Args:
        engine:           CurrencyTranslationEngine with a wired FXRateRepository.
        session:          Active AsyncSession (caller owns commit/rollback).
        period_overrides: Optional mapping of (fiscal_year, fiscal_period) →
                          (period_start_date, period_end_date) for non-calendar
                          fiscal years or filer-specific overrides.
    """

    def __init__(
        self,
        engine: CurrencyTranslationEngine,
        session: AsyncSession,
        period_overrides: PeriodOverrides | None = None,
    ) -> None:
        self._engine = engine
        self._session = session
        self._period_overrides: PeriodOverrides = period_overrides or {}

    # ── Public entry points ────────────────────────────────────────────────────

    async def translate_for_job(
        self,
        job_id: uuid.UUID,
    ) -> BulkTranslationResult:
        """
        Translate all untranslated line items associated with a FinancialJob.

        Loads the job to obtain ``company_id`` and ``fiscal_year``, queries all
        non-restated FinancialLineItem rows for that company + year with
        ``value_usd IS NULL``, then delegates to ``_translate_batch``.

        If the job does not exist or is in a terminal state that precludes
        translation (e.g., 'cancelled'), returns an empty result without
        modifying the database.

        Args:
            job_id: UUID of the FinancialJob driving this translation run.

        Returns:
            BulkTranslationResult with per-category counts.
        """
        job = await self._session.get(FinancialJob, job_id)
        if job is None:
            log.warning(
                "bulk_fx.job_not_found",
                job_id=str(job_id),
            )
            return BulkTranslationResult()

        if job.status not in _TRANSLATABLE_STATUSES:
            log.info(
                "bulk_fx.job_skipped",
                job_id=str(job_id),
                status=job.status,
                reason="non_translatable_status",
            )
            return BulkTranslationResult()

        items = await self._load_untranslated_by_company_year(
            company_id=job.company_id,
            fiscal_year=job.fiscal_year,
        )

        log.info(
            "bulk_fx.items_loaded",
            job_id=str(job_id),
            company_id=str(job.company_id),
            fiscal_year=job.fiscal_year,
            item_count=len(items),
        )

        return await self._translate_batch(items, job_id=job_id)

    async def translate_for_company_period(
        self,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
        *,
        job_id: uuid.UUID | None = None,
    ) -> BulkTranslationResult:
        """
        Translate all untranslated items for a specific company / fiscal period.

        Useful for targeted re-runs after a FX data gap has been filled, or
        for translating a single fiscal period without reprocessing the full year.

        Args:
            company_id:    UUID of the company.
            fiscal_year:   Fiscal year integer (e.g. 2024).
            fiscal_period: Fiscal period string: 'FY' | 'Q1' | 'Q2' | 'Q3' | 'Q4'.
            job_id:        Optional FinancialJob UUID for status updates.

        Returns:
            BulkTranslationResult with per-category counts.
        """
        items = await self._load_untranslated_by_period(
            company_id=company_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
        )

        log.info(
            "bulk_fx.items_loaded",
            company_id=str(company_id),
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            item_count=len(items),
            job_id=str(job_id) if job_id else None,
        )

        return await self._translate_batch(items, job_id=job_id)

    # ── Core batch logic ───────────────────────────────────────────────────────

    async def _translate_batch(
        self,
        items: list[FinancialLineItem],
        *,
        job_id: uuid.UUID | None,
    ) -> BulkTranslationResult:
        """
        Translate all items within a single atomic savepoint.

        All ORM row mutations (value_usd, fx_rate_used, updated_at) are applied
        inside ``session.begin_nested()`` so that a MissingFXRateException
        triggers a clean rollback of the entire batch with no partial writes.

        After rollback, if ``job_id`` is provided, the parent FinancialJob status
        is updated to ``'failed_fx_data_gap'`` OUTSIDE the failed savepoint (in
        the caller's outer transaction) so the status update itself persists even
        though the translation batch was rolled back.

        Args:
            items:  FinancialLineItem ORM rows to translate (all already loaded).
            job_id: Optional job UUID for status bookkeeping.

        Returns:
            BulkTranslationResult.
        """
        result = BulkTranslationResult()

        if not items:
            log.info("bulk_fx.empty_batch", job_id=str(job_id) if job_id else None)
            return result

        try:
            async with self._session.begin_nested():
                # ── Savepoint: entire batch inside one atomic boundary ─────────
                for item in items:
                    item_result = await self._translate_item(item)

                    if item_result == "translated":
                        result.rows_translated += 1
                    elif item_result == "skipped_usd":
                        result.rows_skipped_usd += 1
                    elif item_result == "skipped_null":
                        result.rows_skipped_null += 1

                # Flush all mutations to the DB within the savepoint so any
                # constraint violations surface before the caller's commit.
                await self._session.flush()

        except MissingFXRateException as exc:
            # The savepoint context manager rolls back all mutations on exception.
            # Log the gap and update job status OUTSIDE the failed savepoint.
            result.rows_failed = result.rows_translated + 1
            result.rows_translated = 0
            result.missing_rate_details = {
                "currency":    exc.currency,
                "target_date": str(exc.target_date),
                "context":     exc.context,
                "lookback_days": exc.lookback_days,
            }

            log.error(
                "bulk_fx.missing_rate",
                currency=exc.currency,
                target_date=str(exc.target_date),
                context=exc.context,
                lookback_days=exc.lookback_days,
                job_id=str(job_id) if job_id else None,
                batch_size=len(items),
            )

            await self._update_job_status_fx_gap(job_id, exc, result)
            return result

        log.info(
            "bulk_fx.batch_complete",
            job_id=str(job_id) if job_id else None,
            rows_translated=result.rows_translated,
            rows_skipped_usd=result.rows_skipped_usd,
            rows_skipped_null=result.rows_skipped_null,
        )
        return result

    async def _translate_item(
        self,
        item: FinancialLineItem,
    ) -> str:
        """
        Translate a single FinancialLineItem and mutate its ORM row in place.

        Returns a status string for the result counter:
          'translated'   — value_usd and fx_rate_used written.
          'skipped_usd'  — currency is already USD; value_usd = value_reported.
          'skipped_null' — value_reported is NULL; no translation possible.

        Raises:
            MissingFXRateException: Propagated from CurrencyTranslationEngine
                                    when no rate is found within 5 days.
            FXRateLookupError:      Propagated on storage/provider failure.
            ValueError:             Unrecognised statement_type or missing
                                    period_start_date for IS/CF.
        """
        if item.value_reported is None:
            log.debug(
                "bulk_fx.item_skip_null",
                item_id=str(item.id),
                canonical_field=item.canonical_field,
            )
            return "skipped_null"

        currency = (item.reported_currency or "USD").upper().strip()

        # USD identity shortcut — no engine call needed.
        if currency == "USD":
            item.value_usd = _quantise_monetary(item.value_reported)
            item.fx_rate_used = Decimal("1").quantize(
                Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN
            )
            item.updated_at = datetime.now(UTC)
            return "skipped_usd"

        # Derive period dates from fiscal context.
        period_start, period_end = self._resolve_period_dates(
            fiscal_year=item.fiscal_year,
            fiscal_period=item.fiscal_period,
            reporting_standard=str(item.reporting_standard.value)
            if hasattr(item.reporting_standard, "value")
            else str(item.reporting_standard),
        )

        translation = await self._engine.translate(
            value_reported=item.value_reported,
            currency=currency,
            statement_type=item.statement_type,
            period_end_date=period_end,
            period_start_date=period_start,
        )

        # Write back — quantise monetary to NUMERIC(26,2) at the DB boundary.
        item.value_usd = _quantise_monetary(translation.value_usd)
        item.fx_rate_used = translation.fx_rate_used  # already NUMERIC(38,10) from engine
        item.updated_at = datetime.now(UTC)

        log.debug(
            "bulk_fx.item_translated",
            item_id=str(item.id),
            canonical_field=item.canonical_field,
            currency=currency,
            statement_type=item.statement_type,
            rate_type=translation.rate_type,
            fx_rate=str(translation.fx_rate_used),
            value_reported=str(item.value_reported),
            value_usd=str(item.value_usd),
        )
        return "translated"

    # ── Database query helpers ─────────────────────────────────────────────────

    async def _load_untranslated_by_company_year(
        self,
        company_id: uuid.UUID | None,
        fiscal_year: int | None,
    ) -> list[FinancialLineItem]:
        """
        Load all non-restated, untranslated line items for a company + year.

        Filters: is_restated=FALSE AND value_usd IS NULL.
        If company_id or fiscal_year is None (job has no period context),
        returns an empty list.
        """
        if company_id is None or fiscal_year is None:
            log.warning(
                "bulk_fx.load_skip_incomplete_context",
                company_id=str(company_id) if company_id else None,
                fiscal_year=fiscal_year,
            )
            return []

        stmt = (
            select(FinancialLineItem)
            .where(
                FinancialLineItem.company_id == company_id,
                FinancialLineItem.fiscal_year == fiscal_year,
                FinancialLineItem.is_restated.is_(False),
                FinancialLineItem.value_usd.is_(None),
            )
            .order_by(
                FinancialLineItem.fiscal_period,
                FinancialLineItem.statement_type,
                FinancialLineItem.canonical_field,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def _load_untranslated_by_period(
        self,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
    ) -> list[FinancialLineItem]:
        """
        Load untranslated line items for a specific company / fiscal period.

        Filters: is_restated=FALSE AND value_usd IS NULL AND
                 fiscal_period = <period>.
        """
        stmt = (
            select(FinancialLineItem)
            .where(
                FinancialLineItem.company_id == company_id,
                FinancialLineItem.fiscal_year == fiscal_year,
                FinancialLineItem.fiscal_period == fiscal_period,
                FinancialLineItem.is_restated.is_(False),
                FinancialLineItem.value_usd.is_(None),
            )
            .order_by(
                FinancialLineItem.statement_type,
                FinancialLineItem.canonical_field,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    # ── Job status update ──────────────────────────────────────────────────────

    async def _update_job_status_fx_gap(
        self,
        job_id: uuid.UUID | None,
        exc: MissingFXRateException,
        result: BulkTranslationResult,
    ) -> None:
        """
        Mark the parent FinancialJob as 'failed_fx_data_gap'.

        This update runs OUTSIDE the failed savepoint so it persists even
        though the translation batch was rolled back.  The caller's outer
        transaction must still be committed for this status update to reach
        the DB.

        Args:
            job_id: FinancialJob UUID to update. No-op if None.
            exc:    The MissingFXRateException that triggered the rollback.
            result: BulkTranslationResult to record the status change in.
        """
        if job_id is None:
            return

        job = await self._session.get(FinancialJob, job_id)
        if job is None:
            log.warning(
                "bulk_fx.job_not_found_for_status_update",
                job_id=str(job_id),
            )
            return

        job.status = _STATUS_FX_DATA_GAP
        job.error_message = (
            f"FX data gap: no rate for {exc.currency!r} on {exc.target_date} "
            f"(context={exc.context!r}). "
            f"Searched {exc.lookback_days} prior calendar days with no market-active "
            f"closing rate found. "
            f"Batch of {result.rows_failed} item(s) rolled back. "
            f"Populate fx_daily_rates for {exc.currency} around {exc.target_date} "
            f"and re-trigger the translation job."
        )
        job.updated_at = datetime.now(UTC)

        result.job_status_updated_to = _STATUS_FX_DATA_GAP

        log.warning(
            "bulk_fx.job_status_fx_gap",
            job_id=str(job_id),
            currency=exc.currency,
            target_date=str(exc.target_date),
        )

    # ── Period date resolution ─────────────────────────────────────────────────

    def _resolve_period_dates(
        self,
        fiscal_year: int,
        fiscal_period: str,
        reporting_standard: str,
    ) -> tuple[date, date]:
        """
        Return (period_start_date, period_end_date) for this item.

        Checks ``self._period_overrides`` first; falls back to
        ``_derive_period_dates()`` if no override is registered.
        """
        override_key = (fiscal_year, fiscal_period)
        if override_key in self._period_overrides:
            return self._period_overrides[override_key]
        return _derive_period_dates(fiscal_year, fiscal_period, reporting_standard)


# ---------------------------------------------------------------------------
# Period date derivation
# ---------------------------------------------------------------------------


def _derive_period_dates(
    fiscal_year: int,
    fiscal_period: str,
    reporting_standard: str,
) -> tuple[date, date]:
    """
    Derive (period_start_date, period_end_date) from the fiscal context.

    IND_AS (SEBI / MCA filers — April fiscal year start):
      FY = Apr 1 (fiscal_year)  → Mar 31 (fiscal_year + 1)
      Q1 = Apr 1 → Jun 30  (of fiscal_year)
      Q2 = Jul 1 → Sep 30  (of fiscal_year)
      Q3 = Oct 1 → Dec 31  (of fiscal_year)
      Q4 = Jan 1 → Mar 31  (of fiscal_year + 1)

    US_GAAP / IFRS (calendar year default):
      FY = Jan 1 → Dec 31  (of fiscal_year)
      Q1 = Jan 1 → Mar 31
      Q2 = Apr 1 → Jun 30
      Q3 = Jul 1 → Sep 30
      Q4 = Oct 1 → Dec 31

    For non-calendar US GAAP filers (e.g., September year-end), the caller
    should pass ``period_overrides`` to ``BulkCurrencyTranslator`` rather
    than relying on this function.

    Args:
        fiscal_year:       Integer fiscal year (e.g. 2024).
        fiscal_period:     'FY' | 'Q1' | 'Q2' | 'Q3' | 'Q4' (case-insensitive).
        reporting_standard: 'IND_AS' | 'US_GAAP' | 'IFRS'.

    Returns:
        (period_start_date, period_end_date) as date objects.

    Raises:
        ValueError: If fiscal_period is not one of the recognised values.
    """
    fp = fiscal_period.upper().strip()
    std = reporting_standard.upper().strip()
    is_india = std == "IND_AS"

    if is_india:
        # April fiscal year: FY runs Apr 1 (Y) → Mar 31 (Y+1).
        _INDIA_PERIODS: dict[str, tuple[date, date]] = {
            "FY": (date(fiscal_year, 4, 1),     date(fiscal_year + 1, 3, 31)),
            "Q1": (date(fiscal_year, 4, 1),     date(fiscal_year, 6, 30)),
            "Q2": (date(fiscal_year, 7, 1),     date(fiscal_year, 9, 30)),
            "Q3": (date(fiscal_year, 10, 1),    date(fiscal_year, 12, 31)),
            "Q4": (date(fiscal_year + 1, 1, 1), date(fiscal_year + 1, 3, 31)),
        }
        if fp not in _INDIA_PERIODS:
            raise ValueError(
                f"Unknown fiscal_period {fp!r} for IND_AS. "
                f"Expected one of: {sorted(_INDIA_PERIODS.keys())}."
            )
        return _INDIA_PERIODS[fp]

    else:
        # Calendar year: FY runs Jan 1 → Dec 31 (fiscal_year).
        _CALENDAR_PERIODS: dict[str, tuple[date, date]] = {
            "FY": (date(fiscal_year, 1, 1),  date(fiscal_year, 12, 31)),
            "Q1": (date(fiscal_year, 1, 1),  date(fiscal_year, 3, 31)),
            "Q2": (date(fiscal_year, 4, 1),  date(fiscal_year, 6, 30)),
            "Q3": (date(fiscal_year, 7, 1),  date(fiscal_year, 9, 30)),
            "Q4": (date(fiscal_year, 10, 1), date(fiscal_year, 12, 31)),
        }
        if fp not in _CALENDAR_PERIODS:
            raise ValueError(
                f"Unknown fiscal_period {fp!r} for {std}. "
                f"Expected one of: {sorted(_CALENDAR_PERIODS.keys())}."
            )
        return _CALENDAR_PERIODS[fp]


# ---------------------------------------------------------------------------
# Precision helper
# ---------------------------------------------------------------------------


def _quantise_monetary(value: Decimal) -> Decimal:
    """
    Round a monetary Decimal to NUMERIC(26,2) precision using ROUND_HALF_EVEN.

    Amendment V1.2 §1.1: translated absolute monetary values (value_usd)
    must be stored at NUMERIC(26,2).  ROUND_HALF_EVEN (banker's rounding)
    minimises cumulative bias across large data sets.
    """
    return value.quantize(_MONETARY_SCALE, rounding=ROUND_HALF_EVEN)
