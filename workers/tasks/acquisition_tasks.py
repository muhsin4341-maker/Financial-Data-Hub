"""
Acquisition Tasks — Celery task definitions for SEC filing acquisition.

Architecture:
  Celery tasks are synchronous entry points that wrap async service code.
  The pattern is: task → asyncio.run() → AcquisitionJobService.execute().

Design decisions:
  - Tasks receive only a job_id string — no large payloads in the message.
  - asyncio.run() creates a fresh event loop per task execution, which is
    safe and idiomatic for Celery workers running on sync worker pools.
  - Session factory and storage backend are constructed inside the task so
    that each task invocation uses independent DB and S3 connections.
  - The task is idempotent: AcquisitionJobService.execute() validates that
    the job is in 'pending' state before starting. Re-queuing a completed
    job raises ValueError (logged and not retried).

Retry policy:
  - max_retries=3 with exponential backoff (2^n * 60 seconds).
  - Non-retryable errors (ValueError — job not found / wrong state) are
    caught and logged without retry.

Milestone: M3.7 — Acquisition Jobs
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from celery import Task

from workers.celery_app import celery_app

log = structlog.get_logger(__name__)


def _build_service() -> object:
    """
    Build an AcquisitionJobService with production dependencies.

    Imports are deferred to avoid circular imports at module load time
    and to allow worker processes to initialise lazily.

    Session factory
    ---------------
    Celery workers are separate processes; they never execute the FastAPI
    application lifespan, so ``apps.api.core.database.AsyncSessionFactory``
    is always ``None`` in this context.  Instead we build a fresh
    ``async_sessionmaker`` directly from settings here — one engine per
    task is acceptable because each task is short-lived and the pool is
    disposed at task exit.

    Storage backend
    ---------------
    S3StorageBackend is used when ``settings.aws_endpoint_url`` is set
    (LocalStack in dev) or when AWS credentials are available (production).
    Falls back to LocalStorageBackend only as a last resort.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apps.api.core.config import get_settings
    from apps.api.core.s3 import make_s3_client
    from services.acquisition.jobs.service import AcquisitionJobService
    from services.acquisition.storage.backend import (
        LocalStorageBackend,
        S3StorageBackend,
    )

    settings = get_settings()

    # Build a per-task async session factory directly from the database URL.
    # docker-compose injects DATABASE_URL=postgresql+asyncpg://…@db:5432/fdh
    # so this resolves correctly inside all worker containers.
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=2,          # acquisition tasks are I/O-bound; small pool is fine
        max_overflow=2,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Use S3StorageBackend when an S3 endpoint is configured (dev + prod).
    # Fall back to local filesystem only when no AWS config is present.
    if settings.s3_documents_bucket and (
        settings.aws_endpoint_url or settings.aws_access_key_id
    ):
        s3_client = make_s3_client()
        backend: object = S3StorageBackend(
            s3_client=s3_client,
            bucket_name=settings.s3_documents_bucket,
        )
        log.info(
            "acquisition_task.storage_backend",
            backend="S3StorageBackend",
            bucket=settings.s3_documents_bucket,
        )
    else:
        backend = LocalStorageBackend("/tmp/fdh-filings")
        log.warning(
            "acquisition_task.storage_backend",
            backend="LocalStorageBackend",
            reason="No S3 bucket configured — using local filesystem fallback",
        )

    return AcquisitionJobService(
        session_factory=session_factory,
        storage_backend=backend,
        user_agent=settings.edgar_user_agent,
    )


@celery_app.task(
    name="workers.tasks.acquisition_tasks.run_acquisition_job",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def run_acquisition_job(self: Task, job_id: str) -> dict:
    """
    Execute the full acquisition pipeline for one company.

    Args:
        job_id: String representation of the AcquisitionJob UUID.

    Returns:
        dict with 'status', 'filings_new', 'documents_stored' from the
        completed job (for Celery result backend consumption).

    Raises:
        celery.exceptions.Retry: On transient errors (up to max_retries).
    """
    log.info("acquisition_task.started", task_id=self.request.id, job_id=job_id)

    try:
        parsed_id = uuid.UUID(job_id)
    except (ValueError, AttributeError) as exc:
        log.error("acquisition_task.invalid_job_id", job_id=job_id, error=str(exc))
        raise

    service = _build_service()

    async def _run() -> object:
        return await service.execute(parsed_id)  # type: ignore[attr-defined]

    try:
        result = asyncio.run(_run())
    except ValueError as exc:
        # Non-retryable: job not found, wrong state, company not resolvable.
        log.error(
            "acquisition_task.non_retryable_error",
            job_id=job_id,
            error=str(exc),
        )
        raise
    except Exception as exc:
        log.warning(
            "acquisition_task.retryable_error",
            job_id=job_id,
            error=str(exc),
            retries=self.request.retries,
        )
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))

    log.info(
        "acquisition_task.completed",
        task_id=self.request.id,
        job_id=job_id,
        status=result.status,
        documents_stored=result.documents_stored,
    )
    return {
        "status": result.status,
        "filings_new": result.filings_new,
        "documents_fetched": result.documents_fetched,
        "documents_stored": result.documents_stored,
    }
