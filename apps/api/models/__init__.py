"""
ORM Models — Foundation Layer.

Engineering Specification references:
  Part 1, Section 1.2, Decision 1  — UUID v7 primary keys (time-ordered)
  Part 1, Section 1.2, Decision 2  — NUMERIC(20,4) for financial values (future models)
  Part 1, Section 1.2, Decision 3  — Shared schema multi-tenancy; tenant_id on all user-data tables
  Part 1, Section 1.2, Decision 4  — Soft delete via deleted_at TIMESTAMPTZ on all entity tables
  Part 1, Table 4                   — Audit log: append-only, no updates, 7-year retention
  Part 1, Table 5                   — Index strategy per table
  Part 2, Section 8.2, Decision 3  — Roles: OWNER, ADMIN, ANALYST, VIEWER
  Part 2, Section 8.2, Decision 4  — TOTP secret AES-encrypted in users table
  Part 2, Section 8.3              — JWT payload: sub, tid, role, exp, jti
  Part 2, Section 8.3              — Refresh token: 30-day expiry, rotation, Redis blocklist

Milestone: M1-Step 13 (ORM models)
Status: COMPLETE
"""

from __future__ import annotations

import enum
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base


# ---------------------------------------------------------------------------
# UUID v7 generator
# ---------------------------------------------------------------------------
# Engineering Spec Part 1, Section 1.2, Decision 1:
#   "UUID v7 — time-ordered UUIDs preserve B-tree insert performance while
#    providing globally unique IDs safe for distributed systems."
#
# UUID v7 bit layout (RFC 9562):
#   bits 127–80 : 48-bit Unix timestamp in milliseconds
#   bits  79–76 : version nibble = 0x7
#   bits  75–64 : rand_a (12 bits random)
#   bits  63–62 : variant = 0b10
#   bits  61– 0 : rand_b (62 bits random)
# ---------------------------------------------------------------------------

def gen_uuid7() -> uuid.UUID:
    """
    Generate a time-ordered UUID v7.

    Produces monotonically increasing IDs that sort correctly in B-tree indexes
    while remaining globally unique. Safe for use as distributed primary keys.
    """
    ms = int(time.time() * 1000)           # 48-bit millisecond timestamp
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits

    rand_a = (rand >> 68) & 0xFFF                  # 12 bits for rand_a field
    rand_b = rand & 0x3FFF_FFFF_FFFF_FFFF          # 62 bits for rand_b field

    uuid_int = (
        ((ms & 0xFFFF_FFFF_FFFF) << 80)   # bits 127–80: timestamp
        | (0x7 << 76)                      # bits  79–76: version = 7
        | (rand_a << 64)                   # bits  75–64: rand_a
        | (0b10 << 62)                     # bits  63–62: variant = 10
        | rand_b                           # bits  61– 0: rand_b
    )
    return uuid.UUID(int=uuid_int)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    """
    Role-based access control roles.

    Engineering Spec Part 2, Section 8.2, Decision 3:
      OWNER   — Full access, billing, workspace deletion.
      ADMIN   — Full data access, user management.
      ANALYST — Full data access, create and export jobs; cannot manage users.
      VIEWER  — Read-only; cannot create jobs or export.
    """
    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


# ---------------------------------------------------------------------------
# Helper: timezone-aware UTC now
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Model: Tenant
# ---------------------------------------------------------------------------

class Tenant(Base):
    """
    Workspace / organisation entity.

    Engineering Spec Part 1, Section 1.2, Decision 3:
      Every user-data table carries tenant_id for row-level isolation.
      Soft delete enforced — hard delete never permitted on system tables.

    Retention: Indefinite (Spec Part 1, Table 4 — System tables).
    """

    __tablename__ = "tenants"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Identity ─────────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Human-readable workspace name (e.g. 'Acme Capital').",
    )
    slug: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        doc="URL-safe workspace identifier. Must be globally unique.",
    )

    # ── Subscription ─────────────────────────────────────────────────────────
    plan: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="free",
        server_default=text("'free'"),
        doc="Subscription plan: free | pro | enterprise.",
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
        onupdate=_utcnow,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc="Soft delete timestamp. NULL = active. Never hard-delete system records.",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    memberships: Mapped[list[TenantMembership]] = relationship(
        "TenantMembership",
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="select",
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken",
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="select",
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        "AuditLog",
        back_populates="tenant",
        lazy="select",
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug!r} plan={self.plan!r}>"


