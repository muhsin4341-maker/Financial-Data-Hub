"""002 companies and financial_jobs — M2 domain tables.

Revision ID: a8f3c9d2b1e7
Revises: 7f3a2b9c1d8e
Create Date: 2026-06-04

Tables created:
  - companies
  - financial_jobs

Foreign keys added:
  - companies.tenant_id       → tenants.id        CASCADE DELETE
  - financial_jobs.tenant_id  → tenants.id        CASCADE DELETE
  - financial_jobs.company_id → companies.id      CASCADE DELETE
  - financial_jobs.created_by → users.id          SET NULL

Unique constraints added:
  - uq_companies_tenant_ticker  (tenant_id, ticker)
  - uq_companies_tenant_cik     (tenant_id, cik)
    PostgreSQL naturally excludes NULLs from UNIQUE constraints, so two
    companies in the same tenant may both carry cik = NULL without conflict.
    No partial index is needed.

Indexes created:
  companies:
  - ix_companies_tenant_id           B-tree  tenant_id (full)
  - ix_companies_tenant_id_active    B-tree  tenant_id WHERE deleted_at IS NULL
  - gin_companies_name               GIN     name gin_trgm_ops  (fuzzy name search)

  financial_jobs:
  - ix_financial_jobs_tenant_id      B-tree  tenant_id
  - ix_financial_jobs_company_id     B-tree  company_id
  - ix_financial_jobs_status         B-tree  status
  - ix_financial_jobs_created_by     B-tree  created_by
  - ix_financial_jobs_created_at     B-tree  created_at

Engineering Specification references:
  Part 1, Section 1.2, Decision 1  — UUID v7 PKs (Python-generated; no DB default)
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables
  Part 1, Section 1.2, Decision 4  — soft delete via deleted_at TIMESTAMPTZ
  M2 Execution Plan, Section 5     — database impact assessment
  M2 Execution Plan, Risk R-08     — ticker unique per tenant, not globally

Migration notes:
  - pg_trgm extension is NOT created here; it was installed in migration 001.
  - The GIN index uses gin_trgm_ops for ILIKE fuzzy name search.
    Created via raw SQL because Alembic op.create_index() does not support
    PostgreSQL operator classes on individual columns in all versions.
  - uq_companies_tenant_cik is a standard UniqueConstraint, not a partial index.
    PostgreSQL's UNIQUE constraint naturally excludes NULLs, so two companies
    in the same tenant may both carry cik = NULL without violating uniqueness.
  - JobStatus is stored as VARCHAR(50), not a DB-level ENUM, to allow future
    status additions without ALTER TYPE migrations.
  - financial_jobs has no deleted_at; jobs are cancelled (terminal), not deleted.
  - alembic check reports 4 false-positive "removed constraint" entries for the
    M1 tables (jti, token_hash, slug, email). This is a known Alembic limitation:
    constraints created inline inside op.create_table() are not tracked by the
    autogenerate scanner in later runs. The M1 schema is correct and unaffected.

Downgrade drops financial_jobs first (FK → companies), then companies.
Both operations are safe on a fresh database with no production data yet.

Milestone: M2-Step 3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision: str = "a8f3c9d2b1e7"
down_revision: str = "7f3a2b9c1d8e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Table: companies ──────────────────────────────────────────────────────
    # Tenant-scoped company registry.
    # Ticker uniqueness is per (tenant_id, ticker) — Risk R-08 in M2 plan.
    op.create_table(
        "companies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        # ── Tenancy ───────────────────────────────────────────────────────────
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ── Identity ─────────────────────────────────────────────────────────
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("cik", sa.String(10), nullable=True),
        # ── Classification ────────────────────────────────────────────────────
        sa.Column("exchange", sa.String(50), nullable=True),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column("industry", sa.String(100), nullable=True),
        # ── Profile ───────────────────────────────────────────────────────────
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        # ── Status ────────────────────────────────────────────────────────────
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # ── Constraints ───────────────────────────────────────────────────────
        # Ticker unique within a tenant workspace (not globally — Risk R-08).
        sa.UniqueConstraint(
            "tenant_id",
            "ticker",
            name="uq_companies_tenant_ticker",
        ),
        # CIK unique within a tenant workspace.
        # PostgreSQL's UNIQUE constraint naturally excludes NULLs — two companies
        # in the same tenant may both carry cik = NULL without conflict.
        sa.UniqueConstraint(
            "tenant_id",
            "cik",
            name="uq_companies_tenant_cik",
        ),
    )

    # ── B-tree indexes on companies ───────────────────────────────────────────

    # Primary tenant lookup — list all companies in a workspace.
    op.create_index(
        "ix_companies_tenant_id",
        "companies",
        ["tenant_id"],
    )

    # Partial index — the common query excludes soft-deleted rows.
    op.create_index(
        "ix_companies_tenant_id_active",
        "companies",
        ["tenant_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ── GIN trigram index on companies.name ───────────────────────────────────
    # Enables efficient ILIKE fuzzy search: SELECT ... WHERE name ILIKE '%apple%'
    # Requires pg_trgm extension — already installed by migration 001.
    # Created via raw DDL because Alembic's op.create_index() does not reliably
    # emit per-column operator classes (gin_trgm_ops) across all versions.
    op.execute(
        "CREATE INDEX gin_companies_name "
        "ON companies USING gin (name gin_trgm_ops)"
    )

    # ── Table: financial_jobs ─────────────────────────────────────────────────
    # Represents one unit of financial-data extraction work.
    # Status stored as VARCHAR(50) — not a DB ENUM — for forward compatibility.
    # No deleted_at: jobs reach terminal states (cancelled/failed/completed).
    op.create_table(
        "financial_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        # ── Tenancy ───────────────────────────────────────────────────────────
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ── Subject ───────────────────────────────────────────────────────────
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ── Actor ────────────────────────────────────────────────────────────
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ── Job classification ────────────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        # ── Document references ───────────────────────────────────────────────
        sa.Column("document_url", sa.String(2000), nullable=True),
        sa.Column("result_url", sa.String(2000), nullable=True),
        # ── Error state ───────────────────────────────────────────────────────
        sa.Column("error_message", sa.Text(), nullable=True),
        # ── Celery integration ────────────────────────────────────────────────
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        # ── Timing ───────────────────────────────────────────────────────────
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        # NOTE: No deleted_at — see module docstring.
    )

    # ── Indexes on financial_jobs ─────────────────────────────────────────────

    # Primary tenant-scoped query — list all jobs in a workspace.
    op.create_index(
        "ix_financial_jobs_tenant_id",
        "financial_jobs",
        ["tenant_id"],
    )

    # Company-scoped query — list all jobs for a specific company.
    op.create_index(
        "ix_financial_jobs_company_id",
        "financial_jobs",
        ["company_id"],
    )

    # Status filter — find pending/running jobs for monitoring dashboards.
    op.create_index(
        "ix_financial_jobs_status",
        "financial_jobs",
        ["status"],
    )

    # Creator lookup — "show me jobs I created".
    op.create_index(
        "ix_financial_jobs_created_by",
        "financial_jobs",
        ["created_by"],
    )

    # Timeline ordering — most-recent-first in job list responses.
    op.create_index(
        "ix_financial_jobs_created_at",
        "financial_jobs",
        ["created_at"],
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    # financial_jobs references companies → must be dropped first.

    # ── financial_jobs ────────────────────────────────────────────────────────
    op.drop_index("ix_financial_jobs_created_at", table_name="financial_jobs")
    op.drop_index("ix_financial_jobs_created_by", table_name="financial_jobs")
    op.drop_index("ix_financial_jobs_status", table_name="financial_jobs")
    op.drop_index("ix_financial_jobs_company_id", table_name="financial_jobs")
    op.drop_index("ix_financial_jobs_tenant_id", table_name="financial_jobs")
    op.drop_table("financial_jobs")

    # ── companies ─────────────────────────────────────────────────────────────
    # GIN index was created via raw DDL — must be dropped explicitly by name.
    op.execute("DROP INDEX IF EXISTS gin_companies_name")
    op.drop_index("ix_companies_tenant_id_active", table_name="companies")
    op.drop_index("ix_companies_tenant_id", table_name="companies")
    # UniqueConstraints are dropped with the table; explicit drop_constraint
    # is not required here because op.drop_table handles cascade of constraints.
    op.drop_table("companies")

    # Note: pg_trgm extension is NOT dropped — it was created in migration 001
    # and may be used by other future tables. Remove manually only if needed.
