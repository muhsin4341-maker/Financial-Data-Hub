"""
Unit tests — SECCompanyResolver (CompanyResolverProvider implementation).

Strategy
--------
All HTTP calls are intercepted via httpx.MockTransport so no real network
requests are made. The mock transport returns pre-built JSON payloads that
mimic actual SEC EDGAR API responses.

What is mocked
--------------
- httpx.AsyncClient transport — all HTTP calls to SEC EDGAR APIs

What is NOT mocked (real code runs)
------------------------------------
- SECCompanyResolver._load_ticker_map — JSON parsing, caching, normalisation
- SECCompanyResolver.resolve_ticker   — dict lookup, logging
- SECCompanyResolver.resolve_cik      — URL construction, response parsing

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.acquisition.company_resolver.provider import CompanyInfo
from services.acquisition.company_resolver.sec_resolver import SECCompanyResolver

# ---------------------------------------------------------------------------
# Fake SEC EDGAR API payloads
# ---------------------------------------------------------------------------

_TICKERS_PAYLOAD: dict = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}

_AAPL_SUBMISSIONS: dict = {
    "cik": "0000320193",
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "exchanges": ["Nasdaq"],
    "sic": "3571",
    "stateOfIncorporation": "CA",
    "filings": {},
}


def _make_mock_transport(routes: dict[str, tuple[int, object]]) -> httpx.MockTransport:
    """
    Build an httpx.MockTransport that returns pre-defined responses.

    Args:
        routes: dict mapping URL substring → (status_code, json_body).
                The first matching key wins (checked via substring test).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for pattern, (status, body) in routes.items():
            if pattern in url:
                return httpx.Response(
                    status_code=status,
                    content=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404, content=b'{"error": "not found"}')

    return httpx.MockTransport(handler)


def _make_client(routes: dict[str, tuple[int, object]]) -> httpx.AsyncClient:
    """Create an async httpx client with a mock transport."""
    transport = _make_mock_transport(routes)
    return httpx.AsyncClient(transport=transport)


# ---------------------------------------------------------------------------
# resolve_ticker tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_ticker_hit() -> None:
    """resolve_ticker returns CompanyInfo for a known ticker."""
    client = _make_client({
        "company_tickers.json": (200, _TICKERS_PAYLOAD),
    })
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_ticker("AAPL")

    assert result is not None
    assert result.ticker == "AAPL"
    assert result.cik == "0000320193"
    assert result.company_name == "Apple Inc."
    assert result.country == "US"


@pytest.mark.anyio
async def test_resolve_ticker_case_insensitive() -> None:
    """resolve_ticker accepts lowercase input and normalises to uppercase."""
    client = _make_client({
        "company_tickers.json": (200, _TICKERS_PAYLOAD),
    })
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_ticker("aapl")

    assert result is not None
    assert result.ticker == "AAPL"


@pytest.mark.anyio
async def test_resolve_ticker_miss() -> None:
    """resolve_ticker returns None for an unknown ticker."""
    client = _make_client({
        "company_tickers.json": (200, _TICKERS_PAYLOAD),
    })
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_ticker("INVALID_XYZ")

    assert result is None


@pytest.mark.anyio
async def test_resolve_ticker_network_error() -> None:
    """resolve_ticker returns {} and resolve returns None on HTTP error."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_ticker("AAPL")

    assert result is None


@pytest.mark.anyio
async def test_ticker_map_cached_on_second_call() -> None:
    """Ticker map is fetched only once; second call uses in-memory cache."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            content=json.dumps(_TICKERS_PAYLOAD).encode(),
            headers={"Content-Type": "application/json"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = SECCompanyResolver(http_client=client)

    await resolver.resolve_ticker("AAPL")
    await resolver.resolve_ticker("MSFT")

    assert call_count == 1, "Ticker map should be fetched only once"


# ---------------------------------------------------------------------------
# resolve_cik tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_cik_hit() -> None:
    """resolve_cik returns CompanyInfo for a known CIK."""
    client = _make_client({
        "submissions/CIK0000320193": (200, _AAPL_SUBMISSIONS),
    })
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_cik("0000320193")

    assert result is not None
    assert result.cik == "0000320193"
    assert result.ticker == "AAPL"
    assert result.company_name == "Apple Inc."
    assert result.exchange == "Nasdaq"
    assert result.country == "US"


@pytest.mark.anyio
async def test_resolve_cik_normalises_unpadded_input() -> None:
    """resolve_cik zero-pads a short CIK before constructing the URL."""
    client = _make_client({
        "submissions/CIK0000320193": (200, _AAPL_SUBMISSIONS),
    })
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_cik("320193")

    assert result is not None
    assert result.cik == "0000320193"


@pytest.mark.anyio
async def test_resolve_cik_not_found() -> None:
    """resolve_cik returns None when the submissions endpoint returns 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error": "not found"}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_cik("9999999999")

    assert result is None


@pytest.mark.anyio
async def test_resolve_cik_network_error() -> None:
    """resolve_cik returns None on network/HTTP error."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_cik("0000320193")

    assert result is None


@pytest.mark.anyio
async def test_resolve_cik_no_tickers_in_response() -> None:
    """resolve_cik handles submissions response with empty tickers list."""
    payload = {
        "cik": "0000000001",
        "name": "Mystery Corp",
        "tickers": [],
        "exchanges": [],
    }
    client = _make_client({"submissions/CIK0000000001": (200, payload)})
    resolver = SECCompanyResolver(http_client=client)

    result = await resolver.resolve_cik("0000000001")

    assert result is not None
    assert result.ticker == "0000000001"  # falls back to padded CIK
    assert result.company_name == "Mystery Corp"
    assert result.exchange is None
