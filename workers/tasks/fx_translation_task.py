"""
FX Translation Task — Celery background worker for dual-pass currency translation.

Wraps ``BulkCurrencyTranslator`` (services/currency/bulk_processor.py) in a
Celery task that runs on the ``compute`` queue immediately after the AI
extraction pipeline completes for a given job.

Architecture position
─────────────────────

  POST /jobs/{id}/upload-complete  (M4.4 — router)
    ↓  apply_async → QUEUE_AI
  process_pdf_extraction_task  (M4.3 — extraction_tasks.py)
    ↓  writes financial_line_items with value_usd = NULL
    ↓  on COMPLETED: apply_async → QUEUE_COMPUTE  (see chaining blueprint)
  process_fx_translation_task  (M5.6 — this module)
    ↓  BulkCurrencyTranslator queries value_usd IS NULL rows
    ↓  CurrencyTranslationEngine applies dual-pass FX rates
    ↓  UPDATE financial_line_items SET value_usd, fx_rate_used
    ↓  commits (or rolls back savepoint on MissingFXRateException)
    ↓  returns translation summary dict

Chaining blueprint — how to wire into extraction_tasks.py
──────────────────────────────────────────────────────────
Option A — Sequential dispatch after extraction COMPLETED (recommended):
  Insert the following block at the end of ``_run_pdf_extraction`` in
  extraction_tasks.py, AFTER the COMPLETED commit (currently line ~360):

    # ── Dispatch FX translation task ───────────────────────────────────────
    # Deferred import breaks circular dependency (matches M4.4 router pattern).
    from workers.tasks.fx_translation_task import process_fx_translation_task
    from workers.queues import QUEUE_COMPUTE

    process_fx_translation_task.apply_async(
        kwargs={"job_id": job_id_str, "tenant_id": tenant_id_str},
        queue=QUEUE_COMPUTE,
        countdown=5,          # 5 s grace so the COMPLETED commit propagates
        retry=True,
        retry_policy={"max_retries": 3, "interval_start": 10, "interval_step": 10},
    )

  This is the recommended approach: it runs on different queues (AI vs.
  compute), does not block the extraction worker pool, and guarantees the
  COMPLETED commit is durable before translation begins.

Option B — Celery canvas chain (dispatched together at the router):

    from celery import chain
    from workers.tasks.extraction_tasks import process_pdf_extraction_task
    from workers.tasks.fx_translation_task import process_fx_translation_task

    chain(
        process_pdf_extraction_task.si(
            job_id=str(job_id),
            tenant_id=str(tenant_id),
            fiscal_period=fiscal_period,
            filing_date_iso=filing_date_iso,
            reporting_standard=reporting_standard,
        ),
        process_fx_translation_task.si(
            job_id=str(job_id),
            tenant_id=str(tenant_id),
        ),
    ).apply_async(queue=QUEUE_AI)

  Option B is simpler but ties both tasks to the AI queue and dispatches the
  FX task immediately regardless of extraction outcome.

Design principles
─────────────────
1. asyncio.run() wraps the single async entry-point — same pattern as
   extraction_tasks.py.  All async I/O executes inside one event loop per
   task invocation.

2. All dependency objects (sessions, FXRateRepository, engine) are constructed
   inside ``_run_fx_translation()`` — never at module level.  Prevents stale
   connections after Celery worker fork.

3. Idempotency:
   BulkCurrencyTranslator queries only rows where ``value_usd IS NULL``.
   Already-translated rows are silently skipped.  Re-running after a partial
   failure re-translates exactly the rolled-back rows.

4. Transaction ownership:
   BulkCurrencyTranslator wraps translation mutations in a savepoint
   (``session.begin_nested()``).  The Celery task commits the session on
   success.  On MissingFXRateException the savepoint is rolled back by
   BulkCurrencyTranslator; this task then commits the outer session so the
   'failed_fx_data_gap' job-status update (written OUTSIDE the savepoint by
   BulkCurrencyTranslator) becomes durable.

5. MissingFXRateException is NOT retried — re-queuing would loop forever
   until the FX rate table is populated.  The job is marked 'failed_fx_data_gap';
   ops must populate ``daily_fx_rates`` and re-trigger via the API.

Retry policy:
  max_retries=3, exponential back-off starting at 30 s.
  Retried:     transient SQLAlchemyError (DB flakiness, connection reset).
  NOT retried: MissingFXRateException, ValueError (programming / data errors).

Task message contract:
  All arguments are JSON-serialisable primitives (str only).

Milestone: M5.6 — Celery Background FX Translation Task
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from celery import Task

from workers.celery_app import celery_app
from workers.queues import QUEUE_COMPUTE

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exception type helper — avoids a top-level import of MissingFXRateException
# which would force the services layer to load at Celery worker startup.
# ---------------------------------------------------------------------------

def _is_fx_gap_error(exc: BaseException) -> bool:
    """
    Return True when *exc* is a ``MissingFXRateException``.

    The deferred import means the services layer is not loaded until the first
    task invocation, keeping worker startup fast and avoiding import cycles.
    """
    try:
        from services.currency.translator import MissingFXRateException
        return isinstance(exc, MissingFXRateException)
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Dependency initialisation helpers
# ---------------------------------------------------------------------------


def _ensure_db_initialised() -> None:
    """
    Initialise the SQLAlchemy async engine + session factory for this worker.

    Celery workers are long-lived processes that bypass the FastAPI lifespan
    context manager.  This function is idempotent — ``init_db()`` no-ops on
    all calls after the first within the same worker process.
    """
    from apps.api.core.config import get_settings
    from apps.api.core.database import init_db

    settings = get_settings()
    init_db(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        echo=settings.debug,
    )


# ---------------------------------------------------------------------------
# Core async pipeline
# ---------------------------------------------------------------------------


async def _run_fx_translation(
    *,
    job_id_str: str,
    tenant_id_str: str,
    celery_task_id: str,
) -> dict[str, Any]:
    """
    Core async pipeline for a single FX translation run.

    Orchestrates:
      1. Parse and validate UUIDs.
      2. Guard against AsyncSessionFactory being uninitialised.
      3. Open AsyncSession; load FinancialJob; guard terminal / skip states.
      4. Build FXRateRepository → CurrencyTranslationEngine → BulkCurrencyTranslator.
      5a. Success path: BulkCurrencyTranslator writes value_usd / fx_rate_used;
          this function commits the session.
      5b. FX-gap path: savepoint already rolled back inside BulkCurrencyTranslator;
          this function commits so the 'failed_fx_data_gap' job-status persists;
          raises MissingFXRateException (non-retryable, propagates to task wrapper).
      6. Return JSON-serialisable summary dict.

    Args:
        job_id_str:      Job UUID as string (Celery message payload).
        tenant_id_str:   Tenant UUID as string (Celery message payload).
        celery_task_id:  The Celery task ID assigned at dispatch time.

    Returns:
        JSON-serialisable result dict.

    Raises:
        ValueError:              Invalid UUID or job not found.
        MissingFXRateException:  FX data gap — non-retryable; job already marked
                                 'failed_fx_data_gap' before re-raise.
        SQLAlchemyError:         Retryable storage failure.
        RuntimeError:            AsyncSessionFactory uninitialised.
    """
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401 (type annotation)

    from apps.api.core.database import AsyncSessionFactory
    from apps.api.models import FinancialJob
    from apps.api.repositories.fx_rates import FXRateRepository
    from services.currency.bulk_processor import BulkCurrencyTranslator
    from services.currency.translator import (
        CurrencyTranslationEngine,
        MissingFXRateException,
    )

    # ── 0. Parse and validate UUIDs ────────────────────────────────────────────
    job_id = uuid.UUID(job_id_str)
    # tenant_id retained for structured logging; future tenant-scoped repo queries.
    uuid.UUID(tenant_id_str)

    bound_log = log.bind(
        job_id=job_id_str,
        tenant_id=tenant_id_str,
        celery_task_id=celery_task_id,
    )

    # ── 1. Guard: session factory must be ready ────────────────────────────────
    if AsyncSessionFactory is None:
        raise RuntimeError(
            "AsyncSessionFactory is None — _ensure_db_initialised() must be "
            "called before _run_fx_translation()."
        )

    # ── 2. Open session and run translation ────────────────────────────────────
    # A single session owns the entire batch.  BulkCurrencyTranslator uses a
    # savepoint (begin_nested) for the batch mutations — this outer session is
    # committed on success or after flushing the job-status update on FX-gap.
    async with AsyncSessionFactory() as session:
        try:
            # ── 3. Load job, guard terminal states ─────────────────────────────
            job = await session.get(FinancialJob, job_id)

            if job is None:
                bound_log.error(
                    "fx_translation_task.job_not_found",
                    detail="FinancialJob row does not exist.",
                )
                raise ValueError(f"FinancialJob {job_id_str!r} not found.")

            # Jobs already in hard-terminal states are not re-translated.
            # 'failed_fx_data_gap' is a soft-terminal: ops can re-trigger
            # the task after populating the missing rate.
            _SKIP_STATUSES: frozenset[str] = frozenset({
                "cancelled",
                "failed",
            })
            if job.status in _SKIP_STATUSES:
                bound_log.warning(
                    "fx_translation_task.skipped",
                    status=job.status,
                    detail="Job is in a terminal state that precludes FX translation.",
                )
                return {
                    "status": "skipped",
                    "job_id": job_id_str,
                    "reason": f"job status is {job.status!r}",
                }

            bound_log.info(
                "fx_translation_task.running",
                job_status=job.status,
                company_id=str(job.company_id) if job.company_id else None,
                fiscal_year=job.fiscal_year,
            )

            # ── 4. Build dependency graph ──────────────────────────────────────
            # FXRateRepository satisfies both:
            #   • FXRateRepository Protocol   (translator.py) — get_rates_in_range
            #   • FXRateProvider Protocol     (currency.py)   — get_spot_rate /
            #                                                    get_weighted_average_rate
            fx_repo = FXRateRepository(session)
            engine = CurrencyTranslationEngine(repo=fx_repo)
            translator = BulkCurrencyTranslator(engine=engine, session=session)

            # ── 5. Run translation ─────────────────────────────────────────────
            result = await translator.translate_for_job(job_id=job_id)

            if result.success:
                # ── 5a. Success: commit all value_usd / fx_rate_used mutations ─
                await session.commit()

                bound_log.info(
                    "fx_translation_task.completed",
                    rows_translated=result.rows_translated,
                    rows_skipped_usd=result.rows_skipped_usd,
                    rows_skipped_null=result.rows_skipped_null,
                    total_loaded=result.total_loaded,
                    summary=result.summary(),
                )

                return {
                    "status":             "completed",
                    "job_id":             job_id_str,
                    "tenant_id":          tenant_id_str,
                    "rows_translated":    result.rows_translated,
                    "rows_skipped_usd":   result.rows_skipped_usd,
                    "rows_skipped_null":  result.rows_skipped_null,
                    "total_loaded":       result.total_loaded,
                    "job_status_updated": result.job_status_updated_to,
                }

            else:
                # ── 5b. FX data gap ────────────────────────────────────────────
                # BulkCurrencyTranslator already:
                #   - rolled back the inner savepoint (mutations gone)
                #   - wrote 'failed_fx_data_gap' to job.status OUTSIDE the
                #     savepoint (mutation still pending in the outer session)
                # Commit the outer session so the status update is durable.
                await session.commit()

                missing = result.missing_rate_details or {}
                bound_log.error(
                    "fx_translation_task.fx_data_gap",
                    currency=missing.get("currency"),
                    target_date=missing.get("target_date"),
                    context=missing.get("context"),
                    lookback_days=missing.get("lookback_days"),
                    rows_failed=result.rows_failed,
                    action_required=(
                        f"Populate daily_fx_rates for "
                        f"{missing.get('currency')!r} around "
                        f"{missing.get('target_date')} then re-trigger "
                        f"process_fx_translation_task for job {job_id_str!r}."
                    ),
                )

                # Re-raise as MissingFXRateException so the task wrapper can
                # distinguish this non-retryable fault from transient errors.
                raise MissingFXRateException(
                    currency=missing.get("currency", "UNKNOWN"),
                    target_date=missing.get("target_date", "UNKNOWN"),
                    context=missing.get("context", "batch"),
                    lookback_days=int(missing.get("lookback_days", 5)),
                )

        except (MissingFXRateException, ValueError):
            # Structured errors — do not swallow; the outer try/except here is
            # belt-and-suspenders only.  Rollback is safe even if already committed.
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise

        except Exception:
            # Unexpected errors — log full traceback before propagating.
            bound_log.error(
                "fx_translation_task.unexpected_error",
                traceback=traceback.format_exc(),
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise


# ---------------------------------------------------------------------------
# Celery task — single canonical registration
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.tasks.fx_translation_task.process_fx_translation_task",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    track_started=True,
    # FX translation is a DB-bound compute task; grant generous time limits
    # for large batches (full-year IND_AS filing with 500+ line items).
    soft_time_limit=600,
    time_limit=660,
    queue=QUEUE_COMPUTE,
)
def process_fx_translation_task(
    self: Task,
    job_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    """
    Background task: run dual-pass FX currency translation for a single job.

    This is the M5.6 Celery entry point.  It should be dispatched by
    ``process_pdf_extraction_task`` immediately after that task marks the job
    COMPLETED (see chaining blueprint in this module's docstring).

    What it does:
      1. Queries all ``financial_line_items`` rows where ``value_usd IS NULL``
         for the job's company + fiscal_year.
      2. For each non-USD row applies the Amendment V1.2 §3 dual-pass rules:
         - Balance Sheet → spot rate on ``period_end_date``  (Pass 1)
         - IS / CF       → arithmetic weighted-average rate  (Pass 2)
      3. Writes ``value_usd`` and ``fx_rate_used`` to each translated row.
      4. Commits.

    Idempotency:
      ``value_usd IS NULL`` filter ensures only untranslated rows are touched.
      Re-running after a partial failure re-translates exactly the rolled-back
      rows; already-translated rows are silently skipped.

    Retry policy:
      max_retries=3, back-off 30 / 60 / 120 s.
      Retried:     transient ``SQLAlchemyError`` and unexpected exceptions.
      NOT retried: ``MissingFXRateException`` (ops must populate missing rates),
                   ``ValueError`` (bad UUID / missing job).

    Args:
        job_id:    UUID string of the FinancialJob whose untranslated line items
                   should be currency-normalised.
        tenant_id: UUID string of the owning tenant (structured logging and
                   future tenant-scoped repository queries).

    Returns:
        On success::

          {
            'status':             'completed',
            'job_id':             str,
            'tenant_id':          str,
            'rows_translated':    int,   # rows with value_usd written
            'rows_skipped_usd':   int,   # already-USD rows; rate written as 1
            'rows_skipped_null':  int,   # null value_reported; no translation
            'total_loaded':       int,
            'job_status_updated': str | None,
          }

        On idempotent skip::

          {'status': 'skipped', 'job_id': str, 'reason': str}

    Raises:
        MissingFXRateException:  Non-retryable FX data gap; job marked
                                 ``'failed_fx_data_gap'`` before raising.
        celery.exceptions.Retry: Transparent to caller on transient errors.
        ValueError:              Propagated for invalid UUID / missing job.
    """
    bound_log = log.bind(
        task_id=self.request.id,
        job_id=job_id,
        tenant_id=tenant_id,
        retries=self.request.retries,
    )
    bound_log.info("fx_translation_task.received")

    # ── Input validation ────────────────────────────────────────────────────────
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        bound_log.error("fx_translation_task.invalid_job_id", error=str(exc))
        raise ValueError(f"job_id is not a valid UUID: {job_id!r}") from exc

    try:
        uuid.UUID(tenant_id)
    except (ValueError, AttributeError) as exc:
        bound_log.error("fx_translation_task.invalid_tenant_id", error=str(exc))
        raise ValueError(f"tenant_id is not a valid UUID: {tenant_id!r}") from exc

    # ── Ensure DB is initialised for this worker process ───────────────────────
    _ensure_db_initialised()

    # ── Execute async pipeline ─────────────────────────────────────────────────
    async def _run() -> dict[str, Any]:
        return await _run_fx_translation(
            job_id_str=job_id,
            tenant_id_str=tenant_id,
            celery_task_id=self.request.id or "",
        )

    try:
        return asyncio.run(_run())

    except Exception as exc:
        retry_count = self.request.retries
        is_final_attempt = retry_count >= self.max_retries

        # ── Non-retryable: FX data gap ─────────────────────────────────────────
        # Job is already marked 'failed_fx_data_gap' inside _run_fx_translation.
        # Do NOT retry — ops must populate the missing rate(s) first.
        if _is_fx_gap_error(exc):
            bound_log.error(
                "fx_translation_task.non_retryable_fx_gap",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                resolution=(
                    "Populate the missing rate in daily_fx_rates and re-trigger "
                    "process_fx_translation_task via the API or Celery shell."
                ),
            )
            raise  # Surface to Celery result backend without retrying.

        # ── Non-retryable: programming / data error ────────────────────────────
        if isinstance(exc, ValueError):
            bound_log.error(
                "fx_translation_task.non_retryable_value_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

        # ── Retryable: transient storage / unexpected error ────────────────────
        error_summary = f"{type(exc).__name__}: {str(exc)[:500]}"

        if is_final_attempt:
            bound_log.error(
                "fx_translation_task.final_failure",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                traceback=traceback.format_exc()[:2000],
                retries=retry_count,
            )
            _mark_translation_failed_sync(
                job_id=job_id,
                error_message=error_summary,
            )
            raise  # Surface original exception to the Celery result backend.

        # Exponential back-off: 30 s → 60 s → 120 s.
        countdown = 30 * (2 ** retry_count)
        bound_log.warning(
            "fx_translation_task.retrying",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
            retry_number=retry_count + 1,
            countdown_seconds=countdown,
        )
        raise self.retry(exc=exc, countdown=countdown)


# ---------------------------------------------------------------------------
# Best-effort failure marker
# ---------------------------------------------------------------------------


def _mark_translation_failed_sync(
    *,
    job_id: str,
    error_message: str,
) -> None:
    """
    Best-effort synchronous wrapper to transition a job to FAILED state.

    Called from the Celery task's synchronous exception handler on final
    failure (retries exhausted).  Opens a fresh event loop for this single
    DB write so the failure is recorded even though the pipeline's async
    session has already been torn down.

    Silently swallows all exceptions — if this helper fails, the job status
    remains stale but the error is logged at ERROR level.  Operations can
    correct the status manually or via a cleanup job.

    Args:
        job_id:        Job UUID string.
        error_message: Short error description (truncated to 1000 chars).
    """

    async def _mark() -> None:
        from apps.api.core.database import AsyncSessionFactory
        from apps.api.models import FinancialJob, JobStatus

        if AsyncSessionFactory is None:
            return

        async with AsyncSessionFactory() as session:
            try:
                job = await session.get(FinancialJob, uuid.UUID(job_id))
                if job is None:
                    return
                # Respect BulkCurrencyTranslator status if already set
                # (e.g. 'failed_fx_data_gap' was written before the outer
                # exception propagated).  Only override non-terminal states.
                _already_terminal = {
                    "completed", "failed", "cancelled", "failed_fx_data_gap",
                }
                if job.status not in _already_terminal:
                    job.status = JobStatus.FAILED.value
                    job.error_message = error_message[:1000]
                    job.updated_at = datetime.now(UTC)
                    await session.commit()
                    log.info(
                        "fx_translation_task.job_marked_failed",
                        job_id=job_id,
                        error_message=error_message[:200],
                    )
            except Exception as inner_exc:  # noqa: BLE001
                log.warning(
                    "fx_translation_task.mark_failed_error",
                    job_id=job_id,
                    error=str(inner_exc)[:200],
                )
                await session.rollback()

    try:
        asyncio.run(_mark())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "fx_translation_task.mark_failed_sync_error",
            job_id=job_id,
            error=str(exc)[:200],
        )
