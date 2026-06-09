"""012 daily_fx_rates — M5.1 FX Translation database layer.

Revision ID: e6f1a2b3c4d5
Revises: f1a2b3c4d5e6
Create Date: 2026-06-07

Changes applied:
  1. New table: daily_fx_rates
       Stores one daily closing exchange rate per currency pair per calendar date.
       This table is the concrete backing store for the FXRateRepository Protocol
       defined in services/extraction/normaliser/currency.py and consumed by
       services/currency/translator.py::HistoricalFXRateProvider.

Dual-pass FX requirement (Amendment V1.2 §1.3 / §3):
  Balance Sheet (BS) items are translated at the SPOT rate on period_end_date.
  Income Statement (IS) and Cash Flow (CF) items are translated at the
  ARITHMETIC WEIGHTED AVERAGE of daily closing rates over the fiscal period.
  Both translation modes are computed from this single table — no pre-aggregated
  average column is stored; the engine queries a date range and averages in Python.

Table design decisions:
  Composite PK (rate_date, from_currency, to_currency):
    The natural business key provides the uniqueness guarantee required by
    the FX engine without a surrogate UUID PK.  PostgreSQL enforces the
    composite key as a B-tree index on all three columns, which also covers
    the most common query pattern (equality on currency pair + range on date).

  NUMERIC(38, 10) for rate:
    Matches FinancialLineItem.fx_rate_used precision (Amendment V1.2 §1.1).
    Prevents rounding loss when translating large absolute monetary values
    (e.g. Apple FY2024 revenue USD 391 035 000 000 * INR/USD rate).

  No tenant_id:
    Exchange rates are global system reference data, consistent with
    source_configs, filings, and financial_line_items.

  No UUID PK:
    The composite natural key IS the business key. A surrogate PK would add
    B-tree overhead without providing any additional query or JOIN benefit —
    no foreign key references this table from other tables.

Indexes:
  ix_daily_fx_rates_from_to_date (from_currency, to_currency, rate_date):
    Primary read pattern: "all rates for INR→USD between 2024-04-01 and 2024-03-31"
    Column order puts high-selectivity equality filters first; date range last.
    Covers both get_rate() (single date lookup) and _get_range() (period average).

  ix_daily_fx_rates_rate_date (rate_date):
    Secondary pattern: "all pairs on a given date" — used by bulk ingestion to
    check for existing rows before INSERT to avoid duplicate-key errors.

Downgrade:
  DROP TABLE daily_fx_rates CASCADE.
  Fully reversible — the table contains only reference data; no other table
  has a foreign key pointing to it.

Engineering Specification references:
  Amendment V1.2, Section 1.1  — NUMERIC(38,10) for FX coefficients
  Amendment V1.2, Section 1.3  — Dual-pass translation requirement
  Amendment V1.2, Section 3    — Split currency translation (ASC 830 / IAS 21 / Ind AS 21)
  M5.1                         — DailyFXRate database layer
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision: str = "e6f1a2b3c4d5"
down_revision: str = "f1a2b3c4d5e6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Create daily_fx_rates table ───────────────────────────────────────────
    op.create_table(
        "daily_fx_rates",

        # ── Composite primary key ─────────────────────────────────────────────
        # (rate_date, from_currency, to_currency) is the natural unique key for
        # one closing rate per currency pair per calendar day.
        sa.Column(
            "rate_date",
            sa.Date(),
            nullable=False,
            comment=(
                "Calendar date of the closing rate.  "
                "Weekends and holidays are typically absent; "
                "HistoricalFXRateProvider uses a 5-day look-back to fill gaps."
            ),
        ),
        sa.Column(
            "from_currency",
            sa.String(3),
            nullable=False,
            comment="ISO 4217 source currency code (e.g. 'INR', 'EUR', 'GBP').",
        ),
        sa.Column(
            "to_currency",
            sa.String(3),
            nullable=False,
            comment=(
                "ISO 4217 target currency code (e.g. 'USD').  "
                "Most pipelines use USD as the universal target currency."
            ),
        ),

        # ── Rate — NUMERIC(38, 10) per Amendment V1.2 §1.1 ───────────────────
        sa.Column(
            "rate",
            sa.Numeric(38, 10),
            nullable=False,
            comment=(
                "Closing rate: units of to_currency per 1 unit of from_currency.  "
                "Example: from_currency='INR', to_currency='USD', "
                "rate=0.0120187293 means 1 INR = 0.0120187293 USD.  "
                "NUMERIC(38,10) per Amendment V1.2 §1.1."
            ),
        ),

        # ── Audit timestamps ──────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="UTC timestamp when this rate row was first inserted.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment=(
                "UTC timestamp of the most recent update.  "
                "Rates may be revised if the source provider corrects a historical value."
            ),
        ),

        # ── Composite primary key constraint ──────────────────────────────────
        sa.PrimaryKeyConstraint(
            "rate_date",
            "from_currency",
            "to_currency",
            name="pk_daily_fx_rates",
        ),
    )

    # ── Index 1: primary read pattern ─────────────────────────────────────────
    # "All rates for currency pair X between date A and date B"
    # Used by HistoricalFXRateProvider for both single-date lookups and
    # period-average range queries.  Column order: equality filters first,
    # range filter last — optimal for PostgreSQL B-tree range scans.
    op.create_index(
        "ix_daily_fx_rates_from_to_date",
        "daily_fx_rates",
        ["from_currency", "to_currency", "rate_date"],
        unique=False,
    )

    # ── Index 2: secondary pattern ────────────────────────────────────────────
    # "All pairs available on a given date" — used by bulk ingestion to detect
    # existing rows before INSERT (deduplication check).
    op.create_index(
        "ix_daily_fx_rates_rate_date",
        "daily_fx_rates",
        ["rate_date"],
        unique=False,
    )


def downgrade() -> None:
    # ── Drop indexes first (implicit via DROP TABLE, but explicit is safer) ───
    op.drop_index("ix_daily_fx_rates_rate_date", table_name="daily_fx_rates")
    op.drop_index("ix_daily_fx_rates_from_to_date", table_name="daily_fx_rates")

    # ── Drop the table ────────────────────────────────────────────────────────
    # No foreign keys reference this table, so no CASCADE complications.
    op.drop_table("daily_fx_rates")
