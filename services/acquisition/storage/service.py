"""
Document storage service for the acquisition pipeline.

Responsibilities:
  - Accept a FilingDocument from SECFilingDocumentFetcher.
  - Compute the canonical object key.
  - Deduplicate via content_hash before writing to the backend.
  - Persist storage metadata (key, bucket, hash, length, timestamp) to the
    database via StoredDocumentRepository.
  - Return a StoredDocumentRecord from every store() call.

Architecture position:
  SECFilingDocumentFetcher (M3.5)
    ↓  FilingDocument
  DocumentStorageService   (M3.6) ← this module
    ↓  StoredDocumentRecord (metadata in DB)
    ↓  StorageResult        (content in S3 / local filesystem)

Deduplication:
  Before writing to the backend, the service checks whether a StoredDocument
  row with the same accession_number already exists.  If found, the existing
  record is returned immediately (content_hash preserved, no re-upload).

  The content_hash also guards against storing identical bytes under a new
  accession number — useful for amended filings that re-file identical content.

Session ownership:
  The AsyncSession injected at construction time is NOT committed here.
  The caller (acquisition worker / test) owns the transaction boundary.
  Call ``await session.commit()`` after store() if you need persistence.

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.repositories.filing_documents import StoredDocumentRepository
from apps.api.schemas.filing_documents import StoredDocumentCreate
from services.acquisition.document_fetcher.fetcher import FilingDocument
from services.acquisition.storage.backend import (
    StorageBackend,
    StorageError,
    make_object_key,
)

log = structlog.get_logger(__name__)


class DocumentStorageService:
    """
    Orchestrates filing document persistence.

    Combines a StorageBackend (byte storage) with a StoredDocumentRepository
    (metadata persistence) to provide a single, idempotent store() operation.

    Basic usage::

        backend = LocalStorageBackend("/tmp/filings")
        service = DocumentStorageService(backend=backend, session=db)
        record  = await service.store(filing_document)
        print(record.object_key)

    With S3::

        s3_client = make_s3_client()
        backend = S3StorageBackend(s3_client, settings.s3_documents_bucket)
        service = DocumentStorageService(backend=backend, session=db)
        record  = await service.store(filing_document)
    """

    def __init__(
        self,
        *,
        backend: StorageBackend,
        session: AsyncSession,
    ) -> None:
        self._backend = backend
        self._repo = StoredDocumentRepository(session)

    # ── Public interface ───────────────────────────────────────────────────────

    async def store(self, doc: FilingDocument) -> "StoredDocumentRead":
        """
        Store a FilingDocument and persist metadata to the database.

        Workflow:
          1. Check if a StoredDocument record already exists for this
             accession_number.  If yes, return the existing record (idempotent).
          2. Compute the canonical object key.
          3. Call backend.store() — skips upload when key already exists
             (backend-level deduplication) and returns from_cache=True.
          4. Insert a StoredDocument row with storage metadata.
          5. Return the persisted StoredDocumentRead schema.

        Args:
            doc: FilingDocument returned by SECFilingDocumentFetcher.

        Returns:
            StoredDocumentRead — the persisted metadata record.

        Raises:
            StorageError: When the storage backend encounters an unrecoverable error.
        """
        # 1. Idempotency: return existing record without re-upload.
        existing = await self._repo.get_by_accession_number(doc.accession_number)
        if existing is not None:
            log.info(
                "storage.service.already_stored",
                accession_number=doc.accession_number,
                record_id=str(existing.id),
            )
            from apps.api.schemas.filing_documents import StoredDocumentRead
            return StoredDocumentRead.model_validate(existing)

        # 2. Compute key.
        key = make_object_key(doc.accession_number, doc.mime_type)

        # 3. Store content.
        log.info(
            "storage.service.storing",
            accession_number=doc.accession_number,
            filing_type=doc.filing_type,
            key=key,
            content_length=doc.content_length,
        )
        result = await self._backend.store(
            key,
            doc.content,
            mime_type=doc.mime_type,
            content_hash=doc.content_hash,
        )

        # 4. Persist metadata.
        schema = StoredDocumentCreate(
            accession_number=doc.accession_number,
            storage_type=result.storage_type,
            bucket_name=result.bucket_name,
            object_key=result.key,
            content_hash=result.content_hash,
            content_length=result.content_length,
            mime_type=result.mime_type,
            stored_at=result.stored_at,
        )
        record = await self._repo.create(schema)

        log.info(
            "storage.service.stored",
            accession_number=doc.accession_number,
            record_id=str(record.id),
            storage_type=result.storage_type,
            from_cache=result.from_cache,
        )
        from apps.api.schemas.filing_documents import StoredDocumentRead
        return StoredDocumentRead.model_validate(record)

    async def retrieve(self, accession_number: str) -> str | None:
        """
        Retrieve document content by accession number.

        Looks up the object key from the database, then fetches from the backend.

        Returns:
            Decoded document string, or None when not found.
        """
        record = await self._repo.get_by_accession_number(accession_number)
        if record is None:
            log.debug("storage.service.not_found", accession_number=accession_number)
            return None
        return await self._backend.retrieve(record.object_key)

    async def document_exists(self, accession_number: str) -> bool:
        """
        Return True when a stored document record exists for this accession number.

        Checks the database metadata record only; does not validate that the
        backend object actually exists (use for fast path checks).
        """
        return await self._repo.exists_by_accession_number(accession_number)

    async def delete(self, accession_number: str) -> bool:
        """
        Delete document content from the backend and the metadata record.

        Returns:
            True if the document was found and deleted.
            False if no record exists for this accession number.
        """
        record = await self._repo.get_by_accession_number(accession_number)
        if record is None:
            return False

        deleted_from_backend = await self._backend.delete(record.object_key)
        await self._repo.delete(record.id)

        log.info(
            "storage.service.deleted",
            accession_number=accession_number,
            object_key=record.object_key,
            backend_deleted=deleted_from_backend,
        )
        return True
