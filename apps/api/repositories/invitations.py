"""
Invitation repository — all database operations for the team invitation flow.

Engineering Specification references:
  M2 Execution Plan, Section 2.4   — team invitation flow
  M2 Execution Plan, Section 9.4   — invitation token security
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables

Repository contract (matches M1 AuthRepository conventions):
  - All public methods accept ``tenant_id`` as the first positional argument
    where applicable.
  - The session is NEVER committed here; the caller owns the transaction.
  - ``flush([obj])`` is called after writes to populate generated values.

Token lookup:
  ``get_by_token_hash`` does NOT filter by tenant_id — the token hash is
  globally unique (UUID v7 + 288-bit entropy).  Tenant ownership is verified
  by the caller after lookup.

Milestone: M2-Step 9
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import Invitation, InvitationStatus, TenantMembership, User, UserRole
from apps.api.schemas.invitations import InvitationCreate

log = structlog.get_logger(__name__)

#: Invitation validity window per M2 Execution Plan, Section 9.4.
_INVITATION_EXPIRY_HOURS: int = 72


class InvitationRepository:
    """
    Database access layer for Invitation operations.

    Instantiated per-request inside route handlers::

        repo = InvitationRepository(db)
        invitation = await repo.create(tenant_id, invited_by_id, token_hash, schema)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(
        self,
        tenant_id: uuid.UUID,
        invited_by_id: uuid.UUID,
        token_hash: str,
        schema: InvitationCreate,
    ) -> Invitation:
        """
        Persist a new Invitation in PENDING state.

        Args:
            tenant_id:    Tenant scope (from JWT payload).
            invited_by_id: User who is sending the invitation.
            token_hash:   SHA-256 hex digest of the raw URL-safe token.
            schema:       Validated ``InvitationCreate`` Pydantic model.

        Returns:
            Persisted ``Invitation`` with ``id`` and timestamps populated.
        """
        now = datetime.now(UTC)
        invitation = Invitation(
            tenant_id=tenant_id,
            invitee_email=schema.email,
            role=schema.role.value,
            token_hash=token_hash,
            status=InvitationStatus.PENDING.value,
            expires_at=now + timedelta(hours=_INVITATION_EXPIRY_HOURS),
            invited_by_id=invited_by_id,
        )
        self._session.add(invitation)
        await self._session.flush([invitation])
        log.debug(
            "invitation.repository.created",
            invitation_id=str(invitation.id),
            tenant_id=str(tenant_id),
            invitee_email=invitation.invitee_email,
            role=invitation.role,
        )
        return invitation

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_token_hash(self, token_hash: str) -> Invitation | None:
        """
        Look up an invitation by its token hash.

        The partial index on ``(token_hash) WHERE status = 'pending'`` makes
        this efficient.  Expired invitations (status still PENDING but
        expires_at in the past) are returned and must be evaluated by the
        caller using ``invitation.is_usable``.

        Token hashes are globally unique; no tenant_id filter is needed here.
        The caller must verify tenant ownership after lookup.

        Args:
            token_hash: SHA-256 hex digest of the raw URL token.

        Returns:
            ``Invitation`` ORM instance, or ``None`` if not found or not pending.
        """
        result = await self._session.execute(
            select(Invitation).where(
                Invitation.token_hash == token_hash,
                Invitation.status == InvitationStatus.PENDING.value,
            )
        )
        return result.scalar_one_or_none()

    async def get_active_by_email(
        self,
        tenant_id: uuid.UUID,
        email: str,
    ) -> Invitation | None:
        """
        Return the active (pending, non-expired) invitation for an email
        address within a tenant, if one exists.

        Used to prevent duplicate active invitations to the same address.

        Args:
            tenant_id: Tenant scope.
            email:     Lowercased invitee email.

        Returns:
            Pending non-expired ``Invitation``, or ``None``.
        """
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(Invitation).where(
                Invitation.tenant_id == tenant_id,
                Invitation.invitee_email == email,
                Invitation.status == InvitationStatus.PENDING.value,
                Invitation.expires_at > now,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        status: str | None = None,
    ) -> list[Invitation]:
        """
        Return all invitations for a tenant, optionally filtered by status.

        Args:
            tenant_id: Tenant scope.
            status:    Optional ``InvitationStatus`` value to filter by.

        Returns:
            List of ``Invitation`` ORM instances ordered newest-first.
        """
        conditions: list[Any] = [Invitation.tenant_id == tenant_id]
        if status is not None:
            conditions.append(Invitation.status == status)

        result = await self._session.execute(
            select(Invitation)
            .where(*conditions)
            .order_by(Invitation.created_at.desc())
        )
        return list(result.scalars().all())

    # ── Status transitions ────────────────────────────────────────────────────

    async def accept(
        self,
        invitation: Invitation,
        accepted_by_id: uuid.UUID,
    ) -> Invitation:
        """
        Mark the invitation as accepted.

        Does NOT create the TenantMembership — that is the router's
        responsibility so both writes share one transaction.

        Args:
            invitation:    The ``Invitation`` to transition.
            accepted_by_id: UUID of the user who is accepting.

        Returns:
            The mutated ``Invitation`` (still attached to the session).
        """
        now = datetime.now(UTC)
        invitation.status = InvitationStatus.ACCEPTED.value
        invitation.accepted_at = now
        invitation.accepted_by_id = accepted_by_id
        invitation.updated_at = now
        await self._session.flush([invitation])
        log.debug(
            "invitation.repository.accepted",
            invitation_id=str(invitation.id),
            accepted_by=str(accepted_by_id),
        )
        return invitation

    async def cancel(self, invitation: Invitation) -> Invitation:
        """
        Cancel a pending invitation.

        Only PENDING invitations can be cancelled; the caller must verify
        status before calling.

        Args:
            invitation: The ``Invitation`` to cancel.

        Returns:
            The mutated ``Invitation``.
        """
        now = datetime.now(UTC)
        invitation.status = InvitationStatus.CANCELLED.value
        invitation.updated_at = now
        await self._session.flush([invitation])
        log.debug(
            "invitation.repository.cancelled",
            invitation_id=str(invitation.id),
        )
        return invitation

    async def refresh_token(
        self,
        invitation: Invitation,
        new_token_hash: str,
    ) -> Invitation:
        """
        Replace the invitation token and reset the expiry window.

        Called by the resend endpoint to issue a new token without creating
        a new invitation row.

        Args:
            invitation:     The ``Invitation`` to refresh.
            new_token_hash: SHA-256 hex digest of the new raw token.

        Returns:
            The mutated ``Invitation``.
        """
        now = datetime.now(UTC)
        invitation.token_hash = new_token_hash
        invitation.expires_at = now + timedelta(hours=_INVITATION_EXPIRY_HOURS)
        invitation.updated_at = now
        await self._session.flush([invitation])
        log.debug(
            "invitation.repository.token_refreshed",
            invitation_id=str(invitation.id),
        )
        return invitation

    # ── Membership helper ─────────────────────────────────────────────────────

    async def create_membership(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
        invited_by_id: uuid.UUID | None,
    ) -> TenantMembership:
        """
        Create a TenantMembership for the accepted invitee.

        Idempotency: if a membership already exists for (tenant_id, user_id),
        this will raise an IntegrityError (unique constraint on the pair).
        The caller should check for existing membership before calling.

        Args:
            tenant_id:    Tenant scope.
            user_id:      Invitee's User UUID.
            role:         RBAC role string from the invitation.
            invited_by_id: Inviter's user UUID (for audit context).

        Returns:
            Persisted ``TenantMembership`` with timestamps populated.
        """
        now = datetime.now(UTC)
        membership = TenantMembership(
            tenant_id=tenant_id,
            user_id=user_id,
            role=UserRole(role),
            invited_by_id=invited_by_id,
            is_active=True,
            joined_at=now,
        )
        self._session.add(membership)
        await self._session.flush([membership])
        log.debug(
            "invitation.repository.membership_created",
            membership_id=str(membership.id),
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            role=role,
        )
        return membership

    async def get_user_by_email(self, email: str) -> User | None:
        """
        Look up an active user by email address.

        Used during invitation acceptance to determine whether the invitee
        already has an account.

        Args:
            email: Lowercased email address to search for.

        Returns:
            Active ``User`` ORM instance, or ``None`` if not found.
        """
        result = await self._session.execute(
            select(User).where(
                User.email == email,
                User.deleted_at.is_(None),
                User.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def get_membership(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> TenantMembership | None:
        """
        Check whether a user already has a membership in the tenant.

        Used before accept to prevent duplicate memberships.
        """
        result = await self._session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.user_id == user_id,
                TenantMembership.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()