# ---------------------------------------------------------------------------
# Model: User
# ---------------------------------------------------------------------------

class User(Base):
    """
    Platform user / authentication identity.

    Engineering Spec Part 2, Section 8.2:
      - Password stored as bcrypt hash (cost factor 12 via passlib).
      - TOTP secret stored AES-encrypted. Never store plaintext.
      - Password reset token is a short-lived (1-hour) opaque token.
      - A user can be a member of multiple tenants via TenantMembership.

    Retention: Indefinite (Spec Part 1, Table 4 — System tables).
    """

    __tablename__ = "users"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
    )

    # ── Identity ─────────────────────────────────────────────────────────────
    email: Mapped[str] = mapped_column(
        String(254),
        nullable=False,
        unique=True,
        index=True,
        doc="RFC 5321 max length 254. Unique across the platform.",
    )
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # ── Authentication ────────────────────────────────────────────────────────
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="bcrypt hash, cost factor 12, via passlib[bcrypt]. Never store plaintext.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        doc="False = deactivated account; login rejected even with valid credentials.",
    )
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    # ── TOTP / MFA ────────────────────────────────────────────────────────────
    totp_secret: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        doc=(
            "AES-256-GCM encrypted TOTP secret. NULL = MFA not configured. "
            "Decrypted in-memory only; never logged or serialised. "
            "(Spec Part 2, Section 8.2, Decision 4)"
        ),
    )
    totp_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        doc="True only after user has completed MFA setup and verified a valid TOTP code.",
    )

    # ── Password reset ────────────────────────────────────────────────────────
    password_reset_token: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        doc="Opaque reset token (hashed). Valid for 1 hour. NULL when no reset in progress.",
    )
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    # ── Activity ─────────────────────────────────────────────────────────────
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
        onupdate=_utcnow,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc="Soft delete. Deleted users cannot log in regardless of is_active.",
    )

    # ── Table-level indexes ───────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_users_email_active", "email", postgresql_where=text("deleted_at IS NULL")),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    memberships: Mapped[list[TenantMembership]] = relationship(
        "TenantMembership",
        back_populates="user",
        foreign_keys="TenantMembership.user_id",
        cascade="all, delete-orphan",
        lazy="select",
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} active={self.is_active}>"


# ---------------------------------------------------------------------------
# Model: TenantMembership
# ---------------------------------------------------------------------------

class TenantMembership(Base):
    """
    Association between a User and a Tenant with a specific RBAC role.

    A User can belong to multiple Tenants (one row per membership).
    The UNIQUE constraint on (tenant_id, user_id) ensures a user has exactly
    one role per workspace — role changes UPDATE this row, not insert a new one.

    Invitation flow:
      1. Admin calls POST /api/v1/admin/users/invite → row created with
         invitation_token and invitation_expires_at set; joined_at = NULL.
      2. Invitee clicks link → token validated, joined_at = NOW(), token cleared.

    Engineering Spec Part 2, Section 8.2, Decision 3.
    Retention: Indefinite (Spec Part 1, Table 4 — System tables).
    """

    __tablename__ = "tenant_memberships"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
    )

    # ── Foreign keys ─────────────────────────────────────────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── RBAC ─────────────────────────────────────────────────────────────────
    role: Mapped[UserRole] = mapped_column(
        SAEnum(
            UserRole,
            name="user_role",
            create_constraint=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        doc="RBAC role within this tenant. See UserRole enum.",
    )

    # ── Invitation ────────────────────────────────────────────────────────────
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc="User who sent the invitation. NULL for the founding OWNER (self-registration).",
    )
    invitation_token: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        default=None,
        doc=(
            "Opaque 256-bit invitation token (hashed). "
            "72-hour expiry per Spec Part 3, Table 19. "
            "NULL after invitation is accepted or expired."
        ),
    )
    invitation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    # ── State ────────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        doc="False = membership deactivated; user loses access to this tenant.",
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc="Timestamp when invitation was accepted. NULL = invitation pending.",
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
        onupdate=_utcnow,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # Enforce: one role per user per tenant.
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),
        # Fast lookup: all members of a tenant filtered by role (admin list, etc.)
        Index("ix_tenant_memberships_tenant_id_role", "tenant_id", "role"),
        # Fast lookup: all tenants a user belongs to (multi-tenant session context)
        Index("ix_tenant_memberships_user_id_active", "user_id", "is_active"),
        # Invitation token lookup on accept
        Index(
            "ix_tenant_memberships_invitation_token",
            "invitation_token",
            postgresql_where=text("invitation_token IS NOT NULL"),
        ),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    tenant: Mapped[Tenant] = relationship(
        "Tenant",
        back_populates="memberships",
        lazy="select",
    )
    user: Mapped[User] = relationship(
        "User",
        back_populates="memberships",
        foreign_keys=[user_id],
        lazy="select",
    )
    invited_by: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[invited_by_id],
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<TenantMembership tenant={self.tenant_id} "
            f"user={self.user_id} role={self.role.value!r}>"
        )


