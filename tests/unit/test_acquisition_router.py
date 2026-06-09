"""
Unit tests — /api/v1/acquisition router.

Strategy
--------
A minimal FastAPI application is built per-test class with:
  - The acquisition router
  - The APIError exception handler
  - The get_db dependency overridden with an AsyncMock session
  - Auth dependencies overridden to inject a fixed AuthRequestContext
  - AcquisitionJobService and AcquisitionJobRepository patched at the
    router module import path

What is mocked
--------------
- ``AcquisitionJobService``           — create_job method
- ``AcquisitionJobRepository``        — get_by_id, list methods
- ``_get_acquisition_service``        — returns the mock service
- ``_dispatch_celery``                — no-op (prevents real Celery calls)
- Auth dependencies                   — inject fixed AuthRequestContext
- ``get_db``                          — yields AsyncMock session

What is NOT mocked (real code runs)
------------------------------------
- All Pydantic schema validation (AcquisitionJobCreate, AcquisitionJobRead)
- NotFoundError / ConflictError exception handling and response serialisation
- HTTP status codes, response body structure
- Pagination envelope construction (AcquisitionJobListResponse)

Milestone: M3.8 — Acquisition APIs
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_analyst,
    require_authenticated,
)
from apps.api.routers.acquisition import router
from apps.api.schemas.acquisition_jobs import AcquisitionJobRead

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_USER_ID = uuid.uuid4()
_TENANT_ID = uuid.uuid4()
_JOB_ID = uuid.uuid4()
_NOW = datetime.now(UTC)

_ANALYST_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="analyst",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)
_VIEWER_CTX = AuthRequestContext(
    user_id=_USER_ID,
    tenant_id=_TENANT_ID,
    role="viewer",
    jti=str(uuid.uuid4()),
    payload=MagicMock(spec=TokenPayload),
)


def _make_job_read(
    job_id: uuid.UUID | None = None,
    ticker: str = "AAPL",
    status: str = "pending",
    **overrides: Any,
) -> AcquisitionJobRead:
    data: dict[str, Any] = {
        "id": job_id or uuid.uuid4(),
        "ticker": ticker,
        "cik": None,
        "company_name": None,
        "job_type": "sec_filing_discovery",
        "status": status,
        "error_message": None,
        "filings_discovered": 0,
        "filings_new": 0,
        "documents_fetched": 0,
        "documents_stored": 0,
        "started_at": None,
        "completed_at": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        **overrides,
    }
    return AcquisitionJobRead.model_validate(data)


def _make_job_orm(
    job_id: uuid.UUID | None = None,
    ticker: str = "AAPL",
    status: str = "pending",
) -> MagicMock:
    j = MagicMock()
    j.id = job_id or uuid.uuid4()
    j.ticker = ticker
    j.cik = None
    j.company_name = None
    j.job_type = "sec_filing_discovery"
    j.status = status
    j.error_message = None
    j.filings_discovered = 0
    j.filings_new = 0
    j.documents_fetched = 0
    j.documents_stored = 0
    j.started_at = None
    j.completed_at = None
    j.created_at = _NOW
    j.updated_at = _NOW
    return j


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(ctx: AuthRequestContext) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: ctx
    app.dependency_overrides[require_analyst] = lambda: ctx
    app.include_router(router)
    return app


# ===========================================================================
# POST /api/v1/acquisition/jobs
# ===========================================================================


class TestCreateAcquisitionJob:
    @pytest.mark.anyio
    async def test_returns_202_on_success(self) -> None:
        app = _build_app(_ANALYST_CTX)
        job = _make_job_read(job_id=_JOB_ID)
        mock_service = AsyncMock()
        mock_service.create_job.return_value = job

        with (
            patch("apps.api.routers.acquisition._get_acquisition_service", return_value=mock_service),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/acquisition/jobs", json={"ticker": "AAPL"})

        assert resp.status_code == 202

    @pytest.mark.anyio
    async def test_response_body_contains_job_fields(self) -> None:
        app = _build_app(_ANALYST_CTX)
        job = _make_job_read(job_id=_JOB_ID, ticker="AAPL", status="pending")
        mock_service = AsyncMock()
        mock_service.create_job.return_value = job

        with (
            patch("apps.api.routers.acquisition._get_acquisition_service", return_value=mock_service),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/api/v1/acquisition/jobs", json={"ticker": "AAPL"})

        body = resp.json()
        assert body["id"] == str(_JOB_ID)
        assert body["ticker"] == "AAPL"
        assert body["status"] == "pending"
        assert "filings_discovered" in body
        assert "documents_stored" in body

    @pytest.mark.anyio
    async def test_ticker_normalised_to_uppercase(self) -> None:
        app = _build_app(_ANALYST_CTX)
        captured: dict[str, Any] = {}

        async def _capture(ticker: str) -> Any:
            captured["ticker"] = ticker
            return _make_job_read(ticker=ticker.upper())

        mock_service = AsyncMock()
        mock_service.create_job.side_effect = _capture

        with (
            patch("apps.api.routers.acquisition._get_acquisition_service", return_value=mock_service),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/acquisition/jobs", json={"ticker": "aapl"})

        assert captured["ticker"] == "AAPL"

    @pytest.mark.anyio
    async def test_empty_ticker_returns_422(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/acquisition/jobs", json={"ticker": ""})

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_missing_ticker_returns_422(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/acquisition/jobs", json={})

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_celery_dispatch_called(self) -> None:
        app = _build_app(_ANALYST_CTX)
        job = _make_job_read(job_id=_JOB_ID)
        mock_service = AsyncMock()
        mock_service.create_job.return_value = job

        with (
            patch("apps.api.routers.acquisition._get_acquisition_service", return_value=mock_service),
            patch("apps.api.routers.acquisition._dispatch_celery") as mock_dispatch,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/api/v1/acquisition/jobs", json={"ticker": "AAPL"})

        mock_dispatch.assert_called_once_with(_JOB_ID)


# ===========================================================================
# GET /api/v1/acquisition/jobs
# ===========================================================================


class TestListAcquisitionJobs:
    @pytest.mark.anyio
    async def test_returns_200_with_pagination(self) -> None:
        app = _build_app(_VIEWER_CTX)
        jobs = [_make_job_orm(ticker="AAPL"), _make_job_orm(ticker="MSFT")]

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.list.return_value = (jobs, 2)
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/acquisition/jobs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        assert "pages" in body
        assert "page" in body
        assert "page_size" in body

    @pytest.mark.anyio
    async def test_pagination_params_forwarded(self) -> None:
        app = _build_app(_VIEWER_CTX)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.list.return_value = ([], 0)
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/acquisition/jobs?page=2&page_size=5")

            mock_repo.list.assert_called_once_with(
                page=2, page_size=5, status=None, ticker=None
            )

    @pytest.mark.anyio
    async def test_filter_by_status(self) -> None:
        app = _build_app(_VIEWER_CTX)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.list.return_value = ([], 0)
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/acquisition/jobs?status=pending")

            mock_repo.list.assert_called_once_with(
                page=1, page_size=20, status="pending", ticker=None
            )

    @pytest.mark.anyio
    async def test_filter_by_ticker(self) -> None:
        app = _build_app(_VIEWER_CTX)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.list.return_value = ([], 0)
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/api/v1/acquisition/jobs?ticker=AAPL")

            mock_repo.list.assert_called_once_with(
                page=1, page_size=20, status=None, ticker="AAPL"
            )

    @pytest.mark.anyio
    async def test_page_size_above_100_returns_422(self) -> None:
        app = _build_app(_VIEWER_CTX)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/acquisition/jobs?page_size=101")

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_empty_list_returns_correct_envelope(self) -> None:
        app = _build_app(_VIEWER_CTX)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.list.return_value = ([], 0)
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/v1/acquisition/jobs")

        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["pages"] == 0


# ===========================================================================
# GET /api/v1/acquisition/jobs/{job_id}
# ===========================================================================


class TestGetAcquisitionJob:
    @pytest.mark.anyio
    async def test_returns_200_when_found(self) -> None:
        app = _build_app(_VIEWER_CTX)
        job = _make_job_orm(job_id=_JOB_ID, status="completed")

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = job
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/acquisition/jobs/{_JOB_ID}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(_JOB_ID)
        assert body["status"] == "completed"

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_VIEWER_CTX)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = None
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/acquisition/jobs/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ACQUISITIONJOB_NOT_FOUND"

    @pytest.mark.anyio
    async def test_response_has_progress_counters(self) -> None:
        app = _build_app(_VIEWER_CTX)
        job = _make_job_orm(job_id=_JOB_ID, status="completed")
        job.filings_discovered = 10
        job.filings_new = 3
        job.documents_fetched = 3
        job.documents_stored = 3

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = job
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/acquisition/jobs/{_JOB_ID}")

        body = resp.json()
        assert body["filings_discovered"] == 10
        assert body["filings_new"] == 3
        assert body["documents_stored"] == 3


# ===========================================================================
# POST /api/v1/acquisition/jobs/{job_id}/retry
# ===========================================================================


class TestRetryAcquisitionJob:
    @pytest.mark.anyio
    async def test_returns_202_on_successful_retry(self) -> None:
        app = _build_app(_ANALYST_CTX)
        original = _make_job_orm(job_id=_JOB_ID, status="failed")
        new_job = _make_job_read(job_id=uuid.uuid4(), ticker="AAPL", status="pending")
        mock_service = AsyncMock()
        mock_service.create_job.return_value = new_job

        with (
            patch(
                "apps.api.routers.acquisition.AcquisitionJobRepository"
            ) as MockRepo,
            patch(
                "apps.api.routers.acquisition._get_acquisition_service",
                return_value=mock_service,
            ),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = original
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(f"/api/v1/acquisition/jobs/{_JOB_ID}/retry")

        assert resp.status_code == 202

    @pytest.mark.anyio
    async def test_returns_404_when_original_not_found(self) -> None:
        app = _build_app(_ANALYST_CTX)

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = None
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(f"/api/v1/acquisition/jobs/{uuid.uuid4()}/retry")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_returns_409_when_job_is_pending(self) -> None:
        app = _build_app(_ANALYST_CTX)
        job = _make_job_orm(job_id=_JOB_ID, status="pending")

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = job
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(f"/api/v1/acquisition/jobs/{_JOB_ID}/retry")

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.anyio
    async def test_returns_409_when_job_is_running(self) -> None:
        app = _build_app(_ANALYST_CTX)
        job = _make_job_orm(job_id=_JOB_ID, status="running")

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = job
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(f"/api/v1/acquisition/jobs/{_JOB_ID}/retry")

        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_returns_409_when_job_is_completed(self) -> None:
        app = _build_app(_ANALYST_CTX)
        job = _make_job_orm(job_id=_JOB_ID, status="completed")

        with patch(
            "apps.api.routers.acquisition.AcquisitionJobRepository"
        ) as MockRepo:
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = job
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(f"/api/v1/acquisition/jobs/{_JOB_ID}/retry")

        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_new_job_dispatched_to_celery(self) -> None:
        app = _build_app(_ANALYST_CTX)
        original = _make_job_orm(job_id=_JOB_ID, status="failed")
        new_id = uuid.uuid4()
        new_job = _make_job_read(job_id=new_id, ticker="AAPL", status="pending")
        mock_service = AsyncMock()
        mock_service.create_job.return_value = new_job

        with (
            patch(
                "apps.api.routers.acquisition.AcquisitionJobRepository"
            ) as MockRepo,
            patch(
                "apps.api.routers.acquisition._get_acquisition_service",
                return_value=mock_service,
            ),
            patch("apps.api.routers.acquisition._dispatch_celery") as mock_dispatch,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = original
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post(f"/api/v1/acquisition/jobs/{_JOB_ID}/retry")

        mock_dispatch.assert_called_once_with(new_id)

    @pytest.mark.anyio
    async def test_retry_uses_same_ticker(self) -> None:
        app = _build_app(_ANALYST_CTX)
        original = _make_job_orm(job_id=_JOB_ID, status="failed", ticker="TSLA")
        new_job = _make_job_read(job_id=uuid.uuid4(), ticker="TSLA", status="pending")
        mock_service = AsyncMock()
        mock_service.create_job.return_value = new_job

        with (
            patch(
                "apps.api.routers.acquisition.AcquisitionJobRepository"
            ) as MockRepo,
            patch(
                "apps.api.routers.acquisition._get_acquisition_service",
                return_value=mock_service,
            ),
            patch("apps.api.routers.acquisition._dispatch_celery"),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_id.return_value = original
            MockRepo.return_value = mock_repo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post(f"/api/v1/acquisition/jobs/{_JOB_ID}/retry")

        mock_service.create_job.assert_called_once_with("TSLA")
