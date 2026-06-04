"""
Team invitation request/response Pydantic schemas.

Engineering Specification references:
  M2 Execution Plan, Section 2.4   — team invitation flow
  M2 Execution Plan, Section 9.4   — invitation token security

Schemas:
  InvitationCreate   — POST /api/v1/companies/invitations request body
  InvitationResponse — 201 response after a successful invitation dispatch

Security notes (M2 Execution Plan, Section 9.4):
  - Invitation tokens use the same security pattern as password reset tokens:
    288-bit entropy, SHA-256 hashed before storage, 72-hour expiry.
  - The acceptance endpoint returns the same response body for valid and
    invalid tokens to prevent token existence enumeration.
  - Accepting a token sets is_active=True and joined_at on the membership row
    and clears the token fields.

Milestone: M2-Step 4
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from apps.api.models import UserRole

# ---------------------------------------------------------------------------
# Write model
# ---------------------------------------------------------------------------


class InvitationCreate(BaseModel):
    """
    Request body for POST /api/v1/companies/invitations.

    The inviting user must hold ADMIN or OWNER role in the tenant.
    If the ``email`` address is not yet registered, a new user account
    will be created when the invitation is accepted.
    """

    email: EmailStr = Field(
        description=(
            "Email address of the person being invited.  "
            "The invitation link will be sent to this address."
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
        ownership-transfer flow (not yet implemented).  Attempting to invite
        someone as OWNER raises a validation error at the schema layer so the
        route handler never needs to handle this case.
        """
        if v == UserRole.OWNER:
            raise ValueError(
                "Cannot invite a user with the OWNER role.  "
                "The OWNER role may only be assigned via ownership transfer."
            )
        return v


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


class InvitationResponse(BaseModel):
    """
    Response body for POST /api/v1/companies/invitations (HTTP 201).

    Represents the pending membership row created for the invitee.
    The raw invitation token is NEVER included in this response — it is
    delivered exclusively via email.

    ``from_attributes=True`` allows instantiation from a TenantMembership
    ORM instance.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="TenantMembership UUID for this invitation.")
    tenant_id: uuid.UUID = Field(description="Tenant workspace the invitee is joining.")
    invitee_email: str = Field(
        description="Email address of the invited user.",
    )
    role: str = Field(description="Role that will be assigned on acceptance.")
    invited_by_id: uuid.UUID = Field(
        description="UUID of the user who sent the invitation."
    )
    invitation_expires_at: datetime = Field(
        description="ISO 8601 timestamp when the invitation link expires (72 hours)."
    )
    created_at: datetime = Field(description="ISO 8601 timestamp when the invitation was created.")
