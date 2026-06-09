"""
Team invitation request/response Pydantic schemas.

Engineering Specification references:
  M2 Execution Plan, Section 2.4   — team invitation flow
  M2 Execution Plan, Section 9.4   — invitation token security

Schemas:
  InvitationCreate      — POST /api/v1/invitations request body
  InvitationResponse    — full read model (create / validate / resend)
  InvitationAccept      — POST /api/v1/invitations/{token}/accept body
  InvitationValidate    — GET  /api/v1/invitations/{token} (public)

Security notes (M2 Execution Plan, Section 9.4):
  - Invitation tokens: 288-bit entropy, SHA-256 hashed before storage,
    72-hour expiry, single-use enforced by status transition.
  - The raw token is NEVER included in any API response — delivered by
    email only.
  - GET /invitations/{token} returns the same 404 shape for unknown and
    expired tokens to prevent token existence enumeration.

Milestone: M2-Step 9
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from apps.api.models import UserRole

# ---------------------------------------------------------------------------
# Write models
# ---------------------------------------------------------------------------


class InvitationCreate(BaseModel):
    """
    Request body for POST /api/v1/invitations.

    The inviting user must hold ADMIN or OWNER role in the tenant.
    The email is lowercased for consistent storage and lookup.
    OWNER cannot be assigned via invitation.
    """

    email: EmailStr = Field(
        description=(
            "Email address of the person being invited.  "
            "The invitation link is sent to this address."
        ),
        examples=["bob@example.com"],
    )
    role: UserRole = Field(
        description=(
            "RBAC role to assign when the invitation is accepted.  "
            "Must be VIEWER, ANALYST, or ADMIN — cannot invite as OWNER."
        ),
        examples=["analyst", "viewer"],
    )

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        """Lowercase for consistent storage and lookup."""
        return v.lower()

    @field_validator("role")
    @classmethod
    def _reject_owner_invitation(cls, v: UserRole) -> UserRole:
        """
        The OWNER role cannot be assigned via invitation.

        Workspace ownership can only be transferred through an explicit
        ownership-transfer flow (not yet implemented).
        """
        if v == UserRole.OWNER:
            raise ValueError(
                "Cannot invite a user with the OWNER role.  "
                "The OWNER role may only be assigned via ownership transfer."
            )
        return v


class InvitationAccept(BaseModel):
    """
    Optional request body for POST /api/v1/invitations/{token}/accept.

    When the invitee does not yet have an account, they must supply their
    full name and a password so the account can be created atomically during
    acceptance.  Existing users omit this body (or supply an empty object).

    The endpoint checks whether a user with invitee_email already exists:
      - Exists   → body fields ignored; the authenticated JWT user accepts.
      - Not found → full_name + password required to create a new account.
    """

    full_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Required when creating a new account during acceptance.",
    )
    password: str | None = Field(
        default=None,
        min_length=12,
        description=(
            "Required when creating a new account during acceptance.  "
            "Must meet the platform password complexity policy."
        ),
    )


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


class InvitationResponse(BaseModel):
    """
    Full response schema for an Invitation record.

    Returned by:
      - POST /api/v1/invitations          (201)
      - GET  /api/v1/invitations/{token}  (200)
      - POST /api/v1/invitations/{token}/resend (200)

    The raw invitation token is NEVER present in this schema.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Invitation UUID.")
    tenant_id: uuid.UUID = Field(description="Tenant workspace being joined.")
    invitee_email: str = Field(description="Email address of the invitee.")
    role: str = Field(description="Role assigned on acceptance.")
    status: str = Field(
        description="Lifecycle status: pending | accepted | cancelled | expired."
    )
    expires_at: datetime = Field(
        description="UTC timestamp when the token expires (72 hours from creation)."
    )
    accepted_at: datetime | None = Field(
        description="UTC timestamp when the invitation was accepted.  None if pending."
    )
    invited_by_id: uuid.UUID | None = Field(
        description="UUID of the user who sent the invitation."
    )
    created_at: datetime = Field(description="ISO 8601 creation timestamp (UTC).")
    updated_at: datetime = Field(description="ISO 8601 last-update timestamp (UTC).")


class InvitationValidateResponse(BaseModel):
    """
    Public response for GET /api/v1/invitations/{token}.

    Used by the frontend to display invitation details (workspace name,
    role, inviter) before the invitee accepts.  Omits sensitive actor IDs.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Invitation UUID.")
    invitee_email: str = Field(description="Email address this invitation is for.")
    role: str = Field(description="Role that will be assigned on acceptance.")
    status: str = Field(description="Current invitation status.")
    expires_at: datetime = Field(description="Token expiry timestamp (UTC).")
    is_usable: bool = Field(
        description=(
            "True if the invitation can still be accepted "
            "(status=pending and not expired)."
        )
    )
