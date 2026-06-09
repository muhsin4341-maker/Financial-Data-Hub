"""
Integration tests — M3.5 Document Fetcher: VG-08 validation gate.

These tests make real HTTP calls to SEC EDGAR and require network access.
They are skipped by default and must be enabled explicitly.

To run:
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_document_fetcher_integration.py -v

VG-08 validation gate:
  AAPL (CIK 0000320193) latest 10-K and 10-Q:
    - Document retrieved successfully
    - HTML content present and non-empty
    - Plain text extracted from HTML
    - Metadata (accession_number, filing_type, filing_date) preserved
    - Content hash computed (SHA-256)
    - Source URL pointing to SEC Archives

Additional integration coverage:
  - fetch_by_url — explicit URL without FilingMetadata
  - Rate limiter — no SEC rate limit errors across multiple fetches
  - Large filing handling — 10-K can be several MB
  - 8-K document fetch — smaller, current-report filing
  - Repeated fetch idempotency — same result on second call

Milestone: M3.5 — Document Fetcher
"""

from __future__ import annotations

import os
import re

import pytest

from services.acquisition.document_fetcher.fetcher import (
    FilingDocument,
    SECFilingDocumentFetcher,
)
from services.acquisition.source_registry.sources.sec_edgar import (
    FilingMetadata,
    SECEdgarSource,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION_TESTS"),
    reason=(
        "Integration tests disabled by default. "
        "Run: RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_document_fetcher_integration.py -v"
    ),
)

_USER_AGENT = "FinancialDataHub-test contact@example.com"
_AAPL_CIK = "0000320193"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def sec_source() -> SECEdgarSource:  # type: ignore[misc]
    source = SECEdgarSource(user_agent=_USER_AGENT)
    yield source
    await source.close()


@pytest.fixture()
async def doc_fetcher() -> SECFilingDocumentFetcher:  # type: ignore[misc]
    fetcher = SECFilingDocumentFetcher(user_agent=_USER_AGENT)
    yield fetcher
    await fetcher.close()


@pytest.fixture()
async def aapl_10k(sec_source: SECEdgarSource) -> FilingMetadata:
    """Discover AAPL filings and return the most recent 10-K."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    ten_k = sorted(
        [f for f in result.filings if f.filing_type == "10-K"],
        key=lambda f: f.filing_date,
        reverse=True,
    )
    assert ten_k, "AAPL must have 10-K filings in recent submissions"
    return ten_k[0]


@pytest.fixture()
async def aapl_10q(sec_source: SECEdgarSource) -> FilingMetadata:
    """Discover AAPL filings and return the most recent 10-Q."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    ten_q = sorted(
        [f for f in result.filings if f.filing_type == "10-Q"],
        key=lambda f: f.filing_date,
        reverse=True,
    )
    assert ten_q, "AAPL must have 10-Q filings in recent submissions"
    return ten_q[0]


