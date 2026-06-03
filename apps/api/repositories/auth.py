"""
Auth repository — all database operations for authentication flows.

Engineering Specification references:
  Part 1, Section 1.2, Decision 3  — Shared schema multi-tenancy; tenant_id on all tables
  Part 1, Section 1.2, Decision 4  — Soft delete: filter deleted_at IS NULL on user lookup
  Part 2, Section 8.2              — Refresh token: SHA-256 hash stored, raw token in cookie
  Part 1, Table 4                  — AuditLog: append-only, 7-year retention

Repository contract:
  - All methods accept and return ORM model instances.
  - All methods call ``session.flush([obj])`` after adding objects so that
    database-generated values (UUIDs, server defaults) are populated before
    subsequent operations in the same transaction.
  - The session is never committed here; the caller (route handler + get_db
    dependency) owns the transaction boundary.

Milestone: M1-Step18 — POST /auth/register  ✓
           M1-Step19 — POST /auth/login      ✓
Status:    COMPLETE
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from slugify import slugify  # python-slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import (
    AuditLog,
    RefreshToken,
    Tenant,
    TenantMembership,
    User,
    UserRole,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant_slug(name: str) -> str:
    """
    Derive a globally-unique URL-safe slug from a workspace name.

    Uses python-slugify for Unicode-safe transliteration, then appends a
    6-character random hex suffix to avoid conflicts without a DB round-trip.

    Examples:
        "Acme Capital"  → "acme-capital-a3f7b2"
        "日本語"          → "ri-ben-yu-c91d4a"
        ""              → "workspace-4e12f9"
    """
    base = slugify(name, max_length=90, separator="-") or "workspace"
    suffix = uuid.uuid4().hex[:6]
    return f"{base}-{suffix}"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class AuthRepository:
    """
    Database access layer for authentication operations.

    Instantiated per-request inside route handlers, receiving the
    ``AsyncSession`` from the ``get_db`` FastAPI dependency.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Lookups ──────────────────────────────────────────────────────────────

    async def get_user_by_email(self, email: str) -> User | None:
        """
        Fetch an active (non-deleted) user by email address.

        Used for email uniqueness checks during registration and credential
        validation during login. The partial index ``ix_users_email_active``
        makes this query efficient even on large user tables.

        Args:
            email: Lowercase email address to look up.

        Returns:
            ``User`` ORM instance if found, ``None`` otherwise.
        """
        result = await self._session.execute(
            select(User).where(
                User.email == email,
                User.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_active_membership(self, user_id: uuid.UUID) -> TenantMembership | None:
        """
        Return the user's first active TenantMembership.

        For M1, each registered user has exactly one tenant. This query is
        structured to support future multi-tenant context selection (e.g. via
        a ``X-Tenant-ID`` header) without schema changes.

        Args:
            user_id: Primary key of the authenticated user.

        Returns:
            Active ``TenantMembership`` instance, or ``None`` if the user has
            no active tenant context (deactivated membership or data integrity
            issue — both result in a 401 at the call site).
        """
        result = await self._session.execute(
            select(TenantMembership)
            .where(
                TenantMembership.user_id == user_id,
                TenantMembership.is_active.is_(True),
                TenantMembership.deleted_at.is_(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_last_login(self, user: User) -> None:
        """
        Stamp the user's ``last_login_at`` to the current UTC time.

        Called on every successful login. The ``User`` ORM instance must
        already be attached to the current session (loaded by
        ``get_user_by_email``); the change is committed atomically with the
        rest of the login transaction.

        Args:
            user: The authenticated user whose timestamp is to be updated.
        """
        user.last_login_at = datetime.now(UTC)
        # Flush so the UPDATE is part of this transaction's write set.
        await self._session.flush([user])

    # ── Creation ─────────────────────────────────────────────────────────────

    async def create_tenant(self, name: str) -> Tenant:
        """
        Persist a new Tenant workspace.

        Flushes immediately so ``tenant.id`` is populated (UUID v7 generated
        by the ORM default, not a DB sequence) before downstream operations
        that reference it as a foreign key.

        Args:
            name: Human-readable workspace name from the registration request.

        Returns:
            Persisted ``Tenant`` instance with ``id`` populated.
        """
        tenant = Tenant(name=name, slug=_make_tenant_slug(name))
        self._session.add(tenant)
        await self._session.flush([tenant])
        log.debug("auth.repository.tenant_created", tenant_id=str(tenant.id), slug=tenant.slug)
        return tenant

    async def create_user(
        self,
        email: str,
        full_name: str,
        password_hash: str,
    ) -> User:
        """
        Persist a new User with a pre-computed bcrypt password hash.

        The caller is responsible for hashing the password before passing it
        here. This method never receives or stores the plaintext password.

        Args:
            email:          Lowercase, normalised email address.
            full_name:      User display name.
            password_hash:  bcrypt hash from ``hash_password()`` in security.py.

        Returns:
            Persisted ``User`` instance with ``id`` populated.
        """
        user = User(email=email, full_name=full_name, password_hash=password_hash)
        self._session.add(user)
        await self._session.flush([user])
        log.debug("auth.repository.user_created", user_id=str(user.id))
        return user

    async def create_membership(
        self,
        tenant: Tenant,
        user: User,
        role: UserRole,
    ) -> TenantMembership:
        """
        Link a User to a Tenant with a specific RBAC role.

        For self-registration, ``invited_by_id`` is NULL (the founding OWNER
        has no inviter) and ``joined_at`` is set to NOW() (no invitation
        acceptance step — the registration IS the acceptance).

        Args:
            tenant: The target tenant workspace.
            user:   The user being granted membership.
            role:   RBAC role; OWNER for the founding registrant.

        Returns:
            Persisted ``TenantMembership`` instance.
        """
        membership = TenantMembership(
            tenant_id=tenant.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(UTC),
            invited_by_id=None,
        )
        self._session.add(membership)
        await self._session.flush([membership])
        return membership

    async def create_refresh_token(
        self,
        user: User,
        tenant_id: uuid.UUID,
        token_hash: str,
        jti: str,
        expires_at: datetime,
        ip_address: str | None,
        user_agent: str | None,
    ) -> RefreshToken:
        """
        Persist a hashed refresh token record.

        The caller computes ``token_hash = hash_refresh_token(raw_token)`` and
        stores the raw token in an httpOnly cookie. This table never contains
        the raw token.

        Args:
            user:        Token owner.
            tenant_id:   UUID of the tenant context at issuance. Accepting
                         ``uuid.UUID`` directly avoids loading the full Tenant
                         ORM object when only the ID is needed (e.g. login).
            token_hash:  SHA-256 hex digest of the raw opaque token.
            jti:         JWT ID from the paired access token — used as the Redis
                         blocklist key on logout / rotation.
            expires_at:  UTC datetime 30 days from now.
            ip_address:  Client IP for anomaly detection (nullable).
            user_agent:  Client user-agent string (nullable).

        Returns:
            Persisted ``RefreshToken`` instance.
        """
        token = RefreshToken(
            user_id=user.id,
            tenant_id=tenant_id,
            token_hash=token_hash,
            jti=jti,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(token)
        await self._session.flush([token])
        return token

    async def create_audit_log(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        *,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: uuid.UUID | None = None,
        changes: dict[str, Any] | None = None,
    ) -> None:
        """
        Append an immutable audit log entry.

        Per Spec Part 1, Table 4, the audit_log table is APPEND-ONLY — this
        method only ever adds rows; it never updates or deletes.

        Args:
            tenant_id:   Tenant context for the event.
            user_id:     Actor (the user who triggered the action).
            action:      Dot-namespaced event identifier, e.g. "user.registered".
            entity_type: Type of the affected resource, e.g. "user".
            entity_id:   Primary key of the affected resource.
            ip_address:  Client IP address.
            user_agent:  Client user-agent string.
            request_id:  X-Request-ID UUID for distributed tracing correlation.
            changes:     Before/after snapshot: ``{"before": {...}, "after": {...}}``.
        """
        entry = AuditLog(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            changes=changes,
        )
        self._session.add(entry)
        # Not flushed individually — committed atomically with the transaction.
