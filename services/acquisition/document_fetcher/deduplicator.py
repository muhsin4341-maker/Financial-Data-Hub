"""
SHA-256 content hash deduplication for filing documents.

Provides a stateless ContentDeduplicator that computes deterministic hashes
of filing document content.  Hashes are stored in FilingDocument.content_hash
and can be compared across fetches to detect duplicate or unchanged content.

Usage::

    hash_a = ContentDeduplicator.compute_hash(html_content)
    hash_b = ContentDeduplicator.compute_hash(other_html)
    if ContentDeduplicator.is_duplicate(hash_a, hash_b):
        # identical content — skip storage
        ...

Milestone: M3.5 — Document Fetcher
"""

from __future__ import annotations

import hashlib


class ContentDeduplicator:
    """
    Stateless SHA-256 content hasher for filing document deduplication.

    All methods are static — no instance state is required.  Integrate with
    the M3.6 StorageBackend to skip S3 uploads when content is unchanged.
    """

    @staticmethod
    def compute_hash(content: str) -> str:
        """
        Return the SHA-256 hex digest of a content string encoded as UTF-8.

        Identical to the hash stored in FilingDocument.content_hash.

        Args:
            content: Decoded document string (HTML, plain text, or XML).

        Returns:
            64-character lowercase hex string.
        """
        return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def compute_hash_bytes(content: bytes) -> str:
        """
        Return the SHA-256 hex digest of a raw bytes object.

        Use this when operating on the undecoded HTTP response body.

        Args:
            content: Raw response bytes.

        Returns:
            64-character lowercase hex string.
        """
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def is_duplicate(hash_a: str, hash_b: str) -> bool:
        """
        Return True when two content hashes are identical.

        Both hashes must be 64-character lowercase hex strings produced by
        compute_hash() or compute_hash_bytes().

        Args:
            hash_a: First content hash.
            hash_b: Second content hash.

        Returns:
            True if the hashes match (identical content).
        """
        return hash_a == hash_b
