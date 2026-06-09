"""
Filings router — read endpoints for filing records and stored documents.

Two APIRouter instances are exported from this module:

  filings_router            prefix=/api/v1/filings
  company_filings_router    prefix=/api/v1/companies

Endpoints:

  filings_router:
    GET  /api/v1/filings
      List all filings with filters and pagination.

    GET  /api/v1/filings/{accession_number}
      Retrieve a single filing by SEC EDGAR accession number.

    GET  /api/v1/filings/{accession_number}/document
      Stream the raw stored document content for a filing.

  company_filings_router:
    GET  /api/v1/companies/{ticker}/filings
      List all filings associated with a ticker symbol.

Authorization:
  All endpoints require at minimum require_authenticated (any valid JWT).
  Filings are platform-wide system records — no tenant filtering is applied.
  The auth context is used only for audit logging.

Error codes:
  404 FILING_NOT_FOUND            — filing does not exist
  404 STOREDOCUMENT_NOT_FOUND     — no stored document for this accession
  422 VALIDATION_ERROR            — invalid accession number format
  401 UNAUTHORIZED                — missing or invalid JWT
  403 FORBIDDEN                   — authenticated but insufficient role

Notes:
  - The ``accession_number`` path parameter uses an underscore alias because
    FastAPI path parameters cannot contain hyphens in the URL path segment
    itself.  Callers should supply the canonical 'XXXXXXXXXX-YY-ZZZZZZ' form
    (e.g. '0000320193-23-000077') — FastAPI percent-encodes the hyphens
    automatically and decodes them before the validator runs.

  - Document retrieval fetches the raw filing bytes from the storage backend
    (LocalStorage or S3) and returns them as a streaming response.  The
    Content-Type header is taken from the ``StoredDocument.mime_type`` field.

Milestone: M3.8 — Acquisition APIs
"""

from __future__ import annotations

import re
import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.database import get_db
from apps.api.core.exceptions import NotFoundError, ValidationError
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_authenticated,
)
from apps.api.repositories.filing_documents import StoredDocumentRepository
from apps.api.schemas.filings import FilingListResponse, FilingRead
from apps.api.services.filings import FilingService
from services.acquisition.storage.backend import StorageBackend

log = structlog.get_logger(__name__)

# SEC EDGAR accession number pattern: XXXXXXXXXX-YY-ZZZZZZ
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")

