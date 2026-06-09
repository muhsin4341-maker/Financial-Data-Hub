"""
Unit tests — /api/v1/jobs router.

Strategy
--------
A minimal FastAPI application is built per-test with:
  - The jobs router
  - The APIError exception handler
  - The get_db dependency overridden with an AsyncMock session
  - The require_authenticated / require_analyst dependencies overridden to
    inject a fixed AuthRequestContext — bypasses JWT middleware and Redis
    blocklist check entirely in unit tests.

JobRepository and CompanyRepository are patched at the import paths used by
the router so their methods return pre-built MagicMock objects.

What is mocked
--------------
- ``JobRepository``              — all repo methods
- ``CompanyRepository``          — get_by_id (company validation in create)
- ``get_s3_client``              — yields a MagicMock boto3 client
- Auth dependencies              — inject fixed AuthRequestContext
- ``get_db``                     — yields AsyncMock session

What is NOT mocked (real code runs)
------------------------------------
- All Pydantic schema validation (JobCreate, UploadUrlRequest, UploadCompleteRequest)
- NotFoundError / ConflictError / ValidationError exception handling and response serialisation
- _to_response / _to_list_response conversion helpers
- HTTP status codes and response body structure
- is_terminal / is_cancellable computed fields on JobResponse
- make_safe_filename sanitisation logic

Milestone: M2-Step 7
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.database import get_db
from apps.api.core.exceptions import APIError, api_error_handler
from apps.api.core.s3 import get_s3_client
from apps.api.core.security import TokenPayload
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_analyst,
    require_authenticated,
)
from apps.api.models import JobStatus
from apps.api.routers.jobs import router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_COMPANY_ID = uuid.uuid4()
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

_JOB_DATA: dict[str, Any] = {
    "id": _JOB_ID,
    "tenant_id": _TENANT_ID,
    "company_id": _COMPANY_ID,
    "created_by": _USER_ID,
    "status": JobStatus.PENDING.value,
    "job_type": "sec_10k_annual",
    "fiscal_year": 2023,
    "document_url": None,
    "result_url": None,
    "error_message": None,
    "celery_task_id": None,
    "started_at": None,
    "completed_at": None,
    "created_at": _NOW,
    "updated_at": _NOW,
    # ORM property — must exist on mock for is_terminal / is_cancellable
    "is_terminal": False,
    "is_cancellable": True,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app(
    ctx: AuthRequestContext,
    mock_s3: MagicMock | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app wired with the jobs router."""
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

    async def _mock_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[require_authenticated] = lambda: ctx
    app.dependency_overrides[require_analyst] = lambda: ctx

    if mock_s3 is not None:
        app.dependency_overrides[get_s3_client] = lambda: mock_s3

    app.include_router(router)
    return app


def _mock_job(**overrides: Any) -> MagicMock:
    """Return a MagicMock that mimics a FinancialJob ORM object."""
    j = MagicMock()
    data = {**_JOB_DATA, **overrides}
    for k, v in data.items():
        setattr(j, k, v)
    return j


def _mock_company() -> MagicMock:
    """Return a minimal Company mock."""
    c = MagicMock()
    c.id = _COMPANY_ID
    c.tenant_id = _TENANT_ID
    return c


# ---------------------------------------------------------------------------
# POST /api/v1/jobs
# ---------------------------------------------------------------------------


