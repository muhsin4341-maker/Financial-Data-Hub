"""
SEC EDGAR filing document fetcher.

Downloads and parses filing documents (10-K, 10-Q, 8-K, DEF 14A) from the
SEC Archives with SEC-compliant rate limiting, exponential-backoff retry,
optional Redis caching, and content extraction.

Architecture position:

  SECEdgarSource (M3.4)
    ↓  discovers filings and filing URLs
  SECFilingDocumentFetcher (M3.5) ← this module
    ↓  downloads and parses filing documents
  StorageBackend (M3.6)
    ↓  persists raw content to S3 / local disk

Supported content types:
  text/html, application/xhtml+xml → HTML parsing + plain text extraction
  text/plain                       → raw text, no further extraction
  application/xml, text/xml        → XML returned as raw content string

Unsupported (raises UnsupportedContentTypeError):
  application/pdf  — planned for a later milestone

Redis caching:
  Key:    filing_doc:{accession_number}
  TTL:    86 400 seconds (24 hours) by default
  Format: JSON-serialised FilingDocument fields
  Policy: fail-open — on any Redis error the fetcher proceeds without caching

Rate limiting:
  Reuses InProcessRateLimiter / RedisRateLimiter from M3.4 (8 req/s default).

Retry policy (identical to SECEdgarSource._get()):
  HTTP 429, 5xx and connection-level errors → exponential backoff + full jitter.
  Other 4xx → raised immediately (permanent failure).
  Default: 3 retries, 1 s base delay (max ≈ 8.5 s before final failure).

Document URL resolution:
  If FilingMetadata.document_url is already set it is used directly.
  Otherwise the filing index HTML page (filing_url) is fetched and the first
  .htm/.html link is extracted; .txt is used as a last resort.

Milestone: M3.5 — Document Fetcher
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any

import httpx
import structlog

from services.acquisition.source_registry.rate_limiter import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    InProcessRateLimiter,
    RateLimiter,
)
from services.acquisition.source_registry.sources.sec_edgar import FilingMetadata

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT: float = 60.0       # large 10-K filings can be slow
_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BASE_DELAY: float = 1.0
_MAX_JITTER: float = 0.5             # seconds of random jitter on retry delays

_DEFAULT_CACHE_TTL: int = 86_400     # 24 hours

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

_HTML_MIME_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_MIME_TYPES: frozenset[str] = frozenset({"text/plain"})
_XML_MIME_TYPES: frozenset[str] = frozenset({"application/xml", "text/xml"})

_CACHE_KEY_PREFIX: str = "filing_doc:"

# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class DocumentFetchError(Exception):
    """Base exception for document fetch failures."""


class DocumentNotAvailableError(DocumentFetchError):
    """Raised when a document URL cannot be resolved or the document is missing."""


class DocumentParseError(DocumentFetchError):
    """Raised when content parsing fails in an unrecoverable way."""


class UnsupportedContentTypeError(DocumentFetchError):
    """Raised when the server returns a content type we cannot process (e.g. PDF)."""


# ---------------------------------------------------------------------------
# FilingDocument — M3.5 data model
# ---------------------------------------------------------------------------


@dataclass
class FilingDocument:
    """
    A downloaded and parsed SEC EDGAR filing document.

    Returned by SECFilingDocumentFetcher.fetch_document().  Not persisted at
    this layer — persistence occurs in M3.6 via StorageBackend.

    Fields:
      accession_number  SEC accession number ('XXXXXXXXXX-YY-ZZZZZZ').
      filing_type       SEC form type, e.g. '10-K', '10-Q'.
      filing_date       Date the filing was submitted to SEC EDGAR.
      source_url        Actual URL fetched (after any HTTP redirects).
      document_url      Original document_url from FilingMetadata; may equal
                        source_url when there were no redirects.
      mime_type         Detected base MIME type, e.g. 'text/html'.
      content           Raw content decoded as a string.
      content_length    Byte length of the undecoded response body.
      content_hash      SHA-256 hex digest of content (UTF-8 encoded).
      encoding          Character encoding used to decode the response.
      plain_text        Extracted plain text from HTML; None for XML or if
                        extraction is not applicable.
      title             <title> element text for HTML documents; None otherwise.
      fetched_at        UTC datetime when this document was fetched.
      from_cache        True when the result was returned from the Redis cache.
      metadata          Arbitrary key-value store for caller annotations.
    """

    accession_number: str
    filing_type: str
    filing_date: date
    source_url: str
    document_url: str | None
    mime_type: str
    content: str
    content_length: int
    content_hash: str
    encoding: str
    plain_text: str | None
    title: str | None
    fetched_at: datetime
    from_cache: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HTML text extractor
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """
    Minimal HTML → plain-text converter using stdlib html.parser.

    Strips all tags, skips <script>, <style>, <head>, and <noscript> subtrees,
    and joins non-empty text nodes with newlines.  Sufficient for SEC filings
    which are structured HTML with relatively clean markup.
    """

    _SKIP_TAGS: frozenset[str] = frozenset({"script", "style", "head", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._parts)


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------


def _extract_text_from_html(html_content: str) -> str | None:
    """
    Extract plain text from an HTML string using _TextExtractor.

    Returns None if the parser raises (severely malformed HTML).
    """
    try:
        extractor = _TextExtractor()
        extractor.feed(html_content)
        text = extractor.get_text()
        return text or None
    except Exception as exc:  # noqa: BLE001
        log.warning("document_fetcher.html_parse_error", error=str(exc))
        return None


def _extract_title_from_html(html_content: str) -> str | None:
    """Return the <title> element text, or None if absent."""
    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        html_content,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        raw = match.group(1).strip()
        return raw if raw else None
    return None


def _detect_mime_type(response: httpx.Response) -> str:
    """
    Extract the base MIME type from Content-Type, stripping charset params.

    Returns 'application/octet-stream' when the header is absent.
    """
    content_type = response.headers.get("content-type", "")
    base = content_type.split(";")[0].strip().lower()
    return base or "application/octet-stream"


def _detect_encoding(response: httpx.Response) -> str:
    """
    Determine character encoding.

    Priority: charset param in Content-Type → httpx apparent encoding → utf-8.
    """
    content_type = response.headers.get("content-type", "")
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip()
            if charset:
                return charset
    return response.encoding or "utf-8"


def compute_content_hash(content: str) -> str:
    """Return the SHA-256 hex digest of content encoded as UTF-8."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Redis cache helpers
