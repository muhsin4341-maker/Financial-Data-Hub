"""006 stored_documents — M3.6 S3 Storage Pipeline.

Revision ID: a1b2c3d4e5f6
Revises: f3a7b2c5d9e1
Create Date: 2026-06-06

Table created:
  - stored_documents

Purpose:
  Tracks storage metadata for every filing document written to S3 or the
  local filesystem by DocumentStorageService.  One row per unique accession
  number; the row is the single source of truth for where the document lives.

Foreign key:
  - stored_documents.filing_id → filings.id   SET NULL on delete (nullable)

  The FK is advisory: DocumentStorageService may persist a StoredDocument
  before the corresponding Filing row is committed (e.g. in the acquisition
  worker pipeline).  SET NULL prevents orphan failures when a Filing is
  hard-deleted by an admin.

Unique constraint:
  - uq_stored_documents_accession_number (accession_number)
    Prevents duplicate storage records for the same filing.  Mirrors the
    deduplication check in DocumentStorageService.store().

    Listed in _KNOWN_INLINE_CONSTRAINTS in env.py to suppress false-positive
    Alembic drift reports.

Indexes:
  - ix_stored_documents_filing_id       — join from filings to stored_documents
  - ix_stored_documents_content_hash    — deduplication lookup by hash
  - ix_stored_documents_storage_type    — filter by backend ('local' vs 's3')
  - ix_stored_documents_stored_at       — timeline queries

Column notes:
  - storage_type    : VARCHAR(20); values: 'local' | 's3'.
  - bucket_name     : VARCHAR(255); S3 bucket name; NULL for local storage.
  - object_key      : VARCHAR(2000); storage key / relative path.
  - content_hash    : VARCHAR(64); SHA-256 hex digest.
  - content_length  : INTEGER; byte count of UTF-8 encoded content.
  - mime_type       : VARCHAR(100); detected MIME type, e.g. 'text/html'.
  - stored_at       : TIMESTAMPTZ; when the document was first written to storage.

Migration notes:
  - No tenant_id: stored_documents mirrors the global (non-tenant) design of filings.
  - No deleted_at: records are deleted when the document is purged.
  - uq_stored_documents_accession_number is created inline to follow the same pattern
    as uq_filings_accession_number in migration 005.
  - Column comments intentionally omitted — see migration 004 notes for rationale.

Downgrade drops stored_documents (safe — no FK constraints reference this table yet).

Milestone: M3.6 — S3 Storage Pipeline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision: str = "a1b2c3d4e5f6"
down_revision: str = "f3a7b2c5d9e1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Table: stored_documents ───────────────────────────────────────────────
    op.create_table(
        "stored_documents",
        # ── Primary key ───────────────────────────────────────────────────────
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        # ── Advisory FK to filings (nullable) ─────────────────────────────────
        sa.Column(
            "filing_id",
            UUID(as_uuid=True),
            sa.ForeignKey("filings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ── Filing identity ───────────────────────────────────────────────────
        sa.Column("accession_number", sa.String(25), nullable=False),
        # ── Storage location ──────────────────────────────────────────────────
        sa.Column("storage_type", sa.String(20), nullable=False),
        sa.Column("bucket_name", sa.String(255), nullable=True),
        sa.Column("object_key", sa.String(2000), nullable=False),
        # ── Content verification ──────────────────────────────────────────────
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        # ── Timing ───────────────────────────────────────────────────────────
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
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
        # ── Unique constraint (deduplication) ─────────────────────────────────
        sa.UniqueConstraint(
            "accession_number",
            name="uq_stored_documents_accession_number",
        ),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────

    # Join from filings to stored_documents.
    op.create_index(
        "ix_stored_documents_filing_id",
        "stored_documents",
        ["filing_id"],
    )

    # Deduplication and integrity check by content hash.
    op.create_index(
        "ix_stored_documents_content_hash",
        "stored_documents",
        ["content_hash"],
    )

    # Filter by backend type (local vs s3).
    op.create_index(
        "ix_stored_documents_storage_type",
        "stored_documents",
        ["storage_type"],
    )

    # Timeline queries — most recently stored first.
    op.create_index(
        "ix_stored_documents_stored_at",
        "stored_documents",
        ["stored_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_stored_documents_stored_at", table_name="stored_documents")
    op.drop_index("ix_stored_documents_storage_type", table_name="stored_documents")
    op.drop_index("ix_stored_documents_content_hash", table_name="stored_documents")
    op.drop_index("ix_stored_documents_filing_id", table_name="stored_documents")
    op.drop_table("stored_documents")