class TestCreateJob:
    @pytest.mark.anyio
    async def test_returns_201_on_success(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_job = _mock_job()
        mock_job_repo = AsyncMock()
        mock_job_repo.create.return_value = mock_job
        mock_company_repo = AsyncMock()
        mock_company_repo.get_by_id.return_value = _mock_company()

        with (
            patch("apps.api.routers.jobs.JobRepository", return_value=mock_job_repo),
            patch("apps.api.routers.jobs.CompanyRepository", return_value=mock_company_repo),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/jobs",
                    json={"company_id": str(_COMPANY_ID), "job_type": "sec_10k_annual"},
                )

        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_response_body_shape(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_job = _mock_job()
        mock_job_repo = AsyncMock()
        mock_job_repo.create.return_value = mock_job
        mock_company_repo = AsyncMock()
        mock_company_repo.get_by_id.return_value = _mock_company()

        with (
            patch("apps.api.routers.jobs.JobRepository", return_value=mock_job_repo),
            patch("apps.api.routers.jobs.CompanyRepository", return_value=mock_company_repo),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/jobs",
                    json={"company_id": str(_COMPANY_ID), "job_type": "sec_10k_annual"},
                )

        body = resp.json()
        assert "id" in body
        assert body["status"] == "pending"
        assert body["job_type"] == "sec_10k_annual"
        assert body["tenant_id"] == str(_TENANT_ID)
        assert "is_terminal" in body
        assert "is_cancellable" in body
        assert body["is_terminal"] is False
        assert body["is_cancellable"] is True

    @pytest.mark.anyio
    async def test_unknown_company_returns_404(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_company_repo = AsyncMock()
        mock_company_repo.get_by_id.return_value = None

        with (
            patch("apps.api.routers.jobs.JobRepository", return_value=AsyncMock()),
            patch("apps.api.routers.jobs.CompanyRepository", return_value=mock_company_repo),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/jobs",
                    json={"company_id": str(uuid.uuid4()), "job_type": "sec_10k_annual"},
                )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "COMPANY_NOT_FOUND"

    @pytest.mark.anyio
    async def test_missing_required_fields_returns_422(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/jobs", json={"job_type": "sec_10k_annual"})

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_invalid_job_type_returns_422(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/jobs",
                json={"company_id": str(_COMPANY_ID), "job_type": "INVALID TYPE"},
            )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_tenant_id_comes_from_jwt(self) -> None:
        """The router must inject ctx.tenant_id — never from the request body."""
        app = _build_app(_ANALYST_CTX)
        mock_job = _mock_job()
        mock_job_repo = AsyncMock()
        mock_company_repo = AsyncMock()
        mock_company_repo.get_by_id.return_value = _mock_company()
        captured: dict[str, Any] = {}

        async def _capture(tid: Any, cid: Any, uid: Any, schema: Any) -> Any:
            captured["tenant_id"] = tid
            captured["created_by"] = uid
            return mock_job

        mock_job_repo.create.side_effect = _capture

        with (
            patch("apps.api.routers.jobs.JobRepository", return_value=mock_job_repo),
            patch("apps.api.routers.jobs.CompanyRepository", return_value=mock_company_repo),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/jobs",
                    json={"company_id": str(_COMPANY_ID), "job_type": "sec_10k_annual"},
                )

        assert captured["tenant_id"] == _TENANT_ID
        assert captured["created_by"] == _USER_ID

    @pytest.mark.anyio
    async def test_viewer_cannot_create_job(self) -> None:
        """VIEWER role must be rejected — real role guard."""
        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        app.dependency_overrides[get_db] = _mock_db
        app.dependency_overrides[_get_auth_context] = lambda: _VIEWER_CTX
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/jobs",
                json={"company_id": str(_COMPANY_ID), "job_type": "sec_10k_annual"},
            )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    @pytest.mark.anyio
    async def test_returns_200_with_envelope(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = (
            [_mock_job(), _mock_job(id=uuid.uuid4())],
            2,
        )

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/jobs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["page"] == 1
        assert body["page_size"] == 20
        assert "pages" in body
        assert len(body["items"]) == 2

    @pytest.mark.anyio
    async def test_pagination_params_forwarded(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/jobs?page=3&page_size=10")

        call_kwargs = mock_repo.list.call_args[1]
        assert call_kwargs["page"] == 3
        assert call_kwargs["page_size"] == 10

    @pytest.mark.anyio
    async def test_company_id_filter_forwarded(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(f"/api/v1/jobs?company_id={_COMPANY_ID}")

        call_kwargs = mock_repo.list.call_args[1]
        assert call_kwargs["company_id"] == _COMPANY_ID

    @pytest.mark.anyio
    async def test_status_filter_forwarded(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/jobs?status=running")

        call_kwargs = mock_repo.list.call_args[1]
        assert call_kwargs["status"] == "running"

    @pytest.mark.anyio
    async def test_empty_result(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/jobs")

        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["pages"] == 0

    @pytest.mark.anyio
    async def test_pages_computed_correctly(self) -> None:
        """pages = ceil(total / page_size) — not from model_validator."""
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([_mock_job()], 25)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/jobs?page=1&page_size=10")

        assert resp.json()["pages"] == 3  # ceil(25 / 10)

    @pytest.mark.anyio
    async def test_page_size_max_100(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/jobs?page_size=101")

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_tenant_id_scopes_query(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.list.return_value = ([], 0)
        captured: dict[str, Any] = {}

        async def _capture(tid: Any, **kwargs: Any) -> Any:
            captured["tenant_id"] = tid
            return [], 0

        mock_repo.list.side_effect = _capture

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get("/api/v1/jobs")

        assert captured["tenant_id"] == _TENANT_ID


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{job_id}
# ---------------------------------------------------------------------------


class TestGetJob:
    @pytest.mark.anyio
    async def test_returns_200_when_found(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job()

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/jobs/{_JOB_ID}")

        assert resp.status_code == 200
        assert resp.json()["id"] == str(_JOB_ID)

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = None

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/jobs/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"

    @pytest.mark.anyio
    async def test_repo_called_with_tenant_id(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job()

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.get(f"/api/v1/jobs/{_JOB_ID}")

        call_args = mock_repo.get_by_id.call_args[0]
        assert call_args[0] == _TENANT_ID

    @pytest.mark.anyio
    async def test_computed_fields_present(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(
            status="running", is_terminal=False, is_cancellable=True
        )

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/jobs/{_JOB_ID}")

        body = resp.json()
        assert "is_terminal" in body
        assert "is_cancellable" in body


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{job_id}/status
# ---------------------------------------------------------------------------


class TestGetJobStatus:
    @pytest.mark.anyio
    async def test_returns_200_with_status_fields(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job()

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/jobs/{_JOB_ID}/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(_JOB_ID)
        assert body["status"] == "pending"
        assert "started_at" in body
        assert "completed_at" in body
        assert "error_message" in body

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = None

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/jobs/{uuid.uuid4()}/status")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_no_full_job_fields_in_response(self) -> None:
        """Status endpoint must NOT expose heavy fields like document_url, result_url."""
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job()

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/api/v1/jobs/{_JOB_ID}/status")

        body = resp.json()
        assert "document_url" not in body
        assert "result_url" not in body
        assert "celery_task_id" not in body


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


class TestCancelJob:
    @pytest.mark.anyio
    async def test_returns_200_on_successful_cancel(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        pending_job = _mock_job(is_terminal=False)
        cancelled_job = _mock_job(
            status=JobStatus.CANCELLED.value,
            is_terminal=True,
            is_cancellable=False,
        )
        mock_repo.get_by_id.return_value = pending_job
        mock_repo.cancel.return_value = cancelled_job

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/jobs/{_JOB_ID}/cancel")

        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    @pytest.mark.anyio
    async def test_returns_404_when_not_found(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = None

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/jobs/{uuid.uuid4()}/cancel")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"

    @pytest.mark.anyio
    async def test_already_terminal_returns_409(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        terminal_job = _mock_job(
            status=JobStatus.COMPLETED.value,
            is_terminal=True,
            is_cancellable=False,
        )
        mock_repo.get_by_id.return_value = terminal_job

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/jobs/{_JOB_ID}/cancel")

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.anyio
    async def test_cancel_already_cancelled_returns_409(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        cancelled_job = _mock_job(
            status=JobStatus.CANCELLED.value,
            is_terminal=True,
            is_cancellable=False,
        )
        mock_repo.get_by_id.return_value = cancelled_job

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(f"/api/v1/jobs/{_JOB_ID}/cancel")

        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_cancel_repo_called_with_tenant_id(self) -> None:
        app = _build_app(_ANALYST_CTX)
        mock_repo = AsyncMock()
        pending_job = _mock_job(is_terminal=False)
        cancelled_job = _mock_job(
            status=JobStatus.CANCELLED.value, is_terminal=True, is_cancellable=False
        )
        mock_repo.get_by_id.return_value = pending_job
        mock_repo.cancel.return_value = cancelled_job
        captured: dict[str, Any] = {}

        async def _capture(tid: Any, jid: Any) -> Any:
            captured["tenant_id"] = tid
            return cancelled_job

        mock_repo.cancel.side_effect = _capture

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(f"/api/v1/jobs/{_JOB_ID}/cancel")

        assert captured["tenant_id"] == _TENANT_ID

    @pytest.mark.anyio
    async def test_viewer_cannot_cancel_job(self) -> None:
        """VIEWER role must be rejected — real role guard."""
        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        app.dependency_overrides[get_db] = _mock_db
        app.dependency_overrides[_get_auth_context] = lambda: _VIEWER_CTX
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/v1/jobs/{_JOB_ID}/cancel")

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{job_id}/upload-url
# ---------------------------------------------------------------------------

_FAKE_PRESIGNED_URL = "https://s3.amazonaws.com/bucket/key?X-Amz-Signature=abc"
_EXPECTED_KEY_PREFIX = f"{_TENANT_ID}/jobs/{_JOB_ID}/"


class TestGenerateUploadUrl:
    def _make_mock_s3(self) -> MagicMock:
        """Return a MagicMock boto3 S3 client with presigned URL stubbed."""
        s3 = MagicMock()
        s3.generate_presigned_url.return_value = _FAKE_PRESIGNED_URL
        return s3

    @pytest.mark.anyio
    async def test_returns_200_with_url_and_key(self) -> None:
        mock_s3 = self._make_mock_s3()
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        app = _build_app(_ANALYST_CTX, mock_s3=mock_s3)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-url",
                    json={"filename": "annual_report.pdf"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["url"] == _FAKE_PRESIGNED_URL
        assert body["key"].startswith(_EXPECTED_KEY_PREFIX)
        assert body["expires_in"] == 900

    @pytest.mark.anyio
    async def test_key_contains_safe_filename(self) -> None:
        mock_s3 = self._make_mock_s3()
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        app = _build_app(_ANALYST_CTX, mock_s3=mock_s3)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-url",
                    json={"filename": "annual_report.pdf"},
                )

        key = resp.json()["key"]
        assert key == f"{_TENANT_ID}/jobs/{_JOB_ID}/annual_report.pdf"

    @pytest.mark.anyio
    async def test_filename_path_traversal_sanitised(self) -> None:
        """Malicious filenames like ../../etc/passwd must be sanitised."""
        mock_s3 = self._make_mock_s3()
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        app = _build_app(_ANALYST_CTX, mock_s3=mock_s3)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-url",
                    json={"filename": "../../etc/passwd"},
                )

        assert resp.status_code == 200
        key = resp.json()["key"]
        # Key must NOT contain path traversal components
        assert "../" not in key
        assert "etc/passwd" not in key
        # It should end with the sanitised basename only
        assert key.startswith(_EXPECTED_KEY_PREFIX)

    @pytest.mark.anyio
    async def test_job_not_found_returns_404(self) -> None:
        mock_s3 = self._make_mock_s3()
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = None
        app = _build_app(_ANALYST_CTX, mock_s3=mock_s3)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{uuid.uuid4()}/upload-url",
                    json={"filename": "doc.pdf"},
                )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"

    @pytest.mark.anyio
    async def test_terminal_job_returns_409(self) -> None:
        mock_s3 = self._make_mock_s3()
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(
            status=JobStatus.CANCELLED.value, is_terminal=True
        )
        app = _build_app(_ANALYST_CTX, mock_s3=mock_s3)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-url",
                    json={"filename": "doc.pdf"},
                )

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.anyio
    async def test_missing_filename_returns_422(self) -> None:
        mock_s3 = self._make_mock_s3()
        app = _build_app(_ANALYST_CTX, mock_s3=mock_s3)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/jobs/{_JOB_ID}/upload-url", json={}
            )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_s3_called_with_put_object(self) -> None:
        """Verify generate_presigned_url is called with 'put_object' method."""
        mock_s3 = self._make_mock_s3()
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        app = _build_app(_ANALYST_CTX, mock_s3=mock_s3)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-url",
                    json={"filename": "doc.pdf"},
                )

        mock_s3.generate_presigned_url.assert_called_once()
        call_args = mock_s3.generate_presigned_url.call_args
        assert call_args[0][0] == "put_object"
        assert call_args[1]["ExpiresIn"] == 900

    @pytest.mark.anyio
    async def test_viewer_cannot_generate_upload_url(self) -> None:
        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        app.dependency_overrides[get_db] = _mock_db
        app.dependency_overrides[_get_auth_context] = lambda: _VIEWER_CTX
        app.dependency_overrides[get_s3_client] = lambda: MagicMock()
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/jobs/{_JOB_ID}/upload-url",
                json={"filename": "doc.pdf"},
            )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{job_id}/upload-complete
# ---------------------------------------------------------------------------


class TestUploadComplete:
    @pytest.mark.anyio
    async def test_returns_200_with_updated_job(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        updated = _mock_job(
            document_url=f"{_TENANT_ID}/jobs/{_JOB_ID}/report.pdf",
            is_terminal=False,
        )
        mock_repo.set_document_url.return_value = updated
        app = _build_app(_ANALYST_CTX)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-complete",
                    json={"key": f"{_TENANT_ID}/jobs/{_JOB_ID}/report.pdf"},
                )

        assert resp.status_code == 200
        assert resp.json()["document_url"] == f"{_TENANT_ID}/jobs/{_JOB_ID}/report.pdf"

    @pytest.mark.anyio
    async def test_job_not_found_returns_404(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = None
        app = _build_app(_ANALYST_CTX)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{uuid.uuid4()}/upload-complete",
                    json={"key": f"{_TENANT_ID}/jobs/{_JOB_ID}/report.pdf"},
                )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"

    @pytest.mark.anyio
    async def test_wrong_key_prefix_returns_422(self) -> None:
        """A key belonging to a different tenant/job must be rejected."""
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        app = _build_app(_ANALYST_CTX)
        other_tenant = uuid.uuid4()

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-complete",
                    json={"key": f"{other_tenant}/jobs/{_JOB_ID}/report.pdf"},
                )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.anyio
    async def test_arbitrary_key_injection_rejected(self) -> None:
        """Completely unrelated S3 key must be rejected."""
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        app = _build_app(_ANALYST_CTX)

        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-complete",
                    json={"key": "other-tenant/sensitive-data.pdf"},
                )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_missing_key_returns_422(self) -> None:
        app = _build_app(_ANALYST_CTX)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/jobs/{_JOB_ID}/upload-complete", json={}
            )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_repo_set_document_url_called(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = _mock_job(is_terminal=False)
        updated = _mock_job(
            document_url=f"{_TENANT_ID}/jobs/{_JOB_ID}/report.pdf",
            is_terminal=False,
        )
        mock_repo.set_document_url.return_value = updated
        captured: dict[str, Any] = {}
        app = _build_app(_ANALYST_CTX)

        async def _capture(tid: Any, jid: Any, url: Any) -> Any:
            captured["tenant_id"] = tid
            captured["key"] = url
            return updated

        mock_repo.set_document_url.side_effect = _capture

        key = f"{_TENANT_ID}/jobs/{_JOB_ID}/report.pdf"
        with patch("apps.api.routers.jobs.JobRepository", return_value=mock_repo):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    f"/api/v1/jobs/{_JOB_ID}/upload-complete",
                    json={"key": key},
                )

        assert captured["tenant_id"] == _TENANT_ID
        assert captured["key"] == key

    @pytest.mark.anyio
    async def test_viewer_cannot_upload_complete(self) -> None:
        from apps.api.middleware.auth import _get_auth_context  # noqa: PLC0415

        app = FastAPI()
        app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

        async def _mock_db() -> Any:
            yield AsyncMock()

        app.dependency_overrides[get_db] = _mock_db
        app.dependency_overrides[_get_auth_context] = lambda: _VIEWER_CTX
        app.include_router(router)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/jobs/{_JOB_ID}/upload-complete",
                json={"key": f"{_TENANT_ID}/jobs/{_JOB_ID}/report.pdf"},
            )

        assert resp.status_code == 403
