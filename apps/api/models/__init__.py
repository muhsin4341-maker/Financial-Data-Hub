"""
ORM Models.

Engineering Specification references:
  Part 1, Section 1.2, Decision 1  — UUID v7 primary keys (time-ordered)
  Amendment V1.2, Section 1.1      — NUMERIC(26,2) for absolute monetary values (revenue,
                                     assets, liabilities, net_income, cash_flow lines);
                                     NUMERIC(38,10) for per-share metrics, financial ratios,
                                     and forex translation coefficients
  Part 1, Section 1.2, Decision 3  — Shared schema multi-tenancy; tenant_id on all user-data tables
  Part 1, Section 1.2, Decision 4  — Soft delete via deleted_at TIMESTAMPTZ on all entity tables
  Part 1, Table 4                   — Audit log: append-only, no updates, 7-year retention
  Part 1, Table 5                   — Index strategy per table
  Part 2, Section 8.2, Decision 3  — Roles: OWNER, ADMIN, ANALYST, VIEWER
  Part 2, Section 8.2, Decision 4  — TOTP secret AES-encrypted in users table
  Part 2, Section 8.3              — JWT payload: sub, tid, role, exp, jti
  Part 2, Section 8.3              — Refresh token: 30-day expiry, rotation, Redis blocklist
  M2 Execution Plan, Section 5     — companies and financial_jobs tables
  M3 Execution Plan, Section 6.1   — source_configs table
  M3 Execution Plan, M3.3          — filings table
  Amendment V1.2, Section 1.1/1.2  — FinancialLineItem precision tiers and point-in-time schema
  Amendment V1.2, Section 2.1      — ReportingStandard ENUM on FinancialLineItem
  Amendment V1.2, Section 7.2      — Partial index WHERE is_restated = FALSE
  Amendment V1.2, Section 1.3/3    — DailyFXRate: NUMERIC(38,10) daily closing rates for
                                     dual-pass FX translation (BS spot / IS+CF period average)

Milestones:
  M1-Step 13 — Foundation models (Tenant, User, TenantMembership, RefreshToken, AuditLog)
  M2-Step 2  — Domain models (Company, FinancialJob)
  M3.1       — Source Registry (SourceConfig)
  M3.3       — Filing Models (Filing)
  Amendment V1.2 sweep — FinancialLineItem (pre-provisioned for M4)
  M5.1       — DailyFXRate (daily exchange rate store for CurrencyTranslationEngine)
"""

from __future__ import annotations