# ---------------------------------------------------------------------------


def _cache_key(accession_number: str) -> str:
    return f"{_CACHE_KEY_PREFIX}{accession_number}"


def _serialize_document(doc: FilingDocument) -> str:
    """Serialise a FilingDocument to a JSON string for Redis storage."""
    return json.dumps(
        {
            "accession_number": doc.accession_number,
            "filing_type": doc.filing_type,
            "filing_date": doc.filing_date.isoformat(),
            "source_url": doc.source_url,
            "document_url": doc.document_url,
            "mime_type": doc.mime_type,
            "content": doc.content,
            "content_length": doc.content_length,
            "content_hash": doc.content_hash,
            "encoding": doc.encoding,
            "plain_text": doc.plain_text,
            "title": doc.title,
            "fetched_at": doc.fetched_at.isoformat(),
            "metadata": doc.metadata,
        }
    )


def _deserialize_document(raw: str | bytes) -> FilingDocument:
    """Deserialise a JSON string (from Redis) into a FilingDocument."""
    text = raw if isinstance(raw, str) else raw.decode("utf-8")
    data: dict[str, Any] = json.loads(text)
    return FilingDocument(
        accession_number=data["accession_number"],
        filing_type=data["filing_type"],
        filing_date=date.fromisoformat(data["filing_date"]),
        source_url=data["source_url"],
        document_url=data.get("document_url"),
        mime_type=data["mime_type"],
        content=data["content"],
        content_length=data["content_length"],
        content_hash=data["content_hash"],
        encoding=data["encoding"],
        plain_text=data.get("plain_text"),
        title=data.get("title"),
        fetched_at=datetime.fromisoformat(data["fetched_at"]),
        from_cache=True,
        metadata=data.get("metadata", {}),
    )


# ---------------------------------------------------------------------------
# SECFilingDocumentFetcher
# ---------------------------------------------------------------------------


