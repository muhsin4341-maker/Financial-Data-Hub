"""014 reporting_framework — International parser support column.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-08

Changes applied:
  1. Add column: financial_line_items.reporting_framework VARCHAR(50) NULL
       Free-text regulatory filing framework label.  Complements the existing
       ``reporting_standard`` ENUM with the specific filing regime, allowing
       new frameworks (SEBI_BRSR, MCA_AOC, EU_CSRD) to be added without
       ALTER TYPE migrations.

Architecture decision (International Parser Strategy — Task 2):
  The existing ``reporting_standard`` column is a strict PostgreSQL ENUM:
    US_GAAP | IFRS | IND_AS
  This correctly captures the high-level accounting standard, but is too
  coarse for multi-jurisdiction filings where the same standard is used
  under different regulatory frameworks:
    - US_GAAP  → SEC_10K | SEC_10Q | SEC_20F
    - IND_AS   → SEBI_BRSR | MCA_AOC
    - IFRS     → IFRS_AR | EU_CSRD | ...

  A free-text VARCHAR(50) column (not an ENUM) is used deliberately so that
  adding new framework strings requires no DDL migration.  Application code
  uses string constants defined in the parser factory.

Column design:
  - NULL when the framework is not known or not applicable (full backward
    compatibility — existing rows inserted by older code remain valid).
  - VARCHAR(50) — sufficient for all known framework codes; short enough
    for efficient B-tree indexing.
  - No default server-side value — nullable NULL is the explicit default.

Index:
  ix_financial_line_items_framework — single-column index for queries that
  filter by filing framework (e.g. "show all SEBI_BRSR extractions").

Downgrade:
  DROP INDEX + DROP COLUMN.  Fully reversible; no other table references
  this column.  Data loss: the reporting_framework values are dropped, but
  can be reconstructed by re-running the parser pipeline.

Milestone: Task 2 — International Parser Strategy (SEBI BRSR support)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision: str = "b3c4d5e6f7a8"
down_revision: str = "a2b3c4d5e6f7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Add reporting_framework column ────────────────────────────────────────
    op.add_column(
        "financial_line_items",
        sa.Column(
            "reporting_framework",
            sa.String(50),
            nullable=True,
            comment=(
                "Regulatory filing framework (free-text VARCHAR 50).  "
                "Examples: SEC_10K, SEC_10Q, SEBI_BRSR, MCA_AOC, IFRS_AR, EU_CSRD.  "
                "NULL when not applicable or unknown (backwards-compatible default).  "
                "Complements the reporting_standard ENUM with filing-programme granularity."
            ),
        ),
    )

    # ── Index for framework-scoped queries ────────────────────────────────────
    # Used by: "show all SEBI_BRSR rows", framework-split dashboard filters.
    op.create_index(
        "ix_financial_line_items_framework",
        "financial_line_items",
        ["reporting_framework"],
        # Partial index: skip NULL rows (most rows from pre-014 pipeline).
        postgresql_where=sa.text("reporting_framework IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_financial_line_items_framework",
        table_name="financial_line_items",
    )
    op.drop_column("financial_line_items", "reporting_framework")
