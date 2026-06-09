"""011 filing_documents — M3.3 Filing Records & Documents database layer.

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-06-07

Changes applied:
  1. filings table — add two nullable columns:
       fiscal_year   INTEGER      — e.g. 2024
       fiscal_period VARCHAR(10)  — e.g. 'FY', 'Q1', 'Q2', 'Q3', 'Q4'

  2. filings table — add composite index:
       ix_filings_company_fiscal  (company_id, fiscal_year, fiscal_period)
       Covers the primary query pattern: "all Q3 2024 filings for company X".

  3. New table: filing_documents
       Tracks individual documents within a single filing (XBRL XML, primary
       HTML, PDF attachments, etc.).  One Filing has many FilingDocuments.

Foreign keys:
  filing_documents.filing_id → filings.id  CASCADE DELETE
  (documents are owned by their filing; deleting the filing purges its docs)

Unique constraints:
  uq_filing_documents_filing_source  (filing_id, source_url)
  Prevents duplicate document entries when a source is re-scanned.

Indexes on filing_documents:
  ix_filing_documents_filing_id    — "all documents for filing X"
  ix_filing_documents_document_type — "all XBRL_XML docs across all filings"
  ix_filing_documents_s3_key       — quick lookup when key is known (nullable)

Column notes (filing_documents):
  document_type : VARCHAR(50)   — 'XBRL_XML', 'PRIMARY_HTML', 'PDF', etc.
                                  Stored as VARCHAR (not DB ENUM) for forward compat.
  source_url    : VARCHAR(2000) — original URL the document was fetched from.
  s3_key        : VARCHAR(2000) — S3 object key after upload; NULL until stored.
  file_hash     : VARCHAR(64)   — SHA-256 hex digest; NULL until file is fetched.

Downgrade:
  Drop filing_documents, drop composite index, drop the two filings columns.
  Reversible with no data loss (columns are nullable; table is new).

Engineering Specification references:
  M3 Execution Plan, M3.3 — Filing Records & Documents Database Layer
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision: str = "f1a2b3c4d5e6"
down_revision: str = "e5f6a7b8c9d0"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── 1. Add fiscal_year and fiscal_period columns to filings ───────────────
    # Both nullable so all existing rows remain valid after the migration.
    # fiscal_year   : INTEGER   — 4-digit calendar year (e.g. 2024).
    # fiscal_period : VARCHAR   — period label: 'FY', 'Q1', 'Q2', 'Q3', 'Q4'.
    op.add_column(
        "filings",
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "filings",
        sa.Column("fiscal_period", sa.String(10), nullable=True),
    )

    # ── 2. Composite index on (company_id, fiscal_year, fiscal_period) ────────
    # Primary query pattern: "all Q3 2024 10-K filings for company X".
    # Placed on the *filings* table (not filing_documents) because the fiscal
    # coordinates belong to the filing-level record.
    op.create_index(
        "ix_filings_company_fiscal",
        "filings",
        ["company_id", "fiscal_year", "fiscal_period"],
    )

    # ── 3. New table: filing_documents ────────────────────────────────────────
    # Tracks individual documents attached to a single filing record.
    # Examples: primary 10-K HTML, XBRL instance document, R9999 exhibits, PDF.
    # Relationship: filings (1) → (many) filing_documents.
    op.create_table(
        "filing_documents",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        # ── Foreign key ───────────────────────────────────────────────────────
        # CASCADE DELETE: purging a filing removes all its document records too.
        sa.Column(
            "filing_id",
            UUID(as_uuid=True),
            sa.ForeignKey("filings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ── Document classification ───────────────────────────────────────────
        # Known values: 'XBRL_XML', 'PRIMARY_HTML', 'PDF', 'EXHIBIT', 'R_FILE'
        # Stored as VARCHAR for forward compatibility — no DB ENUM.
        sa.Column("document_type", sa.String(50), nullable=False),
        # ── Source location ───────────────────────────────────────────────────
        sa.Column("source_url", sa.String(2000), nullable=False),
        # ── Storage location (populated after S3 upload) ──────────────────────
        # NULL until the document is fetched and uploaded to the bucket.
        sa.Column("s3_key", sa.String(2000), nullable=True),
        # ── Integrity hash (populated after fetch) ────────────────────────────
        # SHA-256 hex digest of the raw file bytes. NULL until file is fetched.
        sa.Column("file_hash", sa.String(64), nullable=True),
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
        # ── Unique constraint ─────────────────────────────────────────────────
        # Prevents duplicate document entries when a filing is re-scanned.
        # A given source URL should appear at most once per filing.
        sa.UniqueConstraint(
            "filing_id",
            "source_url",
            name="uq_filing_documents_filing_source",
        ),
    )

    # ── Indexes on filing_documents ───────────────────────────────────────────

    # Primary access pattern: fetch all documents belonging to a single filing.
    op.create_index(
        "ix_filing_documents_filing_id",
        "filing_documents",
        ["filing_id"],
    )

    # Cross-filing type queries: "list all XBRL_XML documents pending upload".
    op.create_index(
        "ix_filing_documents_document_type",
        "filing_documents",
        ["document_type"],
    )

    # S3 key lookup: resolve a stored key back to its document record.
    # Partial / sparse — only rows where s3_key IS NOT NULL are useful in the
    # index; PostgreSQL skips NULL values in partial indexes automatically.
    op.create_index(
        "ix_filing_documents_s3_key",
        "filing_documents",
        ["s3_key"],
        postgresql_where=sa.text("s3_key IS NOT NULL"),
    )


def downgrade() -> None:
    # ── Reverse Step 3: filing_documents ─────────────────────────────────────
    op.drop_index("ix_filing_documents_s3_key", table_name="filing_documents")
    op.drop_index("ix_filing_documents_document_type", table_name="filing_documents")
    op.drop_index("ix_filing_documents_filing_id", table_name="filing_documents")
    # UniqueConstraint uq_filing_documents_filing_source dropped with the table.
    op.drop_table("filing_documents")

    # ── Reverse Step 2: composite index ──────────────────────────────────────
    op.drop_index("ix_filings_company_fiscal", table_name="filings")

    # ── Reverse Step 1: columns on filings ───────────────────────────────────
    op.drop_column("filings", "fiscal_period")
    op.drop_column("filings", "fiscal_year")
