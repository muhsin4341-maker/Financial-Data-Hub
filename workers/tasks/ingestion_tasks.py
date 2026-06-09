"""
Ingestion Tasks — Celery task definitions for XBRL/PDF ingestion pipeline.

Amendment V1.2, Section 8.2 — Distributed Idempotency Lock:
  Every ingestion task MUST acquire a Redis distributed lock with the key:

      lock:ingestion:{company_id}:{fiscal_year}

  TTL: 10 minutes (600 seconds).  The lock prevents concurrent ingestion
  workers from processing the same company+year combination simultaneously,
  which would produce duplicate financial_line_items rows or race conditions
  on the point-in-time composite unique constraint.

  If the lock cannot be acquired, the task raises IngestionLockError and
  does NOT retry — the existing worker owns that slot.

Architecture — task isolation pattern (mirrors acquisition_tasks.py):
  Celery tasks are synchronous entry points that wrap async service code.
  Pattern: @celery_app.task → asyncio.run() → async pipeline function.

  All I/O objects (storage backend, Redis client, DB session factory) are
  constructed inside each task execution to ensure independent connections
  per worker invocation.  No module-level state is shared across tasks.

Task message contract:
  Tasks receive lightweight primitive payloads only — no large objects,
  no serialised file content, no ORM instances.  The accession_number is
  the stable identifier used to retrieve document content from storage.

Pipeline position (M4 Step 2):
  AcquisitionTask (acquisition_tasks.py)
    ↓  stores document → StorageBackend + DB
  ingest_xbrl_document (this module)  ← current step
    ↓  retrieves bytes from StorageBackend
    ↓  acquires Redis distributed lock (Amendment V1.2 §8.2)
    ↓  calls parse_xbrl_document → list[ParsedLineItem]
    ↓  returns serialised items ready for bulk DB write (M4 Step 3)
  validate_and_store_line_items (validation_tasks.py)  ← M4 Step 3 / M5

Output hand-off:
  The Celery result contains a JSON-serialisable list of dicts mapping to
  ParsedLineItem fields.  Decimal values are serialised as strings to
  preserve exact precision across JSON round-trips.  Dates are serialised
  as ISO 8601 strings.

Milestone: M4-Step 2 — Orchestration & Storage Integration
All tasks must be idempotent (Engineering Spec Part 2, Section 9.2 Decision 2).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from celery import Task

from workers.celery_app import celery_app
from services.acquisition.jobs.redis_lock import IngestionLock, IngestionLockError  # noqa: F401

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------


def _build_storage_backend() -> object:
    """
    Construct the storage backend for document retrieval.

    Deferred import prevents circular imports at Celery worker startup.
    Uses LocalStorageBackend in development; replace with S3StorageBackend
    when production credentials are available.
    """
    from services.acquisition.storage.backend import LocalStorageBackend

    # TODO M4-prod: replace with S3StorageBackend once settings are wired:
    #   from apps.api.core.config import get_settings
    #   from apps.api.core.s3 import make_s3_client
    #   from services.acquisition.storage.backend import S3StorageBackend
    #   settings = get_settings()
    #   return S3StorageBackend(make_s3_client(), settings.s3_documents_bucket)
    return LocalStorageBackend("/tmp/fdh-filings")


def _build_redis_client() -> object:
    """
    Construct an async Redis client for the distributed ingestion lock.

    Uses redis.asyncio with the same broker URL as Celery.  A fresh client
    is built per task execution; the client is closed explicitly in the
    async pipeline function after the lock is released.

    Returns:
        redis.asyncio.Redis instance ready for async operations.
    """
    import redis.asyncio as aioredis  # type: ignore[import]

    from apps.api.core.config import get_settings

    # Use the configured REDIS_URL so the Docker service name ("redis") is
    # resolved correctly inside the worker containers.  The lock database (DB 3)
    # is appended explicitly; it is intentionally separate from the Celery
    # broker (DB 1) and result backend (DB 2) databases.
    # Fallback to localhost for bare metal / local dev outside Docker.
    settings = get_settings()
    base_url = (settings.redis_url or "redis://localhost:6379/0").rstrip("/")
    # Replace the DB index with /3 (deduplicated lock namespace)
    lock_url = "/".join(base_url.rsplit("/", 1)[:-1]) + "/3"
    return aioredis.from_url(lock_url, decode_responses=False)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialise_line_items(items: list[object]) -> list[dict[str, Any]]:
    """
    Convert list[ParsedLineItem] to a list of JSON-serialisable dicts.

    Decimal values are stored as strings to preserve exact precision.
    date values are stored as ISO 8601 strings (YYYY-MM-DD).
    None values are preserved as null.

    The downstream bulk-insert task (M4 Step 3) reconstructs ParsedLineItem
    objects from these dicts before writing to the database.

    Args:
        items: List of ParsedLineItem dataclass instances.

    Returns:
        JSON-safe list of dicts.
    """
    serialised = []
    for item in items:
        row: dict[str, Any] = {}
        for attr, value in item.__dict__.items():
            if isinstance(value, Decimal):
                row[attr] = str(value)
            elif isinstance(value, date):
                row[attr] = value.isoformat()
            else:
                row[attr] = value
        serialised.append(row)
    return serialised


# ---------------------------------------------------------------------------
# Core async pipeline
# ---------------------------------------------------------------------------


async def _run_xbrl_ingestion(
    *,
    accession_number: str,
    company_id: str,
    fiscal_year: int,
    filing_date_iso: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """
    Core async ingestion pipeline for a single XBRL filing.

    Orchestrates:
      1. Document retrieval from the storage backend.
      2. Redis distributed lock acquisition (Amendment V1.2 §8.2).
      3. Streaming XBRL parse via parse_xbrl_document.
      4. Serialisation of ParsedLineItem list for Celery result hand-off.

    This function is the single authoritative async entry point; the Celery
    task wrapper below drives it via asyncio.run().

    Args:
        accession_number: SEC EDGAR accession number — used as the storage key.
        company_id:       Company UUID string — incorporated into the lock key.
        fiscal_year:      Fiscal year integer — incorporated into the lock key.
        filing_date_iso:  Filing submission date in ISO 8601 format (YYYY-MM-DD).
        source_url:       Public URL of the source XBRL document (audit trail).

    Returns:
        JSON-serialisable list of ParsedLineItem dicts, ready for the
        downstream bulk-insert / validation task (M4 Step 3 / M5).

    Raises:
        IngestionLockError: When another worker holds the lock for this
                            company+year combination. NOT retried.
        StorageNotFoundError: When the document is absent from the backend.
        StorageError: On unrecoverable backend I/O failure. Retried by Celery.
    """
    from datetime import date as date_type

    from services.acquisition.storage.backend import StorageNotFoundError
    from services.ingestion.parsers.xbrl_parser import parse_xbrl_document

    # ── Step 1: Retrieve document bytes from the storage backend ──────────────
    #
    # The storage backend's retrieve() method returns a decoded UTF-8 string.
    # parse_xbrl_document requires raw bytes so it can compute the SHA-256
    # digest from the original content (Amendment V1.2 §4.2).
    #
    # Encoding back to UTF-8 preserves byte-level identity for documents that
    # were originally UTF-8 (virtually all SEC iXBRL filings).  For filings
    # that used a different source encoding, the acquisition layer already
    # normalised to UTF-8 at ingestion time (document_fetcher/fetcher.py).

    backend = _build_storage_backend()
    from services.acquisition.storage.backend import make_object_key

    # The canonical object key is derived from the accession number.
    # We don't know the mime_type at this point, so we probe both xml and html
    # variants in the order most likely for XBRL/iXBRL filings.
    content_str: str | None = None
    object_key: str | None = None

    for mime in ("application/xml", "text/html", "application/xhtml+xml"):
        candidate_key = make_object_key(accession_number, mime)
        content_str = await backend.retrieve(candidate_key)  # type: ignore[union-attr]
        if content_str is not None:
            object_key = candidate_key
            break

    if content_str is None:
        raise StorageNotFoundError(
            f"Document not found in storage for accession {accession_number!r}. "
            "Ensure the acquisition task ran to completion before queuing ingestion."
        )

    # Encode to bytes — SHA-256 is computed from the byte stream, not the string.
    content_bytes: bytes = content_str.encode("utf-8")

    log.info(
        "ingestion.document_retrieved",
        accession_number=accession_number,
        object_key=object_key,
        content_bytes=len(content_bytes),
    )

    # ── Step 2: Acquire Redis distributed lock (Amendment V1.2 §8.2) ──────────
    #
    # The lock key encodes the exact work unit: company_id + fiscal_year.
    # A crash of the current worker releases the lock automatically after the
    # 10-minute TTL, preventing permanent blocking of future ingestion runs.

    redis_client = _build_redis_client()
    try:
        async with IngestionLock(
            redis_client,
            company_id=company_id,
            fiscal_year=fiscal_year,
        ):
            log.info(
                "ingestion.lock_acquired",
                company_id=company_id,
                fiscal_year=fiscal_year,
                accession_number=accession_number,
            )

            # ── Step 3: Parse XBRL document ─────────────────────────────────────
            #
            # parse_xbrl_document internally performs two passes over the bytes:
            #   Pass 1 — collect xbrli:context map (period dates per context_ref).
            #   Pass 2 — stream ix:nonFraction facts with taxonomy + sign resolution.
            #
            # The function is CPU-bound (lxml parsing).  It is run inside the lock
            # so that concurrent parses of the same filing cannot interleave DB
            # writes downstream.  For large filings (>50 MB) consider wrapping in
            # asyncio.to_thread() at M4 Step 3 when the DB write is added.

            filing_date_parsed: date_type = date_type.fromisoformat(filing_date_iso)

            parsed_items = parse_xbrl_document(
                content_bytes,
                company_id=company_id,
                filing_date=filing_date_parsed,
                filing_accession=accession_number,
                source_url=source_url,
            )

            log.info(
                "ingestion.parse_complete",
                accession_number=accession_number,
                company_id=company_id,
                fiscal_year=fiscal_year,
                items_produced=len(parsed_items),
            )

            # ── Step 4: Serialise for downstream hand-off ────────────────────────
            #
            # ParsedLineItem contains Decimal and date fields that are not
            # directly JSON-serialisable.  _serialise_line_items() converts
            # these to strings for safe passage through the Celery result
            # backend (Redis, serialised as JSON).
            #
            # The downstream bulk-insert task (M4 Step 3) reconstructs each
            # ParsedLineItem before writing to financial_line_items.
            #
            # DEFERRED: database bulk insertion and dual-dimension validation
            # are NOT performed here.  They are implemented in M4 Step 3 / M5.

            serialised = _serialise_line_items(parsed_items)

    finally:
        # Ensure the async Redis client is closed even if the lock or parse raised.
        await redis_client.aclose()  # type: ignore[union-attr]

    return serialised


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="workers.tasks.ingestion_tasks.ingest_xbrl_document",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def ingest_xbrl_document(
    self: Task,
    accession_number: str,
    company_id: str,
    fiscal_year: int,
    filing_date_iso: str,
    source_url: str = "",
) -> dict[str, Any]:
    """
    Background task: retrieve, lock, parse, and hand off a single XBRL filing.

    This task is the Celery entry point for M4 Step 2 of the ingestion
    pipeline.  It is enqueued by the acquisition worker after a filing
    document has been stored in the S3/local backend.

    Amendment V1.2 §8.2 compliance:
      The distributed lock is acquired BEFORE the parsing block.
      Duplicate invocations for the same company+year are silently rejected
      (IngestionLockError is raised, task is NOT retried).

    Retry policy:
      max_retries=3 with 2-minute base delay, doubled on each retry.
      Retried: StorageError, unexpected exceptions.
      NOT retried: IngestionLockError (another worker owns the slot),
                   StorageNotFoundError (prerequisite task incomplete).

    Args:
        accession_number: SEC EDGAR accession number (e.g. "0000320193-24-000009").
        company_id:       Company UUID string used in the Redis lock key.
        fiscal_year:      Fiscal year integer used in the Redis lock key.
        filing_date_iso:  Filing date as ISO 8601 string (YYYY-MM-DD).
                          Passed through to parse_xbrl_document for DB storage.
        source_url:       Public URL of the XBRL source document.
                          Stored in ParsedLineItem for Sheet 8 audit trail.

    Returns:
        dict with keys:
          - 'status': 'completed'
          - 'accession_number': the accession number processed
          - 'company_id': the company UUID
          - 'fiscal_year': the fiscal year
          - 'items_parsed': count of ParsedLineItem objects produced
          - 'line_items': JSON-serialisable list of ParsedLineItem dicts
                          (Decimal as str, date as ISO 8601 string)

        The 'line_items' key is consumed by the downstream validation and
        bulk-insert task (M4 Step 3).

    Raises:
        IngestionLockError: Another worker is processing this company+year.
        StorageNotFoundError: Document absent from storage (not retried —
                              acquisition must complete first).
        celery.exceptions.Retry: On transient storage/parsing failures.
    """
    log.info(
        "ingestion_task.started",
        task_id=self.request.id,
        accession_number=accession_number,
        company_id=company_id,
        fiscal_year=fiscal_year,
    )

    # Validate fiscal_year is a plausible integer before doing any I/O.
    if not isinstance(fiscal_year, int) or fiscal_year < 1900 or fiscal_year > 2100:
        raise ValueError(
            f"fiscal_year must be an integer in [1900, 2100], got {fiscal_year!r}"
        )

    # Validate company_id is a well-formed UUID string.
    try:
        uuid.UUID(company_id)
    except (ValueError, AttributeError) as exc:
        log.error(
            "ingestion_task.invalid_company_id",
            company_id=company_id,
            error=str(exc),
        )
        raise ValueError(f"company_id is not a valid UUID: {company_id!r}") from exc

    async def _run() -> list[dict[str, Any]]:
        return await _run_xbrl_ingestion(
            accession_number=accession_number,
            company_id=company_id,
            fiscal_year=fiscal_year,
            filing_date_iso=filing_date_iso,
            source_url=source_url,
        )

    try:
        line_items = asyncio.run(_run())

    except IngestionLockError as exc:
        # Non-retryable: another worker owns this company+year slot.
        # Log at WARNING (not ERROR) — this is an expected race condition.
        log.warning(
            "ingestion_task.lock_contention",
            task_id=self.request.id,
            accession_number=accession_number,
            company_id=company_id,
            fiscal_year=fiscal_year,
            error=str(exc),
        )
        raise  # Surface to Celery result backend without retry.

    except __import__("services.acquisition.storage.backend", fromlist=["StorageNotFoundError"]).StorageNotFoundError as exc:  # noqa: E501
        # Non-retryable: document not yet in storage — acquisition incomplete.
        log.error(
            "ingestion_task.document_not_found",
            task_id=self.request.id,
            accession_number=accession_number,
            error=str(exc),
        )
        raise

    except Exception as exc:
        # Retryable: transient storage I/O error, network blip, parse error.
        log.warning(
            "ingestion_task.retryable_error",
            task_id=self.request.id,
            accession_number=accession_number,
            error=str(exc),
            retries=self.request.retries,
        )
        raise self.retry(exc=exc, countdown=120 * (2 ** self.request.retries))

    log.info(
        "ingestion_task.completed",
        task_id=self.request.id,
        accession_number=accession_number,
        company_id=company_id,
        fiscal_year=fiscal_year,
        items_parsed=len(line_items),
    )
    return {
        "status": "completed",
        "accession_number": accession_number,
        "company_id": company_id,
        "fiscal_year": fiscal_year,
        "items_parsed": len(line_items),
        "line_items": line_items,
    }