import enum
import os
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
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
    ms = int(time.time() * 1000)  # 48-bit millisecond timestamp
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits

    rand_a = (rand >> 68) & 0xFFF  # 12 bits for rand_a field
    rand_b = rand & 0x3FFF_FFFF_FFFF_FFFF  # 62 bits for rand_b field

    uuid_int = (
        ((ms & 0xFFFF_FFFF_FFFF) << 80)  # bits 127–80: timestamp
        | (0x7 << 76)  # bits  79–76: version = 7
        | (rand_a << 64)  # bits  75–64: rand_a
        | (0b10 << 62)  # bits  63–62: variant = 10
        | rand_b  # bits  61– 0: rand_b
    )
    return uuid.UUID(int=uuid_int)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class UserRole(enum.StrEnum):
    """
    Role-based access control roles.

    Engineering Spec Part 2, Section 8.2, Decision 3:
      OWNER   — Full access, billing, workspace deletion.
      ADMIN   — Full data access, user management.
      ANALYST — Full data access, create and export jobs; cannot manage users.
      VIEWER  — Read-only; cannot create jobs or export.

    ``enum.StrEnum`` (Python 3.11+) makes each member a ``str`` subclass,
    so ``UserRole.ANALYST == "analyst"`` is True without an extra ``.value``
    dereference. Replaces the legacy ``(str, enum.Enum)`` pattern.
    """

    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class JobStatus(enum.StrEnum):
    """
    Lifecycle states for a FinancialJob.

    M2 Execution Plan, Section 2.3.3 — Job Status Transitions:
      PENDING   → QUEUED     Celery task accepted by broker.
      QUEUED    → RUNNING    Worker picks up the task.
      RUNNING   → COMPLETED  Extraction + export finished successfully.
      RUNNING   → FAILED     Unhandled exception in worker.
      PENDING/QUEUED/RUNNING → CANCELLED  API cancel request received.

    Stored as VARCHAR(50) (not a DB-level ENUM) so future statuses can be
    added without an ALTER TYPE migration.  Application code uses this enum
    for type-safe comparisons; the column default is the string literal
    'pending'.
    """

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Helper: timezone-aware UTC now
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(UTC)


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
    companies: Mapped[list[Company]] = relationship(  # M2
        "Company",
        back_populates="tenant",
        cascade="all, delete-orphan",
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
        return self.revoked_at is None and datetime.now(UTC) < self.expires_at

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
# Model: Company  (M2-Step 2)
# ---------------------------------------------------------------------------


class Company(Base):
    """
    A company being tracked within a tenant workspace.

    Each tenant maintains its own list of companies to analyse.  The same
    real-world company (same ticker / CIK) can exist as separate rows in
    different tenants — no global deduplication at this layer.

    Ticker uniqueness is scoped to (tenant_id, ticker) rather than globally
    unique.  This prevents false conflicts when two tenants independently add
    the same company, and avoids a migration amendment if a ticker is ever
    re-listed on a different exchange.  See M2 Execution Plan Risk R-08.

    Retention: Indefinite within the tenant workspace.
    Engineering Spec Part 1, Section 1.2, Decision 3 — tenant_id isolation.
    Engineering Spec Part 1, Section 1.2, Decision 4 — soft delete.
    """

    __tablename__ = "companies"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Tenancy ───────────────────────────────────────────────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        doc="Owning tenant.  All company data is isolated per tenant.",
    )

    # ── Identity ─────────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Full legal or trading name of the company.",
    )
    ticker: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc=(
            "Stock ticker symbol (e.g. 'AAPL').  Unique within the tenant "
            "workspace — see uq_companies_tenant_ticker constraint."
        ),
    )
    cik: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        default=None,
        doc=(
            "SEC Central Index Key — 10-digit zero-padded identifier used to "
            "query SEC EDGAR.  NULL until resolved by the acquisition service."
        ),
    )

    # ── Classification ────────────────────────────────────────────────────────
    exchange: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        default=None,
        doc="Primary listing exchange (e.g. 'NYSE', 'NASDAQ', 'OTC').",
    )
    sector: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        default=None,
        doc="GICS sector classification.",
    )
    industry: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        default=None,
        doc="GICS industry classification.",
    )

    # ── Profile ───────────────────────────────────────────────────────────────
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        doc="Free-text company description.",
    )
    website: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        default=None,
        doc="Corporate website URL.",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        doc=(
            "False = company is hidden from normal queries but retained for "
            "historical job records.  Not the same as soft delete."
        ),
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
        doc=(
            "Soft delete timestamp.  NULL = active.  Soft-deleted companies are "
            "excluded from normal list queries but their job records are retained."
        ),
    )

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # One ticker per tenant workspace (not globally unique — Risk R-08).
        UniqueConstraint(
            "tenant_id",
            "ticker",
            name="uq_companies_tenant_ticker",
        ),
        # Unique CIK within a tenant when provided.
        UniqueConstraint(
            "tenant_id",
            "cik",
            name="uq_companies_tenant_cik",
            # Only enforce when cik IS NOT NULL; NULL values are excluded from
            # UNIQUE constraints in PostgreSQL, so this is naturally handled.
        ),
        # Primary tenant lookup — list all companies for a workspace.
        Index("ix_companies_tenant_id", "tenant_id"),
        # Fast active-record filter (most common query excludes soft-deleted rows).
        Index(
            "ix_companies_tenant_id_active",
            "tenant_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # GIN trigram index — powers ILIKE fuzzy name search (pg_trgm extension).
        # Extension is already present from migration 001 (CREATE EXTENSION pg_trgm).
        Index(
            "gin_companies_name",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    tenant: Mapped[Tenant] = relationship(
        "Tenant",
        back_populates="companies",
        lazy="select",
    )
    jobs: Mapped[list[FinancialJob]] = relationship(
        "FinancialJob",
        back_populates="company",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Company id={self.id} ticker={self.ticker!r} "
            f"name={self.name!r} tenant={self.tenant_id}>"
        )


# ---------------------------------------------------------------------------
# Model: FinancialJob  (M2-Step 2)
# ---------------------------------------------------------------------------


class FinancialJob(Base):
    """
    A unit of work that extracts and exports financial data for a company.

    Lifecycle (M2 Execution Plan, Section 2.3.3):
      PENDING   → created via API; not yet dispatched to Celery.
      QUEUED    → Celery task ID assigned; waiting for a worker.
      RUNNING   → Worker has picked up the task and is processing.
      COMPLETED → Extraction + export finished; result_url populated.
      FAILED    → Worker raised an unhandled exception; error_message set.
      CANCELLED → Cancelled by API request before or during processing.

    Document storage:
      document_url: S3 key of the source document uploaded by the user.
      result_url:   S3 key of the Excel export (populated in M6).

    Retention: Indefinite within the tenant workspace for audit purposes.
    Engineering Spec Part 1, Section 1.2, Decision 3 — tenant_id isolation.
    Engineering Spec Part 1, Section 1.2, Decision 4 — NO soft delete on jobs.
      Jobs are terminal-state records; they are never deleted, only cancelled.
    """

    __tablename__ = "financial_jobs"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Tenancy ───────────────────────────────────────────────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        doc="Owning tenant.  All job data is isolated per tenant.",
    )

    # ── Subject ───────────────────────────────────────────────────────────────
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        doc="Company this job extracts data for.",
    )

    # ── Actor ────────────────────────────────────────────────────────────────
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc=(
            "User who created the job.  SET NULL on user deletion so the job "
            "record is retained for audit and billing purposes."
        ),
    )

    # ── Job classification ────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=JobStatus.PENDING.value,
        server_default=text("'pending'"),
        doc=(
            "Current lifecycle state.  Use JobStatus enum for comparisons. "
            "Stored as VARCHAR(50) to allow future extension without ALTER TYPE."
        ),
    )
    job_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc=(
            "Identifies the extraction template to run.  "
            "Examples: 'sec_10k_annual', 'sec_10q_quarterly'."
        ),
    )
    fiscal_year: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=None,
        doc="Fiscal year being extracted (e.g. 2023).  NULL for multi-year jobs.",
    )

    # ── Document references ───────────────────────────────────────────────────
    document_url: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
        default=None,
        doc=(
            "S3 key of the source document uploaded for this job.  "
            "Set when the client confirms upload-complete.  "
            "Format: {tenant_id}/jobs/{job_id}/{filename}"
        ),
    )
    result_url: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
        default=None,
        doc=(
            "S3 key of the generated Excel export.  "
            "Populated by the export service in M6.  NULL until then."
        ),
    )

    # ── Error state ───────────────────────────────────────────────────────────
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        doc="Human-readable error description when status = FAILED.",
    )

    # ── Celery integration ────────────────────────────────────────────────────
    celery_task_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        doc=(
            "Celery task ID assigned when the job is dispatched.  "
            "Used to revoke in-progress tasks on cancel.  NULL until dispatched."
        ),
    )

    # ── Timing ───────────────────────────────────────────────────────────────
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc="Set when the worker begins processing (status → RUNNING).",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc=(
            "Set when the job reaches a terminal state "
            "(COMPLETED, FAILED, or CANCELLED)."
        ),
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
    # NOTE: No deleted_at — jobs are terminal-state records; they are cancelled,
    # not deleted.  See class docstring.

    # ── Table-level indexes ───────────────────────────────────────────────────
    __table_args__ = (
        # Primary tenant-scoped query — list all jobs for a workspace.
        Index("ix_financial_jobs_tenant_id", "tenant_id"),
        # Company-scoped query — list all jobs for a specific company.
        Index("ix_financial_jobs_company_id", "company_id"),
        # Status filter — find all pending/running jobs for monitoring.
        Index("ix_financial_jobs_status", "status"),
        # Creator lookup — "show me jobs I created".
        Index("ix_financial_jobs_created_by", "created_by"),
        # Timeline query — most-recent-first ordering for job list API.
        Index("ix_financial_jobs_created_at", "created_at"),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    tenant: Mapped[Tenant] = relationship(
        "Tenant",
        lazy="select",
    )
    company: Mapped[Company] = relationship(
        "Company",
        back_populates="jobs",
        lazy="select",
    )
    creator: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[created_by],
        lazy="select",
    )

    @property
    def is_terminal(self) -> bool:
        """True if the job has reached a final state that cannot be changed."""
        return self.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )

    @property
    def is_cancellable(self) -> bool:
        """True if the job can still be cancelled via the API."""
        return self.status in (
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
        )

    def __repr__(self) -> str:
        return (
            f"<FinancialJob id={self.id} type={self.job_type!r} "
            f"status={self.status!r} company={self.company_id}>"
        )


# ---------------------------------------------------------------------------
# Model: Invitation  (M2-Step 9)
# ---------------------------------------------------------------------------


