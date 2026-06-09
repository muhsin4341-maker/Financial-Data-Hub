"""
Repository for FinancialLineItem — M4.1.

Provides three public async methods:

  bulk_upsert(items)
    Insert a batch of FinancialLineItemCreate rows using a PostgreSQL
    INSERT … ON CONFLICT ON CONSTRAINT uq_financial_line_items_point_in_time DO NOTHING.
    Returns the count of rows actually inserted (skipped rows are not counted).
    UUIDs are generated explicitly via gen_uuid7() so they are time-ordered
    and bypass ORM default-factory timing issues with async bulk inserts.

  get_current_values(company_id, fiscal_year, fiscal_period)
    Return all non-restated line items for a given company-period pair.
    Uses the partial index ix_financial_line_items_current for efficiency.

  list_by_company_period(company_id, fiscal_year, fiscal_period, *, offset, limit)
    Paginated read of ALL rows (including restatements) for a company-period.
    Ordered by filing_date DESC, canonical_field ASC so callers receive the
    most-recent-first view naturally.

Design notes:
  - No session management: the caller is responsible for begin/commit/rollback.
    The repository only calls session.flush() in bulk_upsert to force the
    INSERT before any later operations in the same transaction unit-of-work.
  - bulk_upsert uses the low-level SQLAlchemy Core pg_insert dialect to avoid
    the ORM's SELECT-before-INSERT overhead for large batches.
  - All monetary Decimal values are passed to the DB driver as-is; psycopg2
    and asyncpg both map Python Decimal → NUMERIC natively.
  - The repository is intentionally thin — business logic (sign convention,
    FX translation) belongs in AIExtractionService (M4.2).

Point-in-time model:
  The unique constraint uq_financial_line_items_point_in_time covers
  (company_id, fiscal_year, fiscal_period, canonical_field, filing_date).
  A restatement is a new row with a later filing_date and is_restated=True.
  The original row is NEVER updated.  ON CONFLICT DO NOTHING is therefore
  idempotent: re-running the same extraction job is safe.

Milestone: M4.1 — FinancialLineItem Repository & Schemas
"""

from __future__ import annotations

import uuid
from typing import Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import FinancialLineItem, gen_uuid7
from apps.api.schemas.financial_line_items import FinancialLineItemCreate

log = structlog.get_logger(__name__)


