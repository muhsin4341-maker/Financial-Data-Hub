"""
StoredDocument repository — database operations for filing document storage metadata.

Follows the same conventions as FilingRepository (M3.3):
  - No tenant_id: stored documents are global system records.
  - Session is never committed here; the caller owns the transaction boundary.
  - flush() after add/modify so generated values are available before commit.

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import StoredDocument
from apps.api.schemas.filing_documents import StoredDocumentCreate

log = structlog.get_logger(__name__)


class StoredDocumentRepository:
    """
    Database access layer for StoredDocument records.

    Instantiated per-request or per-task::

        repo = StoredDocumentRepository(session)
        record = await repo.get_by_accession_number("0000320193-24-000009")
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(self, schema: StoredDocumentCreate) -> StoredDocument:
        """
        Persist a new StoredDocument metadata record.

        Args:
            schema: Validated StoredDocumentCreate.

        Returns:
            Persisted StoredDocument with id and timestamps populated.
        """
        record = StoredDocument(
            filing_id=schema.filing_id,
            accession_number=schema.accession_number,
            storage_type=schema.storage_type,
            bucket_name=schema.bucket_name,
            object_key=schema.object_key,
            content_hash=schema.content_hash,
            content_length=schema.content_length,
            mime_type=schema.mime_type,
            stored_at=schema.stored_at,
        )
        self._session.add(record)
        await self._session.flush([record])
        log.debug(
            "stored_document.repository.created",
            record_id=str(record.id),
            accession_number=record.accession_number,
            storage_type=record.storage_type,
            object_key=record.object_key,
        )
        return record

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(self, record_id: uuid.UUID) -> StoredDocument | None:
        result = await self._session.execute(
            select(StoredDocument).where(StoredDocument.id == record_id)
        )
        return result.scalar_one_or_none()

    async def get_by_accession_number(
        self, accession_number: str
    ) -> StoredDocument | None:
        result = await self._session.execute(
            select(StoredDocument).where(
                StoredDocument.accession_number == accession_number
            )
        )
        return result.scalar_one_or_none()

    async def exists_by_accession_number(self, accession_number: str) -> bool:
        result = await self._session.execute(
            select(func.count())
            .select_from(StoredDocument)
            .where(StoredDocument.accession_number == accession_number)
        )
        return result.scalar_one() > 0

    async def get_by_content_hash(self, content_hash: str) -> list[StoredDocument]:
        """
        Return all records with the given content hash.

        Used to detect when semantically different filings share identical
        content (e.g. re-filed amendments with no changes).
        """
        result = await self._session.execute(
            select(StoredDocument).where(StoredDocument.content_hash == content_hash)
        )
        return list(result.scalars().all())

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete(self, record_id: uuid.UUID) -> bool:
        record = await self.get_by_id(record_id)
        if record is None:
            return False
        await self._session.delete(record)
        await self._session.flush()
        log.debug(
            "stored_document.repository.deleted",
            record_id=str(record_id),
        )
        return True
