"""005 filings — M3.3 Filing Models.

Revision ID: f3a7b2c5d9e1
Revises: e1f6a4b2c9d8
Create Date: 2026-06-06

Table created:
  - filings

Foreign keys:
  - filings.company_id        → companies.id       SET NULL on delete (nullable)
  - filings.source_config_id  → source_configs.id  SET NULL on delete (nullable)

  Both FKs are nullable to allow:
    company_id      — a filing may be discovered before being linked to a
                      tenant-specific company record (linked in M3.4 / M3.7).
    source_config_id — a filing may be created without knowing the source yet;
                       linked when the acquisition worker processes the record.

  SET NULL is chosen over CASCADE because filings are independent system
  records and should not be deleted when a company or source is removed.

Unique constraints:
  - uq_filings_accession_number  (accession_number) — globally unique per BR-1.
    Accession numbers are assigned by SEC EDGAR and are globally unique across
    all filers: format '0000320193-23-000077'. No two filings may share the
    same accession number regardless of company, source, or tenant.

  Note: the unique constraint creates an implicit unique index on accession_number
  in PostgreSQL. A separate ix_filings_accession_number is NOT created.

Indexes created:
  - ix_filings_company_id        B-tree  company_id
  - ix_filings_source_config_id  B-tree  source_config_id
  - ix_filings_filing_type       B-tree  filing_type
  - ix_filings_filing_date       B-tree  filing_date
  - ix_filings_cik               B-tree  cik
  - ix_filings_ticker            B-tree  ticker
  - ix_filings_status            B-tree  status
  - ix_filings_created_at        B-tree  created_at

  Query patterns these serve:
    company_id + filing_type  — "give me all 10-K filings for AAPL"
    cik                       — SEC EDGAR lookup by CIK (pre-company-link phase)
    ticker                    — lookup by ticker symbol
    status                    — "find all downloaded filings awaiting processing"
    filing_date               — date-range queries for recent filings
    created_at                — timeline ordering in acquisition job logs

Engineering Specification references:
  Part 1, Section 1.2, Decision 1      — UUID v7 PKs (Python-generated; no DB default)
  M3 Execution Plan, M3.3              — Filing Models milestone
  M3 Execution Plan, Section 6.1       — source_configs FK reference

Column notes:
  - filing_type     : VARCHAR(20); known values: '10-K', '10-Q', '8-K', 'DEF 14A',
                      '20-F', '6-K'. Stored as VARCHAR for forward compatibility
                      (not a DB ENUM) — same pattern as JobStatus, ProviderType.
  - accession_number: VARCHAR(25); SEC EDGAR format '0001234567-23-000001'. Unique.
  - filing_date     : DATE on which the filing was submitted to SEC EDGAR.
  - period_end_date : DATE of the fiscal period covered (e.g. 2023-09-30 for Q3 FY2023).
                      NULL for 8-K and other non-period filings.
  - cik             : VARCHAR(10); 10-digit zero-padded SEC CIK (e.g. '0000320193').
  - ticker          : VARCHAR(20); ticker symbol at time of filing.
  - title           : VARCHAR(500); human-readable title from SEC EDGAR.
  - filing_url      : VARCHAR(2000); URL to the filing index page on SEC EDGAR.
  - document_url    : VARCHAR(2000); URL to the primary filing document (10-K.htm etc).
  - status          : VARCHAR(50); lifecycle state. Values: 'discovered', 'downloading',
                      'downloaded', 'failed'. Stored as VARCHAR for forward compatibility.
  - filing_metadata : JSONB; arbitrary metadata from the SEC EDGAR API response that
                      does not fit standard columns (e.g. form_type, items, file_number).

Migration notes:
  - No tenant_id: filings are global system records representing SEC documents.
    A filing exists once regardless of how many tenants track the same company.
    (Same convention as source_configs — no per-tenant isolation at this layer.)
  - No deleted_at: filings reach terminal states ('downloaded', 'failed') but are
    never soft-deleted. Use status transitions to track lifecycle.
  - uq_filings_accession_number is created inline inside op.create_table() and is
    added to _KNOWN_INLINE_CONSTRAINTS in env.py to suppress false-positive drift.
  - Column comments are intentionally omitted — see migration 004 notes for rationale.
  - filing_metadata is named with the 'filing_' prefix to avoid collision with
    Python builtins and SQLAlchemy metadata attributes.

Downgrade drops filings (safe — no FK constraints reference this table yet).

Milestone: M3.3 — Filing Models
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers
revision: str = "f3a7b2c5d9e1"
down_revision: str = "e1f6a4b2c9d8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Table: filings ────────────────────────────────────────────────────────
    # Global system table — no tenant_id; no deleted_at.
    # Filings are discovered from data sources and stored as system records.
    # Status lifecycle: discovered → downloading → downloaded | failed.
    # Column comments intentionally omitted — see module docstring.
    op.create_table(
        "filings",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        # ── Foreign keys (both nullable) ──────────────────────────────────────
        # company_id: linked after company resolution (M3.4 / M3.7).
        #             NULL = filing not yet linked to a company record.
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # source_config_id: identifies which data source provided this filing.
        #                   NULL = source not yet determined.
        sa.Column(
            "source_config_id",
            UUID(as_uuid=True),
            sa.ForeignKey("source_configs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ── Filing identity ───────────────────────────────────────────────────
        sa.Column("filing_type", sa.String(20), nullable=False),
        sa.Column("accession_number", sa.String(25), nullable=False),
        # ── Dates ─────────────────────────────────────────────────────────────
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("period_end_date", sa.Date(), nullable=True),
        # ── Company identifiers ───────────────────────────────────────────────
        sa.Column("cik", sa.String(10), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=True),
        # ── Content references ────────────────────────────────────────────────
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("filing_url", sa.String(2000), nullable=True),
        sa.Column("document_url", sa.String(2000), nullable=True),
        # ── Lifecycle ─────────────────────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'discovered'"),
        ),
        # ── Flexible metadata ─────────────────────────────────────────────────
        sa.Column("filing_metadata", JSONB(), nullable=True),
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
        # BR-1: accession number is globally unique across all filings.
        # Created inline so the constraint is named consistently.
        # This creates an implicit unique index on accession_number in PostgreSQL.
        # Listed in _KNOWN_INLINE_CONSTRAINTS in env.py to suppress false-positive drift.
        sa.UniqueConstraint("accession_number", name="uq_filings_accession_number"),
    )

    # ── Indexes on filings ────────────────────────────────────────────────────
    # NOTE: ix_filings_accession_number is NOT created here — the unique
    # constraint uq_filings_accession_number already creates an implicit index.

    # Company-scoped query — list all filings for a specific company.
    op.create_index(
        "ix_filings_company_id",
        "filings",
        ["company_id"],
    )

    # Source-scoped query — list all filings from a specific data source.
    op.create_index(
        "ix_filings_source_config_id",
        "filings",
        ["source_config_id"],
    )

    # Filing type filter — list all 10-K filings, all 10-Q filings, etc.
    op.create_index(
        "ix_filings_filing_type",
        "filings",
        ["filing_type"],
    )

    # Date range queries — find filings submitted in a given time window.
    op.create_index(
        "ix_filings_filing_date",
        "filings",
        ["filing_date"],
    )

    # CIK lookup — find all filings for a CIK before company resolution.
    op.create_index(
        "ix_filings_cik",
        "filings",
        ["cik"],
    )

    # Ticker lookup — find all filings by ticker symbol.
    op.create_index(
        "ix_filings_ticker",
        "filings",
        ["ticker"],
    )

    # Status filter — find all 'discovered' filings pending download, etc.
    op.create_index(
        "ix_filings_status",
        "filings",
        ["status"],
    )

    # Timeline ordering — most-recent-first in filing list responses.
    op.create_index(
        "ix_filings_created_at",
        "filings",
        ["created_at"],
    )


def downgrade() -> None:
    # Drop indexes before the table (defensive; op.drop_table cascades anyway).
    op.drop_index("ix_filings_created_at", table_name="filings")
    op.drop_index("ix_filings_status", table_name="filings")
    op.drop_index("ix_filings_ticker", table_name="filings")
    op.drop_index("ix_filings_cik", table_name="filings")
    op.drop_index("ix_filings_filing_date", table_name="filings")
    op.drop_index("ix_filings_filing_type", table_name="filings")
    op.drop_index("ix_filings_source_config_id", table_name="filings")
    op.drop_index("ix_filings_company_id", table_name="filings")
    # UniqueConstraint uq_filings_accession_number and its implicit index
    # are dropped with the table.
    op.drop_table("filings")
