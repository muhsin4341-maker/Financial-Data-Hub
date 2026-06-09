"""
Integration tests — /api/v1/jobs S3 pre-signed URL endpoints.

Tests the upload-url and upload-complete endpoints against a real FastAPI
app backed by PostgreSQL + LocalStack S3.

Skip conditions:
  - DATABASE_URL not set  → PostgreSQL not available
  - AWS_ENDPOINT_URL not set → LocalStack not running

To run:
    docker compose up -d db redis localstack
    DATABASE_URL=... AWS_ENDPOINT_URL=http://localhost:4566 \\
    AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \\
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_jobs_s3.py -v

Milestone: M2-Step 8
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and os.getenv("AWS_ENDPOINT_URL")),
    reason=(
        "S3 integration tests require DATABASE_URL and AWS_ENDPOINT_URL "
        "(LocalStack) to be set."
    ),
)

_STRONG_PASSWORD = "Str0ng!Pass#S3Upload99"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(client: AsyncClient) -> tuple[str, str, str]:
    """
    Register a unique workspace + owner, login, create a company, create a
    job, and return (access_token, company_id, job_id).
    """
    suffix = uuid.uuid4().hex[:8]
    email = f"s3-test-{suffix}@example.com"
    workspace = f"S3 Test WS {suffix}"

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": _STRONG_PASSWORD,
            "full_name": "S3 Tester",
            "workspace_name": workspace,
        },
    )
    assert reg.status_code == 201, reg.text
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create a company
    company_resp = await client.post(
        "/api/v1/companies",
        json={"name": "S3 Test Corp", "ticker": f"S3T{suffix[:4].upper()}"},
        headers=headers,
    )
    assert company_resp.status_code == 201, company_resp.text
    company_id = company_resp.json()["id"]

    # Create a job
    job_resp = await client.post(
        "/api/v1/jobs",
        json={"company_id": company_id, "job_type": "sec_10k_annual"},
        headers=headers,
    )
    assert job_resp.status_code == 201, job_resp.text
    job_id = job_resp.json()["id"]

    return token, company_id, job_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{id}/upload-url
# ---------------------------------------------------------------------------


class TestUploadUrlIntegration:
    @pytest.mark.anyio
    async def test_returns_url_and_key(self, client: AsyncClient) -> None:
        token, _, job_id = await _register_and_login(client)
        resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload-url",
            json={"filename": "report.pdf"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "url" in body
        assert "key" in body
        assert body["expires_in"] == 900
        assert body["key"].endswith("/report.pdf")

    @pytest.mark.anyio
    async def test_key_contains_tenant_and_job_ids(self, client: AsyncClient) -> None:
        token, _, job_id = await _register_and_login(client)
        resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload-url",
            json={"filename": "report.pdf"},
            headers=_auth(token),
        )
        key = resp.json()["key"]
        # Key format: {tenant_id}/jobs/{job_id}/{filename}
        assert f"/jobs/{job_id}/" in key

    @pytest.mark.anyio
    async def test_unknown_job_returns_404(self, client: AsyncClient) -> None:
        token, _, _ = await _register_and_login(client)
        resp = await client.post(
            f"/api/v1/jobs/{uuid.uuid4()}/upload-url",
            json={"filename": "report.pdf"},
            headers=_auth(token),
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/v1/jobs/{uuid.uuid4()}/upload-url",
            json={"filename": "report.pdf"},
        )
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_cross_tenant_isolation(self, client: AsyncClient) -> None:
        """A job from tenant A must not be accessible by tenant B."""
        token_a, _, job_id_a = await _register_and_login(client)
        token_b, _, _ = await _register_and_login(client)

        resp = await client.post(
            f"/api/v1/jobs/{job_id_a}/upload-url",
            json={"filename": "report.pdf"},
            headers=_auth(token_b),
        )
        # Must return 404 — not 403 — to avoid leaking existence
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{id}/upload-complete
# ---------------------------------------------------------------------------


class TestUploadCompleteIntegration:
    @pytest.mark.anyio
    async def test_sets_document_url(self, client: AsyncClient) -> None:
        token, _, job_id = await _register_and_login(client)

        # First get a valid key from upload-url
        url_resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload-url",
            json={"filename": "report.pdf"},
            headers=_auth(token),
        )
        assert url_resp.status_code == 200, url_resp.text
        key = url_resp.json()["key"]

        # Confirm upload complete
        complete_resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload-complete",
            json={"key": key},
            headers=_auth(token),
        )
        assert complete_resp.status_code == 200, complete_resp.text
        body = complete_resp.json()
        assert body["document_url"] == key

    @pytest.mark.anyio
    async def test_unknown_job_returns_404(self, client: AsyncClient) -> None:
        token, _, job_id = await _register_and_login(client)

        # Get a real key format
        url_resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload-url",
            json={"filename": "report.pdf"},
            headers=_auth(token),
        )
        key = url_resp.json()["key"]
        fake_id = uuid.uuid4()
        # Rewrite key with fake job id but valid tenant prefix
        key_for_fake = key.replace(str(job_id), str(fake_id))

        resp = await client.post(
            f"/api/v1/jobs/{fake_id}/upload-complete",
            json={"key": key_for_fake},
            headers=_auth(token),
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_wrong_key_prefix_returns_422(self, client: AsyncClient) -> None:
        token, _, job_id = await _register_and_login(client)

        resp = await client.post(
            f"/api/v1/jobs/{job_id}/upload-complete",
            json={"key": "other-tenant/jobs/arbitrary/file.pdf"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_cross_tenant_isolation(self, client: AsyncClient) -> None:
        token_a, _, job_id_a = await _register_and_login(client)
        token_b, _, job_id_b = await _register_and_login(client)

        # Get the key for job_a's upload
        url_resp = await client.post(
            f"/api/v1/jobs/{job_id_a}/upload-url",
            json={"filename": "report.pdf"},
            headers=_auth(token_a),
        )
        key_a = url_resp.json()["key"]

        # Tenant B tries to mark job_a as complete — must get 404
        resp = await client.post(
            f"/api/v1/jobs/{job_id_a}/upload-complete",
            json={"key": key_a},
            headers=_auth(token_b),
        )
        assert resp.status_code == 404
