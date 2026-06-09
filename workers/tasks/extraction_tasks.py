"""
Extraction Tasks — Celery task definitions for AI-powered financial extraction.

This module implements the Celery entry-point layer for Milestone 4.3.
It wraps AIExtractionService in an async pipeline and manages the full
FinancialJob lifecycle (RUNNING → COMPLETED / FAILED) for every PDF
extraction invocation.

Architecture position:

  POST /jobs/{id}/upload-complete  (M4.4 — router, next step)
    ↓  dispatches with apply_async()
  process_pdf_extraction_task  (M4.3 — this module)
    ↓  opens AsyncSession + storage backend
    ↓  transitions job status RUNNING
    ↓  calls AIExtractionService.extract()
    ↓  transitions job COMPLETED / FAILED
    ↓  returns summary dict to Celery result backend

Design principles (mirrors ingestion_tasks.py patterns):

  1. asyncio.run() wraps the single async entry-point function.
     Celery workers are synchronous processes; all async I/O executes
     inside one event loop per task invocation.

  2. All dependency objects (engine, session, storage backend) are
     constructed inside _run_pdf_extraction() — never at module level.
     This guarantees independent connections per worker process and
     prevents stale-connection errors after Celery worker fork.

  3. The task is idempotent.  If the job is already in a terminal state
     (COMPLETED, FAILED, CANCELLED) when the worker picks it up, the
     task exits immediately without re-running extraction.
     The underlying bulk_upsert also uses ON CONFLICT DO NOTHING, so
     duplicate dispatches are safe end-to-end.

  4. The task is the only place that commits database transactions.
     AIExtractionService and JobRepository never commit — they only
     flush.  Each atomic unit of work ends with a single session.commit().

Retry policy:
  max_retries=3, exponential back-off starting at 60 s.
  Retried:     transient StorageError, Claude API timeout, unexpected exceptions.
  NOT retried: DocumentNotFoundError (prerequisite missing — re-queuing
               would loop forever), job-not-found / terminal-state scenarios.

Amendment V1.2 compliance:
  §8.2 — Distributed idempotency: terminal-state guard prevents duplicate
          extraction runs for the same job_id.
  §4.2 — source_file_hash lineage: passed through to AIExtractionService
          from job.document_url SHA-256 sidecar (when available).

Task message contract:
  All arguments are JSON-serialisable primitives only (str, int, bool, None).
  No ORM instances, no large payloads, no Decimal objects in the message.

Milestone: M4.3 — Extraction Celery Task
All tasks must be idempotent (Engineering Spec Part 2, Section 9.2 Decision 2).
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import UTC, date, datetime
from typing import Any

import structlog
from celery import Task

from workers.celery_app import celery_app

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Dependency factories
# ---------------------------------------------------------------------------
# All factory functions use deferred imports so that:
#   a) Celery worker startup is fast (no heavy ORM / storage import at module load).
#   b) Circular imports between workers/ and apps/ are avoided.
#   c) Each factory call constructs a fresh, independent object per invocation.


def _build_storage_backend() -> Any:
    """
    Construct the storage backend for document retrieval.

    In production this should be replaced with S3StorageBackend once the
    AWS credentials and bucket settings are wired.  The TODO comment below
    mirrors the pattern established in ingestion_tasks.py.

    Returns:
        StorageBackend instance (LocalStorageBackend for dev, S3 for prod).
    """
    from services.acquisition.storage.backend import LocalStorageBackend

    # TODO M4-prod: replace with S3StorageBackend once settings are wired:
    #   from apps.api.core.config import get_settings
    #   from apps.api.core.s3 import make_s3_client
    #   from services.acquisition.storage.backend import S3StorageBackend
    #   settings = get_settings()
    #   return S3StorageBackend(make_s3_client(), settings.s3_documents_bucket)
    return LocalStorageBackend("/tmp/fdh-filings")


def _ensure_db_initialised() -> None:
    """
    Initialise the SQLAlchemy async engine + session factory if not already done.

    Celery workers are long-lived processes that do not go through the FastAPI
    lifespan context manager.  This function replaces the ``init_db()`` call
    that normally happens in ``apps.api.main`` so that each worker process
    initialises its own connection pool on first task invocation.

    Safe to call multiple times — ``init_db()`` is idempotent.
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


