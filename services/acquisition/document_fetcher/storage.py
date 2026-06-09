"""
StorageBackend — re-export from services.acquisition.storage.backend.

The full implementation lives in the storage package (M3.6).
This module preserves the import path used by fetcher.py and any callers
that reference document_fetcher.storage directly.

Milestone: M3.6 — S3 Storage Pipeline
"""

from services.acquisition.storage.backend import (  # noqa: F401
    LocalStorageBackend,
    S3StorageBackend,
    StorageBackend,
    StorageError,
    StorageNotFoundError,
    StorageResult,
    make_object_key,
)

__all__ = [
    "StorageBackend",
    "StorageResult",
    "StorageError",
    "StorageNotFoundError",
    "LocalStorageBackend",
    "S3StorageBackend",
    "make_object_key",
]