filings_router = APIRouter(prefix="/api/v1/filings", tags=["filings"])
company_filings_router = APIRouter(prefix="/api/v1/companies", tags=["filings"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_storage_backend() -> StorageBackend:
    """
    Return the configured document storage backend (shared with acquisition router).
    """
    from apps.api.core.config import get_settings
    settings = get_settings()
    if settings.aws_access_key_id:
        from apps.api.core.s3 import make_s3_client
        from services.acquisition.storage.backend import S3StorageBackend
        return S3StorageBackend(make_s3_client(), settings.s3_documents_bucket)
    from services.acquisition.storage.backend import LocalStorageBackend
    return LocalStorageBackend("/tmp/fdh-documents")


def _validate_accession(accession_number: str) -> str:
    """
    Validate the accession number format and return the canonical form.

    Raises ValidationError (HTTP 422) on invalid format.
    """
    stripped = accession_number.strip()
    if not _ACCESSION_RE.match(stripped):
        raise ValidationError(
            "accession_number must match the SEC EDGAR format "
            "'XXXXXXXXXX-YY-ZZZZZZ' (e.g. '0000320193-23-000077'). "
            f"Received: {accession_number!r}"
        )
    return stripped


# ---------------------------------------------------------------------------
# GET /api/v1/filings
# ---------------------------------------------------------------------------


@filings_router.get(
    "",
    response_model=FilingListResponse,
    status_code=200,
    summary="List filings",
    description=(
        "Return a paginated list of SEC filing records.  "
        "Supports filtering by filing type, status, CIK, ticker, and date range.  "
        "Results are ordered by filing_date descending (most recent first)."
    ),
)
async def list_filings(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        20, ge=1, le=100, description="Items per page (max 100)."
    ),
    filing_type: str | None = Query(
        None,
        description="SEC form type filter (e.g. '10-K', '10-Q', '8-K').",
    ),
    status: str | None = Query(
        None,
        description=(
            "Lifecycle status filter: "
            "discovered | downloading | downloaded | failed."
        ),
    ),
    cik: str | None = Query(
        None,
        description="SEC CIK filter (numeric; zero-padded automatically).",
    ),
    ticker: str | None = Query(
        None,
        description="Ticker symbol filter (case-insensitive, e.g. 'AAPL').",
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> FilingListResponse:
    service = FilingService(db)
    return await service.list(
        page=page,
        page_size=page_size,
        filing_type=filing_type,
        status=status,
        cik=cik,
        ticker=ticker,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/filings/{accession_number}
# ---------------------------------------------------------------------------


@filings_router.get(
    "/{accession_number}",
    response_model=FilingRead,
    status_code=200,
    summary="Get a filing by accession number",
    description=(
        "Return a single filing record identified by its SEC EDGAR accession "
        "number (format: 'XXXXXXXXXX-YY-ZZZZZZ').  "
        "Returns 404 if no filing with that accession number exists.  "
        "Returns 422 if the accession number format is invalid."
    ),
)
async def get_filing_by_accession(
    accession_number: str,
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> FilingRead:
    accession_number = _validate_accession(accession_number)
    service = FilingService(db)
    return await service.get_by_accession_number(accession_number)


# ---------------------------------------------------------------------------
# GET /api/v1/filings/{accession_number}/document
# ---------------------------------------------------------------------------


@filings_router.get(
    "/{accession_number}/document",
    status_code=200,
    summary="Retrieve the stored document for a filing",
    description=(
        "Return the raw binary content of the primary filing document.  "
        "The Content-Type header reflects the stored MIME type "
        "(e.g. 'text/html', 'application/pdf').  "
        "Returns 404 if the filing or its stored document does not exist.  "
        "Returns 422 if the accession number format is invalid."
    ),
    response_class=Response,
    responses={
        200: {
            "description": "Raw filing document bytes.",
            "content": {
                "text/html": {},
                "application/pdf": {},
                "application/xhtml+xml": {},
            },
        },
        404: {"description": "Filing or stored document not found."},
        422: {"description": "Invalid accession number format."},
    },
)
async def get_filing_document(
    accession_number: str,
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Retrieve the raw document bytes for a stored filing.

    Steps:
      1. Validate accession number format.
      2. Look up StoredDocument metadata in the database.
      3. Retrieve raw content from the storage backend.
      4. Return with Content-Type from the stored MIME type.
    """
    accession_number = _validate_accession(accession_number)

    # 2. Metadata lookup
    doc_repo = StoredDocumentRepository(db)
    stored = await doc_repo.get_by_accession_number(accession_number)
    if stored is None:
        raise NotFoundError("StoredDocument", f"accession_number={accession_number}")

    # 3. Content retrieval
    backend = _get_storage_backend()
    content = await backend.retrieve(stored.object_key)
    if content is None:
        log.error(
            "filing.document.backend_miss",
            accession_number=accession_number,
            object_key=stored.object_key,
        )
        raise NotFoundError("StoredDocument", f"object_key={stored.object_key}")

    # 4. Return
    mime_type = stored.mime_type or "application/octet-stream"
    if isinstance(content, str):
        body = content.encode("utf-8")
    else:
        body = content

    log.info(
        "filing.document.retrieved",
        accession_number=accession_number,
        object_key=stored.object_key,
        content_length=len(body),
    )
    return Response(
        content=body,
        media_type=mime_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{accession_number}.{_mime_to_ext(mime_type)}"'
            ),
            "X-Content-Hash": stored.content_hash,
            "X-Accession-Number": accession_number,
        },
    )


def _mime_to_ext(mime_type: str) -> str:
    """Map a MIME type to a file extension for Content-Disposition."""
    _MAP = {
        "text/html": "html",
        "text/plain": "txt",
        "application/pdf": "pdf",
        "application/xhtml+xml": "xhtml",
        "application/xml": "xml",
        "text/xml": "xml",
        "application/json": "json",
    }
    return _MAP.get(mime_type.split(";")[0].strip().lower(), "bin")


# ---------------------------------------------------------------------------
# GET /api/v1/companies/{ticker}/filings
# ---------------------------------------------------------------------------


@company_filings_router.get(
    "/{ticker}/filings",
    response_model=FilingListResponse,
    status_code=200,
    summary="List filings for a company",
    description=(
        "Return a paginated list of all SEC filings associated with the given "
        "ticker symbol.  The ticker is matched case-insensitively against the "
        "filing records.  "
        "Supports pagination and optional filing type filtering.  "
        "Results are ordered by filing_date descending (most recent first)."
    ),
)
async def list_company_filings(
    ticker: str,
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(
        20, ge=1, le=100, description="Items per page (max 100)."
    ),
    filing_type: str | None = Query(
        None,
        description="Filter by SEC form type (e.g. '10-K', '10-Q', '8-K').",
    ),
    status: str | None = Query(
        None,
        description="Filter by lifecycle status.",
    ),
    ctx: AuthRequestContext = Depends(require_authenticated),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> FilingListResponse:
    """
    List filings for a given company ticker.

    The ticker is normalised to uppercase and matched against the ticker
    column in the filings table (populated during acquisition).  Returns an
    empty list (not 404) if the ticker has no associated filings.
    """
    normalised = ticker.strip().upper()
    if not normalised:
        raise ValidationError("ticker must not be blank.")

    service = FilingService(db)
    return await service.list(
        page=page,
        page_size=page_size,
        ticker=normalised,
        filing_type=filing_type,
        status=status,
    )
