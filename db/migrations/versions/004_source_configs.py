"""004 source_configs — M3.1 Source Registry table.

Revision ID: e1f6a4b2c9d8
Revises: c5e7f2a8b3d1
Create Date: 2026-06-06

Table created:
  - source_configs

Foreign keys:
  None — source_configs is a global system table; it carries no tenant_id.

Unique constraints:
  - uq_source_configs_code   (code)  — machine-readable code is globally unique

Indexes:
  - ix_source_configs_provider_type    B-tree  provider_type
  - ix_source_configs_is_active        B-tree  is_active
  - ix_source_configs_country_code     B-tree  country_code

  Note: ix_source_configs_code is NOT created separately — the unique constraint
  uq_source_configs_code creates an implicit index on 'code' in PostgreSQL.
  Creating a separate non-unique index on the same column would be redundant.

Engineering Specification references:
  M3 Execution Plan, Section 6.1       — source_configs table design
  M3 Execution Plan, M3.1              — Source Registry milestone
  Part 1, Section 1.2, Decision 1      — UUID v7 PKs (Python-generated; no DB default)

Column notes:
  - code      : machine-readable identifier ('SEC_EDGAR', 'NSE', 'MANUAL_UPLOAD').
                Stored as VARCHAR(50), immutable after creation (enforced by service).
  - name      : human-readable display name ('SEC EDGAR', 'National Stock Exchange').
  - description: optional free-text description of the source.
  - provider_type: category of provider — 'regulatory', 'exchange', 'manual', 'broker'.
                   Stored as VARCHAR(50) (not a DB ENUM) for forward compatibility.
  - country_code : ISO 3166-1 alpha-2 country of the source. NULL = multi-country.
  - base_url  : root URL used by the acquisition service for HTTP requests.
  - rate_limit_per_minute: maximum requests per minute allowed. Default 60.
  - is_active : soft-enable/disable flag. False = acquisition service skips this source.
                No deleted_at — sources are disabled not deleted (M3 business rule).
  - config    : JSONB blob for source-specific configuration (API keys, endpoints, flags).

Migration notes:
  - No deleted_at column: sources are disabled (is_active=false) rather than hard-deleted.
  - No tenant_id: source configs are platform-wide system records, not per-tenant data.
  - provider_type stored as VARCHAR(50) to allow adding new types without ALTER TYPE.
  - config JSONB allows each source to carry arbitrary extra metadata without schema changes.
  - Column comments are intentionally omitted from op.create_table() because the ORM
    model uses doc= (Python-only documentation) rather than comment= (SQL COMMENT ON COLUMN).
    This keeps the migration consistent with migration 002 (companies/financial_jobs) and
    prevents Alembic autogenerate from detecting spurious comment drift.
  - uq_source_configs_code is created inline inside op.create_table(); it is added to
    _KNOWN_INLINE_CONSTRAINTS in db/migrations/env.py to suppress false-positive drift.
  - The seed file db/seeds/source_configs.sql populates the SEC_EDGAR row on first deploy.

Downgrade drops source_configs entirely (safe — no FK constraints reference this table yet;
FK from filing_records will be added in migration 005).

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers
revision: str = "e1f6a4b2c9d8"
down_revision: str = "c5e7f2a8b3d1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Table: source_configs ─────────────────────────────────────────────────
    # Global system table — no tenant_id; no deleted_at.
    # Sources are disabled (is_active=false) rather than hard-deleted.
    # Column comments are intentionally omitted — see migration notes above.
    op.create_table(
        "source_configs",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        # ── Identity ─────────────────────────────────────────────────────────
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # ── Classification ────────────────────────────────────────────────────
        sa.Column("provider_type", sa.String(50), nullable=False),
        sa.Column("country_code", sa.String(5), nullable=True),
        # ── Connection ────────────────────────────────────────────────────────
        sa.Column("base_url", sa.String(500), nullable=True),
        # ── Rate control ─────────────────────────────────────────────────────
        sa.Column(
            "rate_limit_per_minute",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        # ── Status ────────────────────────────────────────────────────────────
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        # ── Flexible config ───────────────────────────────────────────────────
        sa.Column("config", JSONB(), nullable=True),
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
        # ── Unique constraint ────────────────────────────────────────────────
        # Created inline so the constraint is named consistently.
        # This creates an implicit unique index on 'code' in PostgreSQL —
        # no separate op.create_index is needed for the code column.
        # Listed in _KNOWN_INLINE_CONSTRAINTS in env.py to suppress false-positive drift.
        sa.UniqueConstraint("code", name="uq_source_configs_code"),
    )

    # ── Indexes on source_configs ─────────────────────────────────────────────
    # NOTE: ix_source_configs_code is NOT created here — the unique constraint
    # uq_source_configs_code already creates an implicit index on 'code'.

    # Filter by provider type — list all 'regulatory' sources, etc.
    op.create_index(
        "ix_source_configs_provider_type",
        "source_configs",
        ["provider_type"],
    )

    # Filter active sources — acquisition service queries WHERE is_active = true.
    op.create_index(
        "ix_source_configs_is_active",
        "source_configs",
        ["is_active"],
    )

    # Filter by country — list all sources for a given country.
    op.create_index(
        "ix_source_configs_country_code",
        "source_configs",
        ["country_code"],
    )


def downgrade() -> None:
    # Drop indexes before the table (defensive; op.drop_table cascades anyway).
    op.drop_index("ix_source_configs_country_code", table_name="source_configs")
    op.drop_index("ix_source_configs_is_active", table_name="source_configs")
    op.drop_index("ix_source_configs_provider_type", table_name="source_configs")
    # UniqueConstraint uq_source_configs_code and its implicit index are dropped
    # with the table.
    op.drop_table("source_configs")