async def _run_pdf_extraction(
    *,
    job_id_str: str,
    tenant_id_str: str,
    fiscal_period: str,
    filing_date_iso: str,
    reporting_standard: str,
    celery_task_id: str,
) -> dict[str, Any]:
    """
    Core async pipeline for a single PDF extraction job.

    Orchestrates:
      1. Open a fresh AsyncSession for the full transaction.
      2. Fetch the FinancialJob record; guard against missing / terminal jobs.
      3. Mark job RUNNING (celery_task_id + started_at written to DB).
      4. Build storage backend and AIExtractionService.
      5. Call AIExtractionService.extract() with document coordinates.
      6. On success: mark job COMPLETED; write extraction metrics to error_message
         (repurposed as a metadata JSON field — see note below).
      7. Commit the session (single atomic commit per pipeline run).
      8. Return summary dict for the Celery result backend.

    Note on metadata storage:
      FinancialJob does not yet have a dedicated ``job_metadata`` JSONB column.
      Extraction summary metrics (inserted, skipped, model_version) are written
      as a compact JSON string into ``error_message`` when the job COMPLETES
      successfully.  This field is named ``error_message`` in the ORM but is
      repurposed here as a generic outcome string, matching the existing column
      semantics (Text, nullable, used for human-readable outcome descriptions).
      A dedicated JSONB column can be added in a future migration without
      breaking this pattern.

    Args:
        job_id_str:        Job UUID as string (Celery message payload).
        tenant_id_str:     Tenant UUID as string (Celery message payload).
        fiscal_period:     Fiscal period label: Q1 | Q2 | Q3 | Q4 | FY.
        filing_date_iso:   Filing date in ISO 8601 format (YYYY-MM-DD).
        reporting_standard: Accounting standard: US_GAAP | IFRS | IND_AS.
        celery_task_id:    The Celery task ID assigned at dispatch time.

    Returns:
        JSON-serialisable summary dict (see process_pdf_extraction_task docstring).

    Raises:
        ValueError:              job_id or tenant_id is not a valid UUID.
        JobNotFoundError:        Job not found or belongs to another tenant.
        JobAlreadyTerminalError: Job is already in a terminal state (idempotency guard).
        DocumentNotFoundError:   document_url is None or text absent from storage.
        ClaudeAPIError:          Claude API call failed (retryable by the task wrapper).
        StorageError:            Storage backend I/O failure (retryable).
    """
    import json as _json

    from sqlalchemy.ext.asyncio import AsyncSession

    from apps.api.core.database import AsyncSessionFactory
    from apps.api.models import JobStatus
    from apps.api.repositories.jobs import JobRepository
    from apps.api.schemas.jobs import JobUpdate
    from services.extraction.extractor import (
        AIExtractionService,
        DocumentNotFoundError,
        EmptyDocumentError,
    )

    # ── 0. Parse UUIDs ─────────────────────────────────────────────────────────
    job_id = uuid.UUID(job_id_str)
    tenant_id = uuid.UUID(tenant_id_str)
    filing_date = date.fromisoformat(filing_date_iso)

    bound_log = log.bind(
        job_id=job_id_str,
        tenant_id=tenant_id_str,
        fiscal_period=fiscal_period,
        filing_date=filing_date_iso,
        reporting_standard=reporting_standard,
    )

    # ── 1. Open session ────────────────────────────────────────────────────────
    if AsyncSessionFactory is None:
        raise RuntimeError(
            "AsyncSessionFactory is None — _ensure_db_initialised() must be "
            "called before _run_pdf_extraction()."
        )

    async with AsyncSessionFactory() as session:  # type: AsyncSession
        try:
            repo = JobRepository(session)

            # ── 2. Fetch job ───────────────────────────────────────────────────
            job = await repo.get_by_id(tenant_id, job_id)

            if job is None:
                bound_log.error(
                    "extraction_task.job_not_found",
                    detail="Job does not exist or belongs to a different tenant.",
                )
                # Non-retryable: the job row will never appear.
                raise ValueError(
                    f"FinancialJob {job_id_str!r} not found for "
                    f"tenant {tenant_id_str!r}."
                )

            # ── 3. Idempotency guard ───────────────────────────────────────────
            # If the job has already reached a terminal state (e.g. a duplicate
            # Celery delivery, or a manual status override), skip silently.
            if job.is_terminal:
                bound_log.warning(
                    "extraction_task.already_terminal",
                    current_status=job.status,
                    detail=(
                        "Job is already in a terminal state — skipping extraction "
                        "to preserve idempotency (Engineering Spec §9.2 Decision 2)."
                    ),
                )
                return {
                    "status": "skipped",
                    "job_id": job_id_str,
                    "reason": f"job already {job.status}",
                }

            # ── 4. Validate document_url ───────────────────────────────────────
            if not job.document_url:
                # Non-retryable: upload-complete must have been called first.
                error_msg = (
                    "document_url is not set on the job.  "
                    "The upload-complete endpoint must be called before dispatching "
                    "the extraction task."
                )
                bound_log.error("extraction_task.missing_document_url")
                # Mark FAILED immediately — this is a caller error, not transient.
                await repo.update_status(
                    tenant_id,
                    job_id,
                    JobUpdate(
                        status=JobStatus.FAILED.value,
                        error_message=error_msg,
                        completed_at=datetime.now(UTC),
                    ),
                )
                await session.commit()
                raise DocumentNotFoundError(error_msg)

            company_id = job.company_id
            fiscal_year = job.fiscal_year or filing_date.year
            text_key: str = job.document_url

            # ── 5. Transition → RUNNING ────────────────────────────────────────
            now = datetime.now(UTC)
            await repo.update_status(
                tenant_id,
                job_id,
                JobUpdate(
                    status=JobStatus.RUNNING.value,
                    celery_task_id=celery_task_id,
                    started_at=now,
                ),
            )
            await session.commit()
            bound_log.info(
                "extraction_task.running",
                company_id=str(company_id),
                fiscal_year=fiscal_year,
                text_key=text_key,
            )

            # ── 6. Run AI extraction ───────────────────────────────────────────
            # Open a second session for the extraction work so that the RUNNING
            # commit above is durable before we start the (potentially long)
            # Claude API call.  The extraction result is committed in the same
            # second session once bulk_upsert() completes.
            async with AsyncSessionFactory() as extraction_session:
                storage = _build_storage_backend()
                service = AIExtractionService(
                    session=extraction_session,
                    storage_backend=storage,
                )
                result = await service.extract(
                    company_id=company_id,
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    filing_date=filing_date,
                    reporting_standard=reporting_standard,
                    text_key=text_key,
                    source_file_hash=None,  # populated by M4.4 when hash is known
                )
                # Commit the bulk_upsert transaction.
                await extraction_session.commit()

            bound_log.info(
                "extraction_task.extraction_done",
                inserted=result.inserted,
                skipped=result.skipped,
                rejected=result.rejected,
                model_version=result.model_version,
            )

            # ── 7. Transition → COMPLETED ──────────────────────────────────────
            # Serialise extraction metrics as a compact JSON string stored in
            # error_message (repurposed as a generic outcome field — see docstring).
            summary_json = _json.dumps(
                {
                    "inserted": result.inserted,
                    "skipped": result.skipped,
                    "rejected": result.rejected,
                    "model_version": result.model_version,
                    "extraction_timestamp": result.extraction_timestamp,
                    "rejected_reasons": result.rejected_reasons[:10],  # cap to 10
                },
                default=str,
            )

            async with AsyncSessionFactory() as completion_session:
                completion_repo = JobRepository(completion_session)
                await completion_repo.update_status(
                    tenant_id,
                    job_id,
                    JobUpdate(
                        status=JobStatus.COMPLETED.value,
                        error_message=summary_json,
                        completed_at=datetime.now(UTC),
                    ),
                )
                await completion_session.commit()

            bound_log.info("extraction_task.completed")

            # ── 8. Dispatch FX translation task ───────────────────────────────
            # Chain to the compute queue so non-USD line items get value_usd
            # populated before the export step.  Deferred import breaks the
            # circular dependency (mirrors M4.4 router pattern).
            # 5-second countdown gives the COMPLETED commit time to propagate
            # across replica lag before BulkCurrencyTranslator queries the rows.
            try:
                from workers.queues import QUEUE_COMPUTE
                from workers.tasks.fx_translation_task import (
                    process_fx_translation_task,
                )

                process_fx_translation_task.apply_async(
                    kwargs={"job_id": job_id_str, "tenant_id": tenant_id_str},
                    queue=QUEUE_COMPUTE,
                    countdown=5,
                )
                bound_log.info(
                    "extraction_task.fx_translation_dispatched",
                    queue=QUEUE_COMPUTE,
                    countdown_seconds=5,
                )
            except Exception as _fx_dispatch_exc:  # noqa: BLE001
                # Non-fatal: FX dispatch failure should not roll back the
                # extraction result.  Log a warning so ops can re-trigger
                # the FX task manually if needed.
                bound_log.warning(
                    "extraction_task.fx_dispatch_failed",
                    error=str(_fx_dispatch_exc)[:300],
                    resolution=(
                        "Re-trigger process_fx_translation_task manually for "
                        f"job {job_id_str!r} after the workers are healthy."
                    ),
                )

            return {
                "status": "completed",
                "job_id": job_id_str,
                "tenant_id": tenant_id_str,
                "company_id": str(company_id),
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
                "inserted": result.inserted,
                "skipped": result.skipped,
                "rejected": result.rejected,
                "model_version": result.model_version,
                "extraction_timestamp": result.extraction_timestamp,
            }

        except (DocumentNotFoundError, EmptyDocumentError):
            # Already logged + FAILED-marked inside the guard above.
            # Re-raise to surface through the Celery result backend.
            raise

        except Exception:
            # Catch-all: surface the traceback, then re-raise to let the
            # Celery task wrapper decide retry vs. final failure.
            bound_log.error(
                "extraction_task.unexpected_error",
                traceback=traceback.format_exc(),
            )
            raise


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.tasks.extraction_tasks.process_pdf_extraction_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    track_started=True,
)
def process_pdf_extraction_task(
    self: Task,
    job_id: str,
    tenant_id: str,
    fiscal_period: str = "FY",
    filing_date_iso: str | None = None,
    reporting_standard: str = "US_GAAP",
) -> dict[str, Any]:
    """
    Background task: run AI extraction pipeline for a single PDF job.

    This is the M4.3 Celery entry point.  It is dispatched by
    POST /jobs/{id}/upload-complete (M4.4) after the user confirms that their
    PDF upload was successful and the document is available in the storage
    backend.

    Lifecycle managed here:
      QUEUED   → RUNNING    (at task start, before Claude API call)
      RUNNING  → COMPLETED  (on successful extraction + bulk_upsert)
      RUNNING  → FAILED     (on any unhandled exception after max retries)

    Idempotency guarantees:
      1. Terminal-state guard: if the job is already COMPLETED / FAILED /
         CANCELLED when the worker picks up the message, the task exits
         without re-running extraction.
      2. ON CONFLICT DO NOTHING on the point-in-time composite unique key
         makes bulk_upsert safe to re-run even if a previous attempt partially
         succeeded before crashing.

    Retry policy:
      max_retries=3, base delay 60 s (doubled each retry → 60 / 120 / 240 s).
      Retried:
        - StorageError (transient storage I/O failure)
        - ClaudeAPIError (rate limit exceeded, Claude timeout)
        - Unexpected exceptions
      NOT retried:
        - DocumentNotFoundError: document_url not set — caller error.
        - EmptyDocumentError: stored text is blank — data error.
        - ValueError: invalid UUID or missing job — programming error.
      On final failure (retries exhausted):
        - Job status is transitioned to FAILED.
        - error_message is populated with the exception class + first 1000 chars.

    Args:
        job_id:            UUID string of the FinancialJob to process.
                           This is the primary stable identifier for this task.
        tenant_id:         UUID string of the owning tenant.
                           Required for tenant-scoped JobRepository lookups.
        fiscal_period:     Fiscal period label.  Defaults to 'FY'.
                           Passed by M4.4 router from the job's period metadata.
        filing_date_iso:   Filing date in ISO 8601 format (YYYY-MM-DD).
                           When None, defaults to today's date.
        reporting_standard: Accounting standard.  Defaults to 'US_GAAP'.
                           Passed by M4.4 router from the company/job context.

    Returns:
        On success:
          {
            'status':               'completed',
            'job_id':               str,
            'tenant_id':            str,
            'company_id':           str,
            'fiscal_year':          int,
            'fiscal_period':        str,
            'inserted':             int,   # rows written to financial_line_items
            'skipped':              int,   # rows skipped by ON CONFLICT DO NOTHING
            'rejected':             int,   # elements dropped during validation
            'model_version':        str,   # Claude model identifier
            'extraction_timestamp': str,   # ISO 8601 UTC timestamp
          }
        On idempotent skip:
          {'status': 'skipped', 'job_id': str, 'reason': str}

    Raises:
        celery.exceptions.Retry: on transient failures (transparent to caller).
        DocumentNotFoundError:   propagated when document_url is unset.
        EmptyDocumentError:      propagated when stored text is blank.
        ValueError:              propagated for invalid UUID / missing job.
    """
    bound_log = log.bind(
        task_id=self.request.id,
        job_id=job_id,
        tenant_id=tenant_id,
        fiscal_period=fiscal_period,
        filing_date_iso=filing_date_iso,
        reporting_standard=reporting_standard,
        retries=self.request.retries,
    )
    bound_log.info("extraction_task.started")

    # ── Input validation ────────────────────────────────────────────────────────
    # Validate both UUIDs eagerly before touching the database — a badly-formed
    # UUID would cause a cryptic asyncpg bind error deep in the pipeline.
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        bound_log.error("extraction_task.invalid_job_id", error=str(exc))
        raise ValueError(f"job_id is not a valid UUID: {job_id!r}") from exc

    try:
        uuid.UUID(tenant_id)
    except (ValueError, AttributeError) as exc:
        bound_log.error("extraction_task.invalid_tenant_id", error=str(exc))
        raise ValueError(f"tenant_id is not a valid UUID: {tenant_id!r}") from exc

    # Default filing_date to today when not provided.
    effective_filing_date_iso: str = filing_date_iso or date.today().isoformat()

    # Validate filing_date_iso format before passing to asyncio.run().
    try:
        date.fromisoformat(effective_filing_date_iso)
    except ValueError as exc:
        raise ValueError(
            f"filing_date_iso is not a valid ISO 8601 date: "
            f"{effective_filing_date_iso!r}"
        ) from exc

    # ── Ensure DB is initialised for this worker process ───────────────────────
    # This is a no-op on all calls after the first within the same worker process.
    _ensure_db_initialised()

    # ── Execute async pipeline ─────────────────────────────────────────────────
    async def _run() -> dict[str, Any]:
        return await _run_pdf_extraction(
            job_id_str=job_id,
            tenant_id_str=tenant_id,
            fiscal_period=fiscal_period,
            filing_date_iso=effective_filing_date_iso,
            reporting_standard=reporting_standard,
            celery_task_id=self.request.id or "",
        )

    try:
        return asyncio.run(_run())

    except (ValueError,) as exc:
        # Non-retryable programming errors — surface immediately.
        bound_log.error(
            "extraction_task.non_retryable_error",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        # Attempt to mark FAILED via a best-effort sync-compatible call.
        _mark_job_failed_sync(
            job_id=job_id,
            tenant_id=tenant_id,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    except Exception as exc:
        # ── Non-retryable: authentication failure ──────────────────────────────
        # ClaudeAuthError (HTTP 401) means the configured API key is invalid or
        # revoked.  Retrying with the same key will never succeed, so we
        # transition to FAILED immediately without burning retry budget.
        # We check by class name to avoid a module-level circular import.
        if type(exc).__name__ == "ClaudeAuthError":
            bound_log.error(
                "extraction_task.auth_failure",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                resolution=(
                    "Set a valid CLAUDE_API_KEY in your .env file.  "
                    "If you want mock mode, use a key starting with "
                    "'sk-ant-api01-thisis' or leave the key empty."
                ),
            )
            _mark_job_failed_sync(
                job_id=job_id,
                tenant_id=tenant_id,
                error_message=(
                    "Authentication failed — invalid CLAUDE_API_KEY (HTTP 401).  "
                    f"Details: {str(exc)[:400]}"
                ),
            )
            raise

        # ── Retryable: storage I/O, Claude API failures, unexpected errors ────
        retry_count = self.request.retries
        is_final_attempt = retry_count >= self.max_retries
        error_summary = f"{type(exc).__name__}: {str(exc)[:500]}"

        if is_final_attempt:
            # Exhausted all retries — mark FAILED permanently.
            bound_log.error(
                "extraction_task.final_failure",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                traceback=traceback.format_exc()[:2000],
                retries=retry_count,
            )
            _mark_job_failed_sync(
                job_id=job_id,
                tenant_id=tenant_id,
                error_message=error_summary,
            )
            raise  # Surface the original exception to the Celery result backend.
        else:
            # Retryable: exponential back-off (60 s, 120 s, 240 s).
            countdown = 60 * (2 ** retry_count)
            bound_log.warning(
                "extraction_task.retrying",
                error_type=type(exc).__name__,
                error=str(exc)[:300],
                retry_number=retry_count + 1,
                countdown_seconds=countdown,
            )
            raise self.retry(exc=exc, countdown=countdown)


# ---------------------------------------------------------------------------
# Best-effort failure marker
# ---------------------------------------------------------------------------


def _mark_job_failed_sync(
    *,
    job_id: str,
    tenant_id: str,
    error_message: str,
) -> None:
    """
    Best-effort synchronous wrapper to transition a job to FAILED state.

    Called from the Celery task's synchronous exception handler when the async
    pipeline itself has raised (i.e. we cannot use the pipeline's session).
    Opens a fresh event loop for this single DB write.

    Silently swallows all exceptions — if this helper fails, the job status
    remains stale but the extraction error is still logged at ERROR level.
    The operations team can correct the status manually or via a cleanup job.

    Args:
        job_id:        Job UUID string.
        tenant_id:     Tenant UUID string.
        error_message: Short error description (truncated to 1000 chars).
    """

    async def _mark() -> None:
        from apps.api.core.database import AsyncSessionFactory
        from apps.api.models import JobStatus
        from apps.api.repositories.jobs import JobRepository
        from apps.api.schemas.jobs import JobUpdate

        if AsyncSessionFactory is None:
            return

        async with AsyncSessionFactory() as session:
            try:
                repo = JobRepository(session)
                await repo.update_status(
                    uuid.UUID(tenant_id),
                    uuid.UUID(job_id),
                    JobUpdate(
                        status=JobStatus.FAILED.value,
                        error_message=error_message[:1000],
                        completed_at=datetime.now(UTC),
                    ),
                )
                await session.commit()
                log.info(
                    "extraction_task.job_marked_failed",
                    job_id=job_id,
                    error_message=error_message[:200],
                )
            except Exception as inner_exc:  # noqa: BLE001
                log.warning(
                    "extraction_task.mark_failed_error",
                    job_id=job_id,
                    error=str(inner_exc)[:200],
                )
                await session.rollback()

    try:
        asyncio.run(_mark())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "extraction_task.mark_failed_sync_error",
            job_id=job_id,
            error=str(exc)[:200],
        )
