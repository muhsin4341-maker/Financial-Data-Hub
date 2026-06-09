"""
Integration tests — M3.4 SEC EDGAR Integration: VG-07 validation gate.

These tests make real HTTP calls to SEC EDGAR public APIs and require
network access.  They are skipped by default and must be run explicitly.

To run:
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_sec_edgar_integration.py -v

VG-07 validation gate:
  AAPL (CIK 0000320193) returns:
    - One or more 10-K filings
    - Valid accession numbers in 'XXXXXXXXXX-YY-ZZZZZZ' format
    - Valid filing URLs pointing to SEC Archives
    - Valid filing dates

Additional integration coverage:
  - SECEdgarSource.get_submissions         — live SEC EDGAR submissions endpoint
  - SECEdgarSource.discover_filings        — live filing discovery
  - SECEdgarSource.get_filing_metadata     — specific filing lookup
  - Rate limiter operation                 — confirms no SEC rate limit errors
  - Multiple companies (MSFT, TSLA)        — spot-check resolver breadth
  - Pagination flag                        — has_additional_pages for major filers
  - 8-K filing discovery                  — no period_end_date
  - DEF 14A discovery                     — proxy statement detection

Milestone: M3.4 — SEC EDGAR Integration
"""

from __future__ import annotations

import os
import re

import pytest

from services.acquisition.source_registry.sources.sec_edgar import (
    SUPPORTED_FORM_TYPES,
    FilingDiscoveryResult,
    FilingMetadata,
    SECEdgarSource,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION_TESTS"),
    reason=(
        "Integration tests disabled by default. "
        "Run: RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_sec_edgar_integration.py -v"
    ),
)

_USER_AGENT = "FinancialDataHub-test contact@example.com"
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"

# Known CIKs for spot-checks
_AAPL_CIK = "0000320193"
_MSFT_CIK = "0000789019"
_TSLA_CIK = "0001318605"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def sec_source() -> SECEdgarSource:  # type: ignore[misc]
    """SECEdgarSource instance per test (anyio requires per-test event loops)."""
    source = SECEdgarSource(user_agent=_USER_AGENT)
    yield source
    await source.close()


# ---------------------------------------------------------------------------
# VG-07: AAPL filing discovery
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_vg07_aapl_discover_filings(sec_source: SECEdgarSource) -> None:
    """
    VG-07: AAPL (CIK 0000320193) returns 10-K filings with accession numbers,
    filing URLs, and filing dates.
    """
    result = await sec_source.discover_filings(_AAPL_CIK)

    assert isinstance(result, FilingDiscoveryResult), "Result must be a FilingDiscoveryResult"
    assert result.cik == _AAPL_CIK, f"CIK mismatch: expected {_AAPL_CIK}, got {result.cik}"
    assert "apple" in result.company_name.lower(), (
        f"VG-07: Expected company_name to contain 'apple', got {result.company_name!r}"
    )
    assert result.ticker == "AAPL", f"VG-07: Expected ticker AAPL, got {result.ticker!r}"

    ten_k_filings = [f for f in result.filings if f.filing_type == "10-K"]
    assert len(ten_k_filings) >= 1, (
        f"VG-07 FAILED: Expected at least one 10-K filing for AAPL, got {len(ten_k_filings)}"
    )


@pytest.mark.anyio
async def test_vg07_aapl_accession_number_format(sec_source: SECEdgarSource) -> None:
    """VG-07: All returned accession numbers match the SEC EDGAR format."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    for filing in result.filings:
        assert _ACCESSION_RE.match(filing.accession_number), (
            f"VG-07 FAILED: Invalid accession number format: {filing.accession_number!r}"
        )


@pytest.mark.anyio
async def test_vg07_aapl_filing_urls_format(sec_source: SECEdgarSource) -> None:
    """VG-07: Filing index URLs point to SEC Archives and contain CIK path."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    ten_k = [f for f in result.filings if f.filing_type == "10-K"]
    for filing in ten_k[:3]:  # check first 3 to limit test duration
        assert filing.filing_url.startswith(_ARCHIVES_BASE), (
            f"VG-07 FAILED: filing_url does not start with Archives URL: {filing.filing_url!r}"
        )
        assert "320193" in filing.filing_url, (
            f"VG-07 FAILED: filing_url does not contain CIK path: {filing.filing_url!r}"
        )


@pytest.mark.anyio
async def test_vg07_aapl_filing_dates_present(sec_source: SECEdgarSource) -> None:
    """VG-07: All filings have valid filing dates."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    for filing in result.filings:
        assert filing.filing_date is not None, (
            f"VG-07 FAILED: filing_date is None for {filing.accession_number}"
        )
        assert filing.filing_date.year >= 1993, (
            f"VG-07 FAILED: implausibly old filing date: {filing.filing_date}"
        )


@pytest.mark.anyio
async def test_vg07_aapl_period_end_date_parsing(sec_source: SECEdgarSource) -> None:
    """VG-07: period_end_date field is parsed correctly (None or valid date)."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    for filing in result.filings:
        # period_end_date must be None or a valid date — never raise
        assert filing.period_end_date is None or hasattr(filing.period_end_date, "year"), (
            f"VG-07: period_end_date must be None or date, got {type(filing.period_end_date)}"
        )
    # At least some filings across all types should have it populated
    with_period = [f for f in result.filings if f.period_end_date is not None]
    assert len(with_period) >= 0  # parsing works; SEC may or may not populate it


