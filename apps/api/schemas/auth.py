"""
Auth request/response Pydantic schemas.

Engineering Specification references:
  Part 2, Section 8.2  — Password policy: 12 chars, uppercase, lowercase, digit, special char
  Part 2, Section 8.3  — JWT response payload: access_token, token_type, user_id, tenant_id, role
  Part 3, Section 11.3 — POST /auth/register and POST /auth/login request bodies

Milestone: M1-Step18 — POST /auth/register  ✓
           M1-Step19 — POST /auth/login      ✓
Status:    COMPLETE
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator

from apps.api.core.security import PasswordPolicyError, validate_password_complexity


class RegisterRequest(BaseModel):
    """
    Request body for POST /auth/register.

    Password complexity is validated synchronously by the Pydantic model.
    HIBP breach detection (async) is performed separately in the route handler.
    """

    email: EmailStr = Field(description="User email address — must be globally unique.")
    password: str = Field(
        min_length=12,
        description=(
            "Plaintext password. Minimum 12 characters; must include uppercase, "
            "lowercase, digit, and special character. Never stored — only the "
            "bcrypt hash is persisted."
        ),
    )
    full_name: str = Field(
        min_length=1,
        max_length=255,
        description="User display name (e.g. 'Alice Smith').",
    )
    workspace_name: str = Field(
        min_length=1,
        max_length=255,
        description="Name for the new tenant workspace (e.g. 'Acme Capital').",
    )

    @field_validator("password")
    @classmethod
    def _enforce_password_policy(cls, v: str) -> str:
        """
        Synchronous complexity check (Spec Part 2, Section 8.3).

        Raises ValueError (collected by Pydantic into a 422 response) if any
        rule is violated. The async HIBP breach check runs in the route handler.
        """
        try:
            validate_password_complexity(v)
        except PasswordPolicyError as exc:
            raise ValueError(exc.violations) from exc
        return v

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        """Lowercase the email address for consistent storage and lookup."""
        return v.lower()


class LoginRequest(BaseModel):
    """
    Request body for POST /auth/login.

    No password complexity validation — that is only enforced at registration.
    The email is normalised to lowercase before the database lookup.
    """

    email: EmailStr = Field(description="Registered email address.")
    password: str = Field(
        min_length=1,
        description="Account password (plaintext; never stored or logged).",
    )

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        """Lowercase for consistent lookup against the stored email."""
        return v.lower()


class AuthResponse(BaseModel):
    """
    Response body for successful authentication (register or login).

    The refresh token is NOT included here — it is delivered via an httpOnly
    cookie (``fdh_refresh``) set on the response by the route handler.
    """

    access_token: str = Field(description="Signed JWT access token. Expires in 15 minutes.")
    token_type: str = Field(default="bearer", description="Always 'bearer'.")
    user_id: uuid.UUID = Field(description="UUID of the newly created user.")
    tenant_id: uuid.UUID = Field(description="UUID of the newly created tenant workspace.")
    role: str = Field(description="RBAC role within this tenant: owner | admin | analyst | viewer")