# ---------------------------------------------------------------------------
# VG-08: AAPL 10-K document retrieval
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_vg08_aapl_10k_document_retrieved(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """VG-08: The most recent AAPL 10-K document can be retrieved from SEC EDGAR."""
    doc = await doc_fetcher.fetch_document(aapl_10k)

    assert isinstance(doc, FilingDocument), "Result must be a FilingDocument"
    assert doc.accession_number == aapl_10k.accession_number
    assert doc.filing_type == "10-K"
    assert doc.from_cache is False


@pytest.mark.anyio
async def test_vg08_aapl_10k_html_content(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """VG-08: AAPL 10-K document has non-empty HTML content."""
    doc = await doc_fetcher.fetch_document(aapl_10k)

    assert doc.content, "HTML content must be non-empty"
    assert len(doc.content) > 1000, (
        f"VG-08 FAILED: 10-K content unexpectedly short ({len(doc.content)} bytes). "
        "Expected at least 1 KB of HTML."
    )
    assert doc.content_length > 0, "content_length must be positive"


@pytest.mark.anyio
async def test_vg08_aapl_10k_text_extraction(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """VG-08: Plain text is extracted from the AAPL 10-K HTML document."""
    doc = await doc_fetcher.fetch_document(aapl_10k)

    if "html" in doc.mime_type:
        assert doc.plain_text is not None, (
            "VG-08 FAILED: plain_text must not be None for HTML content"
        )
        assert len(doc.plain_text) > 100, (
            f"VG-08 FAILED: plain_text too short ({len(doc.plain_text)} chars)"
        )
        # Expect common 10-K language somewhere in the text
        text_lower = doc.plain_text.lower()
        assert any(
            keyword in text_lower
            for keyword in ("apple", "annual", "form 10-k", "fiscal", "revenue", "financial")
        ), "VG-08 FAILED: extracted text does not contain expected 10-K content"


@pytest.mark.anyio
async def test_vg08_aapl_10k_metadata_preserved(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """VG-08: Filing metadata fields are preserved in the FilingDocument."""
    doc = await doc_fetcher.fetch_document(aapl_10k)

    assert doc.accession_number == aapl_10k.accession_number
    assert doc.filing_type == aapl_10k.filing_type
    assert doc.filing_date == aapl_10k.filing_date
    assert doc.source_url.startswith("https://www.sec.gov/"), (
        f"VG-08 FAILED: source_url should point to SEC: {doc.source_url!r}"
    )
    assert doc.fetched_at is not None, "fetched_at must be set"
    assert doc.fetched_at.tzinfo is not None, "fetched_at must be timezone-aware"


# ---------------------------------------------------------------------------
# VG-08: AAPL 10-Q document retrieval
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_vg08_aapl_10q_document_retrieved(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10q: FilingMetadata,
) -> None:
    """VG-08: The most recent AAPL 10-Q document can be retrieved."""
    doc = await doc_fetcher.fetch_document(aapl_10q)

    assert isinstance(doc, FilingDocument)
    assert doc.accession_number == aapl_10q.accession_number
    assert doc.filing_type == "10-Q"


@pytest.mark.anyio
async def test_vg08_aapl_10q_html_content(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10q: FilingMetadata,
) -> None:
    """VG-08: AAPL 10-Q document has non-empty content."""
    doc = await doc_fetcher.fetch_document(aapl_10q)
    assert doc.content, "10-Q content must be non-empty"
    assert len(doc.content) > 500


@pytest.mark.anyio
async def test_vg08_aapl_10q_text_extraction(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10q: FilingMetadata,
) -> None:
    """VG-08: Plain text is extractable from the AAPL 10-Q HTML."""
    doc = await doc_fetcher.fetch_document(aapl_10q)
    if "html" in doc.mime_type:
        assert doc.plain_text is not None
        assert len(doc.plain_text) > 50


@pytest.mark.anyio
async def test_vg08_aapl_10q_metadata_preserved(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10q: FilingMetadata,
) -> None:
    """VG-08: Filing date and accession number are preserved for 10-Q."""
    doc = await doc_fetcher.fetch_document(aapl_10q)
    assert doc.accession_number == aapl_10q.accession_number
    assert doc.filing_date == aapl_10q.filing_date


# ---------------------------------------------------------------------------
# Additional integration coverage
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_content_hash_non_empty(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """SHA-256 hash is computed and is a 64-char hex string."""
    doc = await doc_fetcher.fetch_document(aapl_10k)
    assert len(doc.content_hash) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", doc.content_hash), (
        f"content_hash is not valid SHA-256: {doc.content_hash!r}"
    )


@pytest.mark.anyio
async def test_content_length_reasonable(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """AAPL 10-K content_length should reflect the actual byte size."""
    doc = await doc_fetcher.fetch_document(aapl_10k)
    assert doc.content_length > 0
    # 10-K filings are typically at least 100 KB
    assert doc.content_length > 100_000, (
        f"10-K content_length suspiciously small: {doc.content_length} bytes"
    )


@pytest.mark.anyio
async def test_document_url_set(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """document_url is set and points to SEC Archives."""
    doc = await doc_fetcher.fetch_document(aapl_10k)
    # source_url must always point to SEC Archives
    assert _ARCHIVES_BASE in doc.source_url, (
        f"source_url must contain Archives path: {doc.source_url!r}"
    )


@pytest.mark.anyio
async def test_fetch_by_url(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """fetch_by_url retrieves a document given an explicit URL."""
    assert aapl_10k.document_url, "10-K must have document_url for this test"
    doc = await doc_fetcher.fetch_by_url(
        aapl_10k.document_url,
        accession_number=aapl_10k.accession_number,
        filing_type=aapl_10k.filing_type,
        filing_date=aapl_10k.filing_date,
    )
    assert doc.accession_number == aapl_10k.accession_number
    assert doc.content


@pytest.mark.anyio
async def test_rate_limiter_no_errors(
    sec_source: SECEdgarSource,
    doc_fetcher: SECFilingDocumentFetcher,
) -> None:
    """Make two sequential document fetches — rate limiter must not cause errors."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    ten_k = [f for f in result.filings if f.filing_type == "10-K"]
    ten_q = [f for f in result.filings if f.filing_type == "10-Q"]
    assert ten_k and ten_q, "Need both 10-K and 10-Q filings for this test"

    doc_k = await doc_fetcher.fetch_document(ten_k[0])
    doc_q = await doc_fetcher.fetch_document(ten_q[0])

    assert doc_k.content
    assert doc_q.content


@pytest.mark.anyio
async def test_8k_document_fetch(
    sec_source: SECEdgarSource,
    doc_fetcher: SECFilingDocumentFetcher,
) -> None:
    """AAPL 8-K (current report) document can be fetched; period_end_date may be None."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    eight_k = [f for f in result.filings if f.filing_type == "8-K"]
    if not eight_k:
        pytest.skip("No 8-K filings found in AAPL recent submissions")

    doc = await doc_fetcher.fetch_document(eight_k[0])
    assert doc.filing_type == "8-K"
    assert doc.content


@pytest.mark.anyio
async def test_repeated_fetch_same_url_returns_same_hash(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10q: FilingMetadata,
) -> None:
    """Fetching the same document twice returns identical content hashes."""
    doc1 = await doc_fetcher.fetch_document(aapl_10q)
    doc2 = await doc_fetcher.fetch_document(aapl_10q)
    assert doc1.content_hash == doc2.content_hash, (
        "Content hash must be stable across repeated fetches of the same document"
    )


@pytest.mark.anyio
async def test_source_url_is_valid_sec_url(
    doc_fetcher: SECFilingDocumentFetcher,
    aapl_10k: FilingMetadata,
) -> None:
    """source_url after fetch is an absolute HTTPS URL on sec.gov."""
    doc = await doc_fetcher.fetch_document(aapl_10k)
    assert doc.source_url.startswith("https://"), f"source_url must be HTTPS: {doc.source_url!r}"
    assert "sec.gov" in doc.source_url, f"source_url must be on sec.gov: {doc.source_url!r}"