class InvitationStatus(enum.StrEnum):
    """
    Lifecycle states for a team invitation.

    M2 Execution Plan, Section 2.4 — Team Invitation Flow:
      PENDING   → invitation created; email dispatched; awaiting acceptance.
      ACCEPTED  → invitee accepted; TenantMembership created.
      CANCELLED → cancelled by an admin before acceptance.
      EXPIRED   → past expires_at; not yet accepted.  Not stored proactively
                  in the database — evaluated at query time by comparing
                  expires_at to NOW().  The column value remains PENDING until
                  the row is explicitly cancelled or accepted.

    Stored as VARCHAR(20) (not a DB-level ENUM) for the same reason as
    JobStatus — future additions require no ALTER TYPE migration.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Invitation(Base):
    """
    A pending, accepted, or cancelled team invitation.

    Invitations are tenant-scoped.  An admin sends an invitation to an email
    address; the invitee receives a single-use URL containing the raw token.
    On acceptance, a TenantMembership is created linking the invitee's User
    account to the tenant with the specified role.

    Token security (M2 Execution Plan, Section 9.4):
      - 288 bits of entropy (secrets.token_bytes(36) → base64url).
      - SHA-256 hash stored in ``token_hash``; raw token delivered by email only.
      - 72-hour expiry; single use enforced by status transition PENDING → ACCEPTED.

    No soft delete: invitations are cancelled, not deleted.

    Engineering Spec Part 1, Section 1.2, Decision 3 — tenant_id isolation.
    Milestone: M2-Step 9
    """

    __tablename__ = "invitations"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        comment="UUID v7 primary key.",
    )

    # ── Tenancy ───────────────────────────────────────────────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant workspace the invitee is joining.",
    )

    # ── Subject ───────────────────────────────────────────────────────────────
    invitee_email: Mapped[str] = mapped_column(
        String(254),
        nullable=False,
        comment="Email address of the person being invited (lowercased).",
    )
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="RBAC role assigned on acceptance: viewer|analyst|admin.",
    )

    # ── Token ────────────────────────────────────────────────────────────────
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="SHA-256 hex digest of the raw invitation token.",
    )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=InvitationStatus.PENDING.value,
        server_default=text("'pending'"),
        comment="pending | accepted | cancelled.",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="UTC expiry timestamp (72 hours from creation).",
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="Set when the invitee accepts.",
    )

    # ── Actor ────────────────────────────────────────────────────────────────
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who sent the invitation.",
    )
    accepted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        comment="User who accepted the invitation.",
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

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # Unique active invitation per email per tenant.
        # Allows re-inviting after cancellation by checking status in the repo.
        Index("ix_invitations_tenant_id", "tenant_id"),
        Index("ix_invitations_invitee_email", "invitee_email"),
        Index("ix_invitations_status", "status"),
        # Fast token lookup (primary query on accept/validate).
        Index(
            "ix_invitations_token_hash",
            "token_hash",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    tenant: Mapped[Tenant] = relationship("Tenant", lazy="select")
    invited_by: Mapped[User | None] = relationship(
        "User", foreign_keys=[invited_by_id], lazy="select"
    )
    accepted_by: Mapped[User | None] = relationship(
        "User", foreign_keys=[accepted_by_id], lazy="select"
    )

    @property
    def is_expired(self) -> bool:
        """True if the invitation has passed its expiry timestamp."""
        return datetime.now(UTC) > self.expires_at

    @property
    def is_usable(self) -> bool:
        """True if the invitation can still be accepted."""
        return self.status == InvitationStatus.PENDING and not self.is_expired

    def __repr__(self) -> str:
        return (
            f"<Invitation id={self.id} email={self.invitee_email!r} "
            f"status={self.status!r} tenant={self.tenant_id}>"
        )


# ---------------------------------------------------------------------------
# Model: SourceConfig  (M3.1 — Source Registry)
# ---------------------------------------------------------------------------


class ProviderType(enum.StrEnum):
    """
    Category of a data source provider.

    M3 Execution Plan, Section 6.1:
      REGULATORY — Government regulatory filing systems (SEC EDGAR, MCA India).
      EXCHANGE   — Stock exchange data sources (NSE, BSE, NYSE, NASDAQ).
      MANUAL     — Human-uploaded documents; no automated acquisition.
      BROKER     — Broker/financial data providers (future integration).

    Stored as VARCHAR(50) (not a DB-level ENUM) so future provider types can be
    added without an ALTER TYPE migration. Application code uses this enum for
    type-safe comparisons; the column value is the raw string.
    """

    REGULATORY = "regulatory"
    EXCHANGE = "exchange"
    MANUAL = "manual"
    BROKER = "broker"


class SourceConfig(Base):
    """
    Global registry of data acquisition sources.

    Each row describes one provider the acquisition pipeline can reach out to.
    Examples: SEC EDGAR (US regulatory), NSE (India exchange), MANUAL_UPLOAD.

    Design decisions:
      - No tenant_id: source configs are platform-wide system records; every
        tenant uses the same SEC EDGAR endpoint, for example.
      - No deleted_at: sources are disabled (is_active=False) rather than
        hard-deleted. This preserves audit history and prevents FK breakage
        when filing_records references source_config_id (added in migration 005).
      - code is immutable after creation (enforced at service layer). It serves
        as the stable machine-readable identifier used by acquisition workers.
      - config JSONB carries source-specific extra data (API endpoints, flags,
        etc.) without requiring schema changes for each new source.

    M3 Execution Plan, Section 6.1 — source_configs table design.
    Milestone: M3.1 — Source Registry.
    """

    __tablename__ = "source_configs"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Identity ─────────────────────────────────────────────────────────────
    code: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc=(
            "Machine-readable identifier. Always uppercased. Globally unique. "
            "Examples: SEC_EDGAR, NSE, BSE, MANUAL_UPLOAD. "
            "Immutable after creation — enforced by SourceRegistryService."
        ),
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Human-readable display name. Example: 'SEC EDGAR', 'NSE India'.",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        doc="Optional free-text description of the data source.",
    )

    # ── Classification ────────────────────────────────────────────────────────
    provider_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc=(
            "Category of provider. Stored as VARCHAR(50) for forward compatibility. "
            "Use ProviderType enum for comparisons. "
            "Values: regulatory | exchange | manual | broker."
        ),
    )
    country_code: Mapped[str | None] = mapped_column(
        String(5),
        nullable=True,
        default=None,
        doc="ISO 3166-1 alpha-2 country code. NULL = multi-country or global scope.",
    )

    # ── Connection ────────────────────────────────────────────────────────────
    base_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        default=None,
        doc="Root URL used by the acquisition service for HTTP requests.",
    )

    # ── Rate control ─────────────────────────────────────────────────────────
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=60,
        server_default=text("60"),
        doc="Maximum HTTP requests per minute allowed to this source.",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        doc=(
            "When False, the acquisition service skips this source entirely. "
            "Prefer disabling over deleting — see SourceRegistryService.disable()."
        ),
    )

    # ── Flexible config ───────────────────────────────────────────────────────
    config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
        doc=(
            "Source-specific configuration blob (JSONB). "
            "SEC EDGAR example: full_text_search_url, submission_url, "
            "required_user_agent, ixbrl_available_from_year, primary_form_type."
        ),
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
    # NOTE: No deleted_at — sources are disabled (is_active=False), not deleted.
    # NOTE: No tenant_id — source configs are platform-wide system records.

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # Unique constraint on code — creates an implicit unique index in PostgreSQL.
        # Named uq_source_configs_code for explicit, consistent naming.
        # Listed in _KNOWN_INLINE_CONSTRAINTS in env.py to suppress Alembic false-positives.
        UniqueConstraint("code", name="uq_source_configs_code"),
        # Remaining indexes for common query patterns.
        Index("ix_source_configs_provider_type", "provider_type"),
        Index("ix_source_configs_is_active", "is_active"),
        Index("ix_source_configs_country_code", "country_code"),
    )

    def __repr__(self) -> str:
        return (
            f"<SourceConfig id={self.id} code={self.code!r} "
            f"provider_type={self.provider_type!r} active={self.is_active}>"
        )


# ---------------------------------------------------------------------------
# Model: Filing  (M3.3 — Filing Models)
# ---------------------------------------------------------------------------


class FilingType(enum.StrEnum):
    """
    Recognised SEC filing form types.

    M3 Execution Plan, M3.3 — Filing Models:
      10K    — Annual report (10-K)
      10Q    — Quarterly report (10-Q)
      8K     — Current report (8-K)
      DEF14A — Proxy statement (DEF 14A)
      20F    — Foreign private issuer annual report (20-F)
      6K     — Foreign private issuer current report (6-K)

    Stored as VARCHAR(20) (not a DB-level ENUM) for forward compatibility.
    New filing types can be added without an ALTER TYPE migration.
    Application code uses this enum for validation; the column value is
    the raw string (e.g. '10-K', '10-Q').
    """

    K10 = "10-K"
    Q10 = "10-Q"
    K8 = "8-K"
    DEF14A = "DEF 14A"
    F20 = "20-F"
    K6 = "6-K"


class FilingStatus(enum.StrEnum):
    """
    Lifecycle states for a Filing record.

    M3 Execution Plan, M3.3 — Filing Models:
      DISCOVERED  → Filing found via SEC EDGAR; metadata captured; no document yet.
      DOWNLOADING → Document download dispatched to the document fetcher.
      DOWNLOADED  → Primary document stored in S3; ready for extraction.
      FAILED      → Download or processing error; error details in filing_metadata.

    Stored as VARCHAR(50) (not a DB ENUM) so future states can be added
    without an ALTER TYPE migration.  Application code uses this enum for
    type-safe comparisons; the column default is the string literal 'discovered'.
    """

    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    FAILED = "failed"


class Filing(Base):
    """
    A SEC filing record discovered and tracked by the acquisition pipeline.

    Each row represents one regulatory filing (10-K, 10-Q, 8-K, etc.) as
    submitted to SEC EDGAR.  Filings are global system records — they are not
    scoped per tenant.  The same 10-K filing (identified by its globally unique
    accession number) is stored once regardless of how many tenants track
    the same company.

    Lifecycle:
      DISCOVERED  → created when the SEC EDGAR integration discovers a new filing.
      DOWNLOADING → document fetch dispatched by the acquisition worker.
      DOWNLOADED  → primary document stored in S3; ready for extraction pipeline.
      FAILED      → download/processing error; details stored in filing_metadata.

    FK behaviour:
      company_id      is SET NULL when the linked company record is deleted.
      source_config_id is SET NULL when the linked source config is deleted.
      Both FKs are nullable to decouple filing discovery from company resolution.

    Design decisions:
      - No tenant_id: filings are global; tenant isolation happens at the job layer.
      - No deleted_at: filings are never soft-deleted; status captures lifecycle.
      - accession_number is immutable after creation (enforced at service layer).

    M3 Execution Plan, M3.3 — Filing Models.
    Milestone: M3.3
    """

    __tablename__ = "filings"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Foreign keys (both nullable) ──────────────────────────────────────────
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc=(
            "Linked company record. NULL until the filing is matched to a "
            "tenant company (populated in M3.4 / M3.7 acquisition workers)."
        ),
    )
    source_config_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_configs.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc=(
            "Data source that provided this filing. "
            "NULL if source is not yet determined."
        ),
    )

    # ── Filing identity ───────────────────────────────────────────────────────
    filing_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc=(
            "SEC form type. Stored as VARCHAR(20) for forward compatibility. "
            "Use FilingType enum for validation. "
            "Values: '10-K' | '10-Q' | '8-K' | 'DEF 14A' | '20-F' | '6-K'."
        ),
    )
    accession_number: Mapped[str] = mapped_column(
        String(25),
        nullable=False,
        doc=(
            "SEC EDGAR accession number — globally unique filing identifier. "
            "Format: 'XXXXXXXXXX-YY-ZZZZZZ' (e.g. '0000320193-23-000077'). "
            "Immutable after creation — enforced by FilingService."
        ),
    )

    # ── Dates ─────────────────────────────────────────────────────────────────
    filing_date: Mapped[date] = mapped_column(
        Date(),
        nullable=False,
        doc="Date on which the filing was submitted to SEC EDGAR.",
    )
    period_end_date: Mapped[date | None] = mapped_column(
        Date(),
        nullable=True,
        default=None,
        doc=(
            "End date of the fiscal period covered by this filing. "
            "NULL for 8-K and other non-periodic filings."
        ),
    )

    # ── Company identifiers ───────────────────────────────────────────────────
    cik: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        doc=(
            "SEC Central Index Key — 10-digit zero-padded identifier. "
            "Captured directly from the SEC EDGAR API response. "
            "Used for CIK-based queries before company_id is resolved."
        ),
    )
    ticker: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        default=None,
        doc=(
            "Ticker symbol at the time of filing. "
            "May be NULL if not available from the data source."
        ),
    )

    # ── Content references ────────────────────────────────────────────────────
    title: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        default=None,
        doc="Human-readable filing title from SEC EDGAR (e.g. '10-K for FY2023').",
    )
    filing_url: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
        default=None,
        doc="URL to the filing index page on SEC EDGAR (EDGAR filing detail page).",
    )
    document_url: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
        default=None,
        doc=(
            "URL to the primary filing document (e.g. the 10-K.htm file). "
            "Populated by the document fetcher in M3.5."
        ),
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=FilingStatus.DISCOVERED.value,
        server_default=text("'discovered'"),
        doc=(
            "Current lifecycle state. Use FilingStatus enum for comparisons. "
            "Stored as VARCHAR(50) for forward compatibility. "
            "Values: 'discovered' | 'downloading' | 'downloaded' | 'failed'."
        ),
    )

    # ── Fiscal period coordinates (M3.3) ─────────────────────────────────────
    # Both nullable — existing filings migrated without these values.
    fiscal_year: Mapped[int | None] = mapped_column(
        Integer(),
        nullable=True,
        default=None,
        doc=(
            "4-digit fiscal year (e.g. 2024). NULL until populated by the "
            "acquisition worker during XBRL extraction (M3.5)."
        ),
    )
    fiscal_period: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        default=None,
        doc=(
            "Fiscal period label: 'FY', 'Q1', 'Q2', 'Q3', or 'Q4'. "
            "NULL until populated by the acquisition worker (M3.5)."
        ),
    )

    # ── Flexible metadata ─────────────────────────────────────────────────────
    filing_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
        doc=(
            "Arbitrary metadata from the SEC EDGAR API response that does not "
            "fit standard columns (e.g. form_type, items, file_number, "
            "act, file_number_href, film_number). Also stores error details "
            "when status = 'failed'."
        ),
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
    # NOTE: No deleted_at — filings are not soft-deleted; use status transitions.
    # NOTE: No tenant_id — filings are global system records.

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # BR-1: Accession number is globally unique across all filings.
        # Named uq_filings_accession_number for explicit, consistent naming.
        # Listed in _KNOWN_INLINE_CONSTRAINTS in env.py to suppress false-positives.
        UniqueConstraint("accession_number", name="uq_filings_accession_number"),
        # Indexes for common query patterns (see migration 005 notes).
        Index("ix_filings_company_id", "company_id"),
        Index("ix_filings_source_config_id", "source_config_id"),
        Index("ix_filings_filing_type", "filing_type"),
        Index("ix_filings_filing_date", "filing_date"),
        Index("ix_filings_cik", "cik"),
        Index("ix_filings_ticker", "ticker"),
        Index("ix_filings_status", "status"),
        Index("ix_filings_created_at", "created_at"),
        # M3.3 — composite index for fiscal-period queries.
        # Query: "all filings for company X in fiscal year 2024, period Q3".
        Index("ix_filings_company_fiscal", "company_id", "fiscal_year", "fiscal_period"),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    company: Mapped[Company | None] = relationship(
        "Company",
        lazy="select",
    )
    source_config: Mapped[SourceConfig | None] = relationship(
        "SourceConfig",
        lazy="select",
    )
    # One-to-many: a filing can contain multiple documents (primary HTML,
    # XBRL instance, PDF, exhibits, etc.).  Populated after M3.5 fetching.
    documents: Mapped[list["FilingDocument"]] = relationship(
        "FilingDocument",
        back_populates="filing",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="FilingDocument.created_at",
    )

    @property
    def is_terminal(self) -> bool:
        """True if the filing has reached a final lifecycle state."""
        return self.status in (
            FilingStatus.DOWNLOADED,
            FilingStatus.FAILED,
        )

    def __repr__(self) -> str:
        return (
            f"<Filing id={self.id} type={self.filing_type!r} "
            f"accession={self.accession_number!r} status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# Model: FilingDocument  (M3.3 — Filing Records & Documents)
# ---------------------------------------------------------------------------


class FilingDocument(Base):
    """
    An individual document attached to a Filing record.

    One Filing has many FilingDocuments.  Examples of documents within a
    single 10-K filing: the primary HTML narrative, the XBRL instance
    document, inline XBRL, R-files, PDF version, exhibit attachments.

    Lifecycle:
      On creation  — filing_id, document_type, source_url populated.
                     s3_key and file_hash are NULL.
      After fetch  — file_hash populated (SHA-256 of raw bytes).
      After upload — s3_key populated (S3 object key in the documents bucket).

    FK behaviour:
      filing_id → filings.id  CASCADE DELETE
      Documents are owned by their filing; deleting the filing removes all
      associated FilingDocument rows.

    Unique constraint:
      (filing_id, source_url) — a given URL can appear at most once per filing.
      Prevents duplicate document records when a filing is re-scanned.

    Design decisions:
      - document_type is VARCHAR(50), not a DB ENUM — forward compatible.
        Known values: 'XBRL_XML', 'PRIMARY_HTML', 'PDF', 'EXHIBIT', 'R_FILE'.
      - file_hash is a 64-character lowercase hex string (SHA-256).
      - s3_key is NULL until the document has been fetched and stored.
      - No tenant_id — documents are global like their parent filings.

    M3 Execution Plan, M3.3 — Filing Records & Documents Database Layer.
    Milestone: M3.3
    """

    __tablename__ = "filing_documents"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Foreign key ───────────────────────────────────────────────────────────
    filing_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("filings.id", ondelete="CASCADE"),
        nullable=False,
        doc=(
            "Parent Filing record. CASCADE DELETE — removing the filing "
            "removes all its document rows."
        ),
    )

    # ── Document classification ───────────────────────────────────────────────
    document_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc=(
            "Document role within the filing. Stored as VARCHAR(50) for "
            "forward compatibility. Known values: 'XBRL_XML', 'PRIMARY_HTML', "
            "'PDF', 'EXHIBIT', 'R_FILE'."
        ),
    )

    # ── Source location ───────────────────────────────────────────────────────
    source_url: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
        doc=(
            "Original URL from which the document is fetched "
            "(SEC EDGAR CDN link). Unique per (filing_id, source_url)."
        ),
    )

    # ── Storage location ──────────────────────────────────────────────────────
    s3_key: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
        default=None,
        doc=(
            "S3 object key after upload to the documents bucket. "
            "NULL until the document has been fetched and stored (M3.6)."
        ),
    )

    # ── Integrity hash ────────────────────────────────────────────────────────
    file_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
        doc=(
            "SHA-256 hex digest of the raw file bytes (lowercase, 64 chars). "
            "NULL until the document has been fetched (M3.5)."
        ),
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

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # Prevent duplicate documents when a filing is re-scanned.
        UniqueConstraint(
            "filing_id",
            "source_url",
            name="uq_filing_documents_filing_source",
        ),
        # Primary access: "all documents for filing X".
        Index("ix_filing_documents_filing_id", "filing_id"),
        # Cross-filing type queries: "all XBRL_XML docs pending upload".
        Index("ix_filing_documents_document_type", "document_type"),
        # S3 key lookup — sparse (NULL rows excluded by the partial index
        # created in migration 011; ORM index definition for schema parity).
        Index("ix_filing_documents_s3_key", "s3_key"),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    filing: Mapped["Filing"] = relationship(
        "Filing",
        back_populates="documents",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<FilingDocument id={self.id} type={self.document_type!r} "
            f"filing_id={self.filing_id} s3_key={self.s3_key!r}>"
        )


# ---------------------------------------------------------------------------
# Model: AcquisitionJob  (M3.7 — Acquisition Jobs)
# ---------------------------------------------------------------------------


class AcquisitionJobStatus(enum.StrEnum):
    """
    Lifecycle states for an AcquisitionJob.

    M3 Execution Plan, M3.7 — Acquisition Jobs:
      PENDING   → Job created; not yet dispatched to Celery.
      RUNNING   → Worker is executing the acquisition workflow.
      COMPLETED → All steps finished successfully.
      FAILED    → Unrecoverable error; error_message populated.

    Stored as VARCHAR(50) for forward compatibility (not a DB ENUM).
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AcquisitionJob(Base):
    """
    A platform-level job that acquires SEC filing documents for one company.

    Lifecycle:
      PENDING   → created by API or Celery task; ticker is known.
      RUNNING   → worker resolved the company and is processing filings.
      COMPLETED → all eligible filings discovered, fetched, and stored.
      FAILED    → unrecoverable error; error_message contains details.

    Progress counters:
      filings_discovered — total filings returned by SECEdgarSource.
      filings_new        — filings not previously seen in the DB.
      documents_fetched  — documents successfully downloaded.
      documents_stored   — documents successfully persisted to storage.

    Design decisions:
      - No tenant_id: acquisition jobs are platform-wide operations.
        The same filing is never fetched twice regardless of tenant.
      - No deleted_at: jobs reach terminal states; they are never deleted.
      - ticker is captured at creation; cik and company_name are populated
        after the company resolver runs.

    M3 Execution Plan, M3.7 — Acquisition Jobs.
    Milestone: M3.7
    """

    __tablename__ = "acquisition_jobs"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Company identity (ticker at creation; CIK resolved at runtime) ────────
    ticker: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc="Stock ticker symbol supplied at job creation (e.g. 'AAPL').",
    )
    cik: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        default=None,
        doc=(
            "SEC CIK resolved during execution. "
            "NULL until the company resolver has run."
        ),
    )
    company_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        doc="Full company name from SEC EDGAR. NULL until resolved.",
    )

    # ── Classification ────────────────────────────────────────────────────────
    job_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="sec_filing_discovery",
        server_default=text("'sec_filing_discovery'"),
        doc=(
            "Acquisition strategy identifier. "
            "Values: 'sec_filing_discovery'. "
            "Stored as VARCHAR(50) for forward compatibility."
        ),
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=AcquisitionJobStatus.PENDING.value,
        server_default=text("'pending'"),
        doc=(
            "Current lifecycle state. "
            "Values: 'pending' | 'running' | 'completed' | 'failed'. "
            "Stored as VARCHAR(50) for forward compatibility."
        ),
    )

    # ── Error ─────────────────────────────────────────────────────────────────
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        doc="Human-readable error description when status = 'failed'.",
    )

    # ── Progress counters ─────────────────────────────────────────────────────
    filings_discovered: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        doc="Total filings returned by SECEdgarSource.discover_filings().",
    )
    filings_new: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        doc="New filings not previously present in the filings table.",
    )
    documents_fetched: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        doc="Documents successfully downloaded by SECFilingDocumentFetcher.",
    )
    documents_stored: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        doc="Documents successfully persisted by DocumentStorageService.",
    )

    # ── Timing ───────────────────────────────────────────────────────────────
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc="Set when the worker begins execution (status → RUNNING).",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        doc="Set when the job reaches a terminal state (COMPLETED or FAILED).",
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
    # NOTE: No deleted_at — jobs reach terminal states; they are never deleted.
    # NOTE: No tenant_id — acquisition jobs are platform-wide system records.

    # ── Table-level indexes ───────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_acquisition_jobs_status", "status"),
        Index("ix_acquisition_jobs_ticker", "ticker"),
        Index("ix_acquisition_jobs_cik", "cik"),
        Index("ix_acquisition_jobs_created_at", "created_at"),
    )

    @property
    def is_terminal(self) -> bool:
        """True if the job has reached a final state that cannot change."""
        return self.status in (
            AcquisitionJobStatus.COMPLETED,
            AcquisitionJobStatus.FAILED,
        )

    def __repr__(self) -> str:
        return (
            f"<AcquisitionJob id={self.id} ticker={self.ticker!r} "
            f"status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# Model: StoredDocument  (M3.6 — S3 Storage Pipeline)
# ---------------------------------------------------------------------------


class StoredDocument(Base):
    """
    Metadata record for a filing document persisted to storage.

    Each row tracks one stored copy of a primary filing document, capturing
    where the content lives (bucket/key for S3, or filesystem path for local
    storage) along with content verification metadata.

    Design decisions:
      - No tenant_id: stored documents are global system records, mirroring the
        Filing model they are linked to.
      - No deleted_at: records are deleted when the document is purged.
      - accession_number is the stable lookup key; the FK to filings is
        advisory and can be NULL during the acquisition pipeline before filing
        records are created.
      - content_hash enables integrity verification at retrieval time and
        deduplication at store time.

    M3 Execution Plan, M3.6 — S3 Storage Pipeline.
    Milestone: M3.6
    """

    __tablename__ = "stored_documents"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Filing reference (advisory FK, nullable) ──────────────────────────────
    filing_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("filings.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc=(
            "Linked Filing record. NULL is permitted to allow storage metadata "
            "to be created before the corresponding Filing row is committed."
        ),
    )

    # ── Filing identity ───────────────────────────────────────────────────────
    accession_number: Mapped[str] = mapped_column(
        String(25),
        nullable=False,
        doc=(
            "SEC EDGAR accession number — globally unique filing identifier. "
            "Format: 'XXXXXXXXXX-YY-ZZZZZZ'. Used as the primary lookup key "
            "for retrieval and deduplication. Immutable after creation."
        ),
    )

    # ── Storage location ──────────────────────────────────────────────────────
    storage_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc="Backend type: 'local' (development) or 's3' (production / MinIO).",
    )
    bucket_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        doc="S3 bucket name. NULL for local filesystem storage.",
    )
    object_key: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
        doc=(
            "Storage key within the backend. "
            "S3: object key (e.g. 'filings/000032019324000009/document.html'). "
            "Local: relative path under the configured storage root."
        ),
    )

    # ── Content verification ──────────────────────────────────────────────────
    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc="SHA-256 hex digest of the stored content (UTF-8 encoded). 64 chars.",
    )
    content_length: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Byte length of the stored content (UTF-8 encoded).",
    )
    mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="MIME type of the stored document, e.g. 'text/html', 'text/plain'.",
    )

    # ── Timing ───────────────────────────────────────────────────────────────
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="UTC timestamp when the document was first written to storage.",
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

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # BR: One storage record per accession number.
        UniqueConstraint("accession_number", name="uq_stored_documents_accession_number"),
        Index("ix_stored_documents_filing_id", "filing_id"),
        Index("ix_stored_documents_content_hash", "content_hash"),
        Index("ix_stored_documents_storage_type", "storage_type"),
        Index("ix_stored_documents_stored_at", "stored_at"),
    )

    # ── Relationship ─────────────────────────────────────────────────────────
    filing: Mapped["Filing | None"] = relationship(
        "Filing",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<StoredDocument id={self.id} accession={self.accession_number!r} "
            f"storage_type={self.storage_type!r} key={self.object_key!r}>"
        )


