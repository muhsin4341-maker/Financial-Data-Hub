"""
SEC EDGAR company resolver.

Resolves US company tickers and CIKs using two SEC EDGAR public endpoints:

  company_tickers.json:
    https://www.sec.gov/files/company_tickers.json
    Returns a flat dict of all registered US companies with their ticker,
    CIK, and name. Used for O(1) ticker → CompanyInfo lookups after a
    one-time fetch per resolver instance.

  Company submissions:
    https://data.sec.gov/submissions/CIK{cik}.json
    Returns full company metadata (name, tickers, exchanges, SIC, state, etc.)
    Used for CIK → CompanyInfo lookups.

SEC EDGAR acceptable use policy requires a descriptive User-Agent header
identifying your application and contact email. The user_agent param is
forwarded to every HTTP request made by this resolver.

Rate limit: SEC EDGAR allows up to 10 requests/second. The resolver does not
enforce the limit internally — the caller (CompanyResolverService) is
responsible for rate limiting via the source registry configuration.

Milestone: M3.2 — Company Resolver
"""

from __future__ import annotations

import asyncio
import random

import httpx
import structlog

from services.acquisition.company_resolver.provider import (
    CompanyInfo,
    CompanyResolverProvider,
)
from services.acquisition.source_registry.rate_limiter import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    InProcessRateLimiter,
    RateLimiter,
)

log = structlog.get_logger(__name__)

# SEC EDGAR public API endpoints — no authentication required.
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"