class FinancialLineItemRepository:
    """
    Async repository for FinancialLineItem persistence and retrieval.

    Instantiate with an active AsyncSession:

        repo = FinancialLineItemRepository(session)
        inserted = await repo.bulk_upsert(items)

    The repository does NOT commit; the caller owns the transaction lifecycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -------------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------------

    async def bulk_upsert(
        self,
        items: Sequence[FinancialLineItemCreate],
    ) -> int:
        """
        Insert a batch of FinancialLineItem rows using ON CONFLICT DO NOTHING.

        Each row is identified by the composite unique constraint:
          uq_financial_line_items_point_in_time
          (company_id, fiscal_year, fiscal_period, canonical_field, filing_date)

        Rows whose constraint key already exists are silently skipped; the
        original row is never overwritten (point-in-time immutability).

        UUIDs are generated explicitly via gen_uuid7() — this is necessary
        because SQLAlchemy's ORM default factories fire per-object at flush
        time, not at INSERT-statement construction time.  Generating them here
        ensures every row has a stable, time-ordered primary key regardless of
        how SQLAlchemy constructs the batch statement.

        Parameters
        ----------
        items:
            Sequence of validated FinancialLineItemCreate instances.

        Returns
        -------
        int
            Number of rows actually inserted (skipped rows are excluded).
            Returns 0 if ``items`` is empty.

        Raises
        ------
        sqlalchemy.exc.SQLAlchemyError
            Propagated as-is; the caller must handle rollback.
        """
        if not items:
            log.debug("bulk_upsert called with empty items list — no-op")
            return 0

        rows = [
            {
                "id": gen_uuid7(),
                "company_id": item.company_id,
                "fiscal_year": item.fiscal_year,
                "fiscal_period": item.fiscal_period,
                "reporting_standard": item.reporting_standard,
                "filing_date": item.filing_date,
                "is_restated": item.is_restated,
                "canonical_field": item.canonical_field,
                "statement_type": item.statement_type,
                "value_usd": item.value_usd,
                "value_reported": item.value_reported,
                "reported_currency": item.reported_currency,
                "fx_rate_used": item.fx_rate_used,
                "source_file_hash": item.source_file_hash,
                "extraction_method": item.extraction_method,
                "derived_expression_formula": item.derived_expression_formula,
                # migration 014 — nullable; getattr guards against older callers
                # that pass FinancialLineItemCreate objects without this field
                "reporting_framework": getattr(item, "reporting_framework", None),
            }
            for item in items
        ]

        stmt = (
            pg_insert(FinancialLineItem)
            .values(rows)
            .on_conflict_do_nothing(
                constraint="uq_financial_line_items_point_in_time"
            )
        )

        result = await self._session.execute(stmt)

        # rowcount is the number of rows that were actually inserted.
        # For ON CONFLICT DO NOTHING, skipped rows are NOT counted.
        inserted: int = result.rowcount if result.rowcount is not None else 0

        log.info(
            "bulk_upsert complete",
            submitted=len(rows),
            inserted=inserted,
            skipped=len(rows) - inserted,
        )
        return inserted

    # -------------------------------------------------------------------------
    # Read — current values (non-restated)
    # -------------------------------------------------------------------------

    async def get_current_values(
        self,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
    ) -> list[FinancialLineItem]:
        """
        Return all non-restated FinancialLineItem rows for a company-period.

        Queries using ``is_restated = FALSE`` which is covered by the partial
        index ix_financial_line_items_current:
          (company_id, fiscal_year, fiscal_period, canonical_field)
          WHERE is_restated = FALSE

        This is the primary read path for financial analysis — it returns the
        "current authoritative" value for each canonical_field in the period.
        Where multiple non-restated rows exist for the same canonical_field
        (e.g. multiple filings on the same filing_date), they are all returned
        and the caller is responsible for disambiguation.

        Parameters
        ----------
        company_id:
            UUID of the company.
        fiscal_year:
            4-digit fiscal year (e.g. 2024).
        fiscal_period:
            Fiscal period label: 'Q1', 'Q2', 'Q3', 'Q4', or 'FY'.

        Returns
        -------
        list[FinancialLineItem]
            ORM instances, ordered by canonical_field ASC.
            Empty list if no matching rows exist.
        """
        stmt = (
            select(FinancialLineItem)
            .where(
                FinancialLineItem.company_id == company_id,
                FinancialLineItem.fiscal_year == fiscal_year,
                FinancialLineItem.fiscal_period == fiscal_period,
                FinancialLineItem.is_restated == False,  # noqa: E712 — SQLAlchemy requires ==
            )
            .order_by(FinancialLineItem.canonical_field.asc())
        )

        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())

        log.debug(
            "get_current_values",
            company_id=str(company_id),
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            row_count=len(rows),
        )
        return rows

    # -------------------------------------------------------------------------
    # Read — paginated full history (includes restatements)
    # -------------------------------------------------------------------------

    async def list_by_company_period(
        self,
        company_id: uuid.UUID,
        fiscal_year: int,
        fiscal_period: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[FinancialLineItem]:
        """
        Return a paginated slice of ALL FinancialLineItem rows for a
        company-period, including restatement rows.

        Ordering: filing_date DESC (most recent first), canonical_field ASC.
        This ordering means the caller sees the latest filing's values for each
        canonical_field at the top of the list, followed by earlier or restated
        rows for audit-trail purposes.

        Use ``get_current_values()`` when only the authoritative non-restated
        values are needed.  Use this method for audit trails, restatement
        comparison, or building a full period history.

        Parameters
        ----------
        company_id:
            UUID of the company.
        fiscal_year:
            4-digit fiscal year.
        fiscal_period:
            Fiscal period label: 'Q1' | 'Q2' | 'Q3' | 'Q4' | 'FY'.
        offset:
            Number of rows to skip (0-based).  Defaults to 0.
        limit:
            Maximum rows to return.  Defaults to 100.  Callers should cap
            this at a sensible maximum (e.g. 500) to protect memory.

        Returns
        -------
        list[FinancialLineItem]
            ORM instances.  Empty list if no matching rows exist or if
            ``offset`` exceeds the result set.
        """
        stmt = (
            select(FinancialLineItem)
            .where(
                FinancialLineItem.company_id == company_id,
                FinancialLineItem.fiscal_year == fiscal_year,
                FinancialLineItem.fiscal_period == fiscal_period,
            )
            .order_by(
                FinancialLineItem.filing_date.desc(),
                FinancialLineItem.canonical_field.asc(),
            )
            .offset(offset)
            .limit(limit)
        )

        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())

        log.debug(
            "list_by_company_period",
            company_id=str(company_id),
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            offset=offset,
            limit=limit,
            row_count=len(rows),
        )
        return rows