class SECFilingDocumentFetcher:
    """
    Downloads and parses SEC EDGAR filing documents.

    Accepts a FilingMetadata produced by SECEdgarSource.discover_filings()
    and returns a FilingDocument with raw content, extracted plain text, and
    content hash.  Content is not persisted here — that is M3.6.

    Basic usage::

        fetcher = SECFilingDocumentFetcher(user_agent="MyApp contact@example.com")
        result  = await sec_source.discover_filings("0000320193")
        ten_k   = next(f for f in result.filings if f.filing_type == "10-K")
        doc     = await fetcher.fetch_document(ten_k)
        print(doc.plain_text[:500])
        await fetcher.close()

    With Redis caching (subsequent calls return from cache)::

        fetcher = SECFilingDocumentFetcher(
            user_agent="...",
            redis_client=redis_client,
            cache_ttl=3600,
        )
        doc1 = await fetcher.fetch_document(filing)  # fetches from SEC
        doc2 = await fetcher.fetch_document(filing)  # from Redis cache
        assert doc2.from_cache is True

    For testing, inject an httpx.AsyncClient with MockTransport::

        client  = httpx.AsyncClient(transport=mock_transport)
        fetcher = SECFilingDocumentFetcher(http_client=client)
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
        timeout: float = _DEFAULT_TIMEOUT,
        redis_client: object | None = None,
        cache_ttl: int = _DEFAULT_CACHE_TTL,
    ) -> None:
        """
        Args:
            user_agent:      SEC-required User-Agent (app name + contact email).
            rate_limiter:    Defaults to InProcessRateLimiter(8 req/s, burst=10).
            circuit_breaker: CircuitBreaker instance (Amendment V1.2 §9.2). Defaults
                             to CircuitBreaker("SEC_EDGAR_FETCHER") — trips open after
                             5 consecutive failures and holds for 30 minutes.
            http_client:     Pre-built httpx.AsyncClient (for testing).
            max_retries:     Max retry attempts for transient failures (default 3).
            base_delay:      Exponential backoff base in seconds (default 1.0).
            timeout:         HTTP request timeout in seconds (default 60).
            redis_client:    Optional async Redis client for document caching.
            cache_ttl:       Redis TTL in seconds (default 86 400 = 24 h).
        """
        # Amendment V1.2 §4.1: resolve User-Agent from settings so that a real
        # contact address is always sent — never fall back to a placeholder domain.
        if user_agent is None:
            from apps.api.core.config import get_settings
            user_agent = get_settings().edgar_user_agent
        self._user_agent = user_agent
        self._rate_limiter = rate_limiter or InProcessRateLimiter(rate=8.0, burst=10)
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            source_name="SEC_EDGAR_FETCHER"
        )
        self._client = http_client
        self._owns_client = http_client is None
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._timeout = timeout
        self._redis = redis_client
        self._cache_ttl = cache_ttl

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Release the HTTP client if owned by this instance."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── HTTP with rate limiting and retry ──────────────────────────────────────

    async def _get(self, url: str) -> httpx.Response:
        """
        Perform a GET request with rate limiting and exponential-backoff retry.

        Retry policy (identical to SECEdgarSource._get()):
          HTTP 429, 500–504: retry; honour Retry-After header on 429.
          Other 4xx:         record failure and raise immediately.
          ConnectError / TimeoutException / RemoteProtocolError: retry.
          After max_retries exhausted: record failure and re-raise.

        Circuit breaker (Amendment V1.2 §9.2):
          check() called before the retry loop — raises CircuitBreakerOpenError
          immediately if open. record_failure() / record_success() called on
          every terminal outcome. CircuitBreakerOpenError is never swallowed.

        Jitter: base_delay × 2^attempt + uniform(0, 0.5 s).

        Raises:
            CircuitBreakerOpenError: Breaker is open — do not retry.
            httpx.HTTPStatusError:   Non-retryable 4xx.
            httpx.HTTPError:         Network failure after all retries exhausted.
        """
        # Amendment V1.2 §9.2: check breaker before any attempt.
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
                        "document_fetcher.request_retrying",
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
                    "document_fetcher.network_error_retrying",
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

    # ── Document URL resolution ────────────────────────────────────────────────

    async def _resolve_document_url(self, filing: FilingMetadata) -> str:
        """
        Determine the primary document URL for a filing.

        If FilingMetadata.document_url is already populated it is returned
        immediately.  Otherwise the filing index HTML page (filing_url) is
        fetched and the first .htm/.html hyperlink is extracted.  A .txt link
        is used as a fallback.

        Raises:
            DocumentNotAvailableError: Index fetch failed or no document found.
        """
        if filing.document_url:
            return filing.document_url

        index_url = filing.filing_url
        log.debug(
            "document_fetcher.resolving_from_index",
            accession_number=filing.accession_number,
            index_url=index_url,
        )

        try:
            resp = await self._get(index_url)
        except Exception as exc:
            raise DocumentNotAvailableError(
                f"Failed to fetch filing index for {filing.accession_number}: {exc}"
            ) from exc

        if resp.status_code == 404:
            raise DocumentNotAvailableError(
                f"Filing index not found (404) for {filing.accession_number}: {index_url}"
            )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DocumentNotAvailableError(
                f"Filing index error ({resp.status_code}) for "
                f"{filing.accession_number}: {exc}"
            ) from exc

        html_content = resp.text
        acc_no_dashes = filing.accession_number.replace("-", "")

        # Collect .htm/.html links — exclude the index page itself.
        htm_links: list[str] = re.findall(
            r'href="([^"]+\.(?:htm|html))"', html_content, re.IGNORECASE
        )
        htm_links = [
            lnk for lnk in htm_links
            if not lnk.lower().endswith("-index.htm")
            and acc_no_dashes.lower() not in lnk.lower()
        ]

        txt_links: list[str] = re.findall(
            r'href="([^"]+\.txt)"', html_content, re.IGNORECASE
        )

        primary_filename: str | None = htm_links[0] if htm_links else (
            txt_links[0] if txt_links else None
        )

        if primary_filename is None:
            raise DocumentNotAvailableError(
                f"No .htm or .txt document found in filing index "
                f"for {filing.accession_number}: {index_url}"
            )

        # Build absolute URL if the extracted href is relative.
        if primary_filename.startswith("http"):
            return primary_filename

        cik_int = str(int(filing.cik)) if filing.cik else ""
        base = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dashes}"
            if cik_int
            else filing.filing_url.rstrip("/")
        )
        return f"{base}/{primary_filename.lstrip('/')}"

    # ── Cache helpers ──────────────────────────────────────────────────────────

    async def _cache_get(self, accession_number: str) -> FilingDocument | None:
        """Read a cached FilingDocument from Redis. Returns None on miss or error."""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(_cache_key(accession_number))  # type: ignore[union-attr]
            if raw is None:
                return None
            doc = _deserialize_document(raw)
            log.debug("document_fetcher.cache_hit", accession_number=accession_number)
            return doc
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "document_fetcher.cache_get_error",
                accession_number=accession_number,
                error=str(exc),
            )
            return None

    async def _cache_set(self, doc: FilingDocument) -> None:
        """Store a FilingDocument in Redis. Fails open on any error."""
        if self._redis is None:
            return
        try:
            serialized = _serialize_document(doc)
            await self._redis.set(  # type: ignore[union-attr]
                _cache_key(doc.accession_number),
                serialized,
                ex=self._cache_ttl,
            )
            log.debug(
                "document_fetcher.cache_set",
                accession_number=doc.accession_number,
                ttl=self._cache_ttl,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "document_fetcher.cache_set_error",
                accession_number=doc.accession_number,
                error=str(exc),
            )

    # ── Content processing ─────────────────────────────────────────────────────

    def _process_response(
        self,
        resp: httpx.Response,
        filing: FilingMetadata,
        source_url: str,
    ) -> FilingDocument:
        """
        Build a FilingDocument from an HTTP response.

        Detects MIME type, decodes bytes to str, extracts plain text for HTML,
        computes SHA-256 hash, and assembles the FilingDocument dataclass.

        Raises:
            UnsupportedContentTypeError: For PDF and other binary formats.
        """
        mime_type = _detect_mime_type(resp)
        encoding = _detect_encoding(resp)

        if mime_type == "application/pdf":
            raise UnsupportedContentTypeError(
                f"Unsupported content type 'application/pdf' for {source_url}. "
                "PDF processing is not supported in M3.5."
            )

        # Decode body — fall back to utf-8 if the stated encoding is unknown.
        try:
            content = resp.content.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            content = resp.content.decode("utf-8", errors="replace")
            encoding = "utf-8"

        content_hash = compute_content_hash(content)
        content_length = len(resp.content)

        plain_text: str | None = None
        title: str | None = None

        if mime_type in _HTML_MIME_TYPES:
            title = _extract_title_from_html(content)
            plain_text = _extract_text_from_html(content)
        elif mime_type in _TEXT_MIME_TYPES:
            plain_text = content

        log.info(
            "document_fetcher.document_processed",
            accession_number=filing.accession_number,
            filing_type=filing.filing_type,
            mime_type=mime_type,
            content_length=content_length,
            has_plain_text=plain_text is not None,
        )

        return FilingDocument(
            accession_number=filing.accession_number,
            filing_type=filing.filing_type,
            filing_date=filing.filing_date,
            source_url=source_url,
            document_url=filing.document_url,
            mime_type=mime_type,
            content=content,
            content_length=content_length,
            content_hash=content_hash,
            encoding=encoding,
            plain_text=plain_text,
            title=title,
            fetched_at=datetime.now(tz=timezone.utc),
        )

    # ── Public interface ───────────────────────────────────────────────────────

    async def fetch_document(self, filing: FilingMetadata) -> FilingDocument:
        """
        Download and parse the primary filing document.

        Workflow:
          1. Check Redis cache — return cached FilingDocument on hit.
          2. Resolve document URL (use filing.document_url or fetch index).
          3. HTTP GET with rate limiting and retry.
          4. Decode, detect MIME type, extract plain text, compute hash.
          5. Store result in Redis cache.
          6. Return FilingDocument.

        Args:
            filing: FilingMetadata from SECEdgarSource.discover_filings().

        Returns:
            FilingDocument with content, plain_text, and metadata.

        Raises:
            DocumentNotAvailableError:   No resolvable document URL or 404.
            UnsupportedContentTypeError: PDF or other unsupported binary format.
            httpx.HTTPStatusError:       Non-retryable HTTP error from SEC.
            httpx.HTTPError:             Network failure after all retries.
        """
        # 1. Cache check.
        cached = await self._cache_get(filing.accession_number)
        if cached is not None:
            return cached

        # 2. Resolve document URL.
        document_url = await self._resolve_document_url(filing)

        # 3. HTTP GET.
        log.info(
            "document_fetcher.fetching",
            accession_number=filing.accession_number,
            filing_type=filing.filing_type,
            url=document_url,
        )
        resp = await self._get(document_url)

        if resp.status_code == 404:
            raise DocumentNotAvailableError(
                f"Document not found (404) for {filing.accession_number}: {document_url}"
            )
        resp.raise_for_status()

        # 4. Process response.
        source_url = str(resp.url)
        doc = self._process_response(resp, filing, source_url)

        # 5. Cache.
        await self._cache_set(doc)

        return doc

    async def fetch_by_url(
        self,
        url: str,
        *,
        accession_number: str,
        filing_type: str,
        filing_date: date,
    ) -> FilingDocument:
        """
        Download a document by explicit URL without a full FilingMetadata.

        Useful when you have a stored filing record with a known document_url
        but do not want to re-discover via SECEdgarSource.

        Args:
            url:              Full URL to the document.
            accession_number: Accession number for cache keying and metadata.
            filing_type:      Filing form type (e.g. '10-K').
            filing_date:      Filing submission date.

        Returns:
            FilingDocument with content and extracted plain text.
        """
        synthetic = FilingMetadata(
            accession_number=accession_number,
            filing_type=filing_type,
            filing_date=filing_date,
            cik="",
            ticker=None,
            company_name="",
            filing_url="",
            document_url=url,
            title=None,
            period_end_date=None,
        )
        return await self.fetch_document(synthetic)
