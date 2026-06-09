"""
Export Tasks — Asynchronous Excel workbook generation.

Milestone: D2/B4 — Async Excel Export Pipeline

Architecture position
─────────────────────

  POST /api/v1/jobs/{job_id}/export/async  (B5 — export.py)
    ↓  INSERT excel_export_jobs (status=PENDING)
    ↓  apply_async → QUEUE_EXPORT
  generate_excel_export_task  (D2/B4 — this module)
    ↓  flip status → GENERATING
    ↓  asyncio.run(ExcelExportService.export(...))
    ↓  s3.put_object → s3_exports_bucket/{tenant_id}/exports/{export_id}.xlsx
    ↓  generate_presigned_url (GET, TTL=export_signed_url_expiry_seconds)
    ↓  flip status → SUCCESS, write s3_key + download_url
    ↓  on exception → flip status → FAILED, write error_message[:2000]

  Frontend polls GET /api/v1/jobs/export/{export_id}/status (B5)
    ↓  returns {status, download_url, error_message, ...}
    ↓  on SUCCESS: morph button to green "Download Excel Report" link

Session factory
───────────────
Celery workers run as separate processes and never execute the FastAPI
application lifespan.  ``apps.api.core.database.AsyncSessionFactory`` is
therefore always ``None`` in worker processes.  This task builds a fresh
``async_sessionmaker`` from settings on every invocation, following the
pattern established by acquisition_tasks.py.  Engine is disposed at task exit
to release connection pool resources.

S3 upload
─────────
xlsx bytes are uploaded via synchronous ``boto3.put_object`` (S3 uploads are
blocking I/O in boto3; async upload adds complexity with no throughput benefit
at the typical export file size of ~500 KB–2 MB).  The upload runs in a
``loop.run_in_executor(None, ...)`` call inside the async pipeline so the
event loop is not blocked.

Idempotency
───────────
The Celery task is bound (``bind=True``) so ``self.request.id`` can be
attached to log records.  If the worker crashes mid-flight and restarts, the
task will re-run and overwrite the S3 object with an identical xlsx (the
ExcelExportService produces deterministic output for the same input data).
The status is re-set to GENERATING on retry to give the polling frontend an
accurate lifecycle state.

Retry policy
────────────
max_retries=2, countdown=[30, 120] seconds.  Transient failures (DB
connection blip, S3 throttle) trigger a retry; ValueError / data errors
(job not found, no line items) are non-retryable and immediately FAILED.

Milestone: D2/B4 — Async Excel Export Pipeline
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from io import BytesIO
from typing import Any

import structlog

from workers.celery_app import celery_app

log = structlog.get_logger(__name__)

# Retry backoff (seconds) for index 0→first retry, 1→second retry.
_RETRY_BACKOFF = [30, 120]

# Maximum characters stored in excel_export_jobs.error_message.
_ERROR_MESSAGE_MAX_LEN = 2_000


# ---------------------------------------------------------------------------
# Non-retryable sentinel errors
# ---------------------------------------------------------------------------


class ExportJobNotRetryableError(Exception):
    """Raised for data errors that a retry cannot fix (missing job, no data)."""


# ---------------------------------------------------------------------------
# Async pipeline
# ---------------------------------------------------------------------------


async def _run_export(
    export_job_id_str: str,
    session_factory: Any,
    s3_client: Any,
    settings: Any,
) -> dict[str, Any]:
    """
    Core async export pipeline.

    Steps:
      1.  Load ExcelExportJob record; guard against stale/cancelled records.
      2.  Flip status → GENERATING.
      3.  Load the linked FinancialJob to resolve fiscal metadata.
      4.  Invoke ExcelExportService.export() → xlsx bytes.
      5.  Upload bytes to S3 in a thread executor (blocking boto3 call).
      6.  Generate pre-signed GET URL; apply presigned URL base rewrite
          for local development (LocalStack → host-accessible URL).
      7.  Flip status → SUCCESS, persist s3_key + download_url.
      8.  Return summary dict for Celery result backend.

    Raises:
      ExportJobNotRetryableError: export_job record not found, linked job
                                  missing, or no financial line items for job.
      Exception:                  Any other error → propagated to the task
                                  wrapper, which catches it and flips FAILED.
    """
    from sqlalchemy import select, update

    from apps.api.models import ExcelExportJob, ExcelExportStatus, FinancialJob, Company
    from services.export.excel_generator import (
        ExcelExportService,
        ExportJobNotFoundError,
        ExportCompanyNotFoundError,
        ExportNoDataError,
    )

    export_job_id = uuid.UUID(export_job_id_str)

    async with session_factory() as session:
        # ── 1. Load export job record ─────────────────────────────────────────
        export_job: ExcelExportJob | None = await session.get(
            ExcelExportJob, export_job_id
        )
        if export_job is None:
            raise ExportJobNotRetryableError(
                f"ExcelExportJob {export_job_id_str} not found. "
                "Record may have been deleted before the worker picked it up."
            )

        # Guard: if a previous retry already succeeded (race condition), bail out.
        if export_job.status == ExcelExportStatus.SUCCESS:
            log.info(
                "export_task.already_succeeded",
                export_job_id=export_job_id_str,
            )
            return {
                "status": "already_succeeded",
                "export_job_id": export_job_id_str,
                "download_url": export_job.download_url,
            }

        # ── 2. Flip status → GENERATING ───────────────────────────────────────
        await session.execute(
            update(ExcelExportJob)
            .where(ExcelExportJob.id == export_job_id)
            .values(status=ExcelExportStatus.GENERATING)
        )
        await session.commit()

        log.info(
            "export_task.generating",
            export_job_id=export_job_id_str,
            job_id=str(export_job.job_id),
        )

        # ── 3. Resolve linked FinancialJob for metadata ───────────────────────
        fin_job: FinancialJob | None = await session.get(
            FinancialJob, export_job.job_id
        )
        if fin_job is None:
            raise ExportJobNotRetryableError(
                f"FinancialJob {export_job.job_id} linked to export "
                f"{export_job_id_str} not found."
            )

        company: Company | None = None
        if fin_job.company_id:
            company = await session.get(Company, fin_job.company_id)

        # ── 4. Build xlsx workbook ────────────────────────────────────────────
        service = ExcelExportService()
        try:
            xlsx_bytes: bytes = await service.export(
                job_id=export_job.job_id,
                session=session,
            )
        except ExportJobNotFoundError as exc:
            raise ExportJobNotRetryableError(str(exc)) from exc
        except ExportCompanyNotFoundError as exc:
            raise ExportJobNotRetryableError(str(exc)) from exc
        except ExportNoDataError as exc:
            raise ExportJobNotRetryableError(str(exc)) from exc

        log.info(
            "export_task.workbook_built",
            export_job_id=export_job_id_str,
            size_bytes=len(xlsx_bytes),
        )

        # ── 5. Upload bytes to S3 (blocking call in executor) ─────────────────
        s3_key = (
            f"{export_job.tenant_id}/exports/{export_job_id_str}.xlsx"
        )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: s3_client.put_object(
                Bucket=settings.s3_exports_bucket,
                Key=s3_key,
                Body=BytesIO(xlsx_bytes),
                ContentType=(
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ),
                ContentDisposition=(
                    f'attachment; filename="{export_job_id_str}.xlsx"'
                ),
            ),
        )

        log.info(
            "export_task.s3_uploaded",
            export_job_id=export_job_id_str,
            s3_key=s3_key,
            bucket=settings.s3_exports_bucket,
        )

        # ── 6. Generate pre-signed download URL ───────────────────────────────
        presigned_url: str = await loop.run_in_executor(
            None,
            lambda: s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.s3_exports_bucket,
                    "Key": s3_key,
                },
                ExpiresIn=settings.export_signed_url_expiry_seconds,
            ),
        )

        # Rewrite host for local dev (LocalStack internal → browser-accessible).
        if settings.s3_presigned_url_base and settings.aws_endpoint_url:
            presigned_url = presigned_url.replace(
                settings.aws_endpoint_url,
                settings.s3_presigned_url_base,
                1,
            )

        # ── 7. Flip status → SUCCESS ──────────────────────────────────────────
        await session.execute(
            update(ExcelExportJob)
            .where(ExcelExportJob.id == export_job_id)
            .values(
                status=ExcelExportStatus.SUCCESS,
                s3_key=s3_key,
                download_url=presigned_url,
            )
        )
        await session.commit()

        log.info(
            "export_task.success",
            export_job_id=export_job_id_str,
            s3_key=s3_key,
        )

        return {
            "status": "success",
            "export_job_id": export_job_id_str,
            "s3_key": s3_key,
            "download_url": presigned_url,
            "size_bytes": len(xlsx_bytes),
        }


# ---------------------------------------------------------------------------
# Failure handler
# ---------------------------------------------------------------------------


async def _mark_failed(
    export_job_id: uuid.UUID,
    error_text: str,
    session_factory: Any,
) -> None:
    """Flip the ExcelExportJob status to FAILED and persist the error message."""
    from sqlalchemy import update
    from apps.api.models import ExcelExportJob, ExcelExportStatus

    try:
        async with session_factory() as session:
            await session.execute(
                update(ExcelExportJob)
                .where(ExcelExportJob.id == export_job_id)
                .values(
                    status=ExcelExportStatus.FAILED,
                    error_message=error_text[:_ERROR_MESSAGE_MAX_LEN],
                )
            )
            await session.commit()
    except Exception:
        log.exception(
            "export_task.failed_to_persist_failure",
            export_job_id=str(export_job_id),
        )


# ---------------------------------------------------------------------------
# Celery task entry point
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.tasks.export_tasks.generate_excel_export_task",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def generate_excel_export_task(self: Any, export_job_id_str: str) -> dict[str, Any]:
    """
    Asynchronous Excel workbook generation task.

    Dispatched to ``QUEUE_EXPORT`` by POST /api/v1/jobs/{job_id}/export/async.

    Args:
        export_job_id_str: String UUID of the ExcelExportJob record created
                           by the trigger endpoint.

    Returns:
        Dict with status, export_job_id, s3_key, download_url, size_bytes
        on SUCCESS; or status="failed" with error on terminal failure.

    Retry policy:
        max_retries=2 with exponential backoff (30s, 120s).
        ExportJobNotRetryableError skips retries immediately.
    """
    bound_log = log.bind(
        task_id=self.request.id,
        export_job_id=export_job_id_str,
    )
    bound_log.info("export_task.received")

    # ── Build per-task DB session factory ─────────────────────────────────────
    # Workers don't run the FastAPI lifespan; we create a fresh engine here.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from apps.api.core.config import get_settings
    from apps.api.core.s3 import make_s3_client

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    s3_client = make_s3_client()

    export_job_uuid = uuid.UUID(export_job_id_str)

    try:
        result = asyncio.run(
            _run_export(
                export_job_id_str=export_job_id_str,
                session_factory=session_factory,
                s3_client=s3_client,
                settings=settings,
            )
        )
        bound_log.info("export_task.completed", **result)
        return result

    except ExportJobNotRetryableError as exc:
        # Data error — retrying will not help; mark FAILED immediately.
        error_text = f"{type(exc).__name__}: {exc}"
        bound_log.error("export_task.not_retryable", error=error_text)
        asyncio.run(_mark_failed(export_job_uuid, error_text, session_factory))
        return {"status": "failed", "export_job_id": export_job_id_str, "error": error_text}

    except Exception as exc:
        # Potentially transient error — retry with backoff.
        retry_index = self.request.retries  # 0-based
        backoff = _RETRY_BACKOFF[retry_index] if retry_index < len(_RETRY_BACKOFF) else 300

        error_text = (
            f"{type(exc).__name__}: {exc}\n"
            + "".join(traceback.format_tb(exc.__traceback__))
        )
        bound_log.warning(
            "export_task.retrying",
            error=str(exc),
            retry=retry_index + 1,
            max_retries=self.max_retries,
            countdown=backoff,
        )

        if retry_index >= self.max_retries - 1:
            # Final attempt exhausted — persist FAILED.
            asyncio.run(_mark_failed(export_job_uuid, error_text, session_factory))
            return {"status": "failed", "export_job_id": export_job_id_str, "error": str(exc)}

        raise self.retry(exc=exc, countdown=backoff) from exc

    finally:
        # Dispose engine connection pool regardless of outcome.
        asyncio.run(engine.dispose())
