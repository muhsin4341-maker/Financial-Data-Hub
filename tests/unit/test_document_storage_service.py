"""
Unit tests — M3.6 DocumentStorageService.

Covers:
  store() — new document, idempotent re-store (existing record returned)
  retrieve() — delegates to backend via object key from DB record
  document_exists() — delegates to repository
  delete() — removes backend content and DB record

All storage backend and repository calls are mocked — no real I/O.

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acquisition.document_fetcher.fetcher import FilingDocument
from services.acquisition.storage.backend import (
    LocalStorageBackend,
    StorageResult,
)
from services.acquisition.storage.service import DocumentStorageService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACCESSION = "0000320193-24-000009"
_CONTENT = "<html><body>Apple 10-K FY2024</body></html>"
_HASH = "a" * 64
_KEY = "filings/000032019324000009/document.html"
_MIME = "text/html"
_NOW = datetime(2024, 11, 1, 12, 0, 0, tzinfo=UTC)


def _make_filing_document() -> FilingDocument:
    return FilingDocument(
        accession_number=_ACCESSION,
        filing_type="10-K",
        filing_date=date(2024, 10, 31),
        source_url="https://www.sec.gov/Archives/edgar/data/320193/...",
        document_url=None,
        mime_type=_MIME,
        content=_CONTENT,
        content_length=len(_CONTENT.encode()),
        content_hash=_HASH,
        encoding="utf-8",
        plain_text="Apple 10-K FY2024",
        title="Apple Inc. Form 10-K",
        fetched_at=_NOW,
    )


def _make_storage_result(from_cache: bool = False) -> StorageResult:
    return StorageResult(
        key=_KEY,
        bucket_name=None,
        storage_type="local",
        content_length=len(_CONTENT.encode()),
        content_hash=_HASH,
        mime_type=_MIME,
        stored_at=_NOW,
        from_cache=from_cache,
    )


def _make_stored_doc_orm() -> MagicMock:
    """Minimal ORM-like object matching StoredDocumentRead fields."""
    import uuid
    rec = MagicMock()
    rec.id = uuid.uuid4()
    rec.accession_number = _ACCESSION
    rec.storage_type = "local"
    rec.bucket_name = None
    rec.object_key = _KEY
    rec.content_hash = _HASH
    rec.content_length = len(_CONTENT.encode())
    rec.mime_type = _MIME
    rec.stored_at = _NOW
    rec.filing_id = None
    rec.created_at = _NOW
    rec.updated_at = _NOW
    return rec


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDocumentStorageServiceStore:
    @pytest.mark.asyncio
    async def test_store_new_document(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        backend.store.return_value = _make_storage_result(from_cache=False)
        backend.storage_type = "local"

        repo = AsyncMock()
        repo.get_by_accession_number.return_value = None
        repo.create.return_value = _make_stored_doc_orm()

        session = MagicMock()
        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        result = await service.store(_make_filing_document())

        backend.store.assert_called_once()
        repo.create.assert_called_once()
        assert result.accession_number == _ACCESSION

    @pytest.mark.asyncio
    async def test_store_idempotent_returns_existing(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        existing_orm = _make_stored_doc_orm()

        repo = AsyncMock()
        repo.get_by_accession_number.return_value = existing_orm

        session = MagicMock()
        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        result = await service.store(_make_filing_document())

        backend.store.assert_not_called()
        repo.create.assert_not_called()
        assert result.accession_number == _ACCESSION

    @pytest.mark.asyncio
    async def test_store_uses_correct_key(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        backend.store.return_value = _make_storage_result()
        backend.storage_type = "local"

        repo = AsyncMock()
        repo.get_by_accession_number.return_value = None
        repo.create.return_value = _make_stored_doc_orm()

        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        await service.store(_make_filing_document())

        call_args = backend.store.call_args
        assert call_args[0][0] == _KEY  # positional key argument

    @pytest.mark.asyncio
    async def test_store_passes_content_hash(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        backend.store.return_value = _make_storage_result()
        backend.storage_type = "local"

        repo = AsyncMock()
        repo.get_by_accession_number.return_value = None
        repo.create.return_value = _make_stored_doc_orm()

        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        await service.store(_make_filing_document())

        call_kwargs = backend.store.call_args.kwargs
        assert call_kwargs["content_hash"] == _HASH


class TestDocumentStorageServiceRetrieve:
    @pytest.mark.asyncio
    async def test_retrieve_existing(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        backend.retrieve.return_value = _CONTENT

        repo = AsyncMock()
        repo.get_by_accession_number.return_value = _make_stored_doc_orm()

        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        content = await service.retrieve(_ACCESSION)
        assert content == _CONTENT
        backend.retrieve.assert_called_once_with(_KEY)

    @pytest.mark.asyncio
    async def test_retrieve_missing_returns_none(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        repo = AsyncMock()
        repo.get_by_accession_number.return_value = None

        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        result = await service.retrieve("NONEXISTENT-00-000000")
        assert result is None
        backend.retrieve.assert_not_called()


class TestDocumentStorageServiceExists:
    @pytest.mark.asyncio
    async def test_exists_delegates_to_repo(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        repo = AsyncMock()
        repo.exists_by_accession_number.return_value = True

        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        assert await service.document_exists(_ACCESSION) is True
        repo.exists_by_accession_number.assert_called_once_with(_ACCESSION)


class TestDocumentStorageServiceDelete:
    @pytest.mark.asyncio
    async def test_delete_existing(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        backend.delete.return_value = True

        orm = _make_stored_doc_orm()
        repo = AsyncMock()
        repo.get_by_accession_number.return_value = orm
        repo.delete.return_value = True

        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        result = await service.delete(_ACCESSION)
        assert result is True
        backend.delete.assert_called_once_with(_KEY)
        repo.delete.assert_called_once_with(orm.id)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self) -> None:
        backend = AsyncMock(spec=LocalStorageBackend)
        repo = AsyncMock()
        repo.get_by_accession_number.return_value = None

        service = DocumentStorageService.__new__(DocumentStorageService)
        service._backend = backend
        service._repo = repo

        result = await service.delete("NONEXISTENT-00-000000")
        assert result is False
        backend.delete.assert_not_called()
