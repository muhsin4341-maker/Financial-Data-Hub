"""
Company Resolver Service.

Resolves ticker symbols or SEC CIKs to canonical CompanyInfo records.
Results are cached in Redis to avoid redundant external API calls.

Public interface (strict — raises on failure):
  resolve_by_ticker(ticker) -> CompanyInfo
  resolve_by_cik(cik)       -> CompanyInfo

Both methods raise CompanyResolutionError when the identifier cannot be
resolved, so the caller never needs to check for None. The AcquisitionJob
pipeline catches CompanyResolutionError and transitions the job to FAILED.

Cache key format (shared namespace across all resolver instances):
  company:ticker:{TICKER}   — e.g. company:ticker:AAPL
  company:cik:{CIK}         — e.g. company:cik:0000320193

Default TTL: 86400 seconds (24 hours). Company identifiers change rarely;
a 24-hour cache avoids stale data after ticker re-listings while remaining
aggressive enough to substantially reduce SEC EDGAR request volume.

Redis fail-open: if Redis is unreachable, resolution proceeds without cache
and the failure is logged at WARNING. A Redis outage never blocks resolution.

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

import json

import structlog

from services.acquisition.company_resolver.provider import (
    CompanyInfo,
    CompanyResolutionError,
    CompanyResolverProvider,
)
from services.acquisition.company_resolver.sec_resolver import SECCompanyResolver

log = structlog.get_logger(__name__)

_DEFAULT_CACHE_TTL = 86400  # 24 hours


class CompanyResolverService:
    """
    High-level company identifier resolver with Redis caching.

    Strict interface — raises ``CompanyResolutionError`` on failure::

        service = CompanyResolverService(user_agent="FDH contact@example.com")

        # Happy path
        info = await service.resolve_by_ticker("AAPL")
        # CompanyInfo(ticker='AAPL', cik='0000320193', ...)

        # Failure path — always raises, never returns None
        try:
            info = await service.resolve_by_ticker("ZZZZZ")
        except CompanyResolutionError as exc:
            print(exc.query)           # 'ZZZZZ'
            print(exc.strategies_tried) # ['SEC_EDGAR_TICKERS']

    The service wraps a ``CompanyResolverProvider`` (default: SECCompanyResolver).
    Cache misses fall through to the provider; hits are returned immediately.

    When a ticker resolves successfully the result is written to BOTH the
    ticker cache key and the CIK cache key so that a subsequent CIK lookup
    avoids a second provider call.
    """

    def __init__(
        self,
        provider: CompanyResolverProvider | None = None,
        redis_client: object | None = None,
        cache_ttl: int = _DEFAULT_CACHE_TTL,
        user_agent: str = "FinancialDataHub contact@example.com",
    ) -> None:
        self._provider: CompanyResolverProvider = provider or SECCompanyResolver(
            user_agent=user_agent,
        )
        self._redis = redis_client
        self._cache_ttl = cache_ttl

    # ── Redis helpers (fail-open) ─────────────────────────────────────────────

    async def _cache_get(self, key: str) -> CompanyInfo | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)  # type: ignore[union-attr]
            if raw is None:
                return None
            data = json.loads(raw)
            return CompanyInfo(
                ticker=data["ticker"],
                company_name=data["company_name"],
                cik=data["cik"],
                exchange=data.get("exchange"),
                country=data.get("country", "US"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "company_resolver.cache_get_error",
                key=key,
                error=str(exc),
            )
            return None

    async def _cache_set(self, key: str, info: CompanyInfo) -> None:
        if self._redis is None:
            return
        try:
            payload = json.dumps({
                "ticker": info.ticker,
                "company_name": info.company_name,
                "cik": info.cik,
                "exchange": info.exchange,
                "country": info.country,
            })
            await self._redis.setex(  # type: ignore[union-attr]
                key, self._cache_ttl, payload
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "company_resolver.cache_set_error",
                key=key,
                error=str(exc),
            )

    # ── Public interface ──────────────────────────────────────────────────────

    async def resolve_by_ticker(self, ticker: str) -> CompanyInfo:
        """
        Resolve a ticker symbol to canonical CompanyInfo.

        Cache lookup order:
          1. Redis key company:ticker:{TICKER} — return immediately on hit.
          2. Provider (SECCompanyResolver) — on miss, fetch from external API.
          3. Write result to company:ticker:{TICKER} and company:cik:{CIK}.

        Args:
            ticker: Ticker symbol (case-insensitive; normalised to uppercase).

        Returns:
            CompanyInfo on success — always a fully populated record with a
            10-digit zero-padded CIK (e.g. ``'0000320193'``).

        Raises:
            CompanyResolutionError: The ticker is unknown to the provider
                after all strategies are exhausted. The exception message
                is suitable for writing directly to FinancialJob.error_message.
        """
        normalized = ticker.strip().upper()
        cache_key = f"company:ticker:{normalized}"

        cached = await self._cache_get(cache_key)
        if cached is not None:
            log.debug("company_resolver.cache_hit", ticker=normalized)
            return cached

        info = await self._provider.resolve_ticker(normalized)

        if info is None:
            log.warning("company_resolver.ticker_unresolved", ticker=normalized)
            raise CompanyResolutionError(
                normalized,
                strategies_tried=["SEC_EDGAR_TICKERS"],
                reason="ticker not found in SEC EDGAR company registry",
            )

        await self._cache_set(cache_key, info)
        await self._cache_set(f"company:cik:{info.cik}", info)
        log.info(
            "company_resolver.resolved_by_ticker",
            ticker=normalized,
            cik=info.cik,
            company_name=info.company_name,
        )
        return info

    async def resolve_by_cik(self, cik: str) -> CompanyInfo:
        """
        Resolve a CIK to canonical CompanyInfo.

        CIK is normalised to a 10-digit zero-padded string before lookup.

        Cache lookup order:
          1. Redis key company:cik:{CIK} — return immediately on hit.
          2. Provider — on miss, fetch from external API.
          3. Write result to company:cik:{CIK} and company:ticker:{TICKER}.

        Args:
            cik: CIK string — numeric ('320193') or padded ('0000320193').

        Returns:
            CompanyInfo on success — always a fully populated record.

        Raises:
            CompanyResolutionError: The CIK is unknown to the provider.
        """
        padded = cik.strip().lstrip("0").zfill(10)
        cache_key = f"company:cik:{padded}"

        cached = await self._cache_get(cache_key)
        if cached is not None:
            log.debug("company_resolver.cache_hit", cik=padded)
            return cached

        info = await self._provider.resolve_cik(padded)

        if info is None:
            log.warning("company_resolver.cik_unresolved", cik=padded)
            raise CompanyResolutionError(
                padded,
                strategies_tried=["SEC_EDGAR_SUBMISSIONS"],
                reason="CIK not found in SEC EDGAR submissions API",
            )

        await self._cache_set(cache_key, info)
        if info.ticker:
            await self._cache_set(f"company:ticker:{info.ticker.upper()}", info)
        log.info(
            "company_resolver.resolved_by_cik",
            cik=padded,
            ticker=info.ticker,
            company_name=info.company_name,
        )
        return info
