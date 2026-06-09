"""
Storage backend abstraction for filing document persistence.

Provides:
  StorageBackend       — ABC defining the storage contract.
  StorageResult        — Returned by every write operation.
  LocalStorageBackend  — Filesystem storage for development and testing.
  S3StorageBackend     — AWS S3 / MinIO storage for production.

Object key scheme:
  filings/{accession_nodashes}/document.{ext}
  Example: filings/000032019324000009/document.html

  The accession number with dashes removed gives a compact, filesystem-safe
  identifier.  The extension is derived from the MIME type.

  LocalStorageBackend mirrors this structure under a configurable base
  directory, so path-based reasoning translates between both backends.

S3 threading:
  boto3 is a synchronous library.  S3StorageBackend runs all boto3 calls in
  asyncio.to_thread() to avoid blocking the event loop.

Error handling:
  All backends raise StorageError on unrecoverable failures.
  Callers should catch StorageError; retrieval methods return None on miss.

Deduplication:
  content_hash is stored as S3 object metadata (x-amz-meta-content-hash) and
  as a sidecar file content.hash for LocalStorageBackend.  DocumentStorageService
  checks content_hash before writing to avoid re-uploading identical content.

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Extension map
# ---------------------------------------------------------------------------

_MIME_TO_EXT: dict[str, str] = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "txt",
    "application/xml": "xml",
    "text/xml": "xml",
}
_DEFAULT_EXT = "bin"


def _ext_for_mime(mime_type: str) -> str:
    return _MIME_TO_EXT.get(mime_type.split(";")[0].strip().lower(), _DEFAULT_EXT)


def _accession_nodashes(accession_number: str) -> str:
    """Return accession number with dashes removed: '0000320193-24-000009' → '000032019324000009'."""
    return accession_number.replace("-", "")


def make_object_key(accession_number: str, mime_type: str) -> str:
    """
    Build the canonical storage key for a filing document.

    Format: filings/{accession_nodashes}/document.{ext}
    """
    acc = _accession_nodashes(accession_number)
    ext = _ext_for_mime(mime_type)
    return f"filings/{acc}/document.{ext}"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class StorageResult:
    """
    Result of a document storage operation.

    Returned by StorageBackend.store() and used to populate
    StoredDocument metadata records.
    """

    key: str
    bucket_name: str | None
    storage_type: str
    content_length: int
    content_hash: str
    mime_type: str
    stored_at: datetime
    from_cache: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """Raised on unrecoverable storage backend failures."""


class StorageNotFoundError(StorageError):
    """Raised when a requested document does not exist in the backend."""


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class StorageBackend(ABC):
    """
    Abstract storage backend for filing document content.

    All implementations must be async-safe and may be used concurrently
    from multiple coroutines.

    Deduplication:
      Backends MUST NOT silently overwrite existing content when
      ``allow_overwrite=False`` (the default).  When the key already
      exists the backend returns a StorageResult with ``from_cache=True``,
      indicating the existing copy was retained.
    """

    @abstractmethod
    async def store(
        self,
        key: str,
        content: str,
        *,
        mime_type: str,
        content_hash: str,
        allow_overwrite: bool = False,
    ) -> StorageResult:
        """
        Persist document content under ``key``.

        Args:
            key:             Object key returned by make_object_key().
            content:         Decoded document string.
            mime_type:       MIME type of the content.
            content_hash:    SHA-256 hex digest of the content (UTF-8 encoded).
            allow_overwrite: When False and the key already exists, return the
                             existing StorageResult with from_cache=True.

        Returns:
            StorageResult describing the stored (or existing) object.

        Raises:
            StorageError: On unrecoverable write failure.
        """

    @abstractmethod
    async def retrieve(self, key: str) -> str | None:
        """
        Retrieve document content by key.

        Returns:
            Decoded document string, or None when the key does not exist.

        Raises:
            StorageError: On unrecoverable read failure.
        """

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """
        Return True when the key exists in the backend.

        Raises:
            StorageError: On backend failure (not on simple miss).
        """

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """
        Delete the object at ``key``.

        Returns:
            True if the object existed and was deleted.
            False if the object was not found.

        Raises:
            StorageError: On unrecoverable delete failure.
        """

    @property
    @abstractmethod
    def storage_type(self) -> str:
        """Short identifier for this backend: 'local' or 's3'."""


# ---------------------------------------------------------------------------
# LocalStorageBackend
# ---------------------------------------------------------------------------


class LocalStorageBackend(StorageBackend):
    """
    Filesystem-based storage backend for development and testing.

    Directory layout under ``base_dir``:
      {base_dir}/{key}                    — document content (UTF-8)
      {base_dir}/{key}.meta.json          — JSON metadata sidecar

    The sidecar stores content_hash, mime_type, and stored_at so that
    exists() can read metadata without loading the full content.

    Thread safety: asyncio.to_thread is used for all file I/O so that
    large documents do not block the event loop.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir).resolve()

    @property
    def storage_type(self) -> str:
        return "local"

    def _full_path(self, key: str) -> Path:
        """Resolve the absolute path for a key, preventing path traversal."""
        candidate = (self._base / key).resolve()
        if not str(candidate).startswith(str(self._base)):
            raise StorageError(f"Path traversal detected in key: {key!r}")
        return candidate

    def _meta_path(self, key: str) -> Path:
        return Path(str(self._full_path(key)) + ".meta.json")

    # ── Sync helpers (run in thread executor) ─────────────────────────────────

    def _sync_store(
        self,
        key: str,
        content: str,
        *,
        mime_type: str,
        content_hash: str,
        allow_overwrite: bool,
    ) -> StorageResult:
        full = self._full_path(key)
        meta = self._meta_path(key)
        now = datetime.now(UTC)

        if full.exists() and not allow_overwrite:
            # Return the existing StorageResult without overwriting.
            existing_meta = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
            log.debug(
                "storage.local.skip_existing",
                key=key,
                reason="content_already_present",
            )
            return StorageResult(
                key=key,
                bucket_name=None,
                storage_type="local",
                content_length=existing_meta.get("content_length", len(content.encode("utf-8"))),
                content_hash=existing_meta.get("content_hash", content_hash),
                mime_type=existing_meta.get("mime_type", mime_type),
                stored_at=datetime.fromisoformat(existing_meta["stored_at"]) if "stored_at" in existing_meta else now,
                from_cache=True,
            )

        full.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        full.write_bytes(encoded)

        meta_data = {
            "content_hash": content_hash,
            "content_length": len(encoded),
            "mime_type": mime_type,
            "stored_at": now.isoformat(),
        }
        meta.write_text(json.dumps(meta_data), encoding="utf-8")

        log.info(
            "storage.local.stored",
            key=key,
            content_length=len(encoded),
            content_hash=content_hash[:16] + "...",
        )
        return StorageResult(
            key=key,
            bucket_name=None,
            storage_type="local",
            content_length=len(encoded),
            content_hash=content_hash,
            mime_type=mime_type,
            stored_at=now,
            from_cache=False,
        )

    def _sync_retrieve(self, key: str) -> str | None:
        full = self._full_path(key)
        if not full.exists():
            return None
        return full.read_bytes().decode("utf-8", errors="replace")

    def _sync_exists(self, key: str) -> bool:
        return self._full_path(key).exists()

    def _sync_delete(self, key: str) -> bool:
        full = self._full_path(key)
        meta = self._meta_path(key)
        if not full.exists():
            return False
        full.unlink()
        if meta.exists():
            meta.unlink()
        log.debug("storage.local.deleted", key=key)
        return True

    # ── Async interface ────────────────────────────────────────────────────────

    async def store(
        self,
        key: str,
        content: str,
        *,
        mime_type: str,
        content_hash: str,
        allow_overwrite: bool = False,
    ) -> StorageResult:
        return await asyncio.to_thread(
            self._sync_store,
            key,
            content,
            mime_type=mime_type,
            content_hash=content_hash,
            allow_overwrite=allow_overwrite,
        )

    async def retrieve(self, key: str) -> str | None:
        return await asyncio.to_thread(self._sync_retrieve, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._sync_exists, key)

    async def delete(self, key: str) -> bool:
        return await asyncio.to_thread(self._sync_delete, key)


