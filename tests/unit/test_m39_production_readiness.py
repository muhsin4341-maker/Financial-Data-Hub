"""
M3.9 — Production Readiness: Unit validation tests.

Covers:
  VG-15  API Validation — status codes, validation errors, pagination
         boundaries, header presence, and filter semantics for all 7
         acquisition/filing endpoints (no live network or DB required).

  VG-16  Failure Recovery — retry endpoint behaviour, conflict guards,
         fail-open Celery dispatch, and storage backend miss handling.

Test pattern follows ``test_filings_router.py`` and
``test_acquisition_router.py``:
  - Routers imported at module level (already registered with FastAPI).
  - App built once per class via ``_build_*_app()`` helpers.
  - Services/repos patched per-test inside ``with patch(...)`` blocks.
  - ``TestClient(raise_server_exceptions=False)`` for HTTP error path tests.

Milestone: M3.9 — End-to-End Validation
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, NotFoundError, api_error_handler
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_analyst,
    require_authenticated,
)
from apps.api.routers.acquisition import router as acquisition_router
from apps.api.routers.filings import company_filings_router, filings_router
from apps.api.schemas.acquisition_jobs import (
    AcquisitionJobListResponse,
    AcquisitionJobRead,
)
from apps.api.schemas.filings import FilingListResponse, FilingRead

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_TENANT_ID = uuid.uuid4()
_NOW = datetime.now(UTC)
_TODAY = date.today()
_ACCESSION = "0000320193-23-000077"

_AUTH_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="analyst",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)

_FILING_DATA: dict[str, Any] = {
    "id": uuid.uuid4(),
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
# Factories
# ---------------------------------------------------------------------------


def _make_filing_read(**overrides: Any) -> FilingRead:
    return FilingRead.model_validate({**_FILING_DATA, **overrides})


def _make_filing_list_response(
    items: list[FilingRead] | None = None,
    total: int = 0,
    page: int = 1,
    page_size: int = 20,
) -> FilingListResponse:
    items = items or []
    return FilingListResponse(
        items=items,
        total=total or len(items),
        page=page,
        page_size=page_size,
        pages=math.ceil((total or len(items)) / page_size) if page_size else 0,
    )


def _make_job_read(
    status: str = "pending",
    ticker: str = "AAPL",
    filings_discovered: int = 0,
    documents_stored: int = 0,
    **overrides: Any,
) -> AcquisitionJobRead:
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "ticker": ticker,
        "cik": None,
        "company_name": None,
        "job_type": "sec_filing_discovery",
        "status": status,
        "error_message": None,
        "filings_discovered": filings_discovered,
        "filings_new": 0,
        "documents_fetched": 0,
        "documents_stored": documents_stored,
        "started_at": None,
        "completed_at": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        **overrides,
    }
    return AcquisitionJobRead.model_validate(data)


def _make_job_list_response(
    items: list[AcquisitionJobRead] | None = None,
    total: int = 0,
    page: int = 1,
    page_size: int = 20,
) -> AcquisitionJobListResponse:
    items = items or []
    actual_total = total or len(items)
    return AcquisitionJobListResponse(
        items=items,
        total=actual_total,
        page=page,
        page_size=page_size,
        pages=math.ceil(actual_total / page_size) if page_size else 0,
    )


# ---------------------------------------------------------------------------
# App builders (module-level, no patches — patches applied per-test)
# ---------------------------------------------------------------------------


def _build_acquisition_app() -> FastAPI:
    """Minimal app with acquisition router; auth/db overridden."""
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: _AUTH_CTX
    app.dependency_overrides[require_analyst] = lambda: _AUTH_CTX
    app.include_router(acquisition_router)
    return app


def _build_filings_app() -> FastAPI:
    """Minimal app with filings router; auth/db overridden."""
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: _AUTH_CTX
    app.include_router(filings_router)
    return app


def _build_company_filings_app() -> FastAPI:
    """Minimal app with company filings router; auth/db overridden."""
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: _AUTH_CTX
    app.include_router(company_filings_router)
    return app


# Module-level app instances shared within each test class
_ACQ_APP = _build_acquisition_app()
_FILINGS_APP = _build_filings_app()
_COMPANY_APP = _build_company_filings_app()


# ===========================================================================
# VG-15 — API Validation
# ===========================================================================


class TestVG15AcquisitionJobsAPI:
    """VG-15: Acquisition Jobs API — status codes, validation, pagination."""

    def test_vg15_post_jobs_returns_202_with_correct_body(self) -> None:
        """VG-15.1: POST /jobs returns 202 with required schema fields."""
        job = _make_job_read(status="pending")
        mock_svc = MagicMock()
        mock_svc.create_job = AsyncMock(return_value=job)

        with (
            patch(
                "apps.api.routers.acquisition._get_acquisition_service",
                return_value=mock_svc,
            ),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post("/api/v1/acquisition/jobs", json={"ticker": "AAPL"})

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        assert body["ticker"] == "AAPL"
        assert "id" in body
        assert "filings_discovered" in body
        assert "documents_stored" in body

    def test_vg15_post_jobs_empty_ticker_returns_422(self) -> None:
        """VG-15.1 validation: empty ticker must return 422."""
        with patch("apps.api.routers.acquisition._dispatch_celery"):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post("/api/v1/acquisition/jobs", json={"ticker": ""})
        assert resp.status_code == 422

    def test_vg15_post_jobs_missing_ticker_returns_422(self) -> None:
        """VG-15.1 validation: missing ticker field must return 422."""
        client = TestClient(_ACQ_APP, raise_server_exceptions=False)
        resp = client.post("/api/v1/acquisition/jobs", json={})
        assert resp.status_code == 422

    def test_vg15_get_jobs_returns_200_pagination_envelope(self) -> None:
        """VG-15.2: GET /jobs returns paginated envelope with correct fields."""
        # repo.list() returns (items_orm_list, total_int) — NOT AcquisitionJobListResponse
        jobs_orm = [MagicMock() for _ in range(3)]
        for i, j in enumerate(jobs_orm):
            j.id = uuid.uuid4()
            j.ticker = "AAPL"
            j.cik = None
            j.company_name = None
            j.job_type = "sec_filing_discovery"
            j.status = "pending"
            j.error_message = None
            j.filings_discovered = 0
            j.filings_new = 0
            j.documents_fetched = 0
            j.documents_stored = 0
            j.started_at = None
            j.completed_at = None
            j.created_at = _NOW
            j.updated_at = _NOW

        mock_repo = MagicMock()
        mock_repo.list = AsyncMock(return_value=(jobs_orm, 3))

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/acquisition/jobs?page=1&page_size=10")

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "page" in body
        assert "page_size" in body
        assert "pages" in body
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert len(body["items"]) == 3

    def test_vg15_get_jobs_page_size_max_100(self) -> None:
        """VG-15.2: page_size > 100 must return 422."""
        client = TestClient(_ACQ_APP, raise_server_exceptions=False)
        resp = client.get("/api/v1/acquisition/jobs?page_size=101")
        assert resp.status_code == 422

    def test_vg15_get_jobs_page_min_1(self) -> None:
        """VG-15.2: page < 1 must return 422."""
        client = TestClient(_ACQ_APP, raise_server_exceptions=False)
        resp = client.get("/api/v1/acquisition/jobs?page=0")
        assert resp.status_code == 422

    def test_vg15_get_job_by_id_returns_200(self) -> None:
        """VG-15.3: GET /jobs/{id} returns 200 with all expected fields."""
        job = _make_job_read(status="completed", filings_discovered=5, documents_stored=5)
        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=job)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.get(f"/api/v1/acquisition/jobs/{job.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(job.id)
        assert body["status"] == "completed"
        assert body["filings_discovered"] == 5
        assert body["documents_stored"] == 5

    def test_vg15_get_nonexistent_job_returns_404(self) -> None:
        """VG-15.3 error: non-existent job ID returns 404."""
        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(side_effect=NotFoundError("AcquisitionJob"))

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.get(f"/api/v1/acquisition/jobs/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert "NOT_FOUND" in resp.json()["error"]["code"]

    def test_vg15_retry_returns_202(self) -> None:
        """VG-15.4: POST /jobs/{id}/retry on a failed job returns 202."""
        failed_job = MagicMock()
        failed_job.status = "failed"
        failed_job.ticker = "AAPL"

        new_job = _make_job_read(status="pending")
        mock_svc = MagicMock()
        mock_svc.create_job = AsyncMock(return_value=new_job)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=failed_job)

        with (
            patch(
                "apps.api.routers.acquisition._get_acquisition_service",
                return_value=mock_svc,
            ),
            patch(
                "apps.api.routers.acquisition.AcquisitionJobRepository",
                return_value=mock_repo,
            ),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post(f"/api/v1/acquisition/jobs/{uuid.uuid4()}/retry")

        assert resp.status_code == 202
        assert resp.json()["status"] == "pending"

    def test_vg15_pages_computed_correctly(self) -> None:
        """VG-15.2: pages = ceil(total / page_size) — 25 total, page_size=10 → 3."""
        # Return empty list with total=25 to test page math; items irrelevant here
        mock_repo = MagicMock()
        mock_repo.list = AsyncMock(return_value=([], 25))

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/acquisition/jobs?page=2&page_size=10")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 25
        assert body["pages"] == 3
        assert body["page"] == 2
        assert body["page_size"] == 10


class TestVG15FilingsAPI:
    """VG-15: Filing and company filing endpoints — status codes and validation."""

    def test_vg15_list_filings_returns_200(self) -> None:
        """VG-15.5: GET /filings returns 200 with pagination envelope."""
        filing = _make_filing_read()
        list_resp = _make_filing_list_response([filing])
        mock_fs = MagicMock()
        mock_fs.list = AsyncMock(return_value=list_resp)

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/filings")

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "pages" in body

    def test_vg15_get_filing_by_accession_returns_200(self) -> None:
        """VG-15.6: GET /filings/{accession} returns 200 with filing details."""
        filing = _make_filing_read()
        mock_fs = MagicMock()
        mock_fs.get_by_accession_number = AsyncMock(return_value=filing)

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get(f"/api/v1/filings/{_ACCESSION}")

        assert resp.status_code == 200
        body = resp.json()
        assert "accession_number" in body
        assert "filing_type" in body
        assert "cik" in body
        assert "status" in body

    def test_vg15_invalid_accession_returns_422(self) -> None:
        """VG-15.6 validation: malformed accession number returns 422."""
        client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
        for bad in ["NOTVALID", "000032019-23-000077", "0000320193-23-0000"]:
            resp = client.get(f"/api/v1/filings/{bad}")
            assert resp.status_code == 422, (
                f"Expected 422 for {bad!r}, got {resp.status_code}"
            )

    def test_vg15_get_filing_not_found_returns_404(self) -> None:
        """VG-15.6 error: non-existent accession returns 404."""
        mock_fs = MagicMock()
        mock_fs.get_by_accession_number = AsyncMock(
            side_effect=NotFoundError("Filing")
        )

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get(f"/api/v1/filings/{_ACCESSION}")

        assert resp.status_code == 404
        assert "NOT_FOUND" in resp.json()["error"]["code"]

    def test_vg15_get_document_returns_200_with_correct_headers(self) -> None:
        """VG-15.7: GET /filings/{accession}/document returns raw bytes + headers."""
        stored = MagicMock()
        stored.object_key = "some/key.html"
        stored.mime_type = "text/html"
        stored.content_hash = "abc123"

        mock_dr = MagicMock()
        mock_dr.get_by_accession_number = AsyncMock(return_value=stored)

        mock_be = MagicMock()
        mock_be.retrieve = AsyncMock(return_value=b"<html>test</html>")

        with (
            patch(
                "apps.api.routers.filings.StoredDocumentRepository",
                return_value=mock_dr,
            ),
            patch(
                "apps.api.routers.filings._get_storage_backend",
                return_value=mock_be,
            ),
        ):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "X-Content-Hash" in resp.headers
        assert resp.headers["X-Content-Hash"] == "abc123"
        assert "X-Accession-Number" in resp.headers
        assert "Content-Disposition" in resp.headers
        assert len(resp.content) > 0

    def test_vg15_get_document_no_stored_doc_returns_404(self) -> None:
        """VG-15.7 error: no stored document row → 404."""
        mock_dr = MagicMock()
        mock_dr.get_by_accession_number = AsyncMock(return_value=None)

        with patch(
            "apps.api.routers.filings.StoredDocumentRepository",
            return_value=mock_dr,
        ):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert resp.status_code == 404
        assert "NOT_FOUND" in resp.json()["error"]["code"]

    def test_vg15_company_filings_returns_200(self) -> None:
        """VG-15.8: GET /companies/{ticker}/filings returns 200."""
        filing = _make_filing_read()
        list_resp = _make_filing_list_response([filing])
        mock_fs = MagicMock()
        mock_fs.list = AsyncMock(return_value=list_resp)

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_COMPANY_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/companies/AAPL/filings")

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body

    def test_vg15_company_filings_blank_ticker_returns_422(self) -> None:
        """VG-15.8 validation: blank ticker (%20) returns 422."""
        client = TestClient(_COMPANY_APP, raise_server_exceptions=False)
        resp = client.get("/api/v1/companies/%20/filings")  # URL-encoded space
        assert resp.status_code == 422

    def test_vg15_company_filings_type_filter_passed_to_service(self) -> None:
        """VG-15.8: filing_type query param is forwarded to FilingService."""
        mock_fs = MagicMock()
        mock_fs.list = AsyncMock(return_value=_make_filing_list_response())

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_COMPANY_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/companies/AAPL/filings?filing_type=10-K")

        assert resp.status_code == 200
        call_kwargs = mock_fs.list.call_args.kwargs
        assert call_kwargs.get("filing_type") == "10-K"
        assert call_kwargs.get("ticker") == "AAPL"

    def test_vg15_company_filings_ticker_normalised_to_uppercase(self) -> None:
        """VG-15.8: lowercase ticker is normalised to uppercase before service call."""
        mock_fs = MagicMock()
        mock_fs.list = AsyncMock(return_value=_make_filing_list_response())

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_COMPANY_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/companies/aapl/filings")

        assert resp.status_code == 200
        call_kwargs = mock_fs.list.call_args.kwargs
        assert call_kwargs.get("ticker") == "AAPL"

    def test_vg15_filings_page_size_boundary_1(self) -> None:
        """VG-15.5: page_size=1 is valid (minimum boundary)."""
        mock_fs = MagicMock()
        mock_fs.list = AsyncMock(
            return_value=_make_filing_list_response(page_size=1)
        )

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/filings?page_size=1")

        assert resp.status_code == 200

    def test_vg15_filings_page_size_boundary_100(self) -> None:
        """VG-15.5: page_size=100 is valid (maximum boundary)."""
        mock_fs = MagicMock()
        mock_fs.list = AsyncMock(
            return_value=_make_filing_list_response(page_size=100)
        )

        with patch("apps.api.routers.filings.FilingService", return_value=mock_fs):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get("/api/v1/filings?page_size=100")

        assert resp.status_code == 200

    def test_vg15_filings_page_size_over_100_returns_422(self) -> None:
        """VG-15.5: page_size=101 exceeds maximum — must return 422."""
        client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
        resp = client.get("/api/v1/filings?page_size=101")
        assert resp.status_code == 422


# ===========================================================================
# VG-16 — Failure Recovery
# ===========================================================================


class TestVG16RetryWorkflow:
    """VG-16: Retry endpoint guards and new-job creation behaviour."""

    def test_vg16_retry_pending_job_returns_409(self) -> None:
        """VG-16.1: Retrying a pending (non-failed) job returns 409 Conflict."""
        pending_job = MagicMock()
        pending_job.status = "pending"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=pending_job)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post(f"/api/v1/acquisition/jobs/{uuid.uuid4()}/retry")

        assert resp.status_code == 409

    def test_vg16_retry_running_job_returns_409(self) -> None:
        """VG-16.2: Retrying a running job returns 409 Conflict."""
        running_job = MagicMock()
        running_job.status = "running"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=running_job)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post(f"/api/v1/acquisition/jobs/{uuid.uuid4()}/retry")

        assert resp.status_code == 409

    def test_vg16_retry_completed_job_returns_409(self) -> None:
        """VG-16.3: Retrying a completed job returns 409 Conflict."""
        completed_job = MagicMock()
        completed_job.status = "completed"

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=completed_job)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post(f"/api/v1/acquisition/jobs/{uuid.uuid4()}/retry")

        assert resp.status_code == 409

    def test_vg16_retry_creates_new_job_not_resetting_original(self) -> None:
        """VG-16.4: Retry creates a NEW job; original job is not mutated."""
        original_id = uuid.uuid4()
        failed_job = MagicMock()
        failed_job.id = original_id
        failed_job.status = "failed"
        failed_job.ticker = "AAPL"

        new_job = _make_job_read(status="pending")
        assert new_job.id != original_id

        mock_svc = MagicMock()
        mock_svc.create_job = AsyncMock(return_value=new_job)

        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=failed_job)

        with (
            patch(
                "apps.api.routers.acquisition._get_acquisition_service",
                return_value=mock_svc,
            ),
            patch(
                "apps.api.routers.acquisition.AcquisitionJobRepository",
                return_value=mock_repo,
            ),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post(f"/api/v1/acquisition/jobs/{original_id}/retry")

        assert resp.status_code == 202
        body = resp.json()
        assert body["id"] != str(original_id), "Response must be the NEW job ID"
        assert body["status"] == "pending"
        mock_svc.create_job.assert_called_once_with(failed_job.ticker)

    def test_vg16_retry_nonexistent_job_returns_404(self) -> None:
        """VG-16.5: Retry on unknown job ID returns 404."""
        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(
            side_effect=NotFoundError("AcquisitionJob")
        )

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository",
            return_value=mock_repo,
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post(f"/api/v1/acquisition/jobs/{uuid.uuid4()}/retry")

        assert resp.status_code == 404

    def test_vg16_celery_broker_down_returns_202(self) -> None:
        """
        VG-16.6: Celery task.delay() failure does NOT propagate to the HTTP
        response (fail-open behaviour is inside _dispatch_celery).
        """
        job = _make_job_read(status="pending")
        mock_svc = MagicMock()
        mock_svc.create_job = AsyncMock(return_value=job)

        # Simulate broker down by making the Celery task's .delay() raise.
        # _dispatch_celery catches any exception internally — the 202 must
        # still be returned.
        mock_task = MagicMock()
        mock_task.delay.side_effect = RuntimeError("Celery broker unreachable")

        with (
            patch(
                "apps.api.routers.acquisition._get_acquisition_service",
                return_value=mock_svc,
            ),
            patch(
                "workers.tasks.acquisition_tasks.run_acquisition_job",
                mock_task,
            ),
        ):
            client = TestClient(_ACQ_APP, raise_server_exceptions=False)
            resp = client.post("/api/v1/acquisition/jobs", json={"ticker": "AAPL"})

        assert resp.status_code == 202, (
            "Fail-open: Celery broker down must not prevent 202 response"
        )


class TestVG16StorageFailureHandling:
    """VG-16: Storage backend miss handling."""

    def test_vg16_document_backend_miss_returns_404(self) -> None:
        """VG-16.7: Storage backend returning None for a key → 404, not 500."""
        stored = MagicMock()
        stored.object_key = "missing/key.html"
        stored.mime_type = "text/html"
        stored.content_hash = "abc"

        mock_dr = MagicMock()
        mock_dr.get_by_accession_number = AsyncMock(return_value=stored)

        mock_be = MagicMock()
        mock_be.retrieve = AsyncMock(return_value=None)  # backend miss

        with (
            patch(
                "apps.api.routers.filings.StoredDocumentRepository",
                return_value=mock_dr,
            ),
            patch(
                "apps.api.routers.filings._get_storage_backend",
                return_value=mock_be,
            ),
        ):
            client = TestClient(_FILINGS_APP, raise_server_exceptions=False)
            resp = client.get(f"/api/v1/filings/{_ACCESSION}/document")

        assert resp.status_code == 404
        assert "NOT_FOUND" in resp.json()["error"]["code"]
