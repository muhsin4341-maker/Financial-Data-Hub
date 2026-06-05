"""
Integration tests — /api/v1/companies endpoints.

Uses the full FastAPI app (auth middleware + JWT + Redis + PostgreSQL).
Each test registers a fresh user + workspace, obtains a real JWT, and
makes HTTP requests through the ASGI transport.

Skipped automatically when DATABASE_URL is not set.

To run:
    docker compose up -d db redis
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/test_companies_router.py -v

Milestone: M2-Step 6
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="Integration tests require DATABASE_URL env var.",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRONG_PASSWORD = "Str0ng!Pass#Companies99"


async def _register_and_login(client: AsyncClient) -> tuple[str, str]:
    """
    Register a unique workspace + owner, login, and return
    (access_token, user_email).
    """
    suffix = uuid.uuid4().hex[:8]
    email = f"co-test-{suffix}@example.com"
    workspace = f"Co Test WS {suffix}"

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": _STRONG_PASSWORD,
            "full_name": "Company Tester",
            "workspace_name": workspace,
        },
    )
    assert reg.status_code == 201, reg.text
    token = reg.json()["access_token"]
    return token, email


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# POST /api/v1/companies
# ---------------------------------------------------------------------------


class TestCreateCompanyIntegration:
    @pytest.mark.anyio
    async def test_create_returns_201(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.post(
            "/api/v1/companies",
            json={"name": "Apple Inc.", "ticker": "aapl"},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert body["name"] == "Apple Inc."
        assert "id" in body

    @pytest.mark.anyio
    async def test_create_with_all_fields(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.post(
            "/api/v1/companies",
            json={
                "name": "Microsoft",
                "ticker": "MSFT",
                "cik": "789019",
                "exchange": "NASDAQ",
                "sector": "Technology",
                "industry": "Software",
                "description": "Software company",
                "website": "https://microsoft.com",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["cik"] == "0000789019"  # zero-padded
        assert body["exchange"] == "NASDAQ"

    @pytest.mark.anyio
    async def test_duplicate_ticker_returns_409(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        payload = {"name": "Test Co", "ticker": "XDUP"}
        await client.post("/api/v1/companies", json=payload, headers=_auth(token))
        resp = await client.post("/api/v1/companies", json=payload, headers=_auth(token))
        assert resp.status_code == 409
        assert "conflict" in resp.json()["error"]["code"].lower()

    @pytest.mark.anyio
    async def test_same_ticker_different_tenant_allowed(self, client: AsyncClient) -> None:
        """Two separate workspaces can have the same ticker."""
        token_a, _ = await _register_and_login(client)
        token_b, _ = await _register_and_login(client)

        r1 = await client.post(
            "/api/v1/companies", json={"name": "Co A", "ticker": "SAME"},
            headers=_auth(token_a),
        )
        r2 = await client.post(
            "/api/v1/companies", json={"name": "Co B", "ticker": "SAME"},
            headers=_auth(token_b),
        )
        assert r1.status_code == 201
        assert r2.status_code == 201

    @pytest.mark.anyio
    async def test_unauthenticated_returns_401(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/companies", json={"name": "Test", "ticker": "T"}
        )
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_missing_required_field_returns_422(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.post(
            "/api/v1/companies",
            json={"name": "No Ticker"},
            headers=_auth(token),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/companies
# ---------------------------------------------------------------------------


class TestListCompaniesIntegration:
    @pytest.mark.anyio
    async def test_list_returns_200_with_envelope(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.get("/api/v1/companies", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "page" in body
        assert "page_size" in body
        assert "pages" in body

    @pytest.mark.anyio
    async def test_created_company_appears_in_list(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        await client.post(
            "/api/v1/companies",
            json={"name": "List Test Co", "ticker": "LTC"},
            headers=_auth(token),
        )
        resp = await client.get("/api/v1/companies", headers=_auth(token))
        tickers = [c["ticker"] for c in resp.json()["items"]]
        assert "LTC" in tickers

    @pytest.mark.anyio
    async def test_search_filters_by_name(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        await client.post(
            "/api/v1/companies",
            json={"name": "Unique Search Corp", "ticker": "USC"},
            headers=_auth(token),
        )
        await client.post(
            "/api/v1/companies",
            json={"name": "Other Company", "ticker": "OTH"},
            headers=_auth(token),
        )
        resp = await client.get(
            "/api/v1/companies?search=Unique+Search",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["ticker"] == "USC"

    @pytest.mark.anyio
    async def test_cross_tenant_isolation(self, client: AsyncClient) -> None:
        token_a, _ = await _register_and_login(client)
        token_b, _ = await _register_and_login(client)

        await client.post(
            "/api/v1/companies",
            json={"name": "Tenant A Co", "ticker": "TAA"},
            headers=_auth(token_a),
        )

        resp = await client.get("/api/v1/companies", headers=_auth(token_b))
        tickers = [c["ticker"] for c in resp.json()["items"]]
        assert "TAA" not in tickers

    @pytest.mark.anyio
    async def test_pagination_defaults(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.get("/api/v1/companies", headers=_auth(token))
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 20

    @pytest.mark.anyio
    async def test_unauthenticated_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/companies")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/companies/{id}
# ---------------------------------------------------------------------------


class TestGetCompanyIntegration:
    @pytest.mark.anyio
    async def test_get_returns_company(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        create = await client.post(
            "/api/v1/companies",
            json={"name": "Get Test Co", "ticker": "GTC"},
            headers=_auth(token),
        )
        company_id = create.json()["id"]

        resp = await client.get(
            f"/api/v1/companies/{company_id}", headers=_auth(token)
        )
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "GTC"

    @pytest.mark.anyio
    async def test_not_found_returns_404(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.get(
            f"/api/v1/companies/{uuid.uuid4()}", headers=_auth(token)
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_cross_tenant_returns_404(self, client: AsyncClient) -> None:
        token_a, _ = await _register_and_login(client)
        token_b, _ = await _register_and_login(client)

        create = await client.post(
            "/api/v1/companies",
            json={"name": "Private Co", "ticker": "PVT"},
            headers=_auth(token_a),
        )
        company_id = create.json()["id"]

        resp = await client.get(
            f"/api/v1/companies/{company_id}", headers=_auth(token_b)
        )
        assert resp.status_code == 404  # not 403 — no existence leakage


# ---------------------------------------------------------------------------
# PATCH /api/v1/companies/{id}
# ---------------------------------------------------------------------------


class TestUpdateCompanyIntegration:
    @pytest.mark.anyio
    async def test_patch_name_only(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        create = await client.post(
            "/api/v1/companies",
            json={"name": "Old Name", "ticker": "OLD"},
            headers=_auth(token),
        )
        company_id = create.json()["id"]

        resp = await client.patch(
            f"/api/v1/companies/{company_id}",
            json={"name": "New Name"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "New Name"
        assert body["ticker"] == "OLD"  # unchanged

    @pytest.mark.anyio
    async def test_patch_ticker_conflict_returns_409(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        await client.post(
            "/api/v1/companies",
            json={"name": "Co A", "ticker": "TAKEN"},
            headers=_auth(token),
        )
        r2 = await client.post(
            "/api/v1/companies",
            json={"name": "Co B", "ticker": "FREE"},
            headers=_auth(token),
        )
        company_b_id = r2.json()["id"]

        resp = await client.patch(
            f"/api/v1/companies/{company_b_id}",
            json={"ticker": "TAKEN"},
            headers=_auth(token),
        )
        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_patch_not_found_returns_404(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.patch(
            f"/api/v1/companies/{uuid.uuid4()}",
            json={"name": "Ghost"},
            headers=_auth(token),
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_patch_empty_body_returns_422(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        create = await client.post(
            "/api/v1/companies",
            json={"name": "Test", "ticker": "TST"},
            headers=_auth(token),
        )
        company_id = create.json()["id"]

        resp = await client.patch(
            f"/api/v1/companies/{company_id}",
            json={},
            headers=_auth(token),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/v1/companies/{id}
# ---------------------------------------------------------------------------


class TestDeleteCompanyIntegration:
    @pytest.mark.anyio
    async def test_delete_returns_204(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        create = await client.post(
            "/api/v1/companies",
            json={"name": "To Delete", "ticker": "DEL"},
            headers=_auth(token),
        )
        company_id = create.json()["id"]

        resp = await client.delete(
            f"/api/v1/companies/{company_id}", headers=_auth(token)
        )
        assert resp.status_code == 204
        assert resp.content == b""

    @pytest.mark.anyio
    async def test_deleted_company_not_in_list(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        create = await client.post(
            "/api/v1/companies",
            json={"name": "Doomed", "ticker": "DOOM"},
            headers=_auth(token),
        )
        company_id = create.json()["id"]

        await client.delete(f"/api/v1/companies/{company_id}", headers=_auth(token))

        resp = await client.get("/api/v1/companies", headers=_auth(token))
        tickers = [c["ticker"] for c in resp.json()["items"]]
        assert "DOOM" not in tickers

    @pytest.mark.anyio
    async def test_deleted_company_returns_404(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        create = await client.post(
            "/api/v1/companies",
            json={"name": "Gone", "ticker": "GONE"},
            headers=_auth(token),
        )
        company_id = create.json()["id"]

        await client.delete(f"/api/v1/companies/{company_id}", headers=_auth(token))

        resp = await client.get(
            f"/api/v1/companies/{company_id}", headers=_auth(token)
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_not_found_returns_404(self, client: AsyncClient) -> None:
        token, _ = await _register_and_login(client)
        resp = await client.delete(
            f"/api/v1/companies/{uuid.uuid4()}", headers=_auth(token)
        )
        assert resp.status_code == 404
