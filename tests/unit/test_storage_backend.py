"""
Unit tests — M3.6 StorageBackend (LocalStorageBackend, S3StorageBackend, helpers).

Covers:
  make_object_key — key generation for various MIME types
  LocalStorageBackend — store, retrieve, exists, delete, deduplication
  S3StorageBackend    — store (new / existing), retrieve, exists, delete
  StorageError raised on backend failure
  Path-traversal guard in LocalStorageBackend

All S3 calls are mocked — no real AWS calls.
LocalStorageBackend uses pytest's tmp_path fixture — no real filesystem side effects.

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.acquisition.storage.backend import (
    LocalStorageBackend,
    S3StorageBackend,
    StorageError,
    StorageResult,
    make_object_key,
)

# ---------------------------------------------------------------------------
# make_object_key
# ---------------------------------------------------------------------------


class TestMakeObjectKey:
    def test_html_extension(self) -> None:
        key = make_object_key("0000320193-24-000009", "text/html")
        assert key == "filings/000032019324000009/document.html"

    def test_txt_extension(self) -> None:
        key = make_object_key("0000320193-24-000009", "text/plain")
        assert key == "filings/000032019324000009/document.txt"

    def test_xml_extension(self) -> None:
        key = make_object_key("0000320193-24-000009", "application/xml")
        assert key == "filings/000032019324000009/document.xml"

    def test_xhtml_maps_to_html(self) -> None:
        key = make_object_key("0000320193-24-000009", "application/xhtml+xml")
        assert key.endswith(".html")

    def test_unknown_mime_falls_back_to_bin(self) -> None:
        key = make_object_key("0000320193-24-000009", "application/octet-stream")
        assert key.endswith(".bin")

    def test_accession_dashes_removed(self) -> None:
        key = make_object_key("0001234567-23-000001", "text/html")
        assert "0001234567-23-000001" not in key
        assert "000123456723000001" in key

    def test_key_starts_with_filings_prefix(self) -> None:
        key = make_object_key("0000320193-24-000009", "text/html")
        assert key.startswith("filings/")


# ---------------------------------------------------------------------------
# LocalStorageBackend
# ---------------------------------------------------------------------------

_SAMPLE_CONTENT = "<html><body>Apple 10-K</body></html>"
_SAMPLE_HASH = "a" * 64
_SAMPLE_ACCESSION = "0000320193-24-000009"
_SAMPLE_KEY = make_object_key(_SAMPLE_ACCESSION, "text/html")
_SAMPLE_MIME = "text/html"


class TestLocalStorageBackend:
    @pytest.fixture
    def backend(self, tmp_path: Path) -> LocalStorageBackend:
        return LocalStorageBackend(tmp_path)

    @pytest.mark.asyncio
    async def test_store_creates_file(self, backend: LocalStorageBackend, tmp_path: Path) -> None:
        result = await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        assert result.storage_type == "local"
        assert result.from_cache is False
        assert result.content_hash == _SAMPLE_HASH
        assert result.content_length > 0
        assert (tmp_path / _SAMPLE_KEY).exists()

    @pytest.mark.asyncio
    async def test_store_creates_sidecar_meta(self, backend: LocalStorageBackend, tmp_path: Path) -> None:
        await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        meta_path = tmp_path / (_SAMPLE_KEY + ".meta.json")
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["content_hash"] == _SAMPLE_HASH
        assert meta["mime_type"] == _SAMPLE_MIME

    @pytest.mark.asyncio
    async def test_retrieve_returns_content(self, backend: LocalStorageBackend) -> None:
        await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        retrieved = await backend.retrieve(_SAMPLE_KEY)
        assert retrieved == _SAMPLE_CONTENT

    @pytest.mark.asyncio
    async def test_retrieve_missing_returns_none(self, backend: LocalStorageBackend) -> None:
        result = await backend.retrieve("filings/nonexistent/document.html")
        assert result is None

    @pytest.mark.asyncio
    async def test_exists_true_after_store(self, backend: LocalStorageBackend) -> None:
        await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        assert await backend.exists(_SAMPLE_KEY) is True

    @pytest.mark.asyncio
    async def test_exists_false_before_store(self, backend: LocalStorageBackend) -> None:
        assert await backend.exists(_SAMPLE_KEY) is False

    @pytest.mark.asyncio
    async def test_delete_removes_file(self, backend: LocalStorageBackend, tmp_path: Path) -> None:
        await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        result = await backend.delete(_SAMPLE_KEY)
        assert result is True
        assert not (tmp_path / _SAMPLE_KEY).exists()

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, backend: LocalStorageBackend) -> None:
        result = await backend.delete(_SAMPLE_KEY)
        assert result is False

    @pytest.mark.asyncio
    async def test_deduplication_no_overwrite(self, backend: LocalStorageBackend) -> None:
        first = await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        assert first.from_cache is False

        second = await backend.store(
            _SAMPLE_KEY,
            "different content",
            mime_type=_SAMPLE_MIME,
            content_hash="b" * 64,
        )
        assert second.from_cache is True
        # Original content preserved — not overwritten.
        retrieved = await backend.retrieve(_SAMPLE_KEY)
        assert retrieved == _SAMPLE_CONTENT

    @pytest.mark.asyncio
    async def test_allow_overwrite_replaces_content(self, backend: LocalStorageBackend) -> None:
        await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        new_content = "<html>New version</html>"
        await backend.store(
            _SAMPLE_KEY,
            new_content,
            mime_type=_SAMPLE_MIME,
            content_hash="c" * 64,
            allow_overwrite=True,
        )
        retrieved = await backend.retrieve(_SAMPLE_KEY)
        assert retrieved == new_content

    @pytest.mark.asyncio
    async def test_path_traversal_raises_storage_error(self, backend: LocalStorageBackend) -> None:
        with pytest.raises(StorageError, match="Path traversal"):
            await backend.store(
                "../../etc/passwd",
                "evil content",
                mime_type="text/plain",
                content_hash="d" * 64,
            )

    def test_storage_type_is_local(self, backend: LocalStorageBackend) -> None:
        assert backend.storage_type == "local"


# ---------------------------------------------------------------------------
# S3StorageBackend
# ---------------------------------------------------------------------------


def _make_s3_mock(key_exists: bool = False) -> MagicMock:
    """Build a boto3 S3 client mock with configurable key-existence state."""
    s3 = MagicMock()

    class _ClientError(Exception):
        def __init__(self, code: str) -> None:
            self.response = {"Error": {"Code": code}}
            super().__init__(f"ClientError: {code}")

    s3.exceptions.ClientError = _ClientError

    if key_exists:
        s3.head_object.return_value = {
            "ContentLength": len(_SAMPLE_CONTENT.encode()),
            "Metadata": {"content-hash": _SAMPLE_HASH},
            "LastModified": datetime.now(UTC),
        }
    else:
        s3.head_object.side_effect = _ClientError("404")

    s3.put_object.return_value = {}
    s3.get_object.return_value = {
        "Body": MagicMock(read=MagicMock(return_value=_SAMPLE_CONTENT.encode())),
    }
    s3.delete_object.return_value = {}

    return s3, _ClientError


class TestS3StorageBackend:
    @pytest.mark.asyncio
    async def test_store_new_object_calls_put(self) -> None:
        s3, _ = _make_s3_mock(key_exists=False)
        backend = S3StorageBackend(s3, "test-bucket")
        result = await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        s3.put_object.assert_called_once()
        assert result.storage_type == "s3"
        assert result.bucket_name == "test-bucket"
        assert result.from_cache is False

    @pytest.mark.asyncio
    async def test_store_existing_key_skips_upload(self) -> None:
        s3, _ = _make_s3_mock(key_exists=True)
        backend = S3StorageBackend(s3, "test-bucket")
        result = await backend.store(
            _SAMPLE_KEY,
            _SAMPLE_CONTENT,
            mime_type=_SAMPLE_MIME,
            content_hash=_SAMPLE_HASH,
        )
        s3.put_object.assert_not_called()
        assert result.from_cache is True

    @pytest.mark.asyncio
    async def test_retrieve_returns_content(self) -> None:
        s3, _ = _make_s3_mock()
        backend = S3StorageBackend(s3, "test-bucket")
        content = await backend.retrieve(_SAMPLE_KEY)
        assert content == _SAMPLE_CONTENT

    @pytest.mark.asyncio
    async def test_retrieve_missing_returns_none(self) -> None:
        s3, ClientError = _make_s3_mock()
        s3.get_object.side_effect = ClientError("NoSuchKey")
        # Patch the ClientError attribute detection
        s3.get_object.side_effect = ClientError("NoSuchKey")
        backend = S3StorageBackend(s3, "test-bucket")
        result = await backend.retrieve(_SAMPLE_KEY)
        assert result is None

    @pytest.mark.asyncio
    async def test_exists_true_when_head_succeeds(self) -> None:
        s3, _ = _make_s3_mock(key_exists=True)
        backend = S3StorageBackend(s3, "test-bucket")
        assert await backend.exists(_SAMPLE_KEY) is True

    @pytest.mark.asyncio
    async def test_exists_false_when_404(self) -> None:
        s3, _ = _make_s3_mock(key_exists=False)
        backend = S3StorageBackend(s3, "test-bucket")
        assert await backend.exists(_SAMPLE_KEY) is False

    @pytest.mark.asyncio
    async def test_delete_existing_object(self) -> None:
        s3, _ = _make_s3_mock(key_exists=True)
        backend = S3StorageBackend(s3, "test-bucket")
        result = await backend.delete(_SAMPLE_KEY)
        s3.delete_object.assert_called_once_with(Bucket="test-bucket", Key=_SAMPLE_KEY)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self) -> None:
        s3, _ = _make_s3_mock(key_exists=False)
        backend = S3StorageBackend(s3, "test-bucket")
        result = await backend.delete(_SAMPLE_KEY)
        s3.delete_object.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_store_put_failure_raises_storage_error(self) -> None:
        s3, ClientError = _make_s3_mock(key_exists=False)
        s3.put_object.side_effect = Exception("Connection refused")
        backend = S3StorageBackend(s3, "test-bucket")
        with pytest.raises(StorageError, match="put_object failed"):
            await backend.store(
                _SAMPLE_KEY,
                _SAMPLE_CONTENT,
                mime_type=_SAMPLE_MIME,
                content_hash=_SAMPLE_HASH,
            )

    def test_storage_type_is_s3(self) -> None:
        s3, _ = _make_s3_mock()
        backend = S3StorageBackend(s3, "test-bucket")
        assert backend.storage_type == "s3"
