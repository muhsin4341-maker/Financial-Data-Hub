"""
M1 Gate — End-to-end password reset flow validation.

Exercises: Register → Forgot Password → Reset Password → Login with new password.

Usage:
    python scripts/e2e_password_reset.py

Exit code 0 = all assertions passed.
Exit code 1 = at least one assertion failed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from unittest.mock import patch

from dotenv import load_dotenv

load_dotenv()

if not os.getenv("DATABASE_URL"):
    print("ERROR: DATABASE_URL not set.")
    sys.exit(1)


async def main() -> None:
    import httpx
    from httpx import ASGITransport
    from apps.api.main import app, lifespan

    suffix = uuid.uuid4().hex[:8]
    email = f"e2e-reset-{suffix}@example.com"
    old_password = "Str0ng!Old#Pass99"
    new_password = "Str0ng!New#Pass01"

    # We patch generate_password_reset_token to return a known value
    # so the e2e test can use it without email access.
    known_token = f"e2etoken{suffix}" + "A" * (48 - 17 - len(suffix))

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

        # ── Register ───────────────────────────────────────────────────────────
        print("\n[setup: register]")
        r = await client.post("/api/v1/auth/register", json={
            "email": email,
            "password": old_password,
            "full_name": "Reset Test User",
            "workspace_name": f"Reset WS {suffix}",
        })
        check("register → 201", r.status_code == 201, str(r.json()))

        # ── Forgot Password ────────────────────────────────────────────────────
        print("\n[forgot-password]")
        with patch(
            "apps.api.routers.auth.generate_password_reset_token",
            return_value=known_token,
        ):
            r = await client.post(
                "/api/v1/auth/forgot-password", json={"email": email}
            )
        check("POST /auth/forgot-password → 200", r.status_code == 200, str(r.json()))
        check("enumeration-safe message", "message" in r.json())

        # Unknown email — same response
        r2 = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "ghost@nowhere.example.com"},
        )
        check("unknown email → 200 (same)", r2.status_code == 200)
        check("same body for unknown email", r.json() == r2.json())

        # ── Reset Password ─────────────────────────────────────────────────────
        print("\n[reset-password]")
        # Invalid token → 400
        r = await client.post("/api/v1/auth/reset-password", json={
            "token": "definitely-invalid-token-zzz",
            "new_password": new_password,
        })
        check("invalid token → 400", r.status_code == 400)
        check("error code INVALID_RESET_TOKEN",
              r.json().get("error", {}).get("code") == "INVALID_RESET_TOKEN")

        # Weak password → 422
        r = await client.post("/api/v1/auth/reset-password", json={
            "token": known_token, "new_password": "weak",
        })
        check("weak password → 422", r.status_code == 422)

        # Valid reset
        r = await client.post("/api/v1/auth/reset-password", json={
            "token": known_token,
            "new_password": new_password,
        })
        check("POST /auth/reset-password → 200", r.status_code == 200, str(r.json()))
        check("success message present", "message" in r.json())

        # Same token rejected after use (one-time link)
        r = await client.post("/api/v1/auth/reset-password", json={
            "token": known_token,
            "new_password": "AnotherStr0ng!Pass#02",
        })
        check("token cannot be reused → 400", r.status_code == 400)

        # ── Login with new password ────────────────────────────────────────────
        print("\n[post-reset login]")
        r = await client.post("/api/v1/auth/login", json={
            "email": email, "password": new_password,
        })
        check("login with NEW password → 200", r.status_code == 200, str(r.json()))

        # Old password rejected
        r = await client.post("/api/v1/auth/login", json={
            "email": email, "password": old_password,
        })
        check("login with OLD password → 401", r.status_code == 401)

    # ── Summary ────────────────────────────────────────────────────────────────
    total = len(passed) + len(failed)
    print(f"\n{'='*50}")
    print(f"E2E Password Reset: {len(passed)}/{total} passed")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("ALL PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())
