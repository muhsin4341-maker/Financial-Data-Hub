"""
M1 Gate — End-to-end authentication flow validation.

Exercises the full Register → Login → Refresh → Logout cycle
against the running FastAPI application in-process (no network needed).

Usage:
    python scripts/e2e_auth.py

Exit code 0 = all assertions passed.
Exit code 1 = at least one assertion failed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

# Confirm DATABASE_URL is present — required for the app lifespan to connect.
if not os.getenv("DATABASE_URL"):
    print("ERROR: DATABASE_URL not set. Ensure .env is present and PostgreSQL is running.")
    sys.exit(1)


async def main() -> None:
    import httpx
    from httpx import ASGITransport

    # Import app and lifespan after dotenv is loaded.
    from apps.api.main import app, lifespan

    suffix = uuid.uuid4().hex[:8]
    email = f"e2e-{suffix}@example.com"
    password = "Str0ng!E2E#Pass99"
    workspace = f"E2E Workspace {suffix}"

    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            passed.append(name)
            print(f"  ✓ {name}")
        else:
            failed.append(name)
            print(f"  ✗ {name}{f': {detail}' if detail else ''}")

    async with lifespan(app), httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:

        # ── /health/ready ──────────────────────────────────────────────────────
        print("\n[health]")
        r = await client.get("/health")
        check("GET /health → 200", r.status_code == 200)

        r = await client.get("/health/ready")
        check("GET /health/ready → 200", r.status_code == 200)
        body = r.json()
        check("database: ok", body.get("database") == "ok", str(body))
        check("redis: ok", body.get("redis") == "ok", str(body))

        # ── Register ───────────────────────────────────────────────────────────
        print("\n[register]")
        r = await client.post("/api/v1/auth/register", json={
            "email": email,
            "password": password,
            "full_name": "E2E Test User",
            "workspace_name": workspace,
        })
        check("POST /auth/register → 201", r.status_code == 201, str(r.json()))
        reg_body = r.json()
        check("access_token present", "access_token" in reg_body)
        check("role = owner", reg_body.get("role") == "owner")
        check("fdh_refresh cookie set", "fdh_refresh" in r.cookies or "set-cookie" in r.headers)
        reg_token = reg_body.get("access_token", "")
        reg_refresh = r.cookies.get("fdh_refresh", "")
        user_id = reg_body.get("user_id", "")
        tenant_id = reg_body.get("tenant_id", "")

        # ── Duplicate registration ─────────────────────────────────────────────
        print("\n[register duplicate]")
        r = await client.post("/api/v1/auth/register", json={
            "email": email,
            "password": password,
            "full_name": "Duplicate",
            "workspace_name": "Dupe WS",
        })
        check("duplicate email → 409", r.status_code == 409)
        check("error code CONFLICT", r.json().get("error", {}).get("code") == "CONFLICT")

        # ── Login ──────────────────────────────────────────────────────────────
        print("\n[login]")
        r = await client.post("/api/v1/auth/login", json={
            "email": email,
            "password": password,
        })
        check("POST /auth/login → 200", r.status_code == 200, str(r.json()))
        login_body = r.json()
        check("access_token present", "access_token" in login_body)
        check("user_id matches", login_body.get("user_id") == user_id)
        check("tenant_id matches", login_body.get("tenant_id") == tenant_id)
        login_token = login_body.get("access_token", "")
        login_refresh = r.cookies.get("fdh_refresh", "")
        check("fdh_refresh cookie set on login", bool(login_refresh))

        # Wrong password
        r = await client.post("/api/v1/auth/login", json={
            "email": email, "password": "WrongPass!999"
        })
        check("wrong password → 401", r.status_code == 401)

        # Unknown email
        r = await client.post("/api/v1/auth/login", json={
            "email": "nobody@nowhere.example.com", "password": "Any!Pass#123"
        })
        check("unknown email → 401", r.status_code == 401)

        # ── Refresh ────────────────────────────────────────────────────────────
        print("\n[refresh]")
        r = await client.post(
            "/api/v1/auth/refresh",
            cookies={"fdh_refresh": login_refresh},
        )
        check("POST /auth/refresh → 200", r.status_code == 200, str(r.json()))
        refresh_body = r.json()
        check("new access_token different from login", refresh_body.get("access_token") != login_token)
        new_refresh = r.cookies.get("fdh_refresh", "")
        check("new fdh_refresh cookie set", bool(new_refresh))
        check("new cookie differs from old", new_refresh != login_refresh)
        new_token = refresh_body.get("access_token", "")

        # Old refresh cookie rejected after rotation
        r = await client.post(
            "/api/v1/auth/refresh",
            cookies={"fdh_refresh": login_refresh},
        )
        check("old refresh cookie rejected → 401", r.status_code == 401)

        # Missing cookie
        r = await client.post("/api/v1/auth/refresh")
        check("no cookie → 401", r.status_code == 401)

        # ── Logout ────────────────────────────────────────────────────────────
        print("\n[logout]")
        r = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        check("POST /auth/logout → 204", r.status_code == 204)
        check("fdh_refresh cleared in response", "fdh_refresh" in r.headers.get("set-cookie", ""))

        # Refresh cookie revoked after logout
        r = await client.post(
            "/api/v1/auth/refresh",
            cookies={"fdh_refresh": new_refresh},
        )
        check("refresh cookie rejected after logout → 401", r.status_code == 401)

        # No token → 401
        r = await client.post("/api/v1/auth/logout")
        check("logout without token → 401", r.status_code == 401)

    # ── Summary ────────────────────────────────────────────────────────────────
    total = len(passed) + len(failed)
    print(f"\n{'='*50}")
    print(f"E2E Auth: {len(passed)}/{total} passed")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("ALL PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())
