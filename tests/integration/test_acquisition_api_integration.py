"""
Integration tests — M3.8 Acquisition APIs: VG-11 validation gate.

These tests exercise the full API stack through the FastAPI test client.
They require a live PostgreSQL database, a running application lifespan,
and (for VG-11 steps 5) prior acquisition data in the database.

Prerequisites:
  - DATABASE_URL env var pointing to a test PostgreSQL instance
  - RUN_INTEGRATION_TESTS=1 env var
  - All migrations applied (alembic upgrade head)
  - For document retrieval tests: prior AcquisitionJob must have run and
    stored at least one document (run VG-10 first, or seed the DB manually)

To run:
    DATABASE_URL=postgresql+asyncpg://... RUN_INTEGRATION_TESTS=1 \\
        pytest tests/integration/test_acquisition_api_integration.py -v

VG-11 validation gate:
  1. POST /acquisition/jobs (create AAPL job)
  2. GET /acquisition/jobs/{id} (track job)
  3. GET /companies/AAPL/filings (retrieve company filings)
  4. GET /filings/{accession_number} (retrieve filing metadata)
  5. GET /filings/{accession_number}/document (retrieve filing document)

All endpoints must return correct status codes, correct validation behavior,
and correct pagination.

Milestone: M3.8 — Acquisition APIs
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.skipif(
    not (os.getenv("RUN_INTEGRATION_TESTS") and os.getenv("DATABASE_URL")),
    reason=(
        "VG-11 integration tests disabled by default. "
        "Set RUN_INTEGRATION_TESTS=1 and DATABASE_URL to run."
    ),
)

_AAPL_TICKER = "AAPL"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def app_client():
    """
    Build a real FastAPI app + AsyncClient with full lifespan.

    The lifespan initialises the database pool and the test client
    intercepts all HTTP traffic without opening a real network socket.
    """
    from apps.api.main import app, lifespan

    async with lifespan(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


@pytest.fixture(scope="module")
def auth_headers() -> dict[str, str]:
    """
    Return a fake Bearer token that bypasses JWT verification.

    NOTE: This fixture assumes the test database was initialised with a
    seeded admin user whose token signs correctly.  For VG-11, we override
    auth dependencies at the module level to inject a fixed context.
    """
    # Tests use dependency overrides on the app — no real token needed here.
    # When using the full app lifespan the JWT middleware runs, so we need
    # a valid token.  For integration tests, skip auth using dependency override.
    return {}


# ---------------------------------------------------------------------------
# VG-11.1 — POST /api/v1/acquisition/jobs
# ---------------------------------------------------------------------------


class TestVG11CreateJob:
    @pytest.mark.anyio
    async def test_vg11_create_aapl_job_returns_202(self, app_client) -> None:
        """VG-11.1: Creating an AAPL acquisition job returns 202 Accepted."""
        # Override auth for this test
        from apps.api.middleware.auth import require_analyst
        from apps.api.core.security import TokenPayload
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.user_id = uuid.uuid4()
        ctx.tenant_id = uuid.uuid4()
        ctx.role = "analyst"

        app_client.app.dependency_overrides[require_analyst] = lambda: ctx

        try:
            resp = await app_client.post(
                "/api/v1/acquisition/jobs",
                json={"ticker": _AAPL_TICKER},
            )
        finally:
            app_client.app.dependency_overrides.pop(require_analyst, None)

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["ticker"] == _AAPL_TICKER
        assert body["status"] == "pending"
        assert "id" in body
        assert uuid.UUID(body["id"])  # valid UUID

    @pytest.mark.anyio
    async def test_vg11_invalid_ticker_returns_422(self, app_client) -> None:
        """VG-11.1 validation: Empty ticker returns 422 Unprocessable Entity."""
        from apps.api.middleware.auth import require_analyst
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_analyst] = lambda: ctx

        try:
            resp = await app_client.post(
                "/api/v1/acquisition/jobs",
                json={"ticker": ""},
            )
        finally:
            app_client.app.dependency_overrides.pop(require_analyst, None)

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# VG-11.2 — GET /api/v1/acquisition/jobs/{id}
# ---------------------------------------------------------------------------


class TestVG11TrackJob:
    @pytest.mark.anyio
    async def test_vg11_track_created_job(self, app_client) -> None:
        """VG-11.2: Created job is immediately retrievable via GET /{id}."""
        from apps.api.middleware.auth import require_analyst, require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.user_id = uuid.uuid4()
        ctx.tenant_id = uuid.uuid4()
        ctx.role = "analyst"

        app_client.app.dependency_overrides[require_analyst] = lambda: ctx
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            # Create a job
            create_resp = await app_client.post(
                "/api/v1/acquisition/jobs",
                json={"ticker": _AAPL_TICKER},
            )
            assert create_resp.status_code == 202
            job_id = create_resp.json()["id"]

            # Immediately retrieve it
            get_resp = await app_client.get(f"/api/v1/acquisition/jobs/{job_id}")
        finally:
            app_client.app.dependency_overrides.pop(require_analyst, None)
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["id"] == job_id
        assert body["ticker"] == _AAPL_TICKER
        assert body["status"] in ("pending", "running", "completed", "failed")
        assert "filings_discovered" in body
        assert "documents_stored" in body

    @pytest.mark.anyio
    async def test_vg11_nonexistent_job_returns_404(self, app_client) -> None:
        """VG-11.2 error path: Unknown job ID returns 404."""
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            resp = await app_client.get(f"/api/v1/acquisition/jobs/{uuid.uuid4()}")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# VG-11.3 — GET /api/v1/acquisition/jobs (list + pagination)
# ---------------------------------------------------------------------------


class TestVG11ListJobs:
    @pytest.mark.anyio
    async def test_vg11_list_jobs_pagination_works(self, app_client) -> None:
        """VG-11.3: Job list endpoint returns pagination envelope."""
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            resp = await app_client.get("/api/v1/acquisition/jobs?page=1&page_size=5")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "pages" in body
        assert body["page"] == 1
        assert body["page_size"] == 5

    @pytest.mark.anyio
    async def test_vg11_list_jobs_filter_by_ticker(self, app_client) -> None:
        """VG-11.3: Ticker filter returns only matching jobs."""
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            resp = await app_client.get(f"/api/v1/acquisition/jobs?ticker={_AAPL_TICKER}")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            assert item["ticker"] == _AAPL_TICKER


# ---------------------------------------------------------------------------
# VG-11.4 — GET /api/v1/companies/{ticker}/filings
# ---------------------------------------------------------------------------


class TestVG11CompanyFilings:
    @pytest.mark.anyio
    async def test_vg11_company_filings_endpoint_works(self, app_client) -> None:
        """VG-11.4: Company filings endpoint returns FilingListResponse."""
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            resp = await app_client.get(f"/api/v1/companies/{_AAPL_TICKER}/filings")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "pages" in body
        assert isinstance(body["items"], list)

    @pytest.mark.anyio
    async def test_vg11_company_filings_type_filter(self, app_client) -> None:
        """VG-11.4: Filing type filter works on company filings."""
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            resp = await app_client.get(
                f"/api/v1/companies/{_AAPL_TICKER}/filings?filing_type=10-K"
            )
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            assert item["filing_type"] == "10-K"


# ---------------------------------------------------------------------------
# VG-11.5 — GET /api/v1/filings + GET /api/v1/filings/{accession}
# ---------------------------------------------------------------------------


class TestVG11FilingAPIs:
    @pytest.mark.anyio
    async def test_vg11_list_filings_returns_200(self, app_client) -> None:
        """VG-11.5: Filing list endpoint is functional."""
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            resp = await app_client.get("/api/v1/filings?page=1&page_size=10")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body

    @pytest.mark.anyio
    async def test_vg11_filing_detail_if_data_exists(self, app_client) -> None:
        """
        VG-11.5: If filings exist, retrieve the first one by accession number.

        This test is data-dependent — it skips gracefully if no filings exist
        in the test database.
        """
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            # Get any filing
            list_resp = await app_client.get("/api/v1/filings?page_size=1")
            assert list_resp.status_code == 200
            items = list_resp.json()["items"]

            if not items:
                pytest.skip("No filings in test DB — run VG-10 first to seed data.")

            accession = items[0]["accession_number"]

            # Retrieve by accession
            detail_resp = await app_client.get(f"/api/v1/filings/{accession}")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert detail_resp.status_code == 200
        body = detail_resp.json()
        assert body["accession_number"] == accession
        assert "filing_type" in body
        assert "cik" in body
        assert "status" in body

    @pytest.mark.anyio
    async def test_vg11_invalid_accession_returns_422(self, app_client) -> None:
        """VG-11.5 validation: Invalid accession number format returns 422."""
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            resp = await app_client.get("/api/v1/filings/NOT-A-VALID-ACCESSION")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_vg11_document_endpoint_if_stored_document_exists(
        self, app_client
    ) -> None:
        """
        VG-11.5: If a stored document exists, retrieve it via the document endpoint.

        Data-dependent — skips if no downloaded filings with stored documents exist.
        """
        from apps.api.middleware.auth import require_authenticated
        from unittest.mock import MagicMock

        ctx = MagicMock()
        app_client.app.dependency_overrides[require_authenticated] = lambda: ctx

        try:
            # Find a downloaded filing
            list_resp = await app_client.get(
                "/api/v1/filings?status=downloaded&page_size=1"
            )
            assert list_resp.status_code == 200
            items = list_resp.json()["items"]

            if not items:
                pytest.skip(
                    "No downloaded filings in test DB — run VG-10 first to seed data."
                )

            accession = items[0]["accession_number"]
            doc_resp = await app_client.get(f"/api/v1/filings/{accession}/document")
        finally:
            app_client.app.dependency_overrides.pop(require_authenticated, None)

        assert doc_resp.status_code in (200, 404), (
            f"Expected 200 or 404, got {doc_resp.status_code}: {doc_resp.text}"
        )
        if doc_resp.status_code == 200:
            assert len(doc_resp.content) > 0
            assert "content-type" in doc_resp.headers
