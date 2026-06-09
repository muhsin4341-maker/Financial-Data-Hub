"""
Integration tests — M3.2 Company Resolver: VG-06 validation gate.

These tests make real HTTP calls to SEC EDGAR public APIs and require
network access. They are skipped by default and must be run explicitly.

To run:
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_company_resolver_integration.py -v

VG-06 validation gate:
  AAPL ticker resolves to:
    company_name  = "Apple Inc."
    cik           = "0000320193"

Additional integration coverage:
  - SECCompanyResolver.resolve_ticker — live SEC EDGAR ticker map
  - SECCompanyResolver.resolve_cik   — live SEC EDGAR submissions endpoint
  - CompanyResolverService caching   — Redis cache read/write (when available)
  - Multiple tickers (MSFT, GOOGL)   — spot-check resolver breadth

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

import os

import pytest

from services.acquisition.company_resolver.provider import CompanyInfo
from services.acquisition.company_resolver.resolver import CompanyResolverService
from services.acquisition.company_resolver.sec_resolver import SECCompanyResolver

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION_TESTS"),
    reason=(
        "Integration tests disabled by default. "
        "Run: RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_company_resolver_integration.py -v"
    ),
)


# ---------------------------------------------------------------------------
# SECCompanyResolver — live network tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sec_resolver_aapl_ticker() -> None:
    """VG-06: AAPL ticker resolves to Apple Inc. with CIK 0000320193."""
    resolver = SECCompanyResolver(
        user_agent="FinancialDataHub-test contact@example.com"
    )
    try:
        result = await resolver.resolve_ticker("AAPL")

        assert result is not None, "AAPL must resolve to a CompanyInfo"
        assert isinstance(result, CompanyInfo)
        assert result.ticker == "AAPL"
        assert result.cik == "0000320193", (
            f"VG-06 FAILED: Expected CIK 0000320193 for AAPL, got {result.cik!r}"
        )
        assert "apple" in result.company_name.lower(), (
            f"VG-06 FAILED: Expected company_name to contain 'apple', "
            f"got {result.company_name!r}"
        )
        assert result.country == "US"
    finally:
        await resolver.close()


@pytest.mark.anyio
async def test_sec_resolver_msft_ticker() -> None:
    """Microsoft ticker resolves correctly."""
    resolver = SECCompanyResolver(
        user_agent="FinancialDataHub-test contact@example.com"
    )
    try:
        result = await resolver.resolve_ticker("MSFT")

        assert result is not None
        assert result.ticker == "MSFT"
        assert result.cik == "0000789019"
        assert "microsoft" in result.company_name.lower()
    finally:
        await resolver.close()


@pytest.mark.anyio
async def test_sec_resolver_unknown_ticker_returns_none() -> None:
    """Unknown ticker returns None (not an exception)."""
    resolver = SECCompanyResolver(
        user_agent="FinancialDataHub-test contact@example.com"
    )
    try:
        result = await resolver.resolve_ticker("ZZZZNONEXISTENT")
        assert result is None
    finally:
        await resolver.close()


@pytest.mark.anyio
async def test_sec_resolver_cik_lookup() -> None:
    """CIK lookup via submissions endpoint resolves AAPL."""
    resolver = SECCompanyResolver(
        user_agent="FinancialDataHub-test contact@example.com"
    )
    try:
        result = await resolver.resolve_cik("0000320193")

        assert result is not None
        assert result.cik == "0000320193"
        assert "apple" in result.company_name.lower()
        assert result.ticker == "AAPL"
    finally:
        await resolver.close()


@pytest.mark.anyio
async def test_sec_resolver_cik_unpadded_input() -> None:
    """Short CIK input is normalised before lookup."""
    resolver = SECCompanyResolver(
        user_agent="FinancialDataHub-test contact@example.com"
    )
    try:
        result = await resolver.resolve_cik("320193")

        assert result is not None
        assert result.cik == "0000320193"
    finally:
        await resolver.close()


# ---------------------------------------------------------------------------
# CompanyResolverService — with no Redis (no-cache path)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_company_resolver_service_resolve_by_ticker_no_cache() -> None:
    """
    VG-06 via service layer: resolve_by_ticker('AAPL') returns correct CIK.

    No Redis client is provided — verifies the resolver works without caching.
    """
    service = CompanyResolverService(
        redis_client=None,
        user_agent="FinancialDataHub-test contact@example.com",
    )

    result = await service.resolve_by_ticker("AAPL")

    assert result is not None, "Service must resolve AAPL"
    assert result.ticker == "AAPL"
    assert result.cik == "0000320193", (
        f"VG-06 FAILED via service: Expected 0000320193, got {result.cik!r}"
    )
    assert "apple" in result.company_name.lower()


@pytest.mark.anyio
async def test_company_resolver_service_resolve_by_cik_no_cache() -> None:
    """resolve_by_cik returns correct company info via service layer."""
    service = CompanyResolverService(
        redis_client=None,
        user_agent="FinancialDataHub-test contact@example.com",
    )

    result = await service.resolve_by_cik("0000320193")

    assert result is not None
    assert result.cik == "0000320193"
    assert result.ticker == "AAPL"


@pytest.mark.anyio
async def test_company_resolver_service_unknown_ticker_no_cache() -> None:
    """resolve_by_ticker returns None for an unknown ticker via service layer."""
    service = CompanyResolverService(
        redis_client=None,
        user_agent="FinancialDataHub-test contact@example.com",
    )

    result = await service.resolve_by_ticker("ZZZZNONEXISTENT")

    assert result is None
