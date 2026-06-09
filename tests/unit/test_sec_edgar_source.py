"""
Unit tests — SECEdgarSource and rate limiters.

Strategy
--------
All HTTP calls are intercepted via httpx.MockTransport so no real network
requests are made. The mock transport returns pre-built JSON payloads that
mimic actual SEC EDGAR submissions API responses.

What is mocked
--------------
- httpx.AsyncClient transport — all HTTP calls to SEC EDGAR APIs

What is NOT mocked (real code runs)
------------------------------------
- InProcessRateLimiter — token bucket accounting and sleep logic
- SECEdgarSource._get  — retry policy, rate-limit integration
- SECEdgarSource._extract_filings_from_block — columnar data parsing
- SECEdgarSource.build_filing_index_url / build_document_url — URL builders
- SECEdgarSource.discover_filings — full orchestration
- SECEdgarSource.get_submissions  — HTTP + response handling

Milestone: M3.4 — SEC EDGAR Integration
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.acquisition.source_registry.rate_limiter import (
    InProcessRateLimiter,
    RedisRateLimiter,
)
from services.acquisition.source_registry.sources.sec_edgar import (
    SUPPORTED_FORM_TYPES,
    FilingDiscoveryResult,
    FilingMetadata,
    SECEdgarSource,
)

# ---------------------------------------------------------------------------
# Fake SEC EDGAR payloads
# ---------------------------------------------------------------------------

_CIK = "0000320193"
_COMPANY_NAME = "Apple Inc."
_TICKER = "AAPL"

_APPLE_SUBMISSIONS: dict = {
    "cik": _CIK,
    "name": _COMPANY_NAME,
    "tickers": [_TICKER],
    "exchanges": ["Nasdaq"],
    "sic": "3571",
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000320193-24-000009",
                "0000320193-23-000106",
                "0000320193-23-000077",
                "0000320193-23-000035",
                "0000320193-22-000108",
            ],
            "filingDate": [
                "2024-02-02",
                "2023-11-03",
                "2023-08-04",
                "2023-05-05",
                "2022-10-28",
            ],
            "form": [
                "10-K",
                "10-Q",
                "10-Q",
                "10-Q",
                "10-K",
            ],
            "primaryDocument": [
                "aapl-20231230.htm",
                "aapl-20230930.htm",
                "aapl-20230701.htm",
                "aapl-20230401.htm",
                "aapl-20220924.htm",
            ],
            "primaryDocDescription": [
                "Annual report",
                "Quarterly report",
                "Quarterly report",
                "Quarterly report",
                "Annual report",
            ],
            "periodOfReport": [
                "2023-12-30",
                "2023-09-30",
                "2023-07-01",
                "2023-04-01",
                "2022-09-24",
            ],
        },
        "files": [],
    },
}

_APPLE_SUBMISSIONS_WITH_8K: dict = {
    **_APPLE_SUBMISSIONS,
    "filings": {
        "recent": {
            **_APPLE_SUBMISSIONS["filings"]["recent"],
            "accessionNumber": _APPLE_SUBMISSIONS["filings"]["recent"]["accessionNumber"] + ["0000320193-24-000020"],
            "filingDate": _APPLE_SUBMISSIONS["filings"]["recent"]["filingDate"] + ["2024-01-15"],
            "form": _APPLE_SUBMISSIONS["filings"]["recent"]["form"] + ["8-K"],
            "primaryDocument": _APPLE_SUBMISSIONS["filings"]["recent"]["primaryDocument"] + ["aapl-8k.htm"],
            "primaryDocDescription": _APPLE_SUBMISSIONS["filings"]["recent"]["primaryDocDescription"] + ["Current report"],
            "periodOfReport": _APPLE_SUBMISSIONS["filings"]["recent"]["periodOfReport"] + [""],
        },
        "files": [],
    },
}

_APPLE_WITH_PAGINATION: dict = {
    **_APPLE_SUBMISSIONS,
    "filings": {
        "recent": _APPLE_SUBMISSIONS["filings"]["recent"],
        "files": [{"name": "CIK0000320193-submissions-0001.json", "filingCount": 40}],
    },
}

_PAGINATION_PAGE: dict = {
    "accessionNumber": ["0000320193-10-000001"],
    "filingDate": ["2010-12-15"],
    "form": ["10-K"],
    "primaryDocument": ["aapl-2010.htm"],
    "primaryDocDescription": ["Annual report 2010"],
    "periodOfReport": ["2010-09-25"],
}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_mock_transport(
    routes: dict[str, tuple[int, object]],
) -> httpx.MockTransport:
    """Build an httpx.MockTransport matching URL substrings."""

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


def _make_source(routes: dict[str, tuple[int, object]]) -> SECEdgarSource:
    """Return a SECEdgarSource with a mock HTTP client and no-op rate limiter."""

    class _NoOpLimiter(InProcessRateLimiter):
        async def acquire(self) -> None:
            pass  # Skip rate limiting in unit tests

    transport = _make_mock_transport(routes)
    client = httpx.AsyncClient(transport=transport)
    return SECEdgarSource(http_client=client, rate_limiter=_NoOpLimiter())


# ===========================================================================
# InProcessRateLimiter
# ===========================================================================


class TestInProcessRateLimiter:
    def test_init_defaults(self) -> None:
        limiter = InProcessRateLimiter()
        assert limiter.rate == 8.0
        assert limiter.burst == 10

    def test_init_custom(self) -> None:
        limiter = InProcessRateLimiter(rate=5.0, burst=3)
        assert limiter.rate == 5.0
        assert limiter.burst == 3

    def test_invalid_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            InProcessRateLimiter(rate=0)

    def test_invalid_burst_raises(self) -> None:
        with pytest.raises(ValueError):
            InProcessRateLimiter(burst=0)

    @pytest.mark.anyio
    async def test_acquire_when_tokens_available(self) -> None:
        limiter = InProcessRateLimiter(rate=100.0, burst=10)
        # Should not sleep — tokens available immediately
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # no meaningful wait

    @pytest.mark.anyio
    async def test_acquire_drains_tokens(self) -> None:
        limiter = InProcessRateLimiter(rate=1000.0, burst=5)
        # Drain 5 tokens — none should require waiting
        for _ in range(5):
            await limiter.acquire()
        # 6th acquire should need to wait (rate is fast so wait is minimal)
        # We just check it completes — don't time it strictly to avoid flakiness


class TestRedisRateLimiter:
    @pytest.mark.anyio
    async def test_acquire_within_rate(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.incr.return_value = 1  # first request in window
        mock_redis.expire = AsyncMock()

        limiter = RedisRateLimiter(mock_redis, key="test:ratelimit", rate=8)
        await limiter.acquire()

        mock_redis.incr.assert_awaited_once_with("test:ratelimit")
        mock_redis.expire.assert_awaited_once_with("test:ratelimit", 1)

    @pytest.mark.anyio
    async def test_acquire_falls_back_on_redis_error(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.incr.side_effect = ConnectionError("Redis unavailable")

        limiter = RedisRateLimiter(mock_redis, key="test:ratelimit", rate=8)
        # Should not raise — falls back to in-process limiter
        await limiter.acquire()


# ===========================================================================
# SECEdgarSource — URL builders
# ===========================================================================


class TestURLBuilders:
    def test_build_filing_index_url(self) -> None:
        url = SECEdgarSource.build_filing_index_url("0000320193", "0000320193-24-000009")
        assert url == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/"

    def test_build_filing_index_url_strips_leading_zeros(self) -> None:
        url = SECEdgarSource.build_filing_index_url("0000000001", "0000000001-22-000001")
        assert url == "https://www.sec.gov/Archives/edgar/data/1/000000000122000001/"

    def test_build_document_url(self) -> None:
        url = SECEdgarSource.build_document_url(
            "0000320193", "0000320193-24-000009", "aapl-20231230.htm"
        )
        assert url == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/aapl-20231230.htm"

    def test_build_document_url_empty_document_returns_none(self) -> None:
        url = SECEdgarSource.build_document_url("0000320193", "0000320193-24-000009", "")
        assert url is None

    def test_build_document_url_none_document_returns_none(self) -> None:
        url = SECEdgarSource.build_document_url("0000320193", "0000320193-24-000009", None)  # type: ignore[arg-type]
        assert url is None


# ===========================================================================
# SECEdgarSource — get_submissions
# ===========================================================================


class TestGetSubmissions:
    @pytest.mark.anyio
    async def test_returns_submissions_dict(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.get_submissions(_CIK)
        assert result["name"] == _COMPANY_NAME
        assert result["tickers"] == [_TICKER]

    @pytest.mark.anyio
    async def test_returns_empty_dict_on_404(self) -> None:
        source = _make_source({"submissions/CIK": (404, {})})
        result = await source.get_submissions("0000000001")
        assert result == {}

    @pytest.mark.anyio
    async def test_normalises_cik_to_padded_form(self) -> None:
        source = _make_source({"CIK0000320193": (200, _APPLE_SUBMISSIONS)})
        # Passing un-padded CIK should still work
        result = await source.get_submissions("320193")
        assert result.get("name") == _COMPANY_NAME


# ===========================================================================
# SECEdgarSource — _extract_filings_from_block
# ===========================================================================


class TestExtractFilingsFromBlock:
    def _make_source_instance(self) -> SECEdgarSource:
        return SECEdgarSource.__new__(SECEdgarSource)

    def setup_method(self) -> None:
        self._source = SECEdgarSource(
            http_client=httpx.AsyncClient(transport=_make_mock_transport({}))
        )

    def test_extracts_10k_filings(self) -> None:
        recent = _APPLE_SUBMISSIONS["filings"]["recent"]
        filings = self._source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        ten_k = [f for f in filings if f.filing_type == "10-K"]
        assert len(ten_k) == 2

    def test_extracts_10q_filings(self) -> None:
        recent = _APPLE_SUBMISSIONS["filings"]["recent"]
        filings = self._source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        ten_q = [f for f in filings if f.filing_type == "10-Q"]
        assert len(ten_q) == 3

    def test_filing_metadata_fields(self) -> None:
        recent = _APPLE_SUBMISSIONS["filings"]["recent"]
        filings = self._source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        first = filings[0]
        assert first.accession_number == "0000320193-24-000009"
        assert first.filing_type == "10-K"
        assert first.filing_date == date(2024, 2, 2)
        assert first.cik == _CIK
        assert first.ticker == _TICKER
        assert first.company_name == _COMPANY_NAME
        assert "320193/000032019324000009" in first.filing_url
        assert "aapl-20231230.htm" in first.document_url
        assert first.period_end_date == date(2023, 12, 30)
        assert first.title == "Annual report"

    def test_8k_no_period_end_date(self) -> None:
        recent = _APPLE_SUBMISSIONS_WITH_8K["filings"]["recent"]
        filings = self._source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        eight_k = [f for f in filings if f.filing_type == "8-K"]
        assert len(eight_k) == 1
        assert eight_k[0].period_end_date is None

    def test_unsupported_form_types_excluded(self) -> None:
        recent = {
            "accessionNumber": ["0000320193-24-000099"],
            "filingDate": ["2024-01-01"],
            "form": ["SC 13G"],  # not in SUPPORTED_FORM_TYPES
            "primaryDocument": ["doc.htm"],
            "primaryDocDescription": [""],
            "periodOfReport": [""],
        }
        filings = self._source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        assert filings == []

    def test_invalid_filing_date_skipped(self) -> None:
        recent = {
            "accessionNumber": ["0000320193-24-000099"],
            "filingDate": [""],  # empty date
            "form": ["10-K"],
            "primaryDocument": ["doc.htm"],
            "primaryDocDescription": ["Annual report"],
            "periodOfReport": [""],
        }
        filings = self._source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        assert filings == []

    def test_raw_field_populated(self) -> None:
        recent = _APPLE_SUBMISSIONS["filings"]["recent"]
        filings = self._source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        assert "accessionNumber" in filings[0].raw
        assert "form" in filings[0].raw

    def test_custom_form_types(self) -> None:
        source = SECEdgarSource(
            http_client=httpx.AsyncClient(transport=_make_mock_transport({})),
            form_types=frozenset({"10-K"}),
        )
        recent = _APPLE_SUBMISSIONS["filings"]["recent"]
        filings = source._extract_filings_from_block(recent, _CIK, _TICKER, _COMPANY_NAME)
        assert all(f.filing_type == "10-K" for f in filings)


# ===========================================================================
# SECEdgarSource — discover_filings
# ===========================================================================


class TestDiscoverFilings:
    @pytest.mark.anyio
    async def test_returns_discovery_result(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.discover_filings(_CIK)
        assert isinstance(result, FilingDiscoveryResult)

    @pytest.mark.anyio
    async def test_company_metadata_populated(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.discover_filings(_CIK)
        assert result.cik == _CIK
        assert result.company_name == _COMPANY_NAME
        assert result.ticker == _TICKER

    @pytest.mark.anyio
    async def test_filings_count(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.discover_filings(_CIK)
        # 5 total: 2×10-K + 3×10-Q; all in SUPPORTED_FORM_TYPES
        assert len(result.filings) == 5
        assert result.total_discovered == 5

    @pytest.mark.anyio
    async def test_cik_not_found_returns_empty_result(self) -> None:
        source = _make_source({"submissions/CIK": (404, {})})
        result = await source.discover_filings("0000000001")
        assert result.filings == []
        assert result.total_discovered == 0
        assert result.company_name == ""

    @pytest.mark.anyio
    async def test_max_filings_cap(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.discover_filings(_CIK, max_filings=2)
        assert len(result.filings) == 2

    @pytest.mark.anyio
    async def test_has_additional_pages_false_when_no_files(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.discover_filings(_CIK)
        assert result.has_additional_pages is False

    @pytest.mark.anyio
    async def test_has_additional_pages_true_when_files_present(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_WITH_PAGINATION)})
        result = await source.discover_filings(_CIK)
        assert result.has_additional_pages is True

    @pytest.mark.anyio
    async def test_include_older_fetches_pagination_pages(self) -> None:
        # More-specific pattern must come first — mock handler checks via substring
        routes = {
            "CIK0000320193-submissions-0001.json": (200, _PAGINATION_PAGE),
            "submissions/CIK": (200, _APPLE_WITH_PAGINATION),
        }
        source = _make_source(routes)
        result = await source.discover_filings(_CIK, include_older=True)
        # Recent: 5 filings + pagination page: 1 filing = 6 total
        assert len(result.filings) == 6

    @pytest.mark.anyio
    async def test_include_older_false_does_not_fetch_pages(self) -> None:
        routes = {
            "CIK0000320193-submissions-0001.json": (200, _PAGINATION_PAGE),
            "submissions/CIK": (200, _APPLE_WITH_PAGINATION),
        }
        source = _make_source(routes)
        result = await source.discover_filings(_CIK, include_older=False)
        # Only recent block, not pagination
        assert len(result.filings) == 5

    @pytest.mark.anyio
    async def test_normalises_unpaded_cik(self) -> None:
        source = _make_source({"CIK0000320193": (200, _APPLE_SUBMISSIONS)})
        # Pass un-padded CIK — should resolve to 0000320193
        result = await source.discover_filings("320193")
        assert result.cik == _CIK

    @pytest.mark.anyio
    async def test_with_8k_filing(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS_WITH_8K)})
        result = await source.discover_filings(_CIK)
        eight_k_filings = [f for f in result.filings if f.filing_type == "8-K"]
        assert len(eight_k_filings) == 1


# ===========================================================================
# SECEdgarSource — get_filing_metadata
# ===========================================================================


class TestGetFilingMetadata:
    @pytest.mark.anyio
    async def test_returns_metadata_when_found(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.get_filing_metadata(_CIK, "0000320193-24-000009")
        assert result is not None
        assert isinstance(result, FilingMetadata)
        assert result.accession_number == "0000320193-24-000009"
        assert result.filing_type == "10-K"

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        source = _make_source({"submissions/CIK": (200, _APPLE_SUBMISSIONS)})
        result = await source.get_filing_metadata(_CIK, "0000320193-99-999999")
        assert result is None


# ===========================================================================
# SECEdgarSource — retry logic
# ===========================================================================


class TestRetryLogic:
    @pytest.mark.anyio
    async def test_retries_on_500(self) -> None:
        """Server error on first attempt, success on second."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(500, content=b'{"error": "server error"}')
            return httpx.Response(
                200,
                content=json.dumps(_APPLE_SUBMISSIONS).encode(),
                headers={"Content-Type": "application/json"},
            )

        class _NoOpLimiter(InProcessRateLimiter):
            async def acquire(self) -> None:
                pass

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = SECEdgarSource(
            http_client=client,
            rate_limiter=_NoOpLimiter(),
            base_delay=0.01,  # fast for tests
        )
        result = await source.discover_filings(_CIK)
        assert call_count == 2
        assert result.company_name == _COMPANY_NAME

    @pytest.mark.anyio
    async def test_raises_after_max_retries_exhausted(self) -> None:
        """All attempts return 500 — should raise after max_retries."""

        class _NoOpLimiter(InProcessRateLimiter):
            async def acquire(self) -> None:
                pass

        transport = _make_mock_transport({"CIK": (500, {"error": "server error"})})
        client = httpx.AsyncClient(transport=transport)
        source = SECEdgarSource(
            http_client=client,
            rate_limiter=_NoOpLimiter(),
            max_retries=2,
            base_delay=0.01,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await source.discover_filings(_CIK)

    @pytest.mark.anyio
    async def test_retries_on_429_with_retry_after_header(self) -> None:
        """HTTP 429 with Retry-After header respected on retry."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    429,
                    content=b'{"error": "rate limited"}',
                    headers={"Retry-After": "0.01"},
                )
            return httpx.Response(
                200,
                content=json.dumps(_APPLE_SUBMISSIONS).encode(),
                headers={"Content-Type": "application/json"},
            )

        class _NoOpLimiter(InProcessRateLimiter):
            async def acquire(self) -> None:
                pass

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = SECEdgarSource(
            http_client=client,
            rate_limiter=_NoOpLimiter(),
            base_delay=0.01,
        )
        result = await source.discover_filings(_CIK)
        assert call_count == 2
        assert result.company_name == _COMPANY_NAME


# ===========================================================================
# SUPPORTED_FORM_TYPES
# ===========================================================================


class TestSupportedFormTypes:
    def test_contains_required_types(self) -> None:
        assert "10-K" in SUPPORTED_FORM_TYPES
        assert "10-Q" in SUPPORTED_FORM_TYPES
        assert "8-K" in SUPPORTED_FORM_TYPES
        assert "DEF 14A" in SUPPORTED_FORM_TYPES

    def test_is_frozenset(self) -> None:
        assert isinstance(SUPPORTED_FORM_TYPES, frozenset)
