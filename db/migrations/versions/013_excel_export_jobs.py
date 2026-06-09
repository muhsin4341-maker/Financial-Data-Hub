"""013 excel_export_jobs — D2: Async Excel Export Pipeline database layer.

Revision ID: a2b3c4d5e6f7
Revises: e6f1a2b3c4d5
Create Date: 2026-06-08

Changes applied:
  1. New table: excel_export_jobs
       Tracks lifecycle state of asynchronous Excel workbook generation requests
       dispatched via the Celery QUEUE_EXPORT worker queue.

Architecture decision (D2 — Engineering Spec §11.2):
  The legacy synchronous export endpoint (GET /api/v1/jobs/{job_id}/export)
  blocks the web request for up to 30 seconds while the ExcelExportService
  assembles and styles a multi-sheet workbook.  Under concurrent load this
  exhausts FastAPI worker slots and causes request queue buildup.

  The async pipeline decouples submission from delivery:
    1. POST /api/v1/jobs/{job_id}/export/async
       Creates an excel_export_jobs record (status=PENDING) and dispatches
       generate_excel_export_task to QUEUE_EXPORT.  Returns immediately.
    2. Celery worker picks up the task, flips to GENERATING, builds the
       workbook, uploads the .xlsx bytes to S3, generates a pre-signed
       download URL, and flips to SUCCESS.
    3. Frontend polls GET /api/v1/jobs/export/{export_job_id}/status every
       2–3 seconds until SUCCESS or FAILED, then surfaces the download link.

Table design decisions:

  Status stored as VARCHAR(20) (not a DB-level ENUM):
    Allows adding states without ALTER TYPE migrations.  Application code
    uses the ExcelExportStatus Python StrEnum for type-safe comparisons.

  tenant_id + job_id:
    Both foreign keys are present so the status endpoint can enforce tenant
    isolation without a JOINs to financial_jobs.  job_id links to the
    FinancialJob that triggered the export.

  company_id:
    Denormalised copy of FinancialJob.company_id so the export record can be
    queried per-company without going through the job table.

  requested_by:
    FK to users.id — NULL for system-initiated exports.  Needed for audit
    trail and future per-user export rate limiting.

  s3_key / download_url:
    s3_key stores the raw bucket key so the application can delete the object
    on expiry (M8 cleanup beat).  download_url stores the pre-signed GET URL
    generated at SUCCESS time; it has a configurable TTL (default 24 hours).
    Both are NULL until the worker reaches SUCCESS.

  error_message:
    Stores the exception class + first 2 000 characters of the traceback so
    operators can diagnose failures without grepping Celery logs.

Indexes:
  ix_excel_export_jobs_tenant_status (tenant_id, status):
    Primary read pattern: "all pending/generating exports for this tenant".
    Used by the admin monitoring view (M8).

  ix_excel_export_jobs_job_id (job_id):
    Fast lookup: "latest export job for this FinancialJob" — used by the
    POST trigger endpoint to prevent duplicate concurrent exports.

  ix_excel_export_jobs_created_at (created_at):
    Used by the M8 cleanup beat task (delete SUCCESS/FAILED rows older than
    export_file_retention_days).

Downgrade:
  DROP TABLE excel_export_jobs CASCADE.
  Fully reversible — no other table references this table's PK.

Milestone: D2 — Async Excel Export Pipeline database layer
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision: str = "a2b3c4d5e6f7"
down_revision: str = "e6f1a2b3c4d5"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Create excel_export_jobs table ────────────────────────────────────────
    op.create_table(
        "excel_export_jobs",

        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            comment="UUID v7 primary key — time-ordered for B-tree performance.",
        ),

        # ── Tenancy ───────────────────────────────────────────────────────────
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning tenant — used for row-level isolation on status reads.",
        ),

        # ── Job linkage ───────────────────────────────────────────────────────
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("financial_jobs.id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "FinancialJob this export was generated for.  "
                "Multiple export records can share the same job_id (re-runs)."
            ),
        ),
        sa.Column(
            "company_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Denormalised copy of FinancialJob.company_id for fast "
                "per-company export history queries."
            ),
        ),

        # ── Actor ─────────────────────────────────────────────────────────────
        sa.Column(
            "requested_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="User who triggered the export.  NULL for system-initiated runs.",
        ),

        # ── Lifecycle status ──────────────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="PENDING",
            comment=(
                "Lifecycle state of this export job.  "
                "Values: PENDING → GENERATING → SUCCESS | FAILED.  "
                "Stored as VARCHAR to avoid ALTER TYPE migrations."
            ),
        ),

        # ── Output artefact ───────────────────────────────────────────────────
        sa.Column(
            "s3_key",
            sa.String(512),
            nullable=True,
            comment=(
                "S3 object key within the exports bucket.  "
                "Format: {tenant_id}/exports/{export_job_id}.xlsx.  "
                "NULL until worker transitions to SUCCESS."
            ),
        ),
        sa.Column(
            "download_url",
            sa.Text(),
            nullable=True,
            comment=(
                "Pre-signed S3 GET URL valid for export_signed_url_expiry_seconds.  "
                "NULL until worker transitions to SUCCESS."
            ),
        ),

        # ── Error diagnostics ─────────────────────────────────────────────────
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment=(
                "Exception class + truncated traceback from the worker.  "
                "NULL for non-FAILED records.  Max 2 000 characters."
            ),
        ),

        # ── Timestamps ────────────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="UTC timestamp when the export request was received.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="UTC timestamp of the last status transition.",
        ),

        # ── Constraint ────────────────────────────────────────────────────────
        sa.PrimaryKeyConstraint("id", name="pk_excel_export_jobs"),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────

    # (tenant_id, status) — primary admin monitoring pattern
    op.create_index(
        "ix_excel_export_jobs_tenant_status",
        "excel_export_jobs",
        ["tenant_id", "status"],
    )

    # job_id — fast lookup by financial job
    op.create_index(
        "ix_excel_export_jobs_job_id",
        "excel_export_jobs",
        ["job_id"],
    )

    # created_at — M8 cleanup beat task
    op.create_index(
        "ix_excel_export_jobs_created_at",
        "excel_export_jobs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_excel_export_jobs_created_at", table_name="excel_export_jobs")
    op.drop_index("ix_excel_export_jobs_job_id", table_name="excel_export_jobs")
    op.drop_index("ix_excel_export_jobs_tenant_status", table_name="excel_export_jobs")
    op.drop_table("excel_export_jobs")
