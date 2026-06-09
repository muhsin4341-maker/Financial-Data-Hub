"""
FX Rate Ingestion Task — D1/B2.

Fetches daily closing exchange rates from the European Central Bank (ECB)
Statistical Data Warehouse REST API and bulk-upserts them into the
``daily_fx_rates`` table via ``FXRateRepository.bulk_upsert()``.

Architecture position
─────────────────────

  POST /api/v1/admin/fx-rates/sync  (B3 — admin.py)
    ↓  apply_async → QUEUE_FETCH
  sync_fx_rates_task  (D1/B2 — this module)
    ↓  httpx → ECB SDMX 2.1 REST API
    ↓  parse JSON-stat response, convert to X/USD convention
    ↓  FXRateRepository.bulk_upsert()  (ON CONFLICT DO UPDATE)
    ↓  returns summary dict

Rate convention
───────────────
The FX engine (services/currency/translator.py) uses:
    rate = foreign_currency_units_per_1_USD
    value_usd = value_reported / rate

ECB provides rates as "units of FOREIGN currency per 1 EUR":
    USD/EUR rate r_USD means  1 EUR = r_USD USD
    XYZ/EUR rate r_XYZ means  1 EUR = r_XYZ XYZ

Conversion to our convention (X per 1 USD):
    rate_X_per_USD = r_XYZ / r_USD

Special case EUR itself:
    rate_EUR_per_USD = 1 / r_USD   (since EUR/EUR = 1)

USD itself is the target currency — no row is stored for USD (the
FXRateRepository short-circuits USD → 1 directly).

ECB SDMX 2.1 API
─────────────────
Dataset:   EXR (Exchange Rates)
Key:       D.{CURRENCIES}.EUR.SP00.A
  D      = daily frequency
  EUR    = denominator (base currency, always EUR)
  SP00   = spot rate type
  A      = average of observations within the period

Endpoint (multiple currencies via '+' separator):
  https://data-api.ecb.europa.eu/service/data/EXR/D.USD+GBP+JPY+INR+...EUR.SP00.A
  ?startPeriod=YYYY-MM-DD&endPeriod=YYYY-MM-DD&format=jsondata

Idempotency
───────────
``bulk_upsert`` uses ``ON CONFLICT (rate_date, from_currency, to_currency)
DO UPDATE SET rate = EXCLUDED.rate, updated_at = NOW()`` — running the task
multiple times for the same date range is safe.

Milestone: D1/B2 — ECB FX Rate Ingestion Task
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from workers.celery_app import celery_app
from workers.queues import QUEUE_FETCH

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ECB SDMX 2.1 REST API base URL
_ECB_BASE_URL = "https://data-api.ecb.europa.eu/service/data/EXR"

# Supported currency pairs — all relative to USD as the target.
# USD itself is the identity rate and is never stored.
# EUR is included so we can compute all other pairs correctly.
_DEFAULT_CURRENCIES: list[str] = [
    "USD",   # Required anchor — must be first
    "EUR",
    "GBP",
    "JPY",
    "INR",
    "CNY",
    "CHF",
    "AUD",
    "CAD",
    "HKD",
    "SGD",
    "KRW",
    "SEK",
    "NOK",
    "DKK",
    "MXN",
    "BRL",
    "ZAR",
    "TRY",
    "RUB",
    "IDR",
    "MYR",
    "PHP",
    "THB",
    "NZD",
    "PLN",
    "CZK",
    "HUF",
    "RON",
    "ILS",
    "SAR",
    "AED",
    "PKR",
]

# Default number of calendar days back to seed on each sync.
# 90 days covers a full fiscal quarter + look-back buffer.
_DEFAULT_DAYS_BACK: int = 90

# Maximum batch size for a single ECB API request.
# ECB has no documented hard limit, but 30 currencies per request is safe.
_ECB_CURRENCY_BATCH_SIZE: int = 30

# HTTP request timeout in seconds.
_HTTP_TIMEOUT: float = 30.0

# Number of decimal places to round intermediate Decimal calculations.
_RATE_PRECISION: Decimal = Decimal("0.0000000001")  # NUMERIC(38,10)

# Task retry configuration
_MAX_RETRIES: int = 3
_RETRY_BACKOFF: list[int] = [60, 180, 600]  # 1 min, 3 min, 10 min


# ---------------------------------------------------------------------------
# DB initialisation (mirrors fx_translation_task.py pattern)
# ---------------------------------------------------------------------------


def _ensure_db_initialised() -> None:
    """Initialise the SQLAlchemy engine for this Celery worker process."""
    from apps.api.core.config import get_settings  # noqa: PLC0415
    from apps.api.core.database import init_db  # noqa: PLC0415

    settings = get_settings()
    init_db(
        database_url=settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        echo=False,
    )


# ---------------------------------------------------------------------------
# ECB fetch helpers
# ---------------------------------------------------------------------------


async def _fetch_ecb_rates_for_batch(
    currencies: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, dict[str, Decimal]]:
    """
    Fetch daily ECB exchange rates for *currencies* over the given period.

    Returns a nested dict: ``{date_str: {currency_code: rate_vs_eur}}``.
    Each rate is "units of *currency* per 1 EUR" (ECB native convention).

    Args:
        currencies:  List of ISO 4217 codes to fetch (e.g. ["USD","INR"]).
                     Must NOT be empty.
        start_date:  Inclusive start of the fetch window.
        end_date:    Inclusive end of the fetch window.

    Returns:
        Nested dict mapping ISO date strings → currency → ECB rate.
        Missing currencies or dates are simply absent from the dict.

    Raises:
        RuntimeError:  On non-2xx HTTP responses.
        ValueError:    On malformed JSON from the ECB endpoint.
    """
    import httpx  # noqa: PLC0415

    currency_key = "+".join(c.upper() for c in currencies)
    url = (
        f"{_ECB_BASE_URL}"
        f"/D.{currency_key}.EUR.SP00.A"
        f"?startPeriod={start_date.isoformat()}"
        f"&endPeriod={end_date.isoformat()}"
        f"&format=jsondata"
    )

    bound_log = log.bind(
        url=url,
        currencies=currencies,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
    )
    bound_log.debug("ecb.fetch.request")

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.get(url, follow_redirects=True)

    if response.status_code == 404:
        # ECB returns 404 when no data exists for the range — treat as empty.
        bound_log.info("ecb.fetch.no_data", status=404)
        return {}

    if response.status_code != 200:
        raise RuntimeError(
            f"ECB API returned HTTP {response.status_code} for {url!r}. "
            f"Body: {response.text[:300]}"
        )

    payload: dict[str, Any] = response.json()
    return _parse_ecb_jsonstat(payload)


def _parse_ecb_jsonstat(payload: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    """
    Parse the ECB SDMX 2.1 JSON-stat response into a flat mapping.

    ECB JSON-stat structure::

        payload["dataSets"][0]["series"] = {
            "0:0:0:0:0": {                      # series key
                "observations": {
                    "0": [1.0850, ...],          # observation index → [value, ...]
                    "1": [1.0912, ...],
                }
            },
            "1:0:0:0:0": { ... }                # next currency
        }

        payload["structure"]["dimensions"]["series"][1]["values"] = [
            {"id": "USD", "name": "US dollar"},
            {"id": "GBP", "name": "Pound sterling"},
            ...
        ]

        payload["structure"]["dimensions"]["observation"][0]["values"] = [
            {"id": "2024-01-02"},
            {"id": "2024-01-03"},
            ...
        ]

    The series key format is ``"freq_idx:currency_idx:denom_idx:type_idx:suffix_idx"``.
    Currency is always at position 1 (0-indexed).

    Returns:
        ``{date_str: {currency_code: Decimal(rate)}}``
        Rates are in ECB native convention: units of currency per 1 EUR.
    """
    try:
        structure = payload["structure"]
        series_dims = structure["dimensions"]["series"]
        obs_dims = structure["dimensions"]["observation"]

        # Locate the CURRENCY dimension (id="CURRENCY")
        currency_dim_index = next(
            i for i, d in enumerate(series_dims) if d["id"] == "CURRENCY"
        )
        currency_values: list[str] = [
            v["id"] for v in series_dims[currency_dim_index]["values"]
        ]

        # Locate the TIME_PERIOD observation dimension
        time_dim = next(d for d in obs_dims if d["id"] == "TIME_PERIOD")
        date_values: list[str] = [v["id"] for v in time_dim["values"]]

    except (KeyError, StopIteration) as exc:
        raise ValueError(
            f"Unexpected ECB JSON-stat structure — cannot locate "
            f"CURRENCY or TIME_PERIOD dimensions: {exc}"
        ) from exc

    # Build: {date_str: {currency_code: rate}}
    result: dict[str, dict[str, Decimal]] = {}

    datasets = payload.get("dataSets", [])
    if not datasets:
        return result

    series_map: dict[str, Any] = datasets[0].get("series", {})

    for series_key, series_data in series_map.items():
        # Parse the compound key to find the currency index
        key_parts = series_key.split(":")
        try:
            currency_idx = int(key_parts[currency_dim_index])
            currency_code = currency_values[currency_idx]
        except (IndexError, ValueError):
            continue

        observations: dict[str, list[Any]] = series_data.get("observations", {})
        for obs_idx_str, obs_values in observations.items():
            try:
                obs_idx = int(obs_idx_str)
                date_str = date_values[obs_idx]
                raw_value = obs_values[0]
                if raw_value is None:
                    continue
                rate = Decimal(str(raw_value))
            except (IndexError, ValueError, TypeError):
                continue

            if date_str not in result:
                result[date_str] = {}
            result[date_str][currency_code] = rate

    return result


def _convert_ecb_to_usd_convention(
    ecb_data: dict[str, dict[str, Decimal]],
    target_currencies: list[str],
) -> list[tuple[date, str, Decimal]]:
    """
    Convert ECB native rates (X/EUR) to our storage convention (X per 1 USD).

    ECB convention:  1 EUR = r_X units of currency X.
    Our convention:  1 USD = rate units of currency X.

    Conversion for non-EUR, non-USD currencies:
        rate_X_per_USD = r_X / r_USD

    Conversion for EUR:
        rate_EUR_per_USD = 1 / r_USD

    USD itself is the target currency — no row is emitted for USD.

    Args:
        ecb_data:         Raw ECB response from _parse_ecb_jsonstat().
        target_currencies: Which currency codes to emit rows for.

    Returns:
        List of ``(rate_date, from_currency, rate_in_our_convention)`` tuples.
        Dates for which the USD rate is missing are skipped entirely.
    """
    rows: list[tuple[date, str, Decimal]] = []
    # Currencies we emit storage rows for (exclude USD itself — identity rate)
    emit_currencies = [c.upper() for c in target_currencies if c.upper() != "USD"]

    for date_str, currency_map in ecb_data.items():
        # USD/EUR rate is the anchor — without it we cannot normalise anything.
        usd_per_eur = currency_map.get("USD")
        if usd_per_eur is None or usd_per_eur <= Decimal("0"):
            continue  # Skip dates with missing/invalid USD anchor

        try:
            rate_date = date.fromisoformat(date_str)
        except ValueError:
            continue

        for currency in emit_currencies:
            if currency == "EUR":
                # EUR per 1 USD = 1 / (USD per 1 EUR)
                rate_x_per_usd = (Decimal("1") / usd_per_eur).quantize(
                    _RATE_PRECISION
                )
            else:
                x_per_eur = currency_map.get(currency)
                if x_per_eur is None or x_per_eur <= Decimal("0"):
                    continue
                # X per 1 USD = (X per 1 EUR) / (USD per 1 EUR)
                rate_x_per_usd = (x_per_eur / usd_per_eur).quantize(_RATE_PRECISION)

            if rate_x_per_usd <= Decimal("0"):
                continue

            rows.append((rate_date, currency, rate_x_per_usd))

    return rows


# ---------------------------------------------------------------------------
# Core async pipeline
# ---------------------------------------------------------------------------


async def _run_fx_sync(
    days_back: int,
    currencies: list[str],
) -> dict[str, Any]:
    """
    Async pipeline: fetch ECB rates → convert convention → bulk-upsert.

    Returns a summary dict with counts and date range.
    """
    from apps.api.core.database import AsyncSessionLocal  # noqa: PLC0415
    from apps.api.repositories.fx_rates import FXRateCreate, FXRateRepository  # noqa: PLC0415

    end_date = datetime.now(tz=UTC).date()
    start_date = end_date - timedelta(days=days_back)

    bound_log = log.bind(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        currencies=currencies,
        days_back=days_back,
    )
    bound_log.info("fx_sync.pipeline.start")

    # Ensure USD is in the fetch set (needed as anchor for rate conversion).
    fetch_currencies = list({*currencies, "USD"})

    # ECB API accepts batches of currencies in a single request.
    # Split into batches to stay within reasonable URL length limits.
    ecb_data: dict[str, dict[str, Decimal]] = {}
    batch_count = 0

    for i in range(0, len(fetch_currencies), _ECB_CURRENCY_BATCH_SIZE):
        batch = fetch_currencies[i : i + _ECB_CURRENCY_BATCH_SIZE]
        batch_data = await _fetch_ecb_rates_for_batch(batch, start_date, end_date)
        batch_count += 1
        bound_log.debug(
            "ecb.fetch.batch_complete",
            batch=batch,
            dates_returned=len(batch_data),
        )
        # Merge batch_data into ecb_data (deep merge per date)
        for date_str, currency_map in batch_data.items():
            if date_str not in ecb_data:
                ecb_data[date_str] = {}
            ecb_data[date_str].update(currency_map)

    bound_log.info(
        "ecb.fetch.all_batches_complete",
        batches=batch_count,
        unique_dates=len(ecb_data),
    )

    if not ecb_data:
        bound_log.warning("fx_sync.no_data_from_ecb")
        return {
            "status": "ok",
            "rates_upserted": 0,
            "dates_processed": 0,
            "date_range": f"{start_date.isoformat()} → {end_date.isoformat()}",
            "currencies_requested": currencies,
            "warning": "ECB returned no data for the requested range",
        }

    # Convert ECB rates (X/EUR) → our convention (X per 1 USD).
    # Only emit rows for the originally-requested currencies (not the USD anchor).
    emit_currencies = [c for c in currencies if c.upper() != "USD"]
    rows = _convert_ecb_to_usd_convention(ecb_data, emit_currencies)

    bound_log.info(
        "fx_sync.conversion_complete",
        rows_converted=len(rows),
    )

    if not rows:
        bound_log.warning("fx_sync.no_rows_after_conversion")
        return {
            "status": "ok",
            "rates_upserted": 0,
            "dates_processed": len(ecb_data),
            "date_range": f"{start_date.isoformat()} → {end_date.isoformat()}",
            "currencies_requested": currencies,
            "warning": "No rows survived conversion (USD anchor may be missing)",
        }

    # Build FXRateCreate objects and upsert in one DB transaction.
    fx_creates = [
        FXRateCreate(
            rate_date=row_date,
            from_currency=currency,
            to_currency="USD",
            rate=rate,
        )
        for (row_date, currency, rate) in rows
    ]

    async with AsyncSessionLocal() as session:
        repo = FXRateRepository(session)
        affected = await repo.bulk_upsert(fx_creates)
        await session.commit()

    # Derive the actual date range that was processed
    processed_dates = sorted(ecb_data.keys())
    actual_range = (
        f"{processed_dates[0]} → {processed_dates[-1]}"
        if processed_dates
        else f"{start_date.isoformat()} → {end_date.isoformat()}"
    )

    summary = {
        "status": "ok",
        "rates_upserted": affected,
        "dates_processed": len(processed_dates),
        "rows_built": len(fx_creates),
        "date_range": actual_range,
        "currencies_stored": emit_currencies,
        "ecb_batches": batch_count,
    }
    bound_log.info("fx_sync.pipeline.complete", **summary)
    return summary


# ---------------------------------------------------------------------------
# Celery task definition
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.tasks.fx_tasks.sync_fx_rates_task",
    queue=QUEUE_FETCH,
    bind=True,
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def sync_fx_rates_task(
    self: Any,
    *,
    days_back: int = _DEFAULT_DAYS_BACK,
    currencies: list[str] | None = None,
) -> dict[str, Any]:
    """
    Celery task: fetch ECB FX rates and upsert into *daily_fx_rates*.

    Args:
        days_back:   Number of calendar days back from today to seed.
                     Defaults to 90 (one fiscal quarter + buffer).
        currencies:  List of ISO 4217 currency codes to seed.
                     Defaults to the canonical _DEFAULT_CURRENCIES list.

    Returns:
        Summary dict with ``status``, ``rates_upserted``, ``dates_processed``,
        ``date_range``, and ``currencies_stored``.

    Retry policy:
        Non-network errors (bad rate data, DB failures) are retried up to
        _MAX_RETRIES times with exponential back-off via RETRY_BACKOFF.
        All retries use the original arguments.

    Idempotency:
        Fully idempotent — running multiple times for overlapping date ranges
        is safe due to the ON CONFLICT DO UPDATE upsert strategy.
    """
    bound_log = log.bind(
        task_id=self.request.id,
        days_back=days_back,
        currencies=currencies or "default",
        retries=self.request.retries,
    )
    bound_log.info("sync_fx_rates_task.start")

    # Initialise DB engine (idempotent; no-op after first call in this worker)
    _ensure_db_initialised()

    resolved_currencies: list[str] = (
        [c.upper() for c in currencies] if currencies else _DEFAULT_CURRENCIES
    )

    try:
        summary = asyncio.run(
            _run_fx_sync(
                days_back=days_back,
                currencies=resolved_currencies,
            )
        )
        bound_log.info("sync_fx_rates_task.success", **summary)
        return summary

    except Exception as exc:  # noqa: BLE001
        retry_index = min(self.request.retries, len(_RETRY_BACKOFF) - 1)
        countdown = _RETRY_BACKOFF[retry_index]

        bound_log.warning(
            "sync_fx_rates_task.error",
            error=str(exc)[:300],
            retries_so_far=self.request.retries,
            max_retries=_MAX_RETRIES,
            next_retry_in_seconds=countdown if self.request.retries < _MAX_RETRIES else None,
        )

        if self.request.retries < _MAX_RETRIES:
            raise self.retry(exc=exc, countdown=countdown) from exc

        # Exhausted retries — return a failure summary (do NOT re-raise;
        # keeps the task result in FAILURE state with a usable result body).
        return {
            "status": "failed",
            "error": str(exc)[:500],
            "retries_exhausted": _MAX_RETRIES,
            "days_back": days_back,
            "currencies_requested": resolved_currencies,
        }
