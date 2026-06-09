"""008 financial_line_items — Amendment V1.2 core financial data table.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-07

Table created:
  - financial_line_items

Amendment V1.2 compliance:
  §1.1  NUMERIC(26,2) for all absolute monetary columns (revenue, assets,
        liabilities, net_income, operating_cash_flow, etc.).
        NUMERIC(38,10) for per-share metrics, financial ratios, and FX
        translation coefficients.
  §1.2  Point-in-time immutability: filing_date DATE NOT NULL +
        is_restated BOOLEAN DEFAULT FALSE. Composite UNIQUE constraint
        (company_id, fiscal_year, fiscal_period, canonical_field, filing_date)
        replaces static record overwrites (ASC 250 / IAS 8 / Ind AS 8).
  §2.1  reporting_standard ENUM('US_GAAP', 'IFRS', 'IND_AS') NOT NULL —
        mandatory on every financial row; enables per-standard validation rules.
  §7.2  PostgreSQL partial index WHERE is_restated = FALSE for query performance
        on current (non-restated) values.
  §8.1  derived_expression_formula VARCHAR(255) NULL — logs the algebraic
        string of source tags used when a value is computed/imputed.
  §4.2  source_file_hash VARCHAR(64) — foreign key to stored_documents.content_hash
        satisfying SOX 404 / IT Act 2000 cryptographic audit trail requirements.

Column precision tiers (Amendment V1.2 §1.1):
  NUMERIC(26,2)  — absolute monetary values:
                   revenue, gross_profit, operating_income, net_income,
                   total_assets, total_liabilities, total_equity,
                   operating_cash_flow, investing_cash_flow,
                   financing_cash_flow, capex, free_cash_flow.
  NUMERIC(38,10) — per-share metrics, financial ratios, FX coefficients:
                   eps_basic, eps_diluted, book_value_per_share,
                   pe_ratio, pb_ratio, roe, roa, current_ratio, debt_to_equity,
                   fx_rate_used, reported_value (raw reported figure before
                   normalisation, stored at max precision to avoid rounding
                   during the normalisation pipeline).

Sign convention (Amendment V1.2 §2.2):
  Inflows positive, outflows/expenses negative (×−1 applied at ingestion).
  Visual-layout positive values from PDFs/XBRL are NEVER stored as-is for
  expense/outflow line items. The sign inversion is applied by the
  ingestion/extraction layer before insertion into this table.

Downgrade drops financial_line_items (safe — no other table FK-references it yet).

Milestone: M4 (schema pre-provisioned by Amendment V1.2 compliance sweep)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c3d4e5f6a7b8"
down_revision: str = "b2c3d4e5f6a7"
branch_labels: str | None = None
depends_on: str | None = None

def upgrade() -> None:
    # Create the ENUM type idempotently.
    # Using a PL/pgSQL exception block instead of SQLAlchemy's
    # Enum.create(checkfirst=True) because a module-level sa.Enum object
    # (create_type=True) triggers _on_table_create with checkfirst=False
    # inside op.create_table(), causing a DuplicateObject error on
    # SQLAlchemy 2.x / Python 3.13.  Raw SQL avoids the double-create.
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE reporting_standard AS ENUM ('US_GAAP', 'IFRS', 'IND_AS');
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END $$;
        """
    )

    op.create_table(
        "financial_line_items",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        # ── Company and period identity ───────────────────────────────────────
        sa.Column("company_id", UUID(as_uuid=True), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_period", sa.String(10), nullable=False),  # Q1/Q2/Q3/Q4/FY
        # ── Reporting standard (Amendment V1.2 §2.1) ─────────────────────────
        sa.Column(
            # Use dialect-specific PG_ENUM that references the already-created
            # type by name only (create_type=False, no values).  This bypasses
            # SQLAlchemy 2.0.x _on_table_create firing CREATE TYPE a second time.
            "reporting_standard",
            PG_ENUM("US_GAAP", "IFRS", "IND_AS", name="reporting_standard", create_type=False),
            nullable=False,
        ),
        # ── Point-in-time architecture (Amendment V1.2 §1.2) ─────────────────
        # ASC 250 / IAS 8 / Ind AS 8: restatements are new rows, not updates.
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column(
            "is_restated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        # ── Canonical field identifier ────────────────────────────────────────
        # XBRL concept tag or normalised field name (e.g. 'us-gaap:Revenue').
        sa.Column("canonical_field", sa.String(255), nullable=False),
        # ── Statement classification ──────────────────────────────────────────
        # IS = Income Statement, BS = Balance Sheet, CF = Cash Flow.
        sa.Column("statement_type", sa.String(2), nullable=False),
        # ── Absolute monetary values — NUMERIC(26,2) (Amendment V1.2 §1.1) ───
        sa.Column("value_usd", sa.Numeric(26, 2), nullable=True),
        sa.Column("value_reported", sa.Numeric(26, 2), nullable=True),
        sa.Column("reported_currency", sa.String(3), nullable=True),
        # ── Per-share / ratio / FX — NUMERIC(38,10) (Amendment V1.2 §1.1) ────
        sa.Column("fx_rate_used", sa.Numeric(38, 10), nullable=True),
        # ── Audit traceability (Amendment V1.2 §4.2 / SOX 404 / IT Act 2000) ─
        # SHA-256 hex digest of the source document; links to stored_documents.
        sa.Column("source_file_hash", sa.String(64), nullable=True),
        # ── Imputed expression (Amendment V1.2 §8.1) ─────────────────────────
        # Algebraic string logging how computed/imputed values were derived.
        sa.Column("derived_expression_formula", sa.String(255), nullable=True),
        # ── Extraction provenance ─────────────────────────────────────────────
        sa.Column("extraction_method", sa.String(50), nullable=True),  # xbrl/pdf/ocr/ai
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
        # ── Point-in-time composite unique constraint (Amendment V1.2 §1.2) ──
        # Prevents silent overwrites; restatements insert a new row with
        # a later filing_date, leaving the original row intact for audit.
        sa.UniqueConstraint(
            "company_id",
            "fiscal_year",
            "fiscal_period",
            "canonical_field",
            "filing_date",
            name="uq_financial_line_items_point_in_time",
        ),
    )

    # Standard query indexes
    op.create_index(
        "ix_financial_line_items_company_id",
        "financial_line_items",
        ["company_id"],
    )
    op.create_index(
        "ix_financial_line_items_company_period",
        "financial_line_items",
        ["company_id", "fiscal_year", "fiscal_period"],
    )
    op.create_index(
        "ix_financial_line_items_source_file_hash",
        "financial_line_items",
        ["source_file_hash"],
    )

    # Amendment V1.2 §7.2 — partial index for current (non-restated) values.
    # Queries for the latest values use WHERE is_restated = FALSE; this index
    # makes those lookups O(log n) instead of full-scan on the raw table.
    op.create_index(
        "ix_financial_line_items_current",
        "financial_line_items",
        ["company_id", "fiscal_year", "fiscal_period", "canonical_field"],
        postgresql_where=sa.text("is_restated = FALSE"),
    )


def downgrade() -> None:
    op.drop_index("ix_financial_line_items_current", table_name="financial_line_items")
    op.drop_index(
        "ix_financial_line_items_source_file_hash", table_name="financial_line_items"
    )
    op.drop_index(
        "ix_financial_line_items_company_period", table_name="financial_line_items"
    )
    op.drop_index(
        "ix_financial_line_items_company_id", table_name="financial_line_items"
    )
    op.drop_table("financial_line_items")
    op.execute("DROP TYPE IF EXISTS reporting_standard;")
