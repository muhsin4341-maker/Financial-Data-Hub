"""
Financial line item writer — M4 Step 4: bulk DB persistence and versioning.

Responsibilities:
  1. High-precision mapping enforcement (Amendment V1.2 §1.1):
       value_reported / value_usd → NUMERIC(26,2) via Decimal quantisation.
       fx_rate_used               → NUMERIC(38,10) via Decimal quantisation.
       All Decimal values are passed as Python Decimal objects — float is
       never used, preventing IEEE 754 drift on monetary amounts.

  2. Historical restatement & multi-version management (§1.2 / §7.2):
       The composite unique constraint (company_id, fiscal_year, fiscal_period,
       canonical_field, filing_date) enforces point-in-time immutability.

       When an incoming item overlaps an existing non-restated row on the
       (company_id, fiscal_year, fiscal_period, canonical_field) context block:
         • incoming.filing_date > existing.filing_date
             → Mark existing row as is_restated = TRUE  (silent atomic UPDATE).
             → Insert incoming row with is_restated = FALSE.
             (Compliant with ASC 250 / IAS 8 / Ind AS 8 — no row is ever
             deleted or overwritten; the older row survives as audit history.)
         • incoming.filing_date == existing.filing_date
             → Skip: idempotent re-run of the same filing.
         • incoming.filing_date < existing.filing_date
             → Skip: stale data should not replace newer verified data.

  3. Validation log persistence (Amendment V1.2 §5 / §1.8):
       Every write call persists a ValidationResult row into the
       validation_results table so the frontend can query rule-level
       findings and confidence scores per accession.

  4. Export gate — job status update (Amendment V1.2 §5):
       If ValidationReport.is_exportable is False (CRITICAL finding exists),
       all ParsedLineItem rows ARE written to the database for audit tracking,
       but the parent FinancialJob status is set to
       'failed_validation_blocked', freezing automated Excel export.

Architecture position:
  parse_xbrl_document (M4 Step 1)
    ↓  list[ParsedLineItem]
  ValidationEngine.validate_parsed_items (M4 Step 3)
    ↓  ValidationReport
  FinancialLineItemWriter.write (this module)  ← current step
    ↓  commits financial_line_items + validation_results rows
    ↓  optionally marks job as failed_validation_blocked

Transaction ownership:
  This writer NEVER commits the session.  The caller (Celery task, M4 Step 3
  wiring) owns the transaction boundary and must call:
      await session.commit()   — on success
      await session.rollback() — on exception

  This mirrors the convention used by all other repositories in this project.

Precision constants (Amendment V1.2 §1.1):
  _QUANT_26_2  = Decimal("0.01")          — NUMERIC(26,2)  monetary values
  _QUANT_38_10 = Decimal("0.0000000001")  — NUMERIC(38,10) FX / per-share

Milestone: M4 Step 4 — Bulk DB Persistence & Versioning
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import FinancialLineItem, FinancialJob, gen_uuid7

if TYPE_CHECKING:
    from services.ingestion.parsers.xbrl_parser import ParsedLineItem
    from services.validation.engine import ValidationReport

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Precision quantisation constants (Amendment V1.2 §1.1)
# ---------------------------------------------------------------------------

# NUMERIC(26,2) — absolute monetary values (revenue, assets, net_income, etc.)
_QUANT_26_2: Decimal = Decimal("0.01")

# NUMERIC(38,10) — FX translation coefficients, per-share metrics, ratios
_QUANT_38_10: Decimal = Decimal("0.0000000001")

# Job status written when is_exportable=False (stored as VARCHAR(50))
_STATUS_VALIDATION_BLOCKED = "failed_validation_blocked"

# EPS and per-share canonical field names that use NUMERIC(38,10) precision.
# All other financial values use NUMERIC(26,2).
_HIGH_PRECISION_FIELDS: frozenset[str] = frozenset({
    "eps_basic",
    "eps_diluted",
    "shares_basic",
    "shares_diluted",
    "book_value_per_share",
})


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    """
    Summary of a completed FinancialLineItemWriter.write() call.

    Attributes:
        items_inserted:        New rows written with is_restated=FALSE.
        items_restated:        Prior rows marked is_restated=TRUE because a
                               newer filing supersedes them.
        items_skipped:         Items not written — either idempotent re-runs
                               (same filing_date) or stale data (older date).
        validation_result_id:  UUID of the persisted validation_results row.
        job_status_updated_to: New job status string if updated, else None.
    """

    items_inserted: int = 0
    items_restated: int = 0
    items_skipped: int = 0
    validation_result_id: uuid.UUID | None = None
    job_status_updated_to: str | None = None

    @property
    def total_processed(self) -> int:
        return self.items_inserted + self.items_skipped

    def summary(self) -> str:
        return (
            f"WriteResult: {self.items_inserted} inserted, "
            f"{self.items_restated} prior rows restated, "
            f"{self.items_skipped} skipped"
        )


# ---------------------------------------------------------------------------
# Precision helpers
# ---------------------------------------------------------------------------


def _quantise_monetary(value: Decimal | None) -> Decimal | None:
    """
    Round a monetary Decimal to NUMERIC(26,2) precision.

    Amendment V1.2 §1.1: All absolute monetary values must be stored as
    NUMERIC(26,2).  ROUND_HALF_EVEN (banker's rounding) minimises cumulative
    bias across large data sets.

    Returns None unchanged (NULL in the DB column).
    """
    if value is None:
        return None
    return value.quantize(_QUANT_26_2, rounding=ROUND_HALF_EVEN)


def _quantise_fx(value: Decimal | None) -> Decimal | None:
    """
    Round an FX rate to NUMERIC(38,10) precision.

    Amendment V1.2 §1.1: FX translation coefficients use NUMERIC(38,10).
    """
    if value is None:
        return None
    return value.quantize(_QUANT_38_10, rounding=ROUND_HALF_EVEN)


def _quantise_value(canonical_field: str, value: Decimal | None) -> Decimal | None:
    """
    Apply the correct precision tier for a canonical field (Amendment V1.2 §1.1).

    Per-share metrics and EPS fields use NUMERIC(38,10) via _quantise_fx.
    All other monetary values use NUMERIC(26,2) via _quantise_monetary.
    """
    if canonical_field in _HIGH_PRECISION_FIELDS:
        return _quantise_fx(value)
    return _quantise_monetary(value)


# ---------------------------------------------------------------------------
# Serialisation helpers for JSONB columns
# ---------------------------------------------------------------------------


def _serialise_findings(report: ValidationReport) -> list[dict]:
    """Convert ValidationResult.findings to a JSON-safe list for JSONB storage."""
    rows = []
    for f in report.validation_result.findings:
        rows.append({
            "rule_id":  f.rule_id,
            "severity": f.severity.value,
            "message":  f.message,
            "expected": str(f.expected) if f.expected is not None else None,
            "actual":   str(f.actual)   if f.actual   is not None else None,
            "delta":    str(f.delta)    if f.delta     is not None else None,
        })
    return rows


def _serialise_deductions(report: ValidationReport) -> list[dict]:
    """Convert ConfidenceScore.deductions to a JSON-safe list for JSONB storage."""
    return [
        {"rule_id": rule_id, "points": pts, "reason": reason}
        for rule_id, pts, reason in report.confidence.deductions
    ]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class FinancialLineItemWriter:
    """
    Bulk-writes parsed financial data to PostgreSQL with full restatement
    versioning and validation log persistence.

    Instantiated per Celery task execution::

        writer = FinancialLineItemWriter(session)
        result = await writer.write(
            items,
            report,
            job_id=job_id,
            accession_number=accession_number,
        )
        await session.commit()

    The writer NEVER calls session.commit() — the caller owns the boundary.

    Amendment V1.2 §1.1: All Decimal values are quantised to the correct
    precision tier before being bound to INSERT/UPDATE statements.

    Amendment V1.2 §1.2 / §7.2: Restatement logic ensures no row is ever
    silently overwritten. Each distinct filing_date creates its own immutable
    row. Superseded rows receive is_restated=TRUE.

    Amendment V1.2 §5: Every write call persists a validation_results row.
    If is_exportable=False, the job status is set to 'failed_validation_blocked'.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Public interface ───────────────────────────────────────────────────────

    async def write(
        self,
        items: list[ParsedLineItem],
        report: ValidationReport,
        *,
        job_id: uuid.UUID | None = None,
        accession_number: str = "",
    ) -> WriteResult:
        """
        Atomically persist parsed line items and the validation report.

        Processing order (all within the caller's transaction):
          1. Build a batch lookup of existing non-restated rows that overlap
             with the incoming items on (company_id, fiscal_year, fiscal_period,
             canonical_field).
          2. For each incoming item:
               a. No existing match → INSERT with is_restated=FALSE.
               b. Existing match, older filing_date → UPDATE existing
                  is_restated=TRUE, then INSERT new with is_restated=FALSE.
               c. Existing match, same filing_date → skip (idempotent).
               d. Existing match, newer filing_date → skip (stale data).
          3. Persist the ValidationReport to validation_results.
          4. If not report.is_exportable and job_id is set → update
             FinancialJob.status = 'failed_validation_blocked'.

        Args:
            items:            Parsed line items from parse_xbrl_document().
            report:           ValidationReport from validate_parsed_items().
            job_id:           FinancialJob UUID to update status on (optional).
            accession_number: SEC accession number for the validation log.

        Returns:
            WriteResult with insert/restate/skip counts and the
            validation_results row UUID.

        Note:
            This method flushes the session after bulk operations so that
            generated UUIDs and server_defaults are available before the
            caller's commit.  It does NOT commit.
        """
        result = WriteResult()

        if not items:
            log.info("writer.no_items", accession_number=accession_number)
            result.validation_result_id = await self._persist_validation_result(
                report, accession_number=accession_number, job_id=job_id,
                company_id=None, fiscal_year=None, fiscal_period=None,
            )
            await self._maybe_update_job_status(report, job_id, result)
            await self._session.flush()
            return result

        # Derive primary period context from the majority of items (FY preferred).
        primary_company_id, primary_year, primary_period = _infer_primary_context(items)

        # ── Step 1: Batch fetch all overlapping non-restated rows ───────────────
        existing_map = await self._fetch_existing_non_restated(items)

        # ── Step 2: Classify and process each incoming item ────────────────────
        to_insert: list[dict] = []
        ids_to_restate: list[uuid.UUID] = []

        for item in items:
            lookup_key = (
                str(item.company_id),
                item.fiscal_year,
                item.fiscal_period,
                item.canonical_field,
            )
            existing = existing_map.get(lookup_key)

            if existing is None:
                # No overlap — new insert.
                to_insert.append(self._build_row(item, is_restated=False))
                result.items_inserted += 1

            elif item.filing_date > existing.filing_date:
                # Incoming is newer — restate the old row, insert fresh one.
                ids_to_restate.append(existing.id)
                to_insert.append(self._build_row(item, is_restated=False))
                result.items_inserted += 1
                result.items_restated += 1
                log.debug(
                    "writer.restatement",
                    company_id=str(item.company_id),
                    fiscal_year=item.fiscal_year,
                    fiscal_period=item.fiscal_period,
                    canonical_field=item.canonical_field,
                    old_filing_date=str(existing.filing_date),
                    new_filing_date=str(item.filing_date),
                )

            else:
                # Same or older filing_date — skip.
                result.items_skipped += 1
                log.debug(
                    "writer.skip",
                    canonical_field=item.canonical_field,
                    reason=(
                        "idempotent_same_date"
                        if item.filing_date == existing.filing_date
                        else "stale_older_date"
                    ),
                )

        # ── Step 3: Apply restatements atomically ──────────────────────────────
        if ids_to_restate:
            await self._session.execute(
                update(FinancialLineItem)
                .where(FinancialLineItem.id.in_(ids_to_restate))
                .values(is_restated=True, updated_at=datetime.now(UTC))
            )
            log.info(
                "writer.restatements_applied",
                count=len(ids_to_restate),
                accession_number=accession_number,
            )

        # ── Step 4: Bulk-insert new rows ───────────────────────────────────────
        if to_insert:
            await self._session.execute(
                pg_insert(FinancialLineItem),
                to_insert,
            )
            log.info(
                "writer.rows_inserted",
                count=len(to_insert),
                accession_number=accession_number,
            )

        # ── Step 5: Persist validation result ─────────────────────────────────
        result.validation_result_id = await self._persist_validation_result(
            report,
            accession_number=accession_number,
            job_id=job_id,
            company_id=primary_company_id,
            fiscal_year=primary_year,
            fiscal_period=primary_period,
        )

        # ── Step 6: Export gate — update job status ────────────────────────────
        await self._maybe_update_job_status(report, job_id, result)

        await self._session.flush()

        log.info(
            "writer.write_complete",
            accession_number=accession_number,
            items_inserted=result.items_inserted,
            items_restated=result.items_restated,
            items_skipped=result.items_skipped,
            is_exportable=report.is_exportable,
            confidence_score=report.confidence.final_score,
        )
        return result

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _fetch_existing_non_restated(
        self,
        items: list[ParsedLineItem],
    ) -> dict[tuple[str, int, str, str], FinancialLineItem]:
        """
        Batch-fetch all non-restated rows that share a context block with
        any incoming item.

        Uses the partial index ix_financial_line_items_current
        (WHERE is_restated = FALSE) for O(log n) lookup.

        Returns:
            Dict keyed by (company_id_str, fiscal_year, fiscal_period,
            canonical_field) → FinancialLineItem row.
        """
        # Collect unique (company_id, fiscal_year, fiscal_period) groups.
        # Within each group, collect the canonical_fields to check.
        group_to_fields: dict[tuple[str, int, str], set[str]] = {}
        for item in items:
            group_key = (str(item.company_id), item.fiscal_year, item.fiscal_period)
            group_to_fields.setdefault(group_key, set()).add(item.canonical_field)

        if not group_to_fields:
            return {}

        # Build a single SELECT that covers all groups via OR clauses.
        # For typical filings (1 company, 1-2 fiscal periods) this is 1-2 OR arms.
        from sqlalchemy import and_, or_

        conditions = []
        for (cid_str, fy, fp), fields in group_to_fields.items():
            conditions.append(
                and_(
                    FinancialLineItem.company_id == uuid.UUID(cid_str),
                    FinancialLineItem.fiscal_year == fy,
                    FinancialLineItem.fiscal_period == fp,
                    FinancialLineItem.canonical_field.in_(list(fields)),
                    FinancialLineItem.is_restated.is_(False),
                )
            )

        stmt = select(FinancialLineItem).where(or_(*conditions))
        rows = (await self._session.execute(stmt)).scalars().all()

        return {
            (
                str(row.company_id),
                row.fiscal_year,
                row.fiscal_period,
                row.canonical_field,
            ): row
            for row in rows
        }

    @staticmethod
    def _build_row(item: ParsedLineItem, *, is_restated: bool) -> dict:
        """
        Convert a ParsedLineItem into a dict ready for bulk INSERT.

        Amendment V1.2 §1.1: Decimal values are quantised to the correct
        precision tier before binding (NUMERIC(26,2) vs NUMERIC(38,10)).
        """
        now = datetime.now(UTC)
        return {
            "id":                       gen_uuid7(),
            "company_id":               uuid.UUID(str(item.company_id)),
            "fiscal_year":              int(item.fiscal_year),
            "fiscal_period":            item.fiscal_period,
            "reporting_standard":       item.reporting_standard,
            "filing_date":              item.filing_date,
            "is_restated":              is_restated,
            "canonical_field":          item.canonical_field,
            "statement_type":           item.statement_type,
            # §1.1 — quantise monetary value to NUMERIC(26,2) or NUMERIC(38,10)
            "value_reported":           _quantise_value(
                                            item.canonical_field,
                                            item.value_reported,
                                        ),
            # value_usd is populated by the currency normaliser (M4 Step 5);
            # left NULL here — the normaliser will UPDATE this column.
            "value_usd":                None,
            "reported_currency":        item.reported_currency,
            # fx_rate_used is populated by the currency normaliser; NULL for now.
            "fx_rate_used":             None,
            # §4.2 — SHA-256 audit trail link.
            "source_file_hash":         item.source_file_hash,
            "derived_expression_formula": item.derived_expression_formula,
            "extraction_method":        item.extraction_method,
            "created_at":               now,
            "updated_at":               now,
        }

    async def _persist_validation_result(
        self,
        report: ValidationReport,
        *,
        accession_number: str,
        job_id: uuid.UUID | None,
        company_id: str | None,
        fiscal_year: int | None,
        fiscal_period: str | None,
    ) -> uuid.UUID:
        """
        Insert one row into validation_results.

        Stores the full finding list and deduction log in JSONB columns so the
        frontend data grid can render per-rule validation status (Amendment V1.2 §5)
        and confidence breakdown (Amendment V1.2 §1.8).

        Returns the new row's UUID.
        """
        from sqlalchemy import text as sa_text
        from sqlalchemy.dialects.postgresql import insert as pg_insert_local

        row_id = gen_uuid7()
        now = datetime.now(UTC)

        stmt = sa_text(
            """
            INSERT INTO validation_results (
                id, accession_number, company_id, fiscal_year, fiscal_period,
                job_id, items_validated, is_exportable, critical_count,
                warning_count, confidence_score, findings, deductions,
                summary_text, created_at
            ) VALUES (
                :id, :accession_number, :company_id, :fiscal_year, :fiscal_period,
                :job_id, :items_validated, :is_exportable, :critical_count,
                :warning_count, :confidence_score, :findings::jsonb,
                :deductions::jsonb, :summary_text, :created_at
            )
            """
        )

        import json

        await self._session.execute(
            stmt,
            {
                "id":               row_id,
                "accession_number": accession_number,
                "company_id":       uuid.UUID(company_id) if company_id else None,
                "fiscal_year":      fiscal_year,
                "fiscal_period":    fiscal_period,
                "job_id":           job_id,
                "items_validated":  report.items_validated,
                "is_exportable":    report.is_exportable,
                "critical_count":   len(report.validation_result.critical_findings),
                "warning_count":    len(report.validation_result.warning_findings),
                "confidence_score": report.confidence.final_score,
                "findings":         json.dumps(_serialise_findings(report)),
                "deductions":       json.dumps(_serialise_deductions(report)),
                "summary_text":     report.summary(),
                "created_at":       now,
            },
        )

        log.debug(
            "writer.validation_result_persisted",
            validation_result_id=str(row_id),
            accession_number=accession_number,
            is_exportable=report.is_exportable,
            confidence_score=report.confidence.final_score,
        )
        return row_id

    async def _maybe_update_job_status(
        self,
        report: ValidationReport,
        job_id: uuid.UUID | None,
        result: WriteResult,
    ) -> None:
        """
        Amendment V1.2 §5 export gate: if validation is blocked, freeze
        the parent FinancialJob so automated Excel export cannot proceed.

        Sets job.status = 'failed_validation_blocked' when:
          - job_id is provided (not None)
          - report.is_exportable is False (CRITICAL finding exists)
          - The job is not already in a terminal state

        The status string 'failed_validation_blocked' is intentionally stored
        as a plain VARCHAR(50) — the FinancialJob.status column was designed
        for forward compatibility without ALTER TYPE migrations.
        """
        if job_id is None or report.is_exportable:
            return

        job_row = await self._session.get(FinancialJob, job_id)
        if job_row is None:
            log.warning(
                "writer.job_not_found",
                job_id=str(job_id),
                reason="cannot_update_status",
            )
            return

        # Do not overwrite already-terminal states (cancelled, completed, etc.)
        # unless they are in a non-terminal running/queued state.
        _non_terminal = {"pending", "queued", "running"}
        if job_row.status not in _non_terminal:
            log.debug(
                "writer.job_status_skip",
                job_id=str(job_id),
                current_status=job_row.status,
                reason="already_terminal",
            )
            return

        job_row.status = _STATUS_VALIDATION_BLOCKED
        job_row.updated_at = datetime.now(UTC)
        job_row.error_message = (
            f"Validation blocked — "
            f"{len(report.validation_result.critical_findings)} CRITICAL "
            f"finding(s). Confidence: {report.confidence.final_score}/100. "
            f"See validation_results row for details."
        )

        result.job_status_updated_to = _STATUS_VALIDATION_BLOCKED
        log.warning(
            "writer.job_blocked",
            job_id=str(job_id),
            critical_count=len(report.validation_result.critical_findings),
            confidence_score=report.confidence.final_score,
        )


# ---------------------------------------------------------------------------
# Context inference helper
# ---------------------------------------------------------------------------


def _infer_primary_context(
    items: list[ParsedLineItem],
) -> tuple[str | None, int | None, str | None]:
    """
    Infer the primary (company_id, fiscal_year, fiscal_period) from a list
    of ParsedLineItem objects for use in the validation_results row.

    Strategy: prefer the FY period; among FY items pick the most common
    (company_id, fiscal_year).  Falls back to first item if no FY exists.

    Returns:
        (company_id_str, fiscal_year, fiscal_period) or (None, None, None).
    """
    if not items:
        return None, None, None

    fy_items = [i for i in items if i.fiscal_period == "FY"]
    candidates = fy_items if fy_items else items

    # Most common (company_id, fiscal_year) combination.
    from collections import Counter
    counter: Counter[tuple[str, int]] = Counter(
        (str(i.company_id), i.fiscal_year) for i in candidates
    )
    (cid_str, fy), _ = counter.most_common(1)[0]
    return cid_str, fy, "FY" if fy_items else candidates[0].fiscal_period
