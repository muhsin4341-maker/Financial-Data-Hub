"""007 acquisition_jobs — M3.7 Acquisition Jobs.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-06

Table created:
  - acquisition_jobs

Purpose:
  Tracks the lifecycle and progress of platform-level filing acquisition jobs.
  Each row represents one acquisition run for one company (identified by ticker).
  Jobs are dispatched to Celery workers via workers/tasks/acquisition_tasks.py.

Lifecycle:
  pending → running → completed | failed

No foreign keys:
  Acquisition jobs are deliberately decoupled from tenant / company records.
  The company is identified by ticker; the CIK is resolved at runtime.
  This allows jobs to be created before a company record exists in a tenant
  workspace, and lets the acquisition pipeline operate independently of the
  multi-tenant layer.

Indexes:
  - ix_acquisition_jobs_status      — find all pending / running jobs (worker polling)
  - ix_acquisition_jobs_ticker      — find all jobs for a ticker
  - ix_acquisition_jobs_cik         — find all jobs for a resolved CIK
  - ix_acquisition_jobs_created_at  — timeline ordering for admin list views

Column notes:
  - ticker          : VARCHAR(20); normalised to uppercase at service layer.
  - cik             : VARCHAR(10); 10-digit zero-padded; NULL until resolved.
  - company_name    : VARCHAR(255); populated after company resolution.
  - job_type        : VARCHAR(50); 'sec_filing_discovery'. Stored as VARCHAR for
                      forward compatibility.
  - status          : VARCHAR(50); 'pending' | 'running' | 'completed' | 'failed'.
  - error_message   : TEXT; populated when status = 'failed'.
  - filings_discovered : INTEGER; total filings returned by SECEdgarSource.
  - filings_new        : INTEGER; filings not previously in the filings table.
  - documents_fetched  : INTEGER; documents successfully downloaded.
  - documents_stored   : INTEGER; documents successfully persisted to storage.
  - started_at      : TIMESTAMPTZ; set when worker begins execution.
  - completed_at    : TIMESTAMPTZ; set on COMPLETED or FAILED terminal state.

Migration notes:
  - No tenant_id: acquisition jobs are platform-wide records.
  - No deleted_at: jobs are never deleted; they reach terminal states.
  - No inline UniqueConstraints — no entry needed in _KNOWN_INLINE_CONSTRAINTS.
  - Column comments intentionally omitted — see migration 004 notes for rationale.

Downgrade drops acquisition_jobs (safe — no FK constraints reference this table yet).

Milestone: M3.7 — Acquisition Jobs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "b2c3d4e5f6a7"
down_revision: str = "a1b2c3d4e5f6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "acquisition_jobs",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        # ── Company identity ──────────────────────────────────────────────────
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("cik", sa.String(10), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=True),
        # ── Classification ────────────────────────────────────────────────────
        sa.Column(
            "job_type",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'sec_filing_discovery'"),
        ),
        # ── Lifecycle ─────────────────────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        # ── Progress counters ─────────────────────────────────────────────────
        sa.Column(
            "filings_discovered", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "filings_new", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "documents_fetched", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "documents_stored", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
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
    )

    op.create_index("ix_acquisition_jobs_status", "acquisition_jobs", ["status"])
    op.create_index("ix_acquisition_jobs_ticker", "acquisition_jobs", ["ticker"])
    op.create_index("ix_acquisition_jobs_cik", "acquisition_jobs", ["cik"])
    op.create_index("ix_acquisition_jobs_created_at", "acquisition_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_acquisition_jobs_created_at", table_name="acquisition_jobs")
    op.drop_index("ix_acquisition_jobs_cik", table_name="acquisition_jobs")
    op.drop_index("ix_acquisition_jobs_ticker", table_name="acquisition_jobs")
    op.drop_index("ix_acquisition_jobs_status", table_name="acquisition_jobs")
    op.drop_table("acquisition_jobs")
