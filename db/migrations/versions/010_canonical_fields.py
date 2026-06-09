"""010 canonical_fields — source-of-truth field registry.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-07

Tables created:
  - canonical_fields   — master list of all extractable financial fields
  - field_aliases      — label / XBRL concept → canonical_field_key mapping

Engineering Spec Part 2, §5.3 (canonical fields) and §5.4 (alias mapping).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM

revision: str = "e5f6a7b8c9d0"
down_revision: str = "d4e5f6a7b8c9"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── reporting_standard enum (already exists from migration 008) ───────────
    # Create idempotently in case this migration is run in isolation.
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE reporting_standard AS ENUM ('US_GAAP', 'IFRS', 'IND_AS');
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END $$;
        """
    )

    # ── canonical_fields ──────────────────────────────────────────────────────
    op.create_table(
        "canonical_fields",
        sa.Column("field_key", sa.String(100), primary_key=True, nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        # income / balance_sheet / cash_flow
        sa.Column("statement_type", sa.String(20), nullable=False),
        # revenue / cogs / opex / profit / per_share / current_assets / etc.
        sa.Column("section", sa.String(50), nullable=False),
        # 1 = positive inflow/asset, -1 = negative outflow/liability (§2.2)
        sa.Column("sign_convention", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_index(
        "ix_canonical_fields_statement_type",
        "canonical_fields",
        ["statement_type"],
    )
    op.create_index(
        "ix_canonical_fields_section",
        "canonical_fields",
        ["section"],
    )

    # ── field_aliases ─────────────────────────────────────────────────────────
    op.create_table(
        "field_aliases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "canonical_field_key",
            sa.String(100),
            sa.ForeignKey("canonical_fields.field_key", ondelete="CASCADE"),
            nullable=False,
        ),
        # The raw string as it appears in the source (label or XBRL concept tag)
        sa.Column("alias", sa.String(512), nullable=False),
        # 'label' (human-readable PDF/HTML text) or 'xbrl' (concept tag)
        sa.Column("alias_type", sa.String(10), nullable=False),
        # NULL = applies to all standards; else scoped to one standard
        sa.Column(
            "reporting_standard",
            PG_ENUM("US_GAAP", "IFRS", "IND_AS", name="reporting_standard", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "canonical_field_key",
            "alias",
            "reporting_standard",
            name="uq_field_aliases_key_alias_standard",
        ),
    )

    op.create_index(
        "ix_field_aliases_canonical_field_key",
        "field_aliases",
        ["canonical_field_key"],
    )
    op.create_index(
        "ix_field_aliases_alias",
        "field_aliases",
        ["alias"],
    )
    op.create_index(
        "ix_field_aliases_alias_type",
        "field_aliases",
        ["alias_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_field_aliases_alias_type", table_name="field_aliases")
    op.drop_index("ix_field_aliases_alias", table_name="field_aliases")
    op.drop_index("ix_field_aliases_canonical_field_key", table_name="field_aliases")
    op.drop_table("field_aliases")

    op.drop_index("ix_canonical_fields_section", table_name="canonical_fields")
    op.drop_index("ix_canonical_fields_statement_type", table_name="canonical_fields")
    op.drop_table("canonical_fields")