# ---------------------------------------------------------------------------
# Model: FinancialLineItem  (Amendment V1.2 — core financial data table)
# ---------------------------------------------------------------------------


class ReportingStandard(enum.StrEnum):
    """
    Accounting standard under which a financial value was reported.

    Amendment V1.2, Section 2.1 — Reporting Standard Isolation:
      Mandatory on every financial row. Drives which validation rules
      (VAL-001/002/003, XST-001/002/003) are applied during the
      dual-dimension validation pass (Amendment V1.2 §5).

      US_GAAP — United States Generally Accepted Accounting Principles.
      IFRS    — International Financial Reporting Standards (IASB).
      IND_AS  — Indian Accounting Standards (converged with IFRS).
    """

    US_GAAP = "US_GAAP"
    IFRS = "IFRS"
    IND_AS = "IND_AS"


class FinancialLineItem(Base):
    """
    A single extracted financial data point for a company/period/field.

    This is the canonical table for all financial data in the platform.
    Every row is immutable once written — restatements create new rows
    with a later filing_date and is_restated=True, leaving the original
    row intact for audit purposes (ASC 250 / IAS 8 / Ind AS 8).

    Amendment V1.2 compliance:
      §1.1  NUMERIC(26,2) for absolute monetary values (value_usd,
            value_reported); NUMERIC(38,10) for FX coefficients (fx_rate_used).
      §1.2  Point-in-time architecture: filing_date + is_restated + composite
            unique constraint replacing static overwrite pattern.
      §2.1  reporting_standard ENUM NOT NULL on every row.
      §2.2  Sign convention: inflows positive, outflows negative. Sign
            inversion applied by ingestion layer before INSERT.
      §4.2  source_file_hash links to stored_documents.content_hash for
            SOX 404 / IT Act 2000 cryptographic audit trail.
      §7.2  Partial index WHERE is_restated = FALSE on composite key
            for current-value query performance.
      §8.1  derived_expression_formula records algebraic derivation string
            for computed/imputed values.

    No tenant_id: financial line items are global pipeline outputs.
    Tenant isolation happens at the job/query layer (like filings).

    Milestone: M4 (table pre-provisioned by Amendment V1.2 compliance sweep)
    """

    __tablename__ = "financial_line_items"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
        doc="UUID v7 primary key — time-ordered for B-tree performance.",
    )

    # ── Company and period identity ───────────────────────────────────────────
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        doc="UUID of the company this data point belongs to.",
    )
    fiscal_year: Mapped[int] = mapped_column(
        SmallInteger(),
        nullable=False,
        doc="Fiscal year (e.g. 2023).",
    )
    fiscal_period: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        doc="Fiscal period: Q1 | Q2 | Q3 | Q4 | FY.",
    )

    # ── Reporting standard (Amendment V1.2 §2.1) ─────────────────────────────
    reporting_standard: Mapped[ReportingStandard] = mapped_column(
        SAEnum(
            ReportingStandard,
            name="reporting_standard",
            create_constraint=False,  # type created in migration 008
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        doc="Accounting standard: US_GAAP | IFRS | IND_AS. Drives validation rules.",
    )

    # ── Reporting framework — free-text filing regime (migration 014) ─────────
    # Complements the strict ReportingStandard enum with a free-text label that
    # identifies the specific regulatory filing programme.  This is intentionally
    # NOT an enum so that new frameworks (SEBI_BRSR, MCA_AOC, EU_CSRD, …) can be
    # added without a DDL migration (ALTER TYPE).
    #
    # Example values:
    #   "SEC_10K"   — US SEC annual report (US_GAAP)
    #   "SEC_10Q"   — US SEC quarterly report (US_GAAP)
    #   "SEBI_BRSR" — Indian SEBI Business Responsibility and Sustainability Report
    #   "MCA_AOC"   — Indian MCA Annual Return (IND_AS)
    #   "IFRS_AR"   — Generic IFRS annual report
    #   "EU_CSRD"   — European Corporate Sustainability Reporting Directive
    reporting_framework: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        default=None,
        doc=(
            "Regulatory filing framework (free-text, VARCHAR 50).  "
            "Examples: SEC_10K, SEC_10Q, SEBI_BRSR, MCA_AOC, IFRS_AR, EU_CSRD.  "
            "Added in migration 014 — nullable to preserve backwards compatibility."
        ),
    )

    # ── Point-in-time fields (Amendment V1.2 §1.2) ───────────────────────────
    filing_date: Mapped[date] = mapped_column(
        Date(),
        nullable=False,
        doc=(
            "Date the containing document was filed. "
            "Restatements insert a new row with a later filing_date — "
            "original rows are never overwritten (ASC 250 / IAS 8 / Ind AS 8)."
        ),
    )
    is_restated: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=text("FALSE"),
        doc="True when this row supersedes an earlier filing for the same period.",
    )

    # ── Canonical field identifier ────────────────────────────────────────────
    canonical_field: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="XBRL concept tag or normalised field name (e.g. 'us-gaap:Revenues').",
    )

    # ── Statement classification ──────────────────────────────────────────────
    statement_type: Mapped[str] = mapped_column(
        String(2),
        nullable=False,
        doc="IS = Income Statement | BS = Balance Sheet | CF = Cash Flow.",
    )

    # ── Absolute monetary values — NUMERIC(26,2) (Amendment V1.2 §1.1) ───────
    value_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(26, 2),
        nullable=True,
        doc=(
            "Value translated to USD. NUMERIC(26,2). "
            "Sign convention: inflows positive, outflows negative (×−1 at ingestion)."
        ),
    )
    value_reported: Mapped[Decimal | None] = mapped_column(
        Numeric(26, 2),
        nullable=True,
        doc="Value in the original reported currency. NUMERIC(26,2).",
    )
    reported_currency: Mapped[str | None] = mapped_column(
        String(3),
        nullable=True,
        default=None,
        doc="ISO 4217 currency code of the original reported value (e.g. 'USD', 'INR').",
    )

    # ── Per-share / ratio / FX — NUMERIC(38,10) (Amendment V1.2 §1.1) ────────
    fx_rate_used: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 10),
        nullable=True,
        doc=(
            "FX translation coefficient. NUMERIC(38,10). "
            "Balance Sheet: spot rate on period_end_date (ASC 830 / IAS 21). "
            "Income Statement & Cash Flow: weighted average rate over period "
            "(Amendment V1.2 §3 dual-pass split translation)."
        ),
    )

    # ── Audit traceability (Amendment V1.2 §4.2 / SOX 404 / IT Act 2000) ─────
    source_file_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc=(
            "SHA-256 hex digest of the source document. "
            "Links back to stored_documents.content_hash for immutable audit trail."
        ),
    )

    # ── Imputed expression (Amendment V1.2 §8.1) ─────────────────────────────
    derived_expression_formula: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="Algebraic string of source tags used to compute/impute this value.",
    )

    # ── Extraction provenance ─────────────────────────────────────────────────
    extraction_method: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        doc="How the value was extracted: xbrl | pdf | ocr | ai.",
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

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # Amendment V1.2 §1.2 — point-in-time composite unique constraint.
        # Restatements add a new row with a later filing_date; original rows
        # are never updated or deleted (ASC 250 / IAS 8 / Ind AS 8).
        UniqueConstraint(
            "company_id",
            "fiscal_year",
            "fiscal_period",
            "canonical_field",
            "filing_date",
            name="uq_financial_line_items_point_in_time",
        ),
        Index("ix_financial_line_items_company_id", "company_id"),
        Index(
            "ix_financial_line_items_company_period",
            "company_id",
            "fiscal_year",
            "fiscal_period",
        ),
        Index("ix_financial_line_items_source_file_hash", "source_file_hash"),
        # Amendment V1.2 §7.2 — partial index for current (non-restated) values.
        Index(
            "ix_financial_line_items_current",
            "company_id",
            "fiscal_year",
            "fiscal_period",
            "canonical_field",
            postgresql_where=text("is_restated = FALSE"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FinancialLineItem company={self.company_id} "
            f"year={self.fiscal_year} period={self.fiscal_period!r} "
            f"field={self.canonical_field!r} filing_date={self.filing_date}>"
        )


# ---------------------------------------------------------------------------
# Model: DailyFXRate  (M5.1)
# ---------------------------------------------------------------------------


class DailyFXRate(Base):
    """
    Daily closing exchange rate for one currency pair on one calendar date.

    This table is the concrete backing store for the FXRateRepository Protocol
    defined in services/extraction/normaliser/currency.py and consumed by
    services/currency/translator.py::HistoricalFXRateProvider.

    Dual-pass FX translation (Amendment V1.2 §1.3 / §3):
      The CurrencyTranslationEngine requires two distinct rate types:
        - Balance Sheet (BS):  closing SPOT rate on ``period_end_date``.
        - Income Statement (IS) and Cash Flow (CF): arithmetic weighted
          average of daily closing rates over [period_start, period_end].
      Both passes are computed from this table — no separate "average" column
      is stored; the engine queries a date range and averages in Python.

    Rate convention:
      ``rate`` expresses "how many ``to_currency`` units equal 1 ``from_currency``
      unit".  Example: from_currency='INR', to_currency='USD', rate=0.0120187293
      means 1 INR = 0.0120187293 USD.  This is consistent with the
      HistoricalFXRateProvider contract in translator.py (line 189 comment).

    Precision contract (Amendment V1.2 §1.1):
      ``rate`` uses NUMERIC(38, 10) — the same precision tier as
      FinancialLineItem.fx_rate_used — to ensure no rounding loss when
      translating large absolute monetary values (NUMERIC(26,2)).

    Source notes:
      Rates are ingested by the FX data-load task (M5.2 repository +
      future M5.6 Celery task) from an external provider such as:
        - European Central Bank (ECB) daily XML feed (free, EUR-based)
        - Open Exchange Rates API (USD-based)
        - Frankfurter.app (ECB mirror, REST API)
      USD → INR, EUR → USD, GBP → USD, JPY → USD are the primary pairs
      required for SEC and Indian regulatory filings.

    No tenant_id:
      Exchange rates are global system reference data, not tenant-owned.
      All tenants share the same rate table (consistent with source_configs,
      filings, and financial_line_items which are also global).

    No UUID PK:
      The composite primary key (rate_date, from_currency, to_currency) is
      the natural, globally unique identifier for a daily rate.  A surrogate
      UUID PK would add index overhead without any benefit — the composite key
      IS the business key for all query patterns.

    Indexes:
      ix_daily_fx_rates_from_to_date — covers the primary query:
        "give me all rates for USD→INR between date A and date B"
        used by HistoricalFXRateProvider._get_range() for period-average.
      ix_daily_fx_rates_rate_date    — covers date-range scans for all pairs
        on a given date (bulk ingestion deduplication check).

    Milestone: M5.1 — DailyFXRate ORM model
    """

    __tablename__ = "daily_fx_rates"

    # ── Composite primary key ─────────────────────────────────────────────────
    # (rate_date, from_currency, to_currency) is the natural business key.
    # PostgreSQL enforces the constraint as a B-tree index on all three columns.
    rate_date: Mapped[date] = mapped_column(
        Date(),
        primary_key=True,
        nullable=False,
        doc=(
            "Calendar date of the closing exchange rate.  "
            "Weekends and public holidays are typically absent; "
            "HistoricalFXRateProvider uses a 5-day look-back to fill gaps."
        ),
    )
    from_currency: Mapped[str] = mapped_column(
        String(3),
        primary_key=True,
        nullable=False,
        doc=(
            "ISO 4217 source currency code (e.g. 'INR', 'EUR', 'GBP').  "
            "3-character uppercase string — no validation at DB level; "
            "the repository layer enforces valid ISO 4217 codes."
        ),
    )
    to_currency: Mapped[str] = mapped_column(
        String(3),
        primary_key=True,
        nullable=False,
        doc=(
            "ISO 4217 target currency code (e.g. 'USD').  "
            "Most ingestion pipelines use USD as the universal target "
            "to support multi-currency portfolio comparison."
        ),
    )

    # ── Rate ──────────────────────────────────────────────────────────────────
    rate: Mapped[Decimal] = mapped_column(
        Numeric(38, 10),
        nullable=False,
        doc=(
            "Closing exchange rate: units of to_currency per 1 unit of from_currency.  "
            "Example: from_currency='INR', to_currency='USD', rate=0.0120187293 "
            "means 1 INR = 0.0120187293 USD.  "
            "NUMERIC(38, 10) per Amendment V1.2 §1.1 — no floating-point loss."
        ),
    )

    # ── Audit timestamps ──────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
        doc="UTC timestamp when this rate row was first inserted.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
        onupdate=_utcnow,
        doc=(
            "UTC timestamp of the most recent update.  "
            "Rates may be revised if a source provider corrects a historical value."
        ),
    )

    # ── Table-level constraints and indexes ───────────────────────────────────
    __table_args__ = (
        # Primary query pattern: "daily rates for pair X between date A and B"
        # Used by HistoricalFXRateProvider._get_range() for period-average.
        # Column order (from_currency, to_currency, rate_date) puts the
        # high-selectivity equality filters first; rate_date range last.
        Index(
            "ix_daily_fx_rates_from_to_date",
            "from_currency",
            "to_currency",
            "rate_date",
        ),
        # Secondary pattern: "all pairs on date X" — bulk ingestion dedup.
        Index("ix_daily_fx_rates_rate_date", "rate_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<DailyFXRate {self.rate_date} "
            f"{self.from_currency}/{self.to_currency}={self.rate}>"
        )


# ---------------------------------------------------------------------------
# Model: ValidationResultRecord  (M4 Step 4 — validation_results table)
# ---------------------------------------------------------------------------


class ValidationResultRecord(Base):
    """
    ORM read model for the ``validation_results`` table.

    Written by ``FinancialLineItemWriter._persist_validation_result()`` using
    raw SQLAlchemy Core SQL (not the ORM) during the ingestion pipeline.
    This ORM class provides a typed, queryable interface for the API layer
    (``GET /api/v1/jobs/{id}/validation``) without touching the write path.

    One row per ingestion run.  Multiple runs may exist for the same job_id if
    the pipeline is retried; callers should take the most recent row (ORDER BY
    created_at DESC LIMIT 1).

    Column notes:
      confidence_score  — integer in [0, 100].  Starts at 100; each CRITICAL
                          finding deducts 25 pts; each WARNING deducts 5 pts.
      findings          — JSONB list of {rule_id, severity, message, expected,
                          actual, delta} objects produced by ValidationEngine.
      deductions        — JSONB list of {rule_id, points, reason} objects from
                          ConfidenceScore.deductions.

    Milestone: M4 Step 4 / M4.4F — Validation QA Dashboard
    """

    __tablename__ = "validation_results"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
    )

    # ── Source identity ───────────────────────────────────────────────────────
    accession_number: Mapped[str] = mapped_column(String(25), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    fiscal_year: Mapped[int | None] = mapped_column(SmallInteger(), nullable=True)
    fiscal_period: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # ── Job linkage ───────────────────────────────────────────────────────────
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("financial_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Summary counters ──────────────────────────────────────────────────────
    items_validated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    is_exportable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    critical_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    warning_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    confidence_score: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("100")
    )

    # ── Granular findings (JSONB) ─────────────────────────────────────────────
    findings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    deductions: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    # ── Human-readable summary ────────────────────────────────────────────────
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timestamp ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("NOW()"),
    )

    def __repr__(self) -> str:
        return (
            f"<ValidationResultRecord id={self.id} job={self.job_id} "
            f"score={self.confidence_score} exportable={self.is_exportable}>"
        )