_DEFAULT_TIMEOUT: float = 30.0
_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BASE_DELAY: float = 1.0
_MAX_JITTER: float = 0.5

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class SECCompanyResolver(CompanyResolverProvider):
    """
    Resolves US company identifiers via SEC EDGAR public APIs.

    Ticker map is fetched once on first use and cached on the instance.
    CIK lookups always make a live HTTP request (submissions endpoint).

    All outbound HTTP calls are guarded by:
      - A token-bucket RateLimiter (Amendment V1.2 §4.1 — max 10 req/s).
      - A CircuitBreaker (Amendment V1.2 §9.2 — trips open after 5 consecutive
        failures, holds for 30 minutes to prevent SEC IP blacklisting).

    The optional http_client parameter is intended for testing — pass a
    pre-configured httpx.AsyncClient with a MockTransport to avoid real
    network calls. When not provided, a client is created lazily on first use.
    """

    def __init__(
        self,
        user_agent: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        # Amendment V1.2 §4.1: User-Agent must identify the application with
        # real contact details. Resolve from settings rather than using a
        # hardcoded placeholder that would fail SEC acceptable-use validation.
        if user_agent is None:
            from apps.api.core.config import get_settings
            user_agent = get_settings().edgar_user_agent
        self._user_agent = user_agent
        self._client = http_client
        self._owns_client = http_client is None
        self._rate_limiter = rate_limiter or InProcessRateLimiter(rate=8.0, burst=10)
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            source_name="SEC_EDGAR_RESOLVER"
        )
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._timeout = timeout
        # In-memory ticker map: uppercase ticker → CompanyInfo.
        # Populated on first call to resolve_ticker; None = not yet loaded.
        self._ticker_map: dict[str, CompanyInfo] | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client if this resolver owns it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── HTTP with rate limiting, circuit breaker, and retry ───────────────────

    async def _get(self, url: str) -> httpx.Response:
        """
        Perform a GET request with rate limiting, circuit breaker, and retry.

        Mirrors the same contract as SECEdgarSource._get() (Amendment V1.2 §4.1
        and §9.2): check circuit breaker before the retry loop; record success /
        failure on every terminal outcome; propagate CircuitBreakerOpenError
        without swallowing.

        Raises:
            CircuitBreakerOpenError: Breaker is open — do not retry.
            httpx.HTTPStatusError:   Non-retryable 4xx.
            httpx.HTTPError:         Network failure after all retries exhausted.
        """
        # Amendment V1.2 §9.2: check before any attempt.
        self._circuit_breaker.check()

        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            await self._rate_limiter.acquire()

            try:
                client = await self._get_client()
                resp = await client.get(url)

                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt >= self._max_retries:
                        self._circuit_breaker.record_failure()
                        resp.raise_for_status()

                    retry_after_hdr = resp.headers.get("Retry-After")
                    if retry_after_hdr:
                        try:
                            delay = float(retry_after_hdr)
                        except ValueError:
                            delay = self._base_delay * (2 ** attempt)
                    else:
                        delay = self._base_delay * (2 ** attempt)

                    delay += random.uniform(0, _MAX_JITTER)
                    log.warning(
                        "sec_resolver.request_retrying",
                        url=url,
                        status=resp.status_code,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        delay_seconds=round(delay, 3),
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    self._circuit_breaker.record_failure()
                    resp.raise_for_status()

                self._circuit_breaker.record_success()
                return resp

            except CircuitBreakerOpenError:
                raise

            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    self._circuit_breaker.record_failure()
                    raise

                delay = self._base_delay * (2 ** attempt) + random.uniform(0, _MAX_JITTER)
                log.warning(
                    "sec_resolver.network_error_retrying",
                    url=url,
                    error=type(exc).__name__,
                    detail=str(exc),
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    delay_seconds=round(delay, 3),
                )
                await asyncio.sleep(delay)

        raise last_exc or RuntimeError(
            f"Request failed after {self._max_retries} retries: {url}"
        )

    # ── Data loading ──────────────────────────────────────────────────────────

    async def _load_ticker_map(self) -> dict[str, CompanyInfo]:
        """
        Fetch and parse SEC EDGAR company_tickers.json.

        SEC response shape:
          { "0": { "cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc." }, ... }

        Returns a dict keyed by uppercase ticker. Results are cached on the
        instance; subsequent calls return the cached map without a network request.
        """
        if self._ticker_map is not None:
            return self._ticker_map

        try:
            resp = await self._get(_TICKERS_URL)
            resp.raise_for_status()
        except (httpx.HTTPError, CircuitBreakerOpenError) as exc:
            log.error(
                "sec_resolver.ticker_map_fetch_failed",
                url=_TICKERS_URL,
                error=str(exc),
            )
            return {}

        data = resp.json()
        ticker_map: dict[str, CompanyInfo] = {}
        for entry in data.values():
            cik_int = entry.get("cik_str", 0)
            raw_ticker = str(entry.get("ticker", "")).strip()
            title = str(entry.get("title", "")).strip()
            if not raw_ticker:
                continue
            ticker = raw_ticker.upper()
            cik_str = str(cik_int).zfill(10)
            ticker_map[ticker] = CompanyInfo(
                ticker=ticker,
                company_name=title,
                cik=cik_str,
                exchange=None,  # tickers JSON does not include exchange
                country="US",   # SEC EDGAR covers US-registered companies only
            )

        self._ticker_map = ticker_map
        log.info(
            "sec_resolver.ticker_map_loaded",
            company_count=len(ticker_map),
        )
        return ticker_map

    async def resolve_ticker(self, ticker: str) -> CompanyInfo | None:
        """
        Look up a company by ticker symbol.

        Loads the SEC EDGAR company tickers JSON on first call (cached for
        the lifetime of this resolver instance). O(1) dict lookup thereafter.

        Args:
            ticker: Ticker symbol; normalised to uppercase before lookup.

        Returns:
            CompanyInfo if found in SEC EDGAR, None otherwise.
        """
        normalized = ticker.strip().upper()
        ticker_map = await self._load_ticker_map()
        result = ticker_map.get(normalized)
        if result is None:
            log.debug("sec_resolver.ticker_not_found", ticker=normalized)
        else:
            log.debug(
                "sec_resolver.ticker_resolved",
                ticker=normalized,
                cik=result.cik,
            )
        return result

    async def resolve_cik(self, cik: str) -> CompanyInfo | None:
        """
        Look up a company by CIK via the SEC EDGAR submissions endpoint.

        Makes a live HTTP request each call — results should be cached by
        CompanyResolverService to avoid redundant network requests.

        Args:
            cik: CIK string — either numeric ('320193') or zero-padded
                 ('0000320193'). Normalised to 10-digit padded internally.

        Returns:
            CompanyInfo if found, None if the CIK returns HTTP 404.
        """
        padded_cik = cik.strip().lstrip("0").zfill(10)
        url = _SUBMISSIONS_URL_TEMPLATE.format(cik=padded_cik)

        try:
            resp = await self._get(url)
            if resp.status_code == 404:
                log.debug("sec_resolver.cik_not_found", cik=padded_cik)
                return None
            resp.raise_for_status()
        except (httpx.HTTPError, CircuitBreakerOpenError) as exc:
            log.error(
                "sec_resolver.cik_fetch_failed",
                cik=padded_cik,
                url=url,
                error=str(exc),
            )
            return None

        data = resp.json()
        name = data.get("name", "").strip()
        tickers_list = data.get("tickers", [])
        exchanges_list = data.get("exchanges", [])

        ticker = tickers_list[0].upper() if tickers_list else padded_cik
        exchange = exchanges_list[0] if exchanges_list else None

        info = CompanyInfo(
            ticker=ticker,
            company_name=name,
            cik=padded_cik,
            exchange=exchange,
            country="US",
        )
        log.debug(
            "sec_resolver.cik_resolved",
            cik=padded_cik,
            ticker=ticker,
        )
        return info
