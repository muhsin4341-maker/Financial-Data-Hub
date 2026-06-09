"""
Unit tests — M3.5 Document Fetcher.

Covers:
  FilingDocument dataclass
  HTML text extraction (_TextExtractor)
  HTML title extraction
  MIME type and encoding detection
  Content hash (SHA-256)
  Cache serialisation / deserialisation
  ContentDeduplicator
  Document URL resolution (with document_url set vs. from index page)
  SECFilingDocumentFetcher.fetch_document (HTML, plain text, 404, PDF, cache)
  SECFilingDocumentFetcher.fetch_by_url
  Retry logic (429 retry, 503 retry, 404 no retry)
  _process_response (HTML, text/plain, XML)

All HTTP calls use httpx.MockTransport — no real network traffic.

Milestone: M3.5 — Document Fetcher
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.acquisition.document_fetcher.deduplicator import ContentDeduplicator
from services.acquisition.document_fetcher.fetcher import (
    DocumentNotAvailableError,
    FilingDocument,
    SECFilingDocumentFetcher,
    UnsupportedContentTypeError,
    _TextExtractor,
    _cache_key,
    _deserialize_document,
    _detect_encoding,
    _detect_mime_type,
    _extract_text_from_html,
    _extract_title_from_html,
    _serialize_document,
    compute_content_hash,
)
from services.acquisition.source_registry.rate_limiter import InProcessRateLimiter
from services.acquisition.source_registry.sources.sec_edgar import FilingMetadata

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Apple Inc. Annual Report 2023</title>
  <style>body { font-family: Arial; }</style>
</head>
<body>
  <h1>Form 10-K</h1>
  <p>Apple Inc. is a technology company.</p>
  <script>console.log("hidden");</script>
  <p>Revenue: $383.3 billion</p>
</body>
</html>"""

_SAMPLE_INDEX_HTML = """<html>
<body>
<table>
<tr><td><a href="aapl-20231230.htm">aapl-20231230.htm</a></td></tr>
<tr><td><a href="0000320193-23-000077-index.htm">Index</a></td></tr>
</table>
</body>
</html>"""

_AAPL_FILING_DATE = date(2024, 2, 2)
_AAPL_ACCESSION = "0000320193-24-000009"


def _make_filing_meta(
    document_url: str | None = "https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/aapl-20231230.htm",
    accession_number: str = _AAPL_ACCESSION,
    filing_type: str = "10-K",
) -> FilingMetadata:
    return FilingMetadata(
        accession_number=accession_number,
        filing_type=filing_type,
        filing_date=_AAPL_FILING_DATE,
        cik="0000320193",
        ticker="AAPL",
        company_name="Apple Inc.",
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/",
        document_url=document_url,
        title="Annual report [10-K]",
        period_end_date=date(2023, 12, 30),
    )


class _NoOpLimiter(InProcessRateLimiter):
    async def acquire(self) -> None:
        pass