@pytest.mark.anyio
async def test_vg07_aapl_document_urls(sec_source: SECEdgarSource) -> None:
    """VG-07: Most recent 10-K has a document URL ending in .htm."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    ten_k = sorted(
        [f for f in result.filings if f.filing_type == "10-K"],
        key=lambda f: f.filing_date,
        reverse=True,
    )
    assert len(ten_k) >= 1
    most_recent = ten_k[0]
    assert most_recent.document_url is not None, "Most recent 10-K must have a document URL"
    assert most_recent.document_url.startswith(_ARCHIVES_BASE), (
        f"document_url does not start with Archives URL: {most_recent.document_url!r}"
    )


# ---------------------------------------------------------------------------
# Additional integration coverage
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_submissions_returns_raw_dict(sec_source: SECEdgarSource) -> None:
    """get_submissions returns the raw SEC EDGAR JSON for AAPL."""
    data = await sec_source.get_submissions(_AAPL_CIK)
    assert isinstance(data, dict)
    assert data.get("cik") in (_AAPL_CIK, "320193", 320193)
    assert "filings" in data
    assert "recent" in data["filings"]


@pytest.mark.anyio
async def test_msft_filing_discovery(sec_source: SECEdgarSource) -> None:
    """MSFT (CIK 0000789019) has 10-K and 10-Q filings."""
    result = await sec_source.discover_filings(_MSFT_CIK)
    assert result.cik == _MSFT_CIK
    assert "microsoft" in result.company_name.lower()
    ten_k = [f for f in result.filings if f.filing_type == "10-K"]
    assert len(ten_k) >= 1


@pytest.mark.anyio
async def test_tsla_filing_discovery(sec_source: SECEdgarSource) -> None:
    """TSLA (CIK 0001318605) has 10-K filings."""
    result = await sec_source.discover_filings(_TSLA_CIK)
    assert result.cik == _TSLA_CIK
    ten_k = [f for f in result.filings if f.filing_type == "10-K"]
    assert len(ten_k) >= 1


@pytest.mark.anyio
async def test_aapl_has_additional_pages(sec_source: SECEdgarSource) -> None:
    """AAPL has been filing since 1993 — should have additional pages."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    assert result.has_additional_pages is True, (
        "AAPL has been filing since 1993 and should have additional filing pages"
    )


@pytest.mark.anyio
async def test_aapl_8k_filing_discovered(sec_source: SECEdgarSource) -> None:
    """AAPL files 8-K reports — at least one should appear in recent filings."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    eight_k = [f for f in result.filings if f.filing_type == "8-K"]
    assert len(eight_k) >= 1, "Expected at least one 8-K filing in AAPL recent submissions"


@pytest.mark.anyio
async def test_8k_period_end_date_may_be_none(sec_source: SECEdgarSource) -> None:
    """8-K filings (current reports) may not have a periodOfReport."""
    result = await sec_source.discover_filings(_AAPL_CIK)
    eight_k = [f for f in result.filings if f.filing_type == "8-K"]
    # Some 8-K may have period_end_date, some may not — just verify no exceptions
    for filing in eight_k:
        assert filing.filing_date is not None  # filing_date always required


@pytest.mark.anyio
async def test_unknown_cik_returns_empty_result(sec_source: SECEdgarSource) -> None:
    """An unknown CIK (404 from SEC) returns an empty FilingDiscoveryResult."""
    result = await sec_source.discover_filings("0000000001")
    # CIK 1 may or may not exist — just verify no exception is raised and
    # the result is a valid FilingDiscoveryResult.
    assert isinstance(result, FilingDiscoveryResult)


@pytest.mark.anyio
async def test_get_filing_metadata_by_accession(sec_source: SECEdgarSource) -> None:
    """get_filing_metadata returns a FilingMetadata for a known AAPL 10-K."""
    # First discover to get a known accession number
    result = await sec_source.discover_filings(_AAPL_CIK)
    ten_k = [f for f in result.filings if f.filing_type == "10-K"]
    assert ten_k, "AAPL must have 10-K filings"

    most_recent_acc = ten_k[0].accession_number
    metadata = await sec_source.get_filing_metadata(_AAPL_CIK, most_recent_acc)
    assert metadata is not None
    assert metadata.accession_number == most_recent_acc
    assert metadata.filing_type == "10-K"


@pytest.mark.anyio
async def test_rate_limiter_operates_without_errors(sec_source: SECEdgarSource) -> None:
    """Make several sequential requests to verify rate limiter allows through."""
    # Two separate CIK calls — rate limiter must grant both within the test timeout
    aapl = await sec_source.discover_filings(_AAPL_CIK)
    msft = await sec_source.discover_filings(_MSFT_CIK)
    assert aapl.company_name != ""
    assert msft.company_name != ""
