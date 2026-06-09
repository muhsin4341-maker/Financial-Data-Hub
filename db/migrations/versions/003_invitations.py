"""003 invitations — M2 team invitation table.

Revision ID: c5e7f2a8b3d1
Revises: a8f3c9d2b1e7
Create Date: 2026-06-05

Table created:
  - invitations

Foreign keys:
  - invitations.tenant_id       → tenants.id   CASCADE DELETE
  - invitations.invited_by_id   → users.id     SET NULL
  - invitations.accepted_by_id  → users.id     SET NULL

Unique constraint:
  - invitations.token_hash  (global — tokens are cryptographically unique)

Indexes:
  - ix_invitations_tenant_id        B-tree  tenant_id
  - ix_invitations_invitee_email    B-tree  invitee_email
  - ix_invitations_status           B-tree  status
  - ix_invitations_token_hash       B-tree  token_hash WHERE status = 'pending'

Engineering Specification references:
  M2 Execution Plan, Section 2.4   — team invitation flow
  M2 Execution Plan, Section 9.4   — invitation token security
  Part 1, Section 1.2, Decision 1  — UUID v7 PKs
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables

Migration notes:
  - token_hash is a 64-character SHA-256 hex digest of the 288-bit raw token.
  - status is VARCHAR(20), not a DB-level ENUM, to allow future additions
    without ALTER TYPE migrations.
  - expires_at is set at insert time (Python: now + 72 hours); the column
    has no server default because the expiry window is application-controlled.
  - No deleted_at: invitations are cancelled (status='cancelled'), not deleted.

Downgrade drops the invitations table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "c5e7f2a8b3d1"
down_revision: str = "a8f3c9d2b1e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            comment="UUID v7 primary key.",
        ),
        # ── Tenancy ───────────────────────────────────────────────────────────
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tenant workspace the invitee is joining.",
        ),
        # ── Invitation target ─────────────────────────────────────────────────
        sa.Column(
            "invitee_email",
            sa.String(254),
            nullable=False,
            comment="Email address of the person being invited (lowercased).",
        ),
        sa.Column(
            "role",
            sa.String(50),
            nullable=False,
            comment="RBAC role assigned on acceptance: viewer|analyst|admin.",
        ),
        # ── Token ────────────────────────────────────────────────────────────
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            comment="SHA-256 hex digest of the raw invitation token.",
        ),
        # ── Lifecycle ────────────────────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
            comment="pending | accepted | cancelled.",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="UTC expiry timestamp (72 hours from creation).",
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set when the invitee accepts.",
        ),
        # ── Actors ───────────────────────────────────────────────────────────
        sa.Column(
            "invited_by_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="User who sent the invitation.",
        ),
        sa.Column(
            "accepted_by_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="User who accepted the invitation.",
        ),
        # ── Timestamps ───────────────────────────────────────────────────────
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
        # ── Foreign keys ─────────────────────────────────────────────────────
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_invitations_tenant_id",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_invitations_invited_by_id",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_invitations_accepted_by_id",
        ),
        # ── Unique constraint ────────────────────────────────────────────────
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.create_index("ix_invitations_tenant_id", "invitations", ["tenant_id"])
    op.create_index("ix_invitations_invitee_email", "invitations", ["invitee_email"])
    op.create_index("ix_invitations_status", "invitations", ["status"])

    # Partial index on token_hash — only pending invitations are looked up by token.
    op.execute(
        "CREATE INDEX ix_invitations_token_hash "
        "ON invitations (token_hash) "
        "WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.drop_table("invitations")
