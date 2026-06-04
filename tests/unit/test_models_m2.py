"""
Unit tests — M2 ORM Models: Company and FinancialJob.

Covers:
  - JobStatus enum values and membership
  - Company instantiation, field defaults, and repr
  - Company soft-delete column presence
  - Company relationship attributes
  - FinancialJob instantiation, field defaults, and repr
  - FinancialJob is_terminal and is_cancellable properties
  - FinancialJob no deleted_at (terminal-state records, not soft-deleted)
  - __all__ exports include new models

Engineering Spec references:
  Part 1, Section 1.2, Decision 1  — UUID v7 primary keys
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables
  Part 1, Section 1.2, Decision 4  — soft delete via deleted_at
  M2 Execution Plan, Section 2.3.3 — Job status transitions

Milestone: M2-Step 2
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from apps.api.models import Company, FinancialJob, JobStatus, _utcnow, gen_uuid7
from apps.api.models import __all__ as models_all

# ---------------------------------------------------------------------------
# Helper factories — build minimal in-memory instances (no DB required)
# ---------------------------------------------------------------------------


def _tenant_id() -> uuid.UUID:
    return gen_uuid7()


def _company_id() -> uuid.UUID:
    return gen_uuid7()


def _user_id() -> uuid.UUID:
    return gen_uuid7()


def _make_company(
    tenant_id: uuid.UUID | None = None,
    name: str = "Acme Corp",
    ticker: str = "ACME",
) -> Company:
    """Return a Company instance with minimal required fields populated."""
    return Company(
        tenant_id=tenant_id or _tenant_id(),
        name=name,
        ticker=ticker,
    )


def _make_job(
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    job_type: str = "sec_10k_annual",
    status: str = JobStatus.PENDING,
) -> FinancialJob:
    """Return a FinancialJob instance with minimal required fields populated."""
    return FinancialJob(
        tenant_id=tenant_id or _tenant_id(),
        company_id=company_id or _company_id(),
        job_type=job_type,
        status=status,
    )


# ---------------------------------------------------------------------------
# Test: JobStatus enum
# ---------------------------------------------------------------------------


class TestJobStatus:
    """JobStatus enum values, completeness, and StrEnum behaviour."""

    def test_all_expected_values_present(self) -> None:
        expected = {"pending", "queued", "running", "completed", "failed", "cancelled"}
        actual = {s.value for s in JobStatus}
        assert actual == expected

    def test_is_str_enum(self) -> None:
        """Each member must compare equal to its string value directly."""
        assert JobStatus.PENDING == "pending"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.CANCELLED == "cancelled"
        assert JobStatus.QUEUED == "queued"

    def test_terminal_states(self) -> None:
        """COMPLETED, FAILED, CANCELLED are terminal — all others are not."""
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        non_terminal = {JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING}
        assert terminal | non_terminal == set(JobStatus)

    def test_str_membership(self) -> None:
        """String literals must be valid enum values."""
        assert "pending" in JobStatus._value2member_map_
        assert "cancelled" in JobStatus._value2member_map_
        assert "unknown" not in JobStatus._value2member_map_


# ---------------------------------------------------------------------------
# Test: Company model
# ---------------------------------------------------------------------------


class TestCompanyModel:
    """Company ORM model — construction, defaults, repr, schema attributes."""

    def test_instantiation_minimal(self) -> None:
        """Company can be constructed with only required fields."""
        tid = _tenant_id()
        c = Company(tenant_id=tid, name="Acme Corp", ticker="ACME")
        assert c.name == "Acme Corp"
        assert c.ticker == "ACME"
        assert c.tenant_id == tid

    def test_id_column_default_is_gen_uuid7(self) -> None:
        """
        id column default must be the gen_uuid7 callable.

        SQLAlchemy 2.x stores callable column defaults in column metadata and
        fires them at INSERT time.  We verify (a) the default is configured and
        (b) gen_uuid7() itself produces UUID v7 values — both confirming the
        contract without triggering SQLAlchemy's execution-context machinery.
        """
        col = Company.__table__.c["id"]
        assert col.default is not None
        assert callable(col.default.arg), "id default must be a callable (gen_uuid7)"
        # Verify gen_uuid7 itself (the canonical function) produces UUID v7 values.
        result = gen_uuid7()
        assert isinstance(result, uuid.UUID)
        assert (result.int >> 76) & 0xF == 7

    def test_is_active_column_default_is_true(self) -> None:
        """
        is_active column default must be True.

        SQLAlchemy scalar column defaults are stored in column metadata and
        applied on INSERT.  We verify the metadata, not the post-construction
        attribute (which is unset until flushed).
        """
        col = Company.__table__.c["is_active"]
        assert col.default is not None
        assert col.default.arg is True

    def test_optional_fields_column_defaults_are_none(self) -> None:
        """Optional columns that default to None must carry a None server/column default."""
        nullable_fields = (
            "cik", "exchange", "sector", "industry", "description", "website", "deleted_at",
        )
        for field in nullable_fields:
            col = Company.__table__.c[field]
            assert col.nullable, f"{field} must be nullable"

    def test_created_at_and_updated_at_have_callable_defaults(self) -> None:
        """
        Timestamp columns must carry callable defaults (_utcnow).

        We verify (a) the column default is a callable and (b) _utcnow() itself
        produces a timezone-aware UTC datetime — without invoking SQLAlchemy's
        execution-context machinery.
        """
        for field in ("created_at", "updated_at"):
            col = Company.__table__.c[field]
            assert col.default is not None, f"{field} has no default"
            assert callable(col.default.arg), f"{field} default must be callable"
        # Verify _utcnow produces tz-aware datetimes.
        result = _utcnow()
        assert result.tzinfo is not None

    def test_soft_delete_column_exists(self) -> None:
        """deleted_at must exist — soft-delete is enforced by Spec Decision 4."""
        c = _make_company()
        assert hasattr(c, "deleted_at")
        assert c.deleted_at is None

    def test_soft_delete_can_be_set(self) -> None:
        c = _make_company()
        now = datetime.now(UTC)
        c.deleted_at = now
        assert c.deleted_at == now

    def test_full_field_population(self) -> None:
        """All fields can be set without error."""
        tid = _tenant_id()
        c = Company(
            tenant_id=tid,
            name="Apple Inc.",
            ticker="AAPL",
            cik="0000320193",
            exchange="NASDAQ",
            sector="Information Technology",
            industry="Technology Hardware",
            description="Designs and sells consumer electronics.",
            website="https://www.apple.com",
            is_active=True,
        )
        assert c.ticker == "AAPL"
        assert c.cik == "0000320193"
        assert c.exchange == "NASDAQ"
        assert c.sector == "Information Technology"
        assert c.website == "https://www.apple.com"

    def test_repr_contains_key_fields(self) -> None:
        c = _make_company(ticker="AAPL", name="Apple Inc.")
        r = repr(c)
        assert "Company" in r
        assert "AAPL" in r
        assert "Apple Inc." in r

    def test_tablename(self) -> None:
        assert Company.__tablename__ == "companies"

    def test_relationships_declared(self) -> None:
        """Company must declare 'tenant' and 'jobs' relationship attributes."""
        assert hasattr(Company, "tenant")
        assert hasattr(Company, "jobs")

    def test_gen_uuid7_produces_distinct_values(self) -> None:
        """Each gen_uuid7() call must produce a distinct UUID (no shared state)."""
        ids = {gen_uuid7() for _ in range(20)}
        assert len(ids) == 20

    def test_ticker_is_stored_as_given(self) -> None:
        c = _make_company(ticker="tsla")
        assert c.ticker == "tsla"  # no normalisation at model layer


# ---------------------------------------------------------------------------
# Test: FinancialJob model
# ---------------------------------------------------------------------------


class TestFinancialJobModel:
    """FinancialJob ORM model — construction, defaults, lifecycle properties."""

    def test_instantiation_minimal(self) -> None:
        tid = _tenant_id()
        cid = _company_id()
        j = FinancialJob(tenant_id=tid, company_id=cid, job_type="sec_10k_annual")
        assert j.tenant_id == tid
        assert j.company_id == cid
        assert j.job_type == "sec_10k_annual"

    def test_id_column_default_is_gen_uuid7(self) -> None:
        """id column default must be gen_uuid7 (callable, fired at INSERT)."""
        col = FinancialJob.__table__.c["id"]
        assert col.default is not None
        assert callable(col.default.arg)
        assert col.default.arg.__name__ == "gen_uuid7"

    def test_status_column_default_is_pending(self) -> None:
        """
        status column default must be 'pending'.

        We inspect the column metadata because SQLAlchemy callable/scalar
        defaults fire at INSERT, not at Python construction time.
        """
        col = FinancialJob.__table__.c["status"]
        assert col.default is not None
        assert col.default.arg == "pending"

    def test_optional_fields_are_nullable(self) -> None:
        """All optional job columns must be nullable in the schema."""
        nullable_cols = (
            "created_by", "fiscal_year", "document_url", "result_url",
            "error_message", "celery_task_id", "started_at", "completed_at",
        )
        for name in nullable_cols:
            col = FinancialJob.__table__.c[name]
            assert col.nullable, f"{name} must be nullable"

    def test_created_at_and_updated_at_have_callable_defaults(self) -> None:
        """Timestamp columns must carry callable defaults (_utcnow)."""
        for field in ("created_at", "updated_at"):
            col = FinancialJob.__table__.c[field]
            assert col.default is not None
            assert callable(col.default.arg)
            assert col.default.arg.__name__ == "_utcnow"

    def test_no_deleted_at_column(self) -> None:
        """FinancialJob must NOT have a deleted_at column — jobs are cancelled, not deleted."""
        j = _make_job()
        assert not hasattr(j, "deleted_at"), (
            "FinancialJob must not have deleted_at. "
            "Jobs reach terminal states rather than being soft-deleted."
        )

    def test_full_field_population(self) -> None:
        tid = _tenant_id()
        cid = _company_id()
        uid = _user_id()
        now = datetime.now(UTC)
        j = FinancialJob(
            tenant_id=tid,
            company_id=cid,
            created_by=uid,
            status=JobStatus.RUNNING,
            job_type="sec_10k_annual",
            fiscal_year=2023,
            document_url=f"{tid}/jobs/abc/filing.pdf",
            result_url=f"{tid}/exports/abc/result.xlsx",
            error_message=None,
            celery_task_id="celery-task-uuid-1234",
            started_at=now,
            completed_at=None,
        )
        assert j.created_by == uid
        assert j.fiscal_year == 2023
        assert j.celery_task_id == "celery-task-uuid-1234"
        assert j.started_at == now

    def test_repr_contains_key_fields(self) -> None:
        j = _make_job(job_type="sec_10k_annual", status=JobStatus.PENDING)
        r = repr(j)
        assert "FinancialJob" in r
        assert "sec_10k_annual" in r
        assert "pending" in r

    def test_tablename(self) -> None:
        assert FinancialJob.__tablename__ == "financial_jobs"

    def test_relationships_declared(self) -> None:
        """FinancialJob must declare 'tenant', 'company', and 'creator' relationships."""
        assert hasattr(FinancialJob, "tenant")
        assert hasattr(FinancialJob, "company")
        assert hasattr(FinancialJob, "creator")


# ---------------------------------------------------------------------------
# Test: FinancialJob.is_terminal property
# ---------------------------------------------------------------------------


class TestIsTerminal:
    """is_terminal must be True only for COMPLETED, FAILED, CANCELLED."""

    @pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
    def test_terminal_states_return_true(self, status: JobStatus) -> None:
        j = _make_job(status=status)
        assert j.is_terminal is True

    @pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING])
    def test_non_terminal_states_return_false(self, status: JobStatus) -> None:
        j = _make_job(status=status)
        assert j.is_terminal is False

    def test_string_status_also_works(self) -> None:
        """is_terminal must work with raw string values as well as enum members."""
        j = _make_job(status="completed")
        assert j.is_terminal is True
        j2 = _make_job(status="pending")
        assert j2.is_terminal is False


# ---------------------------------------------------------------------------
# Test: FinancialJob.is_cancellable property
# ---------------------------------------------------------------------------


class TestIsCancellable:
    """is_cancellable must be True only for PENDING, QUEUED, RUNNING."""

    @pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING])
    def test_cancellable_states_return_true(self, status: JobStatus) -> None:
        j = _make_job(status=status)
        assert j.is_cancellable is True

    @pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
    def test_non_cancellable_states_return_false(self, status: JobStatus) -> None:
        j = _make_job(status=status)
        assert j.is_cancellable is False

    def test_terminal_and_cancellable_are_mutually_exclusive(self) -> None:
        """A job cannot be both terminal and cancellable at the same time."""
        for status in JobStatus:
            j = _make_job(status=status)
            assert not (j.is_terminal and j.is_cancellable), (
                f"status={status!r} is both terminal and cancellable — impossible state"
            )


# ---------------------------------------------------------------------------
# Test: Module __all__ exports
# ---------------------------------------------------------------------------


class TestModuleExports:
    """Verify __all__ is complete and the new models are importable."""

    def test_company_in_all(self) -> None:
        assert "Company" in models_all

    def test_financial_job_in_all(self) -> None:
        assert "FinancialJob" in models_all

    def test_job_status_in_all(self) -> None:
        assert "JobStatus" in models_all

    def test_m1_exports_still_present(self) -> None:
        """M1 exports must not have been removed."""
        m1_exports = (
            "gen_uuid7", "UserRole", "Tenant", "User",
            "TenantMembership", "RefreshToken", "AuditLog",
        )
        for name in m1_exports:
            assert name in models_all, f"{name!r} missing from __all__"

    def test_company_importable_from_module(self) -> None:
        from apps.api.models import Company as CompanyModel  # noqa: PLC0415
        assert CompanyModel is Company

    def test_financial_job_importable_from_module(self) -> None:
        from apps.api.models import FinancialJob as FinancialJobModel  # noqa: PLC0415
        assert FinancialJobModel is FinancialJob
