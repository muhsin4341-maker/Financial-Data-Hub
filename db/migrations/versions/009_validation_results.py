"""009 validation_results — ingestion validation log table.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-07

Table created:
  - validation_results

Purpose:
  Persistent log of every ValidationReport produced by the
  dual-dimension validation engine (services/validation/engine.py) during
  the M4 ingestion pipeline.  One row per ingest run (accession_number +
  company + period).

Amendment V1.2, Section 5 — Dual-Dimension Validation Engine:
  The validation engine produces CRITICAL / WARNING / INFO findings.
  Any CRITICAL finding sets is_exportable = FALSE and blocks automated
  Excel export (handled by FinancialLineItemWriter in M4 Step 4).
  This table provides the frontend data grid with per-run rule results
  so analysts can investigate blocked jobs.

Amendment V1.2, Section 1.8 — Extraction Confidence Scoring:
  confidence_score (0-100) is stored here.  Score starts at 100 and
  is reduced by 25 per CRITICAL finding and 5 per WARNING finding.

Column design:
  findings JSONB  — array of {rule_id, severity, message, expected,
                    actual, delta} objects; one entry per fired rule.
  deductions JSONB — array of {rule_id, points, reason} objects from
                     ConfidenceScore.deductions; maps to Sheet 10.
  summary_text TEXT — human-readable summary string from
                      ValidationReport.summary() for quick diagnosis.

Downgrade: drops validation_results (safe — no other table references it).

Milestone: M4 Step 4 — Bulk DB Persistence & Versioning
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "d4e5f6a7b8c9"
down_revision: str = "c3d4e5f6a7b8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "validation_results",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="UUID v7 primary key — time-ordered for B-tree performance.",
        ),
        # ── Source document identity ──────────────────────────────────────────
        sa.Column(
            "accession_number",
            sa.String(25),
            nullable=False,
            comment="SEC EDGAR accession number of the parsed filing.",
        ),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            nullable=True,
            comment="Company the validation run covers. NULL for non-company-linked runs.",
        ),
        sa.Column(
            "fiscal_year",
            sa.SmallInteger(),
            nullable=True,
            comment="Primary fiscal year from the parsed items.",
        ),
        sa.Column(
            "fiscal_period",
            sa.String(10),
            nullable=True,
            comment="Primary fiscal period: Q1 | Q2 | Q3 | Q4 | FY.",
        ),
        # ── Job linkage ───────────────────────────────────────────────────────
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("financial_jobs.id", ondelete="SET NULL"),
            nullable=True,
            comment="FinancialJob that triggered this ingestion run.",
        ),
        # ── Validation summary (Amendment V1.2 §5) ────────────────────────────
        sa.Column(
            "items_validated",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Count of ParsedLineItem objects processed.",
        ),
        sa.Column(
            "is_exportable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
            comment=(
                "FALSE when any CRITICAL validation finding exists. "
                "Excel export is blocked until is_exportable = TRUE."
            ),
        ),
        sa.Column(
            "critical_count",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Number of CRITICAL findings.",
        ),
        sa.Column(
            "warning_count",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Number of WARNING findings.",
        ),
        # ── Confidence scoring (Amendment V1.2 §1.8) ─────────────────────────
        sa.Column(
            "confidence_score",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("100"),
            comment=(
                "Extraction confidence score [0-100]. "
                "Starts at 100; reduced by 25 per CRITICAL, 5 per WARNING."
            ),
        ),
        # ── Granular rule output (for frontend data grid) ─────────────────────
        sa.Column(
            "findings",
            JSONB,
            nullable=True,
            comment=(
                "Array of finding objects: "
                "[{rule_id, severity, message, expected, actual, delta}]. "
                "One entry per fired rule (including INFO/skipped rules)."
            ),
        ),
        sa.Column(
            "deductions",
            JSONB,
            nullable=True,
            comment=(
                "Array of deduction objects: [{rule_id, points, reason}]. "
                "Maps to Amendment V1.2 §1.8 confidence deduction log."
            ),
        ),
        # ── Human-readable summary ────────────────────────────────────────────
        sa.Column(
            "summary_text",
            sa.Text(),
            nullable=True,
            comment="ValidationReport.summary() string for quick diagnostic display.",
        ),
        # ── Timestamps ───────────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.create_index(
        "ix_validation_results_accession_number",
        "validation_results",
        ["accession_number"],
    )
    op.create_index(
        "ix_validation_results_company_id",
        "validation_results",
        ["company_id"],
    )
    op.create_index(
        "ix_validation_results_job_id",
        "validation_results",
        ["job_id"],
    )
    op.create_index(
        "ix_validation_results_is_exportable",
        "validation_results",
        ["is_exportable"],
        postgresql_where=sa.text("is_exportable = FALSE"),
    )
    op.create_index(
        "ix_validation_results_created_at",
        "validation_results",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_validation_results_created_at",    table_name="validation_results")
    op.drop_index("ix_validation_results_is_exportable", table_name="validation_results")
    op.drop_index("ix_validation_results_job_id",        table_name="validation_results")
    op.drop_index("ix_validation_results_company_id",    table_name="validation_results")
    op.drop_index("ix_validation_results_accession_number", table_name="validation_results")
    op.drop_table("validation_results")