def _make_mock_transport(
    routes: dict[str, tuple[int, Any, str | None]],
) -> httpx.MockTransport:
    """
    Build an httpx.MockTransport from a route dict.

    routes format: {url_pattern: (status_code, body, content_type_or_None)}
      body=str   → text response, ct defaults to 'text/html; charset=utf-8'
      body=dict  → JSON response, ct forced to 'application/json'
      body=bytes → binary response, ct defaults to 'application/pdf'
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for pattern, route_data in routes.items():
            if pattern in url:
                status: int = route_data[0]
                body: Any = route_data[1]
                override_ct: str | None = route_data[2] if len(route_data) > 2 else None  # type: ignore[misc]

                if isinstance(body, dict):
                    return httpx.Response(
                        status,
                        json=body,
                        headers={"content-type": "application/json"},
                    )
                elif isinstance(body, bytes):
                    ct = override_ct or "application/pdf"
                    return httpx.Response(status, content=body, headers={"content-type": ct})
                else:
                    ct = override_ct or "text/html; charset=utf-8"
                    return httpx.Response(status, text=str(body), headers={"content-type": ct})

        return httpx.Response(404, text="Not Found", headers={"content-type": "text/plain"})

    return httpx.MockTransport(handler)


def _make_fetcher(
    routes: dict[str, tuple[int, Any, str | None]],
    redis_client: object | None = None,
) -> SECFilingDocumentFetcher:
    transport = _make_mock_transport(routes)
    client = httpx.AsyncClient(transport=transport)
    return SECFilingDocumentFetcher(
        rate_limiter=_NoOpLimiter(),
        http_client=client,
        redis_client=redis_client,
    )


def _make_filing_document(**overrides: Any) -> FilingDocument:
    defaults: dict[str, Any] = {
        "accession_number": _AAPL_ACCESSION,
        "filing_type": "10-K",
        "filing_date": _AAPL_FILING_DATE,
        "source_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/aapl-20231230.htm",
        "document_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/aapl-20231230.htm",
        "mime_type": "text/html",
        "content": _SAMPLE_HTML,
        "content_length": len(_SAMPLE_HTML.encode()),
        "content_hash": compute_content_hash(_SAMPLE_HTML),
        "encoding": "utf-8",
        "plain_text": "Form 10-K\nApple Inc. is a technology company.\nRevenue: $383.3 billion",
        "title": "Apple Inc. Annual Report 2023",
        "fetched_at": datetime(2024, 2, 2, 12, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return FilingDocument(**defaults)


# ---------------------------------------------------------------------------
# TestFilingDocumentDataclass
# ---------------------------------------------------------------------------


class TestFilingDocumentDataclass:
    def test_can_create_filing_document(self) -> None:
        doc = _make_filing_document()
        assert doc.accession_number == _AAPL_ACCESSION
        assert doc.filing_type == "10-K"
        assert doc.content == _SAMPLE_HTML
        assert len(doc.content_hash) == 64  # SHA-256 hex

    def test_from_cache_default_false(self) -> None:
        doc = _make_filing_document()
        assert doc.from_cache is False

    def test_metadata_default_empty_dict(self) -> None:
        doc = _make_filing_document()
        assert doc.metadata == {}


# ---------------------------------------------------------------------------
# TestTextExtractor
# ---------------------------------------------------------------------------


class TestTextExtractor:
    def test_extract_text_basic(self) -> None:
        html = "<html><body><p>Hello world</p></body></html>"
        text = _extract_text_from_html(html)
        assert text is not None
        assert "Hello world" in text

    def test_extract_text_skips_script(self) -> None:
        html = "<html><body><p>Visible</p><script>hidden()</script></body></html>"
        text = _extract_text_from_html(html)
        assert text is not None
        assert "hidden()" not in text
        assert "Visible" in text

    def test_extract_text_skips_style(self) -> None:
        html = "<html><body><style>body{color:red}</style><p>Text</p></body></html>"
        text = _extract_text_from_html(html)
        assert text is not None
        assert "color:red" not in text
        assert "Text" in text

    def test_extract_text_skips_head(self) -> None:
        html = "<html><head><title>Hidden Title</title></head><body><p>Body</p></body></html>"
        extractor = _TextExtractor()
        extractor.feed(html)
        text = extractor.get_text()
        assert "Body" in text
        assert "Hidden Title" not in text

    def test_extract_text_multiline(self) -> None:
        html = "<html><body><p>Line one</p><p>Line two</p></body></html>"
        text = _extract_text_from_html(html)
        assert text is not None
        assert "Line one" in text
        assert "Line two" in text

    def test_extract_text_empty_html_returns_none(self) -> None:
        text = _extract_text_from_html("<html><body></body></html>")
        assert text is None

    def test_extract_text_full_sample(self) -> None:
        text = _extract_text_from_html(_SAMPLE_HTML)
        assert text is not None
        assert "Form 10-K" in text
        assert "Apple Inc." in text
        assert "383.3 billion" in text
        assert "hidden" not in text
        assert "font-family" not in text


# ---------------------------------------------------------------------------
# TestTitleExtraction
# ---------------------------------------------------------------------------


class TestTitleExtraction:
    def test_extract_title_present(self) -> None:
        html = "<html><head><title>Annual Report 2023</title></head></html>"
        assert _extract_title_from_html(html) == "Annual Report 2023"

    def test_extract_title_absent(self) -> None:
        html = "<html><body><p>No title</p></body></html>"
        assert _extract_title_from_html(html) is None

    def test_extract_title_multiline(self) -> None:
        html = "<html><head><title>\n  Apple Inc.\n  Annual Report\n</title></head></html>"
        title = _extract_title_from_html(html)
        assert title is not None
        assert "Apple" in title

    def test_extract_title_case_insensitive(self) -> None:
        html = "<html><head><TITLE>Uppercase Title</TITLE></head></html>"
        assert _extract_title_from_html(html) == "Uppercase Title"


# ---------------------------------------------------------------------------
# TestMimeTypeDetection
# ---------------------------------------------------------------------------


class TestMimeTypeDetection:
    def _make_resp(self, content_type: str) -> httpx.Response:
        return httpx.Response(200, text="body", headers={"content-type": content_type})

    def test_detect_html(self) -> None:
        resp = self._make_resp("text/html; charset=utf-8")
        assert _detect_mime_type(resp) == "text/html"

    def test_detect_text_plain(self) -> None:
        resp = self._make_resp("text/plain")
        assert _detect_mime_type(resp) == "text/plain"

    def test_detect_strips_charset_params(self) -> None:
        resp = self._make_resp("application/xml; charset=iso-8859-1")
        assert _detect_mime_type(resp) == "application/xml"

    def test_detect_missing_header_returns_fallback(self) -> None:
        resp = httpx.Response(200, content=b"data")
        assert _detect_mime_type(resp) == "application/octet-stream"


# ---------------------------------------------------------------------------
# TestEncodingDetection
# ---------------------------------------------------------------------------


class TestEncodingDetection:
    def test_detect_encoding_from_charset_param(self) -> None:
        resp = httpx.Response(
            200, text="hello", headers={"content-type": "text/html; charset=iso-8859-1"}
        )
        assert _detect_encoding(resp) == "iso-8859-1"

    def test_detect_encoding_falls_back_utf8(self) -> None:
        resp = httpx.Response(200, text="hello", headers={"content-type": "text/html"})
        encoding = _detect_encoding(resp)
        assert encoding  # some encoding should be returned

    def test_detect_encoding_mixed_case_charset(self) -> None:
        resp = httpx.Response(
            200, text="x", headers={"content-type": "text/html; Charset=UTF-8"}
        )
        assert _detect_encoding(resp) == "UTF-8"


# ---------------------------------------------------------------------------
# TestContentHash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_hash_is_deterministic(self) -> None:
        content = "Apple Inc. 10-K filing"
        assert compute_content_hash(content) == compute_content_hash(content)

    def test_different_content_different_hash(self) -> None:
        assert compute_content_hash("aaa") != compute_content_hash("bbb")

    def test_hash_is_64_chars(self) -> None:
        assert len(compute_content_hash("test")) == 64

    def test_empty_string_has_known_hash(self) -> None:
        # SHA-256 of empty string is well-known
        import hashlib
        expected = hashlib.sha256(b"").hexdigest()
        assert compute_content_hash("") == expected


# ---------------------------------------------------------------------------
# TestCacheSerialization
# ---------------------------------------------------------------------------


class TestCacheSerialization:
    def test_round_trip(self) -> None:
        doc = _make_filing_document()
        serialized = _serialize_document(doc)
        restored = _deserialize_document(serialized)

        assert restored.accession_number == doc.accession_number
        assert restored.filing_type == doc.filing_type
        assert restored.filing_date == doc.filing_date
        assert restored.content == doc.content
        assert restored.content_hash == doc.content_hash
        assert restored.plain_text == doc.plain_text
        assert restored.title == doc.title

    def test_deserialized_sets_from_cache_true(self) -> None:
        doc = _make_filing_document()
        restored = _deserialize_document(_serialize_document(doc))
        assert restored.from_cache is True

    def test_none_optional_fields_survive_round_trip(self) -> None:
        doc = _make_filing_document(plain_text=None, title=None, document_url=None)
        restored = _deserialize_document(_serialize_document(doc))
        assert restored.plain_text is None
        assert restored.title is None
        assert restored.document_url is None

    def test_cache_key_format(self) -> None:
        key = _cache_key("0000320193-24-000009")
        assert key == "filing_doc:0000320193-24-000009"


# ---------------------------------------------------------------------------
# TestContentDeduplicator
# ---------------------------------------------------------------------------


class TestContentDeduplicator:
    def test_same_content_same_hash(self) -> None:
        h1 = ContentDeduplicator.compute_hash("content")
        h2 = ContentDeduplicator.compute_hash("content")
        assert h1 == h2

    def test_different_content_different_hash(self) -> None:
        assert ContentDeduplicator.compute_hash("a") != ContentDeduplicator.compute_hash("b")

    def test_compute_hash_bytes(self) -> None:
        raw = b"hello bytes"
        h = ContentDeduplicator.compute_hash_bytes(raw)
        assert len(h) == 64

    def test_is_duplicate_true(self) -> None:
        h = ContentDeduplicator.compute_hash("same")
        assert ContentDeduplicator.is_duplicate(h, h) is True

    def test_is_duplicate_false(self) -> None:
        assert (
            ContentDeduplicator.is_duplicate(
                ContentDeduplicator.compute_hash("x"),
                ContentDeduplicator.compute_hash("y"),
            )
            is False
        )


# ---------------------------------------------------------------------------
# TestDocumentURLResolution
# ---------------------------------------------------------------------------


class TestDocumentURLResolution:
    @pytest.mark.anyio
    async def test_returns_document_url_when_set(self) -> None:
        fetcher = _make_fetcher({})
        filing = _make_filing_meta(document_url="https://example.com/doc.htm")
        url = await fetcher._resolve_document_url(filing)
        assert url == "https://example.com/doc.htm"

    @pytest.mark.anyio
    async def test_resolves_from_index_htm_link(self) -> None:
        index_html = '<html><body><a href="aapl-20231230.htm">filing</a></body></html>'
        fetcher = _make_fetcher(
            {"Archives/edgar/data/320193/000032019324000009/": (200, index_html, None)}
        )
        filing = _make_filing_meta(document_url=None)
        url = await fetcher._resolve_document_url(filing)
        assert "aapl-20231230.htm" in url
        assert "320193" in url

    @pytest.mark.anyio
    async def test_resolves_from_txt_when_no_htm(self) -> None:
        index_html = '<html><body><a href="filing-full.txt">full submission</a></body></html>'
        fetcher = _make_fetcher(
            {"Archives/edgar/data/320193/000032019324000009/": (200, index_html, None)}
        )
        filing = _make_filing_meta(document_url=None)
        url = await fetcher._resolve_document_url(filing)
        assert "filing-full.txt" in url

    @pytest.mark.anyio
    async def test_raises_when_index_404(self) -> None:
        fetcher = _make_fetcher(
            {"Archives/edgar/data/320193/000032019324000009/": (404, "Not Found", None)}
        )
        filing = _make_filing_meta(document_url=None)
        with pytest.raises(DocumentNotAvailableError):
            await fetcher._resolve_document_url(filing)

    @pytest.mark.anyio
    async def test_raises_when_no_links_in_index(self) -> None:
        index_html = "<html><body><p>No documents here</p></body></html>"
        fetcher = _make_fetcher(
            {"Archives/edgar/data/320193/000032019324000009/": (200, index_html, None)}
        )
        filing = _make_filing_meta(document_url=None)
        with pytest.raises(DocumentNotAvailableError):
            await fetcher._resolve_document_url(filing)


# ---------------------------------------------------------------------------
# TestFetchDocument
# ---------------------------------------------------------------------------


class TestFetchDocument:
    @pytest.mark.anyio
    async def test_fetch_html_document_success(self) -> None:
        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, _SAMPLE_HTML, "text/html; charset=utf-8")}
        )
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)

        assert doc.accession_number == _AAPL_ACCESSION
        assert doc.filing_type == "10-K"
        assert doc.mime_type == "text/html"
        assert "10-K" in doc.content
        assert doc.plain_text is not None
        assert "Apple Inc." in doc.plain_text
        assert doc.title == "Apple Inc. Annual Report 2023"
        assert doc.from_cache is False

    @pytest.mark.anyio
    async def test_fetch_plain_text_document(self) -> None:
        plain = "This is a plain text 10-K filing.\nRevenue: 383 billion."
        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, plain, "text/plain; charset=utf-8")}
        )
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)

        assert doc.mime_type == "text/plain"
        assert doc.content == plain
        assert doc.plain_text == plain
        assert doc.title is None

    @pytest.mark.anyio
    async def test_fetch_404_raises_not_available(self) -> None:
        fetcher = _make_fetcher({"aapl-20231230.htm": (404, "Not Found", "text/plain")})
        filing = _make_filing_meta()
        with pytest.raises(DocumentNotAvailableError):
            await fetcher.fetch_document(filing)

    @pytest.mark.anyio
    async def test_fetch_pdf_raises_unsupported(self) -> None:
        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, b"%PDF-1.4 binary content", "application/pdf")}
        )
        filing = _make_filing_meta()
        with pytest.raises(UnsupportedContentTypeError):
            await fetcher.fetch_document(filing)

    @pytest.mark.anyio
    async def test_fetch_uses_cache_hit(self) -> None:
        cached_doc = _make_filing_document(from_cache=True)
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=_serialize_document(cached_doc))

        # No HTTP routes — should not be called
        fetcher = _make_fetcher({}, redis_client=mock_redis)
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)

        assert doc.from_cache is True
        assert doc.accession_number == _AAPL_ACCESSION
        mock_redis.get.assert_called_once_with(f"filing_doc:{_AAPL_ACCESSION}")

    @pytest.mark.anyio
    async def test_fetch_stores_in_cache_on_miss(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)   # cache miss
        mock_redis.set = AsyncMock()

        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, _SAMPLE_HTML, "text/html; charset=utf-8")},
            redis_client=mock_redis,
        )
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)

        assert doc.from_cache is False
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert _AAPL_ACCESSION in call_args[0][0]  # cache key contains accession number

    @pytest.mark.anyio
    async def test_cache_error_fails_open(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))

        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, _SAMPLE_HTML, "text/html; charset=utf-8")},
            redis_client=mock_redis,
        )
        filing = _make_filing_meta()
        # Should not raise — cache errors are swallowed
        doc = await fetcher.fetch_document(filing)
        assert doc.accession_number == _AAPL_ACCESSION

    @pytest.mark.anyio
    async def test_content_hash_computed(self) -> None:
        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, _SAMPLE_HTML, "text/html; charset=utf-8")}
        )
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)
        assert len(doc.content_hash) == 64
        assert doc.content_hash == compute_content_hash(doc.content)

    @pytest.mark.anyio
    async def test_filing_date_preserved(self) -> None:
        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, _SAMPLE_HTML, "text/html; charset=utf-8")}
        )
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)
        assert doc.filing_date == _AAPL_FILING_DATE


# ---------------------------------------------------------------------------
# TestFetchByUrl
# ---------------------------------------------------------------------------


class TestFetchByUrl:
    @pytest.mark.anyio
    async def test_fetch_by_url_html(self) -> None:
        fetcher = _make_fetcher(
            {"aapl-20231230.htm": (200, _SAMPLE_HTML, "text/html; charset=utf-8")}
        )
        doc = await fetcher.fetch_by_url(
            "https://www.sec.gov/Archives/edgar/data/320193/000032019324000009/aapl-20231230.htm",
            accession_number=_AAPL_ACCESSION,
            filing_type="10-K",
            filing_date=_AAPL_FILING_DATE,
        )
        assert doc.accession_number == _AAPL_ACCESSION
        assert doc.filing_type == "10-K"
        assert doc.plain_text is not None

    @pytest.mark.anyio
    async def test_fetch_by_url_uses_cache(self) -> None:
        doc_fixture = _make_filing_document()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=_serialize_document(doc_fixture))

        fetcher = _make_fetcher({}, redis_client=mock_redis)
        doc = await fetcher.fetch_by_url(
            "https://example.com/doc.htm",
            accession_number=_AAPL_ACCESSION,
            filing_type="10-K",
            filing_date=_AAPL_FILING_DATE,
        )
        assert doc.from_cache is True


# ---------------------------------------------------------------------------
# TestRetryLogic
# ---------------------------------------------------------------------------


class TestRetryLogic:
    @pytest.mark.anyio
    async def test_retry_on_429_then_success(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    429,
                    text="Rate limited",
                    headers={"content-type": "text/plain", "Retry-After": "0"},
                )
            return httpx.Response(
                200, text=_SAMPLE_HTML, headers={"content-type": "text/html; charset=utf-8"}
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        fetcher = SECFilingDocumentFetcher(
            rate_limiter=_NoOpLimiter(),
            http_client=client,
            base_delay=0.0,   # no delay in tests
        )
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)
        assert doc.accession_number == _AAPL_ACCESSION
        assert call_count == 2

    @pytest.mark.anyio
    async def test_retry_on_503_then_success(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(503, text="Unavailable", headers={"content-type": "text/plain"})
            return httpx.Response(
                200, text=_SAMPLE_HTML, headers={"content-type": "text/html; charset=utf-8"}
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        fetcher = SECFilingDocumentFetcher(
            rate_limiter=_NoOpLimiter(), http_client=client, base_delay=0.0
        )
        filing = _make_filing_meta()
        doc = await fetcher.fetch_document(filing)
        assert call_count == 2
        assert doc.accession_number == _AAPL_ACCESSION

    @pytest.mark.anyio
    async def test_no_retry_on_403(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(403, text="Forbidden", headers={"content-type": "text/plain"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        fetcher = SECFilingDocumentFetcher(
            rate_limiter=_NoOpLimiter(), http_client=client, base_delay=0.0
        )
        filing = _make_filing_meta()
        with pytest.raises(httpx.HTTPStatusError):
            await fetcher.fetch_document(filing)
        assert call_count == 1  # not retried

    @pytest.mark.anyio
    async def test_max_retries_exhausted_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Unavailable", headers={"content-type": "text/plain"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        fetcher = SECFilingDocumentFetcher(
            rate_limiter=_NoOpLimiter(),
            http_client=client,
            max_retries=2,
            base_delay=0.0,
        )
        filing = _make_filing_meta()
        with pytest.raises(httpx.HTTPStatusError):
            await fetcher.fetch_document(filing)


# ---------------------------------------------------------------------------
# TestProcessResponse
# ---------------------------------------------------------------------------


class TestProcessResponse:
    def _make_resp(self, text: str, content_type: str) -> httpx.Response:
        return httpx.Response(200, text=text, headers={"content-type": content_type})

    def test_process_html_extracts_text_and_title(self) -> None:
        fetcher = SECFilingDocumentFetcher(rate_limiter=_NoOpLimiter())
        resp = self._make_resp(_SAMPLE_HTML, "text/html; charset=utf-8")
        filing = _make_filing_meta()
        doc = fetcher._process_response(resp, filing, "https://example.com/doc.htm")

        assert doc.mime_type == "text/html"
        assert doc.title == "Apple Inc. Annual Report 2023"
        assert doc.plain_text is not None
        assert "Apple Inc." in doc.plain_text

    def test_process_text_plain_sets_plain_text(self) -> None:
        fetcher = SECFilingDocumentFetcher(rate_limiter=_NoOpLimiter())
        resp = self._make_resp("Plain text content", "text/plain")
        filing = _make_filing_meta()
        doc = fetcher._process_response(resp, filing, "https://example.com/doc.txt")

        assert doc.mime_type == "text/plain"
        assert doc.plain_text == "Plain text content"
        assert doc.title is None

    def test_process_xml_no_text_extraction(self) -> None:
        fetcher = SECFilingDocumentFetcher(rate_limiter=_NoOpLimiter())
        xml = '<?xml version="1.0"?><root><item>data</item></root>'
        resp = self._make_resp(xml, "application/xml")
        filing = _make_filing_meta()
        doc = fetcher._process_response(resp, filing, "https://example.com/doc.xml")

        assert doc.mime_type == "application/xml"
        assert doc.plain_text is None  # XML not extracted
        assert "data" in doc.content

    def test_process_pdf_raises_unsupported(self) -> None:
        fetcher = SECFilingDocumentFetcher(rate_limiter=_NoOpLimiter())
        resp = httpx.Response(
            200,
            content=b"%PDF-1.4 content",
            headers={"content-type": "application/pdf"},
        )
        filing = _make_filing_meta()
        with pytest.raises(UnsupportedContentTypeError):
            fetcher._process_response(resp, filing, "https://example.com/doc.pdf")

    def test_process_sets_content_length(self) -> None:
        fetcher = SECFilingDocumentFetcher(rate_limiter=_NoOpLimiter())
        resp = self._make_resp("hello", "text/html; charset=utf-8")
        filing = _make_filing_meta()
        doc = fetcher._process_response(resp, filing, "https://example.com/")
        assert doc.content_length > 0

    def test_process_fetched_at_is_utc(self) -> None:
        fetcher = SECFilingDocumentFetcher(rate_limiter=_NoOpLimiter())
        resp = self._make_resp("content", "text/html")
        filing = _make_filing_meta()
        doc = fetcher._process_response(resp, filing, "https://example.com/")
        assert doc.fetched_at.tzinfo is not None