# ---------------------------------------------------------------------------
# Model: RefreshToken
# ---------------------------------------------------------------------------

class RefreshToken(Base):
    """
    Server-side refresh token record.

    Engineering Spec Part 2, Section 8.2, Decision 1:
      JWT access tokens (15-min expiry) + Refresh tokens (30-day expiry).
      Refresh token rotation on every use — revoke old token, issue new one.
      Compromised tokens detected via Redis blocklist on the jti field.

    Security design:
      - The raw opaque token is stored ONLY in the httpOnly cookie.
      - This table stores a SHA-256 hash of the token (never plaintext).
      - jti (JWT ID) matches the jti claim in the corresponding access token,
        allowing full session revocation by blocklisting the jti in Redis.
      - ip_address and user_agent stored for anomaly detection.

    Retention: Row is soft-expired by revoked_at; purge by expires_at.
    No soft delete column — tokens are revoked, not deleted.
    """

    __tablename__ = "refresh_tokens"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
    )

    # ── Foreign keys ─────────────────────────────────────────────────────────
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        doc="Tenant context at the time the token was issued.",
    )

    # ── Token data ────────────────────────────────────────────────────────────
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        doc="SHA-256 hex digest of the opaque refresh token. Never store the raw token.",
    )
    jti: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        unique=True,
        index=True,
        doc=(
            "JWT ID — matches jti claim in the paired access token. "
            "Used as the Redis blocklist key on logout / token rotation."
        ),
    )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Token expiry. 30 days from issuance. Reject if NOW() > expires_at.",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc=(
            "Set on logout or rotation. A non-NULL value means the token has "
            "been consumed; re-use indicates token theft."
        ),
    )

    # ── Device fingerprint ────────────────────────────────────────────────────
    ip_address: Mapped[str | None] = mapped_column(
        INET,
        nullable=True,
        default=None,
        doc="Client IP address at issuance. PostgreSQL INET type.",
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        default=None,
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
    )

    # ── Table-level indexes ───────────────────────────────────────────────────
    __table_args__ = (
        # Efficient cleanup of expired tokens by user
        Index("ix_refresh_tokens_user_id_expires_at", "user_id", "expires_at"),
        # Tenant-scoped token queries
        Index("ix_refresh_tokens_tenant_id_created_at", "tenant_id", "created_at"),
        # Find all active (non-revoked, non-expired) tokens for a user
        Index(
            "ix_refresh_tokens_user_id_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped[User] = relationship(
        "User",
        back_populates="refresh_tokens",
        lazy="select",
    )
    tenant: Mapped[Tenant] = relationship(
        "Tenant",
        back_populates="refresh_tokens",
        lazy="select",
    )

    @property
    def is_valid(self) -> bool:
        """
        True if the token has not been revoked and has not expired.
        Call this before accepting a refresh token — do not issue new tokens
        for invalid entries.
        """
        return self.revoked_at is None and datetime.now(timezone.utc) < self.expires_at

    def __repr__(self) -> str:
        return (
            f"<RefreshToken id={self.id} user={self.user_id} "
            f"expires={self.expires_at.isoformat()} revoked={self.revoked_at is not None}>"
        )


# ---------------------------------------------------------------------------
# Model: AuditLog
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """
    Immutable audit trail for all significant system events.

    Engineering Spec Part 1, Table 4:
      "Audit log: 7 years (financial compliance standard),
       append-only, no updates."

    CRITICAL constraints:
      - This table is APPEND-ONLY. Never UPDATE or soft-DELETE any row.
      - No updated_at column. No deleted_at column.
      - Hard delete is never permitted.
      - Retention: 7 years minimum (financial compliance).

    Usage:
      Written by AuditMiddleware (M1-Step15) on every mutating request.
      Also written directly by service functions for business events
      (e.g. job.created, export.downloaded, user.password_changed).

    Index strategy (Spec Part 1, Table 5):
      (tenant_id, created_at)    — tenant activity timeline queries
      (entity_type, entity_id)   — audit history for a specific resource
    """

    __tablename__ = "audit_log"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 — time-ordered, enables chronological scan without created_at index.",
    )

    # ── Tenancy ───────────────────────────────────────────────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        doc=(
            "Tenant context for this event. RESTRICT prevents tenant deletion "
            "while audit records exist (enforces 7-year retention)."
        ),
    )

    # ── Actor ────────────────────────────────────────────────────────────────
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc=(
            "User who performed the action. NULL for system-initiated events "
            "(e.g. scheduled job, background cleanup)."
        ),
    )

    # ── Event ────────────────────────────────────────────────────────────────
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc=(
            "Dot-namespaced action identifier. Examples: "
            "'user.login', 'user.logout', 'user.password_changed', "
            "'job.created', 'job.completed', 'export.downloaded', "
            "'admin.user_role_changed', 'admin.user_deactivated'."
        ),
    )

    # ── Target resource ───────────────────────────────────────────────────────
    entity_type: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        default=None,
        doc="Type of the affected resource, e.g. 'user', 'job', 'export', 'company'.",
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        default=None,
        doc="Primary key of the affected resource. NULL for non-resource events.",
    )

    # ── Request context ───────────────────────────────────────────────────────
    ip_address: Mapped[str | None] = mapped_column(
        INET,
        nullable=True,
        default=None,
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        default=None,
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        default=None,
        doc="Correlates to the X-Request-ID header for distributed tracing.",
    )

    # ── Change payload ────────────────────────────────────────────────────────
    changes: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
        doc=(
            "Before/after snapshot for mutation events. "
            "Schema: {'before': {...}, 'after': {...}}. "
            "NULL for read events and actions with no state change."
        ),
    )

    # ── Timestamp ────────────────────────────────────────────────────────────
    # NOTE: NO updated_at. NO deleted_at. This table is append-only.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
        doc="Immutable event timestamp. Index anchor for timeline queries.",
    )

    # ── Table-level indexes (Spec Part 1, Table 5) ────────────────────────────
    __table_args__ = (
        # Tenant activity timeline — primary query pattern for admin audit views
        Index("ix_audit_log_tenant_id_created_at", "tenant_id", "created_at"),
        # Resource audit history — "show me all events for job X"
        Index("ix_audit_log_entity_type_entity_id", "entity_type", "entity_id"),
        # User activity — "show me all actions by user Y in this tenant"
        Index("ix_audit_log_user_id_created_at", "user_id", "created_at"),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    tenant: Mapped[Tenant] = relationship(
        "Tenant",
        back_populates="audit_logs",
        lazy="select",
    )
    user: Mapped[User | None] = relationship(
        "User",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} action={self.action!r} "
            f"tenant={self.tenant_id} user={self.user_id}>"
        )


# ---------------------------------------------------------------------------
# Public exports — imported by Alembic env.py and repositories
# ---------------------------------------------------------------------------

__all__ = [
    "gen_uuid7",
    "UserRole",
    "Tenant",
    "User",
    "TenantMembership",
    "RefreshToken",
    "AuditLog",
]
