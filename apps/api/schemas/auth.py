"""
Auth request/response Pydantic schemas.

Engineering Specification references:
  Part 2, Section 8.2  — Password policy: 12 chars, uppercase, lowercase, digit, special char
  Part 2, Section 8.3  — JWT response payload: access_token, token_type, user_id, tenant_id, role
  Part 3, Section 11.3 — POST /auth/* request bodies

Milestone: M1-Step18 — POST /auth/register        ✓
           M1-Step19 — POST /auth/login            ✓
           M1-Step22 — POST /auth/forgot-password  ✓
           M1-Step23 — POST /auth/reset-password   ✓
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


class ForgotPasswordRequest(BaseModel):
    """
    Request body for POST /auth/forgot-password.

    Only the email address is needed — no authentication is required because
    the user has lost access to their account. The email is normalised to
    lowercase before lookup.
    """

    email: EmailStr = Field(description="Email address associated with the account.")

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        return v.lower()


class MessageResponse(BaseModel):
    """
    Generic single-message response for operations with no domain payload.

    Used by forgot-password and similar endpoints where the response must
    be identical regardless of internal outcome (to prevent enumeration).
    """

    message: str = Field(description="Human-readable status message.")


class ResetPasswordRequest(BaseModel):
    """
    Request body for POST /auth/reset-password.

    ``token`` is the raw URL-safe value from the password reset email link.
    The endpoint hashes it internally before the database lookup — the raw
    token never touches the database.

    ``new_password`` is validated against the same complexity policy as
    registration: minimum 12 characters, uppercase, lowercase, digit, and
    special character.
    """

    token: str = Field(
        min_length=1,
        description="Raw reset token from the email link query parameter.",
    )
    new_password: str = Field(
        min_length=12,
        description=(
            "New plaintext password. Must meet complexity requirements: "
            "12+ chars, uppercase, lowercase, digit, special character."
        ),
    )

    @field_validator("new_password")
    @classmethod
    def _enforce_password_policy(cls, v: str) -> str:
        """Apply the same complexity rules as registration."""
        try:
            validate_password_complexity(v)
        except PasswordPolicyError as exc:
            raise ValueError(exc.violations) from exc
        return v


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