# ---------------------------------------------------------------------------
# S3StorageBackend
# ---------------------------------------------------------------------------


class S3StorageBackend(StorageBackend):
    """
    AWS S3 / MinIO storage backend for production use.

    Designed for:
      - AWS S3 (production)
      - MinIO (local Docker-based S3-compatible service)
      - LocalStack (CI / integration testing)

    All boto3 calls execute in asyncio.to_thread() to avoid blocking
    the event loop.

    Object metadata:
      Content hash is stored as an S3 object tag:
        x-amz-meta-content-hash: {sha256_hex}
      This allows deduplication without downloading the full object.

    Multipart uploads:
      Not implemented in M3.6. Documents up to ~1 GB can be uploaded
      as single PutObject calls. Multipart is planned for M3.9+.
    """

    def __init__(
        self,
        s3_client: Any,
        bucket_name: str,
    ) -> None:
        """
        Args:
            s3_client:   Configured boto3 S3 client (sync).
            bucket_name: Target S3 bucket for filing documents.
        """
        self._s3 = s3_client
        self._bucket = bucket_name

    @property
    def storage_type(self) -> str:
        return "s3"

    # ── Sync helpers ──────────────────────────────────────────────────────────

    def _sync_store(
        self,
        key: str,
        content: str,
        *,
        mime_type: str,
        content_hash: str,
        allow_overwrite: bool,
    ) -> StorageResult:
        now = datetime.now(UTC)

        if not allow_overwrite:
            try:
                head = self._s3.head_object(Bucket=self._bucket, Key=key)
                existing_hash = head.get("Metadata", {}).get("content-hash", "")
                existing_length = head.get("ContentLength", 0)
                last_modified = head.get("LastModified", now)
                # Normalise LastModified to UTC-aware datetime
                if hasattr(last_modified, "tzinfo") and last_modified.tzinfo is None:
                    last_modified = last_modified.replace(tzinfo=UTC)
                log.debug(
                    "storage.s3.skip_existing",
                    key=key,
                    bucket=self._bucket,
                    reason="content_already_present",
                )
                return StorageResult(
                    key=key,
                    bucket_name=self._bucket,
                    storage_type="s3",
                    content_length=existing_length,
                    content_hash=existing_hash or content_hash,
                    mime_type=mime_type,
                    stored_at=last_modified,
                    from_cache=True,
                )
            except self._s3.exceptions.ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                if error_code not in ("404", "NoSuchKey"):
                    raise StorageError(
                        f"S3 head_object failed for key {key!r}: {exc}"
                    ) from exc
                # 404 → object does not exist; proceed with upload.

        encoded = content.encode("utf-8")
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=encoded,
                ContentType=mime_type,
                Metadata={"content-hash": content_hash},
            )
        except Exception as exc:
            raise StorageError(
                f"S3 put_object failed for key {key!r}: {exc}"
            ) from exc

        log.info(
            "storage.s3.stored",
            key=key,
            bucket=self._bucket,
            content_length=len(encoded),
            content_hash=content_hash[:16] + "...",
        )
        return StorageResult(
            key=key,
            bucket_name=self._bucket,
            storage_type="s3",
            content_length=len(encoded),
            content_hash=content_hash,
            mime_type=mime_type,
            stored_at=now,
            from_cache=False,
        )

    def _sync_retrieve(self, key: str) -> str | None:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read()
            return body.decode("utf-8", errors="replace")
        except Exception as exc:
            error_code = getattr(getattr(exc, "response", {}), "get", lambda *a: None)("Error", {}).get("Code", "")
            # Handle both botocore ClientError and generic exceptions
            if hasattr(exc, "response"):
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey"):
                    return None
            raise StorageError(
                f"S3 get_object failed for key {key!r}: {exc}"
            ) from exc

    def _sync_exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as exc:
            if hasattr(exc, "response"):
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey"):
                    return False
            raise StorageError(
                f"S3 head_object failed for key {key!r}: {exc}"
            ) from exc

    def _sync_delete(self, key: str) -> bool:
        if not self._sync_exists(key):
            return False
        try:
            self._s3.delete_object(Bucket=self._bucket, Key=key)
            log.debug("storage.s3.deleted", key=key, bucket=self._bucket)
            return True
        except Exception as exc:
            raise StorageError(
                f"S3 delete_object failed for key {key!r}: {exc}"
            ) from exc

    # ── Async interface ────────────────────────────────────────────────────────

    async def store(
        self,
        key: str,
        content: str,
        *,
        mime_type: str,
        content_hash: str,
        allow_overwrite: bool = False,
    ) -> StorageResult:
        return await asyncio.to_thread(
            self._sync_store,
            key,
            content,
            mime_type=mime_type,
            content_hash=content_hash,
            allow_overwrite=allow_overwrite,
        )

    async def retrieve(self, key: str) -> str | None:
        return await asyncio.to_thread(self._sync_retrieve, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._sync_exists, key)

    async def delete(self, key: str) -> bool:
        return await asyncio.to_thread(self._sync_delete, key)