# ---------------------------------------------------------------------------
# Enum: ExcelExportStatus  (D2 — Async Excel Export Pipeline)
# ---------------------------------------------------------------------------


class ExcelExportStatus(enum.StrEnum):
    """
    Lifecycle states for an asynchronous Excel export job.

    D2 — Async Excel Export Pipeline:
      PENDING    → Record created; task dispatched to QUEUE_EXPORT.
      GENERATING → Worker picked up the task; workbook assembly in progress.
      SUCCESS    → Workbook uploaded to S3; pre-signed URL recorded.
      FAILED     → Worker encountered an unrecoverable exception; error_message
                   contains the traceback snippet.

    Stored as VARCHAR(20) so future states can be added without ALTER TYPE.
    """

    PENDING = "PENDING"
    GENERATING = "GENERATING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Model: ExcelExportJob  (D2 — excel_export_jobs table)
# ---------------------------------------------------------------------------


class ExcelExportJob(Base):
    """
    Tracks a single asynchronous Excel workbook generation request.

    Lifecycle:
      POST /api/v1/jobs/{job_id}/export/async
        → INSERT with status=PENDING
        → generate_excel_export_task.apply_async(...)

      Worker picks up task:
        → UPDATE status=GENERATING

      Worker completes successfully:
        → s3.put_object(...)
        → UPDATE status=SUCCESS, s3_key=..., download_url=...

      Worker raises exception:
        → UPDATE status=FAILED, error_message=<traceback[:2000]>

    Frontend polls GET /api/v1/jobs/export/{id}/status until SUCCESS/FAILED.

    Milestone: D2 — Async Excel Export Pipeline
    """

    __tablename__ = "excel_export_jobs"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=gen_uuid7,
    )

    # ── Tenancy ───────────────────────────────────────────────────────────────
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Job linkage ───────────────────────────────────────────────────────────
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("financial_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Actor ─────────────────────────────────────────────────────────────────
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Lifecycle status ──────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ExcelExportStatus.PENDING,
        server_default=text("'PENDING'"),
    )

    # ── Output artefact ───────────────────────────────────────────────────────
    s3_key: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        default=None,
    )
    download_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )

    # ── Error diagnostics ─────────────────────────────────────────────────────
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
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
        onupdate=_utcnow,
        server_default=text("NOW()"),
    )

    def __repr__(self) -> str:
        return (
            f"<ExcelExportJob id={self.id} job={self.job_id} "
            f"status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# Public exports — imported by Alembic env.py and repositories
# ---------------------------------------------------------------------------

__all__ = [
    "gen_uuid7",
    "UserRole",
    "JobStatus",
    "InvitationStatus",
    "ProviderType",
    "FilingType",
    "FilingStatus",
    "Tenant",
    "User",
    "TenantMembership",
    "RefreshToken",
    "AuditLog",
    "Company",
    "FinancialJob",
    "Invitation",
    "SourceConfig",
    "Filing",
    "FilingDocument",
    "AcquisitionJobStatus",
    "AcquisitionJob",
    "StoredDocument",
    "ReportingStandard",
    "FinancialLineItem",
    "DailyFXRate",
    "ValidationResultRecord",
    "ExcelExportStatus",
    "ExcelExportJob",
]
