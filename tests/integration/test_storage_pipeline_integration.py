"""
Integration tests — M3.6 S3 Storage Pipeline: VG-09 validation gate.

These tests make real HTTP calls to SEC EDGAR and require network access.
They are skipped by default and must be enabled explicitly.

To run:
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_storage_pipeline_integration.py -v

VG-09 validation gate:
  AAPL (CIK 0000320193) latest 10-K:
    - Discover filings via SECEdgarSource
    - Fetch document via SECFilingDocumentFetcher
    - Store via DocumentStorageService (LocalStorageBackend)
    - Retrieve and verify content matches
    - Verify content_hash preserved end-to-end
    - Verify metadata preserved (accession_number, filing_type, filing_date)
    - Verify duplicate prevention: second store() returns existing record
    - Delete and verify content is gone

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

import os
import tempfile

import pytest

from services.acquisition.document_fetcher.fetcher import SECFilingDocumentFetcher
from services.acquisition.source_registry.sources.sec_edgar import SECEdgarSource
from services.acquisition.storage.backend import LocalStorageBackend, make_object_key
from services.acquisition.document_fetcher.deduplicator import ContentDeduplicator

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION_TESTS"),
    reason=(
        "Integration tests disabled by default. "
        "Run: RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_storage_pipeline_integration.py -v"
    ),
)

_USER_AGENT = "FinancialDataHub-test contact@example.com"
_AAPL_CIK = "0000320193"


# ---------------------------------------------------------------------------
# VG-09 validation gate — discover → fetch → store → retrieve
# ---------------------------------------------------------------------------


class TestVG09StoragePipeline:
    """VG-09: Full pipeline from discovery through storage and retrieval."""

    @pytest.mark.asyncio
    async def test_vg09_aapl_10k_store_retrieve_hash_preserved(self) -> None:
        """
        VG-09 core: store an AAPL 10-K document and retrieve it intact.

        Pipeline:
          1. Discover filings from SEC EDGAR (AAPL, 10-K).
          2. Fetch primary document via SECFilingDocumentFetcher.
          3. Store via LocalStorageBackend.
          4. Retrieve and compare content + hash.
        """
        # 1. Discover
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        ten_k = next((f for f in result.filings if f.filing_type == "10-K"), None)
        assert ten_k is not None, "No 10-K filings found for AAPL"

        # 2. Fetch
        fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
        try:
            doc = await fetcher.fetch_document(ten_k)
        finally:
            await fetcher.close()

        assert doc.content, "Fetched document has empty content"
        assert doc.content_hash, "Fetched document missing content_hash"
        assert doc.accession_number == ten_k.accession_number

        # 3. Store
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = LocalStorageBackend(tmpdir)
            key = make_object_key(doc.accession_number, doc.mime_type)
            storage_result = await backend.store(
                key,
                doc.content,
                mime_type=doc.mime_type,
                content_hash=doc.content_hash,
            )

            assert storage_result.from_cache is False
            assert storage_result.content_hash == doc.content_hash
            assert storage_result.content_length > 0
            assert storage_result.storage_type == "local"

            # 4. Retrieve and verify
            retrieved = await backend.retrieve(key)
            assert retrieved is not None, "Retrieved content is None"
            assert len(retrieved) > 0, "Retrieved content is empty"

            # Hash preserved
            retrieved_hash = ContentDeduplicator.compute_hash(retrieved)
            assert retrieved_hash == doc.content_hash, (
                f"Hash mismatch: stored={doc.content_hash[:16]}... "
                f"retrieved={retrieved_hash[:16]}..."
            )

            # Content preserved (sample)
            assert retrieved[:100] == doc.content[:100], "Content prefix mismatch"

    @pytest.mark.asyncio
    async def test_vg09_metadata_preserved(self) -> None:
        """VG-09: Verify filing metadata fields survive the pipeline."""
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        ten_k = next((f for f in result.filings if f.filing_type == "10-K"), None)
        assert ten_k is not None

        fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
        try:
            doc = await fetcher.fetch_document(ten_k)
        finally:
            await fetcher.close()

        assert doc.accession_number == ten_k.accession_number
        assert doc.filing_type == "10-K"
        assert doc.filing_date == ten_k.filing_date
        assert doc.content_hash  # SHA-256 hex

    @pytest.mark.asyncio
    async def test_vg09_duplicate_prevention(self) -> None:
        """VG-09: Second store() with same key returns from_cache=True, no re-upload."""
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        filing = next((f for f in result.filings if f.filing_type == "10-Q"), None)
        assert filing is not None

        fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
        try:
            doc = await fetcher.fetch_document(filing)
        finally:
            await fetcher.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = LocalStorageBackend(tmpdir)
            key = make_object_key(doc.accession_number, doc.mime_type)

            first = await backend.store(
                key, doc.content, mime_type=doc.mime_type, content_hash=doc.content_hash
            )
            assert first.from_cache is False

            second = await backend.store(
                key, "DIFFERENT CONTENT", mime_type=doc.mime_type, content_hash="z" * 64
            )
            assert second.from_cache is True

            # Original content still intact
            retrieved = await backend.retrieve(key)
            assert ContentDeduplicator.compute_hash(retrieved) == doc.content_hash

    @pytest.mark.asyncio
    async def test_vg09_large_document_supported(self) -> None:
        """VG-09: Verify large 10-K documents (1+ MB) are stored and retrieved correctly."""
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        ten_k = next((f for f in result.filings if f.filing_type == "10-K"), None)
        assert ten_k is not None

        fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
        try:
            doc = await fetcher.fetch_document(ten_k)
        finally:
            await fetcher.close()

        # AAPL 10-K is typically 1+ MB
        assert doc.content_length > 100_000, (
            f"Expected large document (>100 KB); got {doc.content_length} bytes"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = LocalStorageBackend(tmpdir)
            key = make_object_key(doc.accession_number, doc.mime_type)
            result_meta = await backend.store(
                key, doc.content, mime_type=doc.mime_type, content_hash=doc.content_hash
            )
            assert result_meta.content_length == len(doc.content.encode("utf-8"))
            retrieved = await backend.retrieve(key)
            assert len(retrieved) > 100_000

    @pytest.mark.asyncio
    async def test_vg09_delete_removes_content(self) -> None:
        """VG-09: Delete removes the stored document and exists() returns False."""
        source = SECEdgarSource(user_agent=_USER_AGENT)
        result = await source.discover_filings(_AAPL_CIK)
        filing = next((f for f in result.filings if f.filing_type == "8-K"), None)
        assert filing is not None

        fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
        try:
            doc = await fetcher.fetch_document(filing)
        finally:
            await fetcher.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = LocalStorageBackend(tmpdir)
            key = make_object_key(doc.accession_number, doc.mime_type)

            await backend.store(
                key, doc.content, mime_type=doc.mime_type, content_hash=doc.content_hash
            )
            assert await backend.exists(key) is True

            deleted = await backend.delete(key)
            assert deleted is True
            assert await backend.exists(key) is False
            assert await backend.retrieve(key) is None
