"""
Unit tests — /api/v1/filings and /api/v1/companies/{ticker}/filings routers.

Strategy
--------
A minimal FastAPI application is built per-test class with:
  - The filings_router and/or company_filings_router
  - The APIError exception handler
  - The get_db dependency overridden with an AsyncMock session
  - Auth dependencies overridden to inject a fixed AuthRequestContext
  - FilingService, StoredDocumentRepository, and storage backend patched

What is mocked
--------------
- ``FilingService``                   — list, get_by_accession_number methods
- ``StoredDocumentRepository``        — get_by_accession_number method
- Storage backend                     — retrieve method
- ``_get_storage_backend``            — returns mock backend
- Auth dependencies                   — inject fixed AuthRequestContext
- ``get_db``                          — yields AsyncMock session

What is NOT mocked (real code runs)
------------------------------------
- All Pydantic schema validation
- Accession number format validation (_validate_accession)
- NotFoundError / ValidationError exception handling
- HTTP status codes and response body structure
- Content-Disposition header construction

Milestone: M3.8 — Acquisition APIs
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, NotFoundError, api_error_handler
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_authenticated,
)
from apps.api.routers.filings import company_filings_router, filings_router
from apps.api.schemas.filings import FilingListResponse, FilingRead

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_TENANT_ID = uuid.uuid4()
_FILING_ID = uuid.uuid4()
_NOW = datetime.now(UTC)
_TODAY = date.today()
_ACCESSION = "0000320193-23-000077"

_AUTH_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="viewer",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)

_FILING_DATA: dict[str, Any] = {
    "id": _FILING_ID,
    "company_id": None,
    "source_config_id": None,
    "filing_type": "10-K",
    "accession_number": _ACCESSION,
    "filing_date": _TODAY,
    "period_end_date": None,
    "cik": "0000320193",
    "ticker": "AAPL",
    "title": "Annual report [10-K]",
    "filing_url": "https://www.sec.gov/cgi-bin/browse-edgar",
    "document_url": "https://www.sec.gov/Archives/edgar/.../aapl-20230930.htm",
    "status": "downloaded",
    "filing_metadata": None,
    "created_at": _NOW,
    "updated_at": _NOW,
}


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------


def _build_filings_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: _AUTH_CTX
    app.include_router(filings_router)
    return app


def _build_company_filings_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: _AUTH_CTX
    app.include_router(company_filings_router)
    return app


def _make_filing_read(**overrides: Any) -> FilingRead:
    data = {**_FILING_DATA, **overrides}
    return FilingRead.model_validate(data)


def _make_list_response(
    items: list[FilingRead] | None = None,
    total: int = 0,
    page: int = 1,
    page_size: int = 20,
) -> FilingListResponse:
    return FilingListResponse(
        items=items or [],
        total=total,
        page=page,
        page_size=page_size,
    )


# ===========================================================================
# GET /api/v1/filings
# ===========================================================================


class TestListFilings:
    @pytest.mark.anyio
    async def test_returns_200_with_pagination_envelope(self) -> None:
        app = _build_filings_app()
        filing = _make_filing_read()
        mock_response = _make_list_response([filing], total=1)
        mock_service = AsyncMock()
        mock_service.list.return_value = mock_response

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/filings")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert "pages" in body

    @pytest.mark.anyio
    async def test_pagination_defaults_forwarded(self) -> None:
        app = _build_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response()

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/filings")

        mock_service.list.assert_called_once_with(
            page=1,
            page_size=20,
            filing_type=None,
            status=None,
            cik=None,
            ticker=None,
        )

    @pytest.mark.anyio
    async def test_filing_type_filter_forwarded(self) -> None:
        app = _build_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response()

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/filings?filing_type=10-K")

        call_kwargs = mock_service.list.call_args.kwargs
        assert call_kwargs["filing_type"] == "10-K"

    @pytest.mark.anyio
    async def test_ticker_filter_forwarded(self) -> None:
        app = _build_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response()

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/filings?ticker=AAPL")

        call_kwargs = mock_service.list.call_args.kwargs
        assert call_kwargs["ticker"] == "AAPL"

    @pytest.mark.anyio
    async def test_page_size_101_returns_422(self) -> None:
        app = _build_filings_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/filings?page_size=101")
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_page_0_returns_422(self) -> None:
        app = _build_filings_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/filings?page=0")
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_empty_result_returns_correct_envelope(self) -> None:
        app = _build_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response()

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/filings")

        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []


# ===========================================================================
# GET /api/v1/filings/{accession_number}
# ===========================================================================


class TestGetFilingByAccession:
    @pytest.mark.anyio
    async def test_returns_200_when_found(self) -> None:
        app = _build_filings_app()
        filing = _make_filing_read()
        mock_service = AsyncMock()
        mock_service.get_by_accession_number.return_value = filing

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["accession_number"] == _ACCESSION
        assert body["filing_type"] == "10-K"
        assert body["ticker"] == "AAPL"

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_filings_app()
        mock_service = AsyncMock()
        mock_service.get_by_accession_number.side_effect = NotFoundError(
            "Filing", f"accession_number={_ACCESSION}"
        )

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "FILING_NOT_FOUND"

    @pytest.mark.anyio
    async def test_invalid_accession_format_returns_422(self) -> None:
        app = _build_filings_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/filings/INVALID-ACCESSION")
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_short_accession_returns_422(self) -> None:
        app = _build_filings_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/filings/0000320193-23-77")
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_response_contains_all_filing_fields(self) -> None:
        app = _build_filings_app()
        filing = _make_filing_read(cik="0000320193", status="downloaded")
        mock_service = AsyncMock()
        mock_service.get_by_accession_number.return_value = filing

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}")

        body = resp.json()
        for field in ["id", "filing_type", "accession_number", "filing_date", "cik",
                      "status", "created_at", "updated_at"]:
            assert field in body, f"Missing field: {field}"


# ===========================================================================
# GET /api/v1/filings/{accession_number}/document
# ===========================================================================


class TestGetFilingDocument:
    def _make_stored_doc_orm(self, accession: str = _ACCESSION) -> MagicMock:
        s = MagicMock()
        s.id = uuid.uuid4()
        s.accession_number = accession
        s.object_key = f"filings/{accession.replace('-', '')}/document.html"
        s.content_hash = "a" * 64
        s.content_length = 1000
        s.mime_type = "text/html"
        s.storage_type = "local"
        return s

    @pytest.mark.anyio
    async def test_returns_200_with_content(self) -> None:
        app = _build_filings_app()
        stored = self._make_stored_doc_orm()
        content = "<html><body>10-K Filing</body></html>"
        mock_backend = AsyncMock()
        mock_backend.retrieve.return_value = content
        mock_repo = AsyncMock()
        mock_repo.get_by_accession_number.return_value = stored

        with (
            patch("apps.api.routers.filings.StoredDocumentRepository", return_value=mock_repo),
            patch("apps.api.routers.filings._get_storage_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert resp.status_code == 200
        assert resp.text == content

    @pytest.mark.anyio
    async def test_content_type_from_stored_mime(self) -> None:
        app = _build_filings_app()
        stored = self._make_stored_doc_orm()
        stored.mime_type = "text/html"
        mock_backend = AsyncMock()
        mock_backend.retrieve.return_value = "<html/>"
        mock_repo = AsyncMock()
        mock_repo.get_by_accession_number.return_value = stored

        with (
            patch("apps.api.routers.filings.StoredDocumentRepository", return_value=mock_repo),
            patch("apps.api.routers.filings._get_storage_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.anyio
    async def test_content_disposition_header_present(self) -> None:
        app = _build_filings_app()
        stored = self._make_stored_doc_orm()
        mock_backend = AsyncMock()
        mock_backend.retrieve.return_value = "<html/>"
        mock_repo = AsyncMock()
        mock_repo.get_by_accession_number.return_value = stored

        with (
            patch("apps.api.routers.filings.StoredDocumentRepository", return_value=mock_repo),
            patch("apps.api.routers.filings._get_storage_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert "content-disposition" in resp.headers

    @pytest.mark.anyio
    async def test_returns_404_when_no_stored_document(self) -> None:
        app = _build_filings_app()
        mock_repo = AsyncMock()
        mock_repo.get_by_accession_number.return_value = None

        with (
            patch("apps.api.routers.filings.StoredDocumentRepository", return_value=mock_repo),
            patch("apps.api.routers.filings._get_storage_backend", return_value=AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert resp.status_code == 404
        # NotFoundError("StoredDocument") → code = "STOREDDOCUMENT_NOT_FOUND"
        assert "NOT_FOUND" in resp.json()["error"]["code"]

    @pytest.mark.anyio
    async def test_returns_404_when_backend_miss(self) -> None:
        app = _build_filings_app()
        stored = self._make_stored_doc_orm()
        mock_backend = AsyncMock()
        mock_backend.retrieve.return_value = None
        mock_repo = AsyncMock()
        mock_repo.get_by_accession_number.return_value = stored

        with (
            patch("apps.api.routers.filings.StoredDocumentRepository", return_value=mock_repo),
            patch("apps.api.routers.filings._get_storage_backend", return_value=mock_backend),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_invalid_accession_format_returns_422(self) -> None:
        app = _build_filings_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/filings/GARBAGE/document")
        assert resp.status_code == 422


# ===========================================================================
# GET /api/v1/companies/{ticker}/filings
# ===========================================================================


class TestListCompanyFilings:
    @pytest.mark.anyio
    async def test_returns_200_with_filings(self) -> None:
        app = _build_company_filings_app()
        filing = _make_filing_read(ticker="AAPL")
        mock_response = _make_list_response([filing], total=1)
        mock_service = AsyncMock()
        mock_service.list.return_value = mock_response

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/companies/AAPL/filings")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    @pytest.mark.anyio
    async def test_ticker_normalised_to_uppercase(self) -> None:
        app = _build_company_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response()

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/companies/aapl/filings")

        call_kwargs = mock_service.list.call_args.kwargs
        assert call_kwargs["ticker"] == "AAPL"

    @pytest.mark.anyio
    async def test_filing_type_filter_forwarded(self) -> None:
        app = _build_company_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response()

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/companies/AAPL/filings?filing_type=10-K")

        call_kwargs = mock_service.list.call_args.kwargs
        assert call_kwargs["filing_type"] == "10-K"

    @pytest.mark.anyio
    async def test_pagination_forwarded(self) -> None:
        app = _build_company_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response()

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/companies/AAPL/filings?page=3&page_size=10")

        call_kwargs = mock_service.list.call_args.kwargs
        assert call_kwargs["page"] == 3
        assert call_kwargs["page_size"] == 10

    @pytest.mark.anyio
    async def test_no_filings_returns_empty_list_not_404(self) -> None:
        app = _build_company_filings_app()
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response(total=0)

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/companies/UNKNOWN/filings")

        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []

    @pytest.mark.anyio
    async def test_page_size_above_100_returns_422(self) -> None:
        app = _build_company_filings_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/companies/AAPL/filings?page_size=200")
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_items_contain_all_required_fields(self) -> None:
        app = _build_company_filings_app()
        filing = _make_filing_read(ticker="AAPL")
        mock_service = AsyncMock()
        mock_service.list.return_value = _make_list_response([filing], total=1)

        with patch("apps.api.routers.filings.FilingService", return_value=mock_service):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/companies/AAPL/filings")

        item = resp.json()["items"][0]
        for field in ["id", "accession_number", "filing_type", "filing_date",
                      "cik", "ticker", "status"]:
            assert field in item, f"Missing field: {field}"
