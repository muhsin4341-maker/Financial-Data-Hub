"""
SEC EDGAR acquisition source.

Primary acquisition provider for US regulatory filings (10-K, 10-Q, 8-K,
DEF 14A) from the SEC Electronic Data Gathering, Analysis, and Retrieval
(EDGAR) system.

Architecture position:

  CompanyResolverService  (M3.2)
    ↓  resolves CIK from ticker
  SECEdgarSource          (M3.4) ← this module
    ↓  discovers filings, extracts metadata
  FilingService / FilingRepository  (M3.3)
    ↓  persists Filing records to PostgreSQL

Public API:

  SECEdgarSource.discover_filings(cik)
    → FilingDiscoveryResult containing a list of FilingMetadata

  SECEdgarSource.get_submissions(cik)
    → raw dict from the SEC EDGAR submissions endpoint (for debugging)

  SECEdgarSource.get_filing_metadata(cik, accession_number)
    → single FilingMetadata or None

SEC EDGAR acceptable use:
  The SEC requires a descriptive User-Agent identifying your application and
  contact email, e.g. "FinancialDataHub contact@example.com".  All HTTP
  requests include this header.

  Rate limit: SEC allows 10 req/s.  Default rate limiter is configured to
  8 req/s (20% margin).  Do NOT remove rate limiting — SEC will block IPs
  that exceed the limit.

Rate limiting:
  InProcessRateLimiter (default) — asyncio token bucket, single process.
  RedisRateLimiter (optional)    — distributed, for multi-process Celery workers.
  Inject a custom limiter via the ``rate_limiter`` constructor argument.

Retry policy:
  Transient failures (HTTP 429, 5xx, connection reset, timeout) are retried
  with exponential backoff and full jitter (jitter range: 0–500ms).
  Default: 3 retries, base delay 1 second (max ~8 seconds before final failure).
  HTTP 4xx errors (except 429) are not retried — they indicate permanent failure.

Filing URL format (SEC Archives):
  Index URL:    https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dashes}/
  Document URL: https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dashes}/{primary_document}
  Where:
    cik_int        = CIK with leading zeros stripped (e.g. 320193)
    acc_no_dashes  = accession number with dashes removed (e.g. 000032019324000009)

SEC pagination:
  The submissions endpoint returns a 'recent' block (≤1000 filings) plus a
  'files' array listing older filing pages.  Set include_older=True to also
  fetch paginated pages for companies with long filing histories.

Milestone: M3.4 — SEC EDGAR Integration
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
import structlog

from services.acquisition.source_registry.rate_limiter import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    InProcessRateLimiter,
    RateLimiter,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# SEC EDGAR API endpoints
# ---------------------------------------------------------------------------

# Company submissions — returns metadata + recent filings.
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# Pagination continuation URLs (for companies with >1000 recent filings).
# SEC returns these in the submissions['filings']['files'] list.
_SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{filename}"

# SEC Archives — base path for filing index and document URLs.
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

#: Filing form types this source supports by default.
#: Matches FilingType enum values defined in M3.3.
SUPPORTED_FORM_TYPES: frozenset[str] = frozenset({"10-K", "10-Q", "8-K", "DEF 14A"})

_DEFAULT_TIMEOUT: float = 30.0
_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BASE_DELAY: float = 1.0
_MAX_JITTER: float = 0.5  # seconds of random jitter on retry delays

# HTTP status codes that warrant a retry attempt.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FilingMetadata:
    """
    Normalized metadata for a single SEC EDGAR filing.

    Produced by SECEdgarSource.discover_filings() and suitable for direct
    conversion to FilingCreate (M3.3 schema) for persistence.

    Fields:
      accession_number  SEC EDGAR accession number in 'XXXXXXXXXX-YY-ZZZZZZ' form.
                        Globally unique across all filers. Example: '0000320193-24-000009'.
      filing_type       SEC form type matching FilingType enum values. Example: '10-K'.
      filing_date       Date the filing was submitted to SEC EDGAR.
      cik               10-digit zero-padded SEC CIK. Example: '0000320193'.
      ticker            Primary ticker symbol at time of filing. None if unknown.
      company_name      Company name from SEC EDGAR. Example: 'Apple Inc.'.
      filing_url        URL to the filing index page on SEC Archives.
      document_url      URL to the primary filing document. None if unavailable.
      title             Human-readable filing description from SEC EDGAR.
      period_end_date   Fiscal period end date. None for non-periodic filings (8-K).
      raw               Original columnar row data from the SEC API for audit/debug.
    """

    accession_number: str
    filing_type: str
    filing_date: date
    cik: str
    ticker: str | None
    company_name: str
    filing_url: str
    document_url: str | None
    title: str | None
    period_end_date: date | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FilingDiscoveryResult:
    """
    Result of a company filing discovery operation.

    Returned by SECEdgarSource.discover_filings().

    Fields:
      cik                  10-digit zero-padded CIK of the company queried.
      company_name         Company name from SEC EDGAR.
      ticker               Primary ticker (first from SEC tickers list).
      filings              Discovered filings matching the configured form types.
      total_discovered     Count of filings in the filings list.
      has_additional_pages True if the company has older filings in paginated SEC
                           files not included in this result (set include_older=True
                           to fetch them).
    """

    cik: str
    company_name: str
    ticker: str | None
    filings: list[FilingMetadata]
    total_discovered: int
    has_additional_pages: bool


# ---------------------------------------------------------------------------
# SECEdgarSource
# ---------------------------------------------------------------------------


class SECEdgarSource:
    """
    SEC EDGAR acquisition source — filing discovery and metadata retrieval.

    Instantiate once per process (or per Celery worker) and reuse::

        source = SECEdgarSource(user_agent="MyApp contact@example.com")
        result = await source.discover_filings("0000320193")
        for filing in result.filings:
            print(filing.accession_number, filing.filing_type, filing.filing_date)
        await source.close()

    For multi-process Celery workers, inject a RedisRateLimiter to share
    the rate budget across processes::

        from services.acquisition.source_registry.rate_limiter import RedisRateLimiter
        limiter = RedisRateLimiter(redis_client, key="ratelimit:sec_edgar")
        source = SECEdgarSource(rate_limiter=limiter)

    For testing, inject an httpx.AsyncClient with MockTransport::

        client = httpx.AsyncClient(transport=mock_transport)
        source = SECEdgarSource(http_client=client)
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_delay: float = _DEFAULT_BASE_DELAY,
        form_types: frozenset[str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        """
        Args:
            user_agent:       SEC-required User-Agent header. Must include app name
                              and contact email per SEC EDGAR acceptable use policy.
            rate_limiter:     Custom rate limiter; defaults to InProcessRateLimiter(8 req/s).
            circuit_breaker:  CircuitBreaker instance (Amendment V1.2 §9.2). Defaults to a
                              new CircuitBreaker(source_name="SEC_EDGAR") — trips open after
                              5 consecutive failures and holds for 30 minutes.
            http_client:      Pre-built httpx.AsyncClient (for testing with MockTransport).
                              When None, a client is created lazily and owned by this instance.
            max_retries:      Maximum retry attempts for transient failures (default 3).
            base_delay:       Base delay in seconds for exponential backoff (default 1.0).
            form_types:       Override the set of supported form types to discover.
                              Defaults to SUPPORTED_FORM_TYPES: {10-K, 10-Q, 8-K, DEF 14A}.
            timeout:          HTTP request timeout in seconds (default 30).
        """
        # Amendment V1.2 §4.1: resolve User-Agent from settings so that a real
        # contact address is always sent — never fall back to a placeholder domain.
        if user_agent is None:
            from apps.api.core.config import get_settings
            user_agent = get_settings().edgar_user_agent
        self._user_agent = user_agent
        self._rate_limiter = rate_limiter or InProcessRateLimiter(rate=8.0, burst=10)
        self._circuit_breaker = circuit_breaker or CircuitBreaker(source_name="SEC_EDGAR")
        self._client = http_client
        self._owns_client = http_client is None
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._form_types = form_types if form_types is not None else SUPPORTED_FORM_TYPES
        self._timeout = timeout

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating one lazily if not injected."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """
        Close the HTTP client if owned by this instance.

        Call this when the source is no longer needed (e.g. in a finally block
        or lifespan context manager) to release the underlying connection pool.
        """
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── HTTP with rate limiting and retry ──────────────────────────────────────

    async def _get(self, url: str) -> httpx.Response:
        """
        Perform a GET request with rate limiting and exponential backoff retry.

        Rate limiting:
          Awaits the rate limiter before every request attempt (including retries).

        Retry policy:
          - HTTP 429, 500–504: retry with exponential backoff + jitter.
          - HTTP 4xx (except 429): raise immediately (permanent failure).
          - ConnectError, TimeoutException, RemoteProtocolError: retry.
          - After max_retries exhausted: re-raise the last exception.

        Jitter:
          Each retry delay is base_delay × 2^attempt + uniform(0, 0.5s).
          Full jitter prevents thundering-herd if multiple workers retry simultaneously.

        Args:
            url: Full URL to GET.

        Returns:
            httpx.Response with a 2xx or 3xx status code.

        Raises:
            CircuitBreakerOpenError: If the SEC_EDGAR circuit breaker is open.
                                     Callers must NOT retry — propagate immediately.
            httpx.HTTPStatusError:   Non-retryable 4xx after raise_for_status().
            httpx.HTTPError:         Network failure after all retries exhausted.
        """
        # Amendment V1.2 §9.2: check breaker before any attempt.
        # Raises CircuitBreakerOpenError immediately if open — do not retry.
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

                    # Honour Retry-After header if present (429 responses).
                    retry_after_header = resp.headers.get("Retry-After")
                    if retry_after_header:
                        try:
                            delay = float(retry_after_header)
                        except ValueError:
                            delay = self._base_delay * (2 ** attempt)
                    else:
                        delay = self._base_delay * (2 ** attempt)

                    delay += random.uniform(0, _MAX_JITTER)
                    log.warning(
                        "sec_edgar.request_retrying",
                        url=url,
                        status=resp.status_code,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        delay_seconds=round(delay, 3),
                    )
                    await asyncio.sleep(delay)
                    continue

                # Non-retryable 4xx: record failure and raise immediately.
                if resp.status_code >= 400:
                    self._circuit_breaker.record_failure()
                    resp.raise_for_status()

                # 2xx / 3xx success: reset failure counter and return.
                self._circuit_breaker.record_success()
                return resp

            except CircuitBreakerOpenError:
                raise  # never swallow — propagate directly to caller

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
                    "sec_edgar.network_error_retrying",
                    url=url,
                    error=type(exc).__name__,
                    detail=str(exc),
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    delay_seconds=round(delay, 3),
                )
                await asyncio.sleep(delay)

        # Should be unreachable — all paths above either return or raise.
        raise last_exc or RuntimeError(
            f"Request failed after {self._max_retries} retries: {url}"
        )

    # ── URL builders ───────────────────────────────────────────────────────────

    @staticmethod
    def build_filing_index_url(cik: str, accession_number: str) -> str:
        """
        Build the SEC Archives URL to the filing index page.

        The index page lists all documents submitted as part of the filing
        (primary document, exhibits, XBRL data, etc.).

        Args:
            cik:              10-digit zero-padded CIK (e.g. '0000320193').
            accession_number: Dashed accession number (e.g. '0000320193-24-000009').

        Returns:
            URL string, e.g.:
            'https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/'
        """
        cik_int = str(int(cik))           # strip leading zeros: '0000320193' → '320193'
        acc_no_dashes = accession_number.replace("-", "")
        return f"{_ARCHIVES_BASE}/{cik_int}/{acc_no_dashes}/"

    @staticmethod
    def build_document_url(
        cik: str, accession_number: str, primary_document: str
    ) -> str | None:
        """
        Build the SEC Archives URL to the primary filing document.

        Args:
            cik:              10-digit zero-padded CIK.
            accession_number: Dashed accession number.
            primary_document: Filename of the primary document (e.g. 'aapl-20231230.htm').

        Returns:
            URL string, or None if primary_document is empty or None.
            Example: 'https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/aapl-20231230.htm'
        """
        if not primary_document:
            return None
        cik_int = str(int(cik))
        acc_no_dashes = accession_number.replace("-", "")
        return f"{_ARCHIVES_BASE}/{cik_int}/{acc_no_dashes}/{primary_document}"

    # ── Submissions endpoint ───────────────────────────────────────────────────

    async def get_submissions(self, cik: str) -> dict[str, Any]:
        """
        Fetch raw company submissions from SEC EDGAR.

        Returns the full JSON response from the submissions endpoint.  This
        includes company metadata (name, tickers, exchanges, SIC, address)
        and a 'filings' section containing the 'recent' block (up to ~1000
        most-recent filings) and a 'files' array of pagination continuation URLs.

        Args:
            cik: CIK — accepts zero-padded or numeric form.
                 Normalised internally to 10-digit zero-padded.

        Returns:
            Parsed JSON dict, or empty dict if the CIK returns HTTP 404.

        Raises:
            httpx.HTTPStatusError: For non-retryable non-404 HTTP errors.
            httpx.HTTPError:       For network failures after all retries.
        """
        padded_cik = cik.strip().zfill(10)
        url = _SUBMISSIONS_URL.format(cik=padded_cik)

        log.debug("sec_edgar.fetching_submissions", cik=padded_cik)
        resp = await self._get(url)

        if resp.status_code == 404:
            log.warning("sec_edgar.cik_not_found", cik=padded_cik)
            return {}

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        log.debug(
            "sec_edgar.submissions_fetched",
            cik=padded_cik,
            company_name=data.get("name", ""),
        )
        return data

    async def _fetch_pagination_page(self, filename: str) -> dict[str, Any]:
        """
        Fetch an additional filing page for companies with long filing histories.

        SEC EDGAR returns older filings in separate JSON files listed in
        submissions['filings']['files'].  Each file has the same columnar
        layout as the 'recent' block.

        Args:
            filename: Filename from the files[] array (e.g. 'CIK0000320193-submissions-0001.json').

        Returns:
            Parsed JSON dict of the page (contains a 'recent'-equivalent block).
        """
        url = _SUBMISSIONS_PAGE_URL.format(filename=filename)
        log.debug("sec_edgar.fetching_submissions_page", filename=filename, url=url)
        resp = await self._get(url)
        resp.raise_for_status()
        return resp.json()

    # ── Filing extraction ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_date(date_str: str) -> date | None:
        """
        Parse a 'YYYY-MM-DD' string to a date object.

        Returns None for empty, missing, or malformed date strings rather
        than raising — SEC EDGAR occasionally omits the periodOfReport field
        for non-periodic filings (e.g. 8-K).
        """
        if not date_str:
            return None
        try:
            return date.fromisoformat(date_str)
        except (ValueError, TypeError):
            return None

    def _extract_filings_from_block(
        self,
        recent: dict[str, Any],
        cik: str,
        ticker: str | None,
        company_name: str,
    ) -> list[FilingMetadata]:
        """
        Extract FilingMetadata records from a SEC columnar 'recent' filings block.

        SEC EDGAR returns filing data in a columnar (parallel-array) layout:
          {
            "accessionNumber": ["0000320193-24-000009", ...],
            "filingDate":      ["2024-02-02", ...],
            "form":            ["10-K", ...],
            "primaryDocument": ["aapl-20231230.htm", ...],
            ...
          }

        Only filings whose form type is in self._form_types are extracted.
        Rows with unparseable filing dates are skipped with a warning.

        Args:
            recent:       The 'recent' sub-dict from the submissions JSON.
            cik:          10-digit zero-padded CIK for URL construction.
            ticker:       Primary ticker symbol (may be None).
            company_name: Company name for FilingMetadata records.

        Returns:
            List of FilingMetadata for supported form types in this block.
        """
        acc_numbers: list[str] = recent.get("accessionNumber", [])
        filing_dates: list[str] = recent.get("filingDate", [])
        forms: list[str] = recent.get("form", [])
        primary_docs: list[str] = recent.get("primaryDocument", [])
        primary_descs: list[str] = recent.get("primaryDocDescription", [])
        period_of_reports: list[str] = recent.get("periodOfReport", [])

        results: list[FilingMetadata] = []

        for i, raw_acc_no in enumerate(acc_numbers):
            form_type = forms[i] if i < len(forms) else ""
            if form_type not in self._form_types:
                continue

            accession_number = raw_acc_no.strip()

            raw_filing_date = filing_dates[i] if i < len(filing_dates) else ""
            filing_date = self._parse_date(raw_filing_date)
            if filing_date is None:
                log.warning(
                    "sec_edgar.invalid_filing_date_skipped",
                    accession_number=accession_number,
                    date_str=raw_filing_date,
                )
                continue

            primary_doc = primary_docs[i] if i < len(primary_docs) else ""
            primary_desc = primary_descs[i] if i < len(primary_descs) else ""
            period_str = period_of_reports[i] if i < len(period_of_reports) else ""

            filing_url = self.build_filing_index_url(cik, accession_number)
            document_url = self.build_document_url(cik, accession_number, primary_doc)
            period_end_date = self._parse_date(period_str)

            results.append(
                FilingMetadata(
                    accession_number=accession_number,
                    filing_type=form_type,
                    filing_date=filing_date,
                    cik=cik,
                    ticker=ticker,
                    company_name=company_name,
                    filing_url=filing_url,
                    document_url=document_url,
                    title=primary_desc or None,
                    period_end_date=period_end_date,
                    raw={
                        "accessionNumber": accession_number,
                        "filingDate": raw_filing_date,
                        "form": form_type,
                        "primaryDocument": primary_doc,
                        "primaryDocDescription": primary_desc,
                        "periodOfReport": period_str,
                    },
                )
            )

        return results

    # ── Public discovery interface ─────────────────────────────────────────────

    async def discover_filings(
        self,
        cik: str,
        *,
        include_older: bool = False,
        max_filings: int | None = None,
    ) -> FilingDiscoveryResult:
        """
        Discover filings for a company from SEC EDGAR.

        Fetches the company's submissions JSON and extracts FilingMetadata for
        all supported form types (10-K, 10-Q, 8-K, DEF 14A by default).

        The SEC submissions endpoint returns at most ~1000 recent filings.
        Companies with long filing histories have additional pages listed in
        the 'files' array.  Set include_older=True to also fetch those pages.

        Args:
            cik:           CIK string — accepts numeric or zero-padded form.
                           Normalised internally to 10-digit padded form.
            include_older: If True, also fetch paginated older filing pages
                           from the SEC 'files' array.  Increases request count.
            max_filings:   Optional cap on total FilingMetadata returned.
                           Applied after all pages are fetched.  Useful for
                           testing and initial backfill limits.

        Returns:
            FilingDiscoveryResult with all discovered filings for the company.
            Returns a result with empty filings list if the CIK is not found.

        Raises:
            httpx.HTTPStatusError: For non-retryable HTTP errors.
            httpx.HTTPError:       For network failures after all retries.
        """
        padded_cik = cik.strip().zfill(10)

        data = await self.get_submissions(padded_cik)
        if not data:
            log.warning("sec_edgar.no_data_for_cik", cik=padded_cik)
            return FilingDiscoveryResult(
                cik=padded_cik,
                company_name="",
                ticker=None,
                filings=[],
                total_discovered=0,
                has_additional_pages=False,
            )

        company_name: str = data.get("name", "").strip()
        tickers_list: list[str] = data.get("tickers", [])
        ticker: str | None = tickers_list[0].upper() if tickers_list else None

        filings_section: dict[str, Any] = data.get("filings", {})
        recent: dict[str, Any] = filings_section.get("recent", {})
        older_pages: list[dict[str, Any]] = filings_section.get("files", [])

        filings = self._extract_filings_from_block(recent, padded_cik, ticker, company_name)

        # Fetch paginated older filings if requested.
        if include_older and older_pages:
            for page_info in older_pages:
                page_filename = page_info.get("name", "")
                if not page_filename:
                    continue
                if max_filings is not None and len(filings) >= max_filings:
                    break
                try:
                    page_data = await self._fetch_pagination_page(page_filename)
                    # Pagination pages wrap the recent block differently than submissions.
                    # The page JSON contains the same columnar arrays at top level.
                    page_filings = self._extract_filings_from_block(
                        page_data, padded_cik, ticker, company_name
                    )
                    filings.extend(page_filings)
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "sec_edgar.pagination_page_failed",
                        cik=padded_cik,
                        filename=page_filename,
                        error=str(exc),
                    )

        if max_filings is not None:
            filings = filings[:max_filings]

        log.info(
            "sec_edgar.discovery_complete",
            cik=padded_cik,
            company_name=company_name,
            ticker=ticker,
            filings_found=len(filings),
            has_additional_pages=bool(older_pages),
        )

        return FilingDiscoveryResult(
            cik=padded_cik,
            company_name=company_name,
            ticker=ticker,
            filings=filings,
            total_discovered=len(filings),
            has_additional_pages=bool(older_pages),
        )

    async def get_filing_metadata(
        self, cik: str, accession_number: str
    ) -> FilingMetadata | None:
        """
        Retrieve metadata for a specific filing by its accession number.

        Fetches the company's recent submissions and searches for the matching
        accession number.  Returns None if not found in the recent block.

        Note: this method only searches the recent (~1000) filings block.
        For older filings, use discover_filings(cik, include_older=True) and
        filter the result list.

        Args:
            cik:              CIK — accepts numeric or zero-padded form.
            accession_number: SEC accession number ('XXXXXXXXXX-YY-ZZZZZZ').

        Returns:
            FilingMetadata for the matching filing, or None if not found.
        """
        padded_cik = cik.strip().zfill(10)
        result = await self.discover_filings(padded_cik, include_older=False)
        for filing in result.filings:
            if filing.accession_number == accession_number:
                return filing
        return None
