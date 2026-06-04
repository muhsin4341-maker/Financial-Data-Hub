"""001 initial schema — all M1 foundation tables.

Revision ID: 7f3a2b9c1d8e
Revises:
Create Date: 2026-06-04

Tables created:
  - tenants
  - users
  - tenant_memberships
  - refresh_tokens
  - audit_log

PostgreSQL extensions:
  - pg_trgm (trigram indexes for fuzzy company search — M2+)
  - pgcrypto (UUID generation fallback)

Enum types:
  - user_role: owner | admin | analyst | viewer

Engineering Specification references:
  Part 1, Section 1.2, Decision 1  — UUID v7 PKs (generated in Python; no DB default needed)
  Part 1, Section 1.2, Decision 2  — NUMERIC(20,4) for financial values (future tables)
  Part 1, Section 1.2, Decision 3  — Shared schema; tenant_id on all user-data tables
  Part 1, Section 1.2, Decision 4  — Soft delete via deleted_at TIMESTAMPTZ
  Part 1, Table 4                   — AuditLog: append-only, 7-year retention, ONDELETE RESTRICT
  Part 1, Table 5                   — Index strategy per table
  Part 2, Section 8.2, Decision 3  — RBAC roles: OWNER > ADMIN > ANALYST > VIEWER

Milestone: M0 gate (docker compose up + alembic upgrade head)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

# revision identifiers
revision: str = "7f3a2b9c1d8e"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── PostgreSQL extensions ──────────────────────────────────────────────────
    # pg_trgm: trigram similarity for fuzzy company name search (used from M2).
    # pgcrypto: provides gen_random_uuid() as a DB-level fallback.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── Table: tenants ────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column(
            "plan",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'free'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    # ── Table: users ──────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("totp_secret", sa.String(255), nullable=True),
        sa.Column(
            "totp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("password_reset_token", sa.String(255), nullable=True),
        sa.Column(
            "password_reset_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    # Standard email lookup index
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    # Partial index for active-user email lookups (Spec Part 1, Table 5)
    op.create_index(
        "ix_users_email_active",
        "users",
        ["email"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ── Table: tenant_memberships ─────────────────────────────────────────────
    op.create_table(
        "tenant_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "admin", "analyst", "viewer",
                name="user_role",
            ),
            nullable=False,
        ),
        sa.Column(
            "invited_by_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("invitation_token", sa.String(255), nullable=True),
        sa.Column(
            "invitation_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # Spec: one role per user per tenant
        sa.UniqueConstraint(
            "tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"
        ),
        # Spec: invitation token must be globally unique
        sa.UniqueConstraint(
            "invitation_token", name="uq_tenant_memberships_invitation_token"
        ),
    )
    # Fast lookup: all members of a tenant filtered by role
    op.create_index(
        "ix_tenant_memberships_tenant_id_role",
        "tenant_memberships",
        ["tenant_id", "role"],
    )
    # Fast lookup: all tenants a user belongs to
    op.create_index(
        "ix_tenant_memberships_user_id_active",
        "tenant_memberships",
        ["user_id", "is_active"],
    )
    # Invitation token lookup — partial index (only non-NULL tokens)
    op.create_index(
        "ix_tenant_memberships_invitation_token",
        "tenant_memberships",
        ["invitation_token"],
        postgresql_where=sa.text("invitation_token IS NOT NULL"),
    )

    # ── Table: refresh_tokens ─────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("jti", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", INET(), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        sa.UniqueConstraint("jti", name="uq_refresh_tokens_jti"),
    )
    op.create_index(
        "ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_refresh_tokens_jti", "refresh_tokens", ["jti"], unique=True
    )
    # Efficient cleanup of expired tokens by user
    op.create_index(
        "ix_refresh_tokens_user_id_expires_at",
        "refresh_tokens",
        ["user_id", "expires_at"],
    )
    # Tenant-scoped token queries
    op.create_index(
        "ix_refresh_tokens_tenant_id_created_at",
        "refresh_tokens",
        ["tenant_id", "created_at"],
    )
    # Active (non-revoked) tokens per user — partial index
    op.create_index(
        "ix_refresh_tokens_user_id_active",
        "refresh_tokens",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ── Table: audit_log ──────────────────────────────────────────────────────
    # APPEND-ONLY: no updated_at, no deleted_at. Never UPDATE or DELETE rows.
    # Retention: 7 years (Spec Part 1, Table 4 — financial compliance).
    # RESTRICT on tenant FK: prevents tenant deletion while audit records exist.
    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=True),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("ip_address", INET(), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("request_id", UUID(as_uuid=True), nullable=True),
        sa.Column("changes", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    # Tenant activity timeline — primary query for admin audit views
    op.create_index(
        "ix_audit_log_tenant_id_created_at",
        "audit_log",
        ["tenant_id", "created_at"],
    )
    # Resource audit history — "show all events for entity X"
    op.create_index(
        "ix_audit_log_entity_type_entity_id",
        "audit_log",
        ["entity_type", "entity_id"],
    )
    # User activity — "show all actions by user Y"
    op.create_index(
        "ix_audit_log_user_id_created_at",
        "audit_log",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    # audit_log has RESTRICT FK on tenants — must drop before tenants.

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.drop_index("ix_audit_log_user_id_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_type_entity_id", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_id_created_at", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index(
        "ix_refresh_tokens_user_id_active", table_name="refresh_tokens"
    )
    op.drop_index(
        "ix_refresh_tokens_tenant_id_created_at", table_name="refresh_tokens"
    )
    op.drop_index(
        "ix_refresh_tokens_user_id_expires_at", table_name="refresh_tokens"
    )
    op.drop_index("ix_refresh_tokens_jti", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index(
        "ix_tenant_memberships_invitation_token", table_name="tenant_memberships"
    )
    op.drop_index(
        "ix_tenant_memberships_user_id_active", table_name="tenant_memberships"
    )
    op.drop_index(
        "ix_tenant_memberships_tenant_id_role", table_name="tenant_memberships"
    )
    op.drop_table("tenant_memberships")

    op.drop_index("ix_users_email_active", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_tenants_slug", table_name="tenants")
    op.drop_table("tenants")

    # ── Enum ──────────────────────────────────────────────────────────────────
    sa.Enum(name="user_role").drop(op.get_bind(), checkfirst=True)

    # Note: extensions are NOT dropped in downgrade — they may be used by other
    # applications or future migrations. Remove manually if truly needed.
