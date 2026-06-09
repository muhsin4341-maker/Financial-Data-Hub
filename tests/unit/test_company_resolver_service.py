"""
Unit tests — CompanyResolverService.

Strategy
--------
The CompanyResolverProvider and the Redis client are both mocked so that the
service's caching logic and identifier normalisation are tested in isolation.

What is mocked
--------------
- CompanyResolverProvider   — resolve_ticker / resolve_cik return pre-built CompanyInfo
- Redis client              — get / setex return pre-set values via AsyncMock

What is NOT mocked (real code runs)
------------------------------------
- CompanyResolverService.resolve_by_ticker — cache key construction, cache miss/hit flow
- CompanyResolverService.resolve_by_cik   — CIK normalisation, dual-key write
- CompanyResolverService._cache_get/_cache_set — JSON serialisation, fail-open
- Ticker/CIK normalisation (strip, upper, zfill)

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acquisition.company_resolver.provider import CompanyInfo, CompanyResolverProvider
from services.acquisition.company_resolver.resolver import CompanyResolverService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AAPL = CompanyInfo(
    ticker="AAPL",
    company_name="Apple Inc.",
    cik="0000320193",
    exchange=None,
    country="US",
)


def _make_mock_provider(
    ticker_result: CompanyInfo | None = _AAPL,
    cik_result: CompanyInfo | None = _AAPL,
) -> MagicMock:
    provider = MagicMock(spec=CompanyResolverProvider)
    provider.resolve_ticker = AsyncMock(return_value=ticker_result)
    provider.resolve_cik = AsyncMock(return_value=cik_result)
    return provider


def _make_mock_redis(cached_value: object | None = None) -> MagicMock:
    redis = MagicMock()
    raw = json.dumps({
        "ticker": _AAPL.ticker,
        "company_name": _AAPL.company_name,
        "cik": _AAPL.cik,
        "exchange": _AAPL.exchange,
        "country": _AAPL.country,
    }).encode() if cached_value is not None else None
    redis.get = AsyncMock(return_value=raw)
    redis.setex = AsyncMock(return_value=True)
    return redis


# ---------------------------------------------------------------------------
# resolve_by_ticker — cache miss
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_by_ticker_cache_miss_calls_provider() -> None:
    """On cache miss, provider is called and result is returned."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    result = await service.resolve_by_ticker("AAPL")

    assert result == _AAPL
    provider.resolve_ticker.assert_called_once_with("AAPL")


@pytest.mark.anyio
async def test_resolve_by_ticker_cache_miss_writes_both_keys() -> None:
    """On cache miss + successful resolution, both ticker and CIK keys are written."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    await service.resolve_by_ticker("AAPL")

    # setex should be called twice: once for ticker key, once for CIK key.
    assert redis.setex.call_count == 2
    calls = {call.args[0] for call in redis.setex.call_args_list}
    assert "company:ticker:AAPL" in calls
    assert "company:cik:0000320193" in calls


@pytest.mark.anyio
async def test_resolve_by_ticker_normalises_to_uppercase() -> None:
    """Lowercase ticker input is normalised to uppercase before lookup."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    await service.resolve_by_ticker("aapl")

    provider.resolve_ticker.assert_called_once_with("AAPL")


@pytest.mark.anyio
async def test_resolve_by_ticker_returns_none_on_provider_miss() -> None:
    """Returns None when the provider cannot resolve the ticker."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider(ticker_result=None)
    service = CompanyResolverService(provider=provider, redis_client=redis)

    result = await service.resolve_by_ticker("INVALID_XYZ")

    assert result is None
    redis.setex.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_by_ticker — cache hit
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_by_ticker_cache_hit_skips_provider() -> None:
    """On cache hit, provider is not called."""
    redis = _make_mock_redis(cached_value=_AAPL)  # non-None triggers cache hit
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    result = await service.resolve_by_ticker("AAPL")

    assert result == _AAPL
    provider.resolve_ticker.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_by_cik — cache miss
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_by_cik_cache_miss_calls_provider() -> None:
    """On CIK cache miss, provider is called and result is returned."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    result = await service.resolve_by_cik("0000320193")

    assert result == _AAPL
    provider.resolve_cik.assert_called_once_with("0000320193")


@pytest.mark.anyio
async def test_resolve_by_cik_normalises_short_cik() -> None:
    """Short CIK is zero-padded to 10 digits before lookup."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    await service.resolve_by_cik("320193")

    provider.resolve_cik.assert_called_once_with("0000320193")


@pytest.mark.anyio
async def test_resolve_by_cik_writes_both_keys() -> None:
    """On successful CIK resolution, both CIK and ticker cache keys are written."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    await service.resolve_by_cik("0000320193")

    assert redis.setex.call_count == 2
    calls = {call.args[0] for call in redis.setex.call_args_list}
    assert "company:cik:0000320193" in calls
    assert "company:ticker:AAPL" in calls


# ---------------------------------------------------------------------------
# Redis fail-open
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_by_ticker_without_redis_calls_provider() -> None:
    """When no Redis client is provided, provider is always called."""
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=None)

    result = await service.resolve_by_ticker("AAPL")

    assert result == _AAPL
    provider.resolve_ticker.assert_called_once_with("AAPL")


@pytest.mark.anyio
async def test_redis_get_error_falls_through_to_provider() -> None:
    """Redis.get raising an exception does not prevent resolution."""
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=ConnectionError("Redis unreachable"))
    redis.setex = AsyncMock(return_value=True)

    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    result = await service.resolve_by_ticker("AAPL")

    assert result == _AAPL
    provider.resolve_ticker.assert_called_once_with("AAPL")


@pytest.mark.anyio
async def test_redis_setex_error_does_not_prevent_result() -> None:
    """Redis.setex raising an exception does not prevent the result from being returned."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)  # cache miss
    redis.setex = AsyncMock(side_effect=ConnectionError("Redis unreachable"))

    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    result = await service.resolve_by_ticker("AAPL")

    assert result == _AAPL


# ---------------------------------------------------------------------------
# TTL and cache key format
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cache_ttl_passed_to_setex() -> None:
    """Cache TTL from constructor is passed to Redis.setex."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(
        provider=provider, redis_client=redis, cache_ttl=3600
    )

    await service.resolve_by_ticker("AAPL")

    # Both setex calls should use TTL=3600
    for call in redis.setex.call_args_list:
        assert call.args[1] == 3600


@pytest.mark.anyio
async def test_cache_key_format_ticker() -> None:
    """Ticker cache key format is 'company:ticker:{TICKER}'."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    await service.resolve_by_ticker("AAPL")

    get_call_arg = redis.get.call_args.args[0]
    assert get_call_arg == "company:ticker:AAPL"


@pytest.mark.anyio
async def test_cache_key_format_cik() -> None:
    """CIK cache key format is 'company:cik:{CIK}'."""
    redis = _make_mock_redis(cached_value=None)
    provider = _make_mock_provider()
    service = CompanyResolverService(provider=provider, redis_client=redis)

    await service.resolve_by_cik("0000320193")

    get_call_arg = redis.get.call_args.args[0]
    assert get_call_arg == "company:cik:0000320193"
