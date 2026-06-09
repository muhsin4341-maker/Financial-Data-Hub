"""
Unit tests — M2 Pydantic schemas: Company, Job, Invitation.

Tests cover:
  CompanyCreate       — required fields, ticker normalisation, CIK validation/padding
  CompanyUpdate       — optional fields, empty body rejection, partial update
  CompanyResponse     — from_attributes (ORM model compat), field types
  CompanyListResponse — pagination auto-compute, boundary values

  JobCreate           — required fields, job_type snake_case validation, fiscal_year range
  JobUpdate           — status validation, partial update
  JobResponse         — from_attributes, status computed properties
  JobListResponse     — pagination
  JobStatusResponse   — lightweight read model

  InvitationCreate    — email normalisation, role validation, OWNER rejection
  InvitationResponse  — from_attributes

  schemas/__init__.py — all M2 names present in __all__

Engineering Spec references:
  M2 Execution Plan, Section 2.2.3 — Company schemas
  M2 Execution Plan, Section 6.4   — Pagination contract
  M2 Execution Plan, Section 9.4   — Invitation security

Milestone: M2-Step 4
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from apps.api.models import JobStatus, UserRole
from apps.api.schemas.companies import (
    CompanyCreate,
    CompanyListResponse,
    CompanyResponse,
    CompanyUpdate,
)
from apps.api.schemas.invitations import InvitationCreate, InvitationResponse
from apps.api.schemas.jobs import (
    JobCreate,
    JobListResponse,
    JobResponse,
    JobStatusResponse,
    JobUpdate,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_UUID = uuid.uuid4


def _company_response_data(**overrides: object) -> dict:
    base: dict = {
        "id": _UUID(),
        "tenant_id": _UUID(),
        "name": "Acme Corp",
        "ticker": "ACME",
        "cik": None,
        "exchange": None,
        "sector": None,
        "industry": None,
        "description": None,
        "website": None,
        "is_active": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


def _job_response_data(**overrides: object) -> dict:
    base: dict = {
        "id": _UUID(),
        "tenant_id": _UUID(),
        "company_id": _UUID(),
        "created_by": None,
        "status": "pending",
        "job_type": "sec_10k_annual",
        "fiscal_year": None,
        "document_url": None,
        "result_url": None,
        "error_message": None,
        "celery_task_id": None,
        "started_at": None,
        "completed_at": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


# ===========================================================================
# CompanyCreate
# ===========================================================================


class TestCompanyCreate:
    def test_valid_minimal(self) -> None:
        c = CompanyCreate(name="Acme Corp", ticker="acme")
        assert c.name == "Acme Corp"
        assert c.ticker == "ACME"  # uppercased

    def test_all_fields(self) -> None:
        c = CompanyCreate(
            name="Apple Inc.",
            ticker="aapl",
            cik="320193",
            exchange="NASDAQ",
            sector="Information Technology",
            industry="Technology Hardware",
            description="Designs consumer electronics.",
            website="https://www.apple.com",
        )
        assert c.ticker == "AAPL"
        assert c.cik == "0000320193"
        assert c.exchange == "NASDAQ"

    # ── Ticker normalisation ──────────────────────────────────────────────────

    def test_ticker_uppercased(self) -> None:
        assert CompanyCreate(name="Test", ticker="msft").ticker == "MSFT"

    def test_ticker_stripped(self) -> None:
        assert CompanyCreate(name="Test", ticker="  tsla  ").ticker == "TSLA"

    def test_ticker_min_length_enforced(self) -> None:
        with pytest.raises(ValidationError) as exc:
            CompanyCreate(name="Test", ticker="")
        assert "ticker" in str(exc.value).lower()

    def test_ticker_max_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate(name="Test", ticker="A" * 21)

    # ── CIK validation and padding ────────────────────────────────────────────

    def test_cik_padded_to_10_digits(self) -> None:
        c = CompanyCreate(name="Test", ticker="T", cik="320193")
        assert c.cik == "0000320193"
        assert len(c.cik) == 10

    def test_cik_already_10_digits(self) -> None:
        c = CompanyCreate(name="Test", ticker="T", cik="0000320193")
        assert c.cik == "0000320193"

    def test_cik_none_allowed(self) -> None:
        c = CompanyCreate(name="Test", ticker="T")
        assert c.cik is None

    def test_cik_empty_string_becomes_none(self) -> None:
        c = CompanyCreate(name="Test", ticker="T", cik="  ")
        assert c.cik is None

    def test_cik_rejects_non_digits(self) -> None:
        with pytest.raises(ValidationError) as exc:
            CompanyCreate(name="Test", ticker="T", cik="ABC123")
        assert "digit" in str(exc.value).lower()

    def test_cik_rejects_more_than_10_digits(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate(name="Test", ticker="T", cik="12345678901")

    # ── Name validation ───────────────────────────────────────────────────────

    def test_name_stripped(self) -> None:
        c = CompanyCreate(name="  Acme Corp  ", ticker="ACME")
        assert c.name == "Acme Corp"

    def test_name_blank_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            CompanyCreate(name="   ", ticker="ACME")
        assert "blank" in str(exc.value).lower()

    def test_name_max_length(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate(name="A" * 256, ticker="T")

    # ── Optional fields default to None ──────────────────────────────────────

    def test_optional_fields_default_none(self) -> None:
        c = CompanyCreate(name="Test", ticker="T")
        assert c.cik is None
        assert c.exchange is None
        assert c.sector is None
        assert c.industry is None
        assert c.description is None
        assert c.website is None

    # ── Website length ────────────────────────────────────────────────────────

    def test_website_max_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate(name="Test", ticker="T", website="https://" + "a" * 500)


# ===========================================================================
# CompanyUpdate
# ===========================================================================


class TestCompanyUpdate:
    def test_single_field_update(self) -> None:
        u = CompanyUpdate(name="New Name")
        assert u.name == "New Name"
        assert u.ticker is None

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            CompanyUpdate()
        assert "at least one field" in str(exc.value).lower()

    def test_is_active_can_be_set_false(self) -> None:
        u = CompanyUpdate(is_active=False)
        assert u.is_active is False

    def test_ticker_normalised_in_update(self) -> None:
        u = CompanyUpdate(ticker=" msft ")
        assert u.ticker == "MSFT"

    def test_cik_normalised_in_update(self) -> None:
        u = CompanyUpdate(cik="789019")
        assert u.cik == "0000789019"

    def test_model_fields_set_tracks_provided_fields(self) -> None:
        u = CompanyUpdate(name="Updated")
        assert "name" in u.model_fields_set
        assert "ticker" not in u.model_fields_set

    def test_null_cik_allowed_to_clear_field(self) -> None:
        # Explicit None means "clear this field"
        u = CompanyUpdate(name="Test", cik=None)
        assert u.cik is None

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CompanyUpdate(name="   ")


# ===========================================================================
# CompanyResponse
# ===========================================================================


class TestCompanyResponse:
    def test_from_dict(self) -> None:
        r = CompanyResponse(**_company_response_data())
        assert isinstance(r.id, uuid.UUID)
        assert r.is_active is True

    def test_from_orm_object(self) -> None:
        """CompanyResponse must be constructible from an ORM-like object."""
        mock_orm = MagicMock()
        mock_orm.id = uuid.uuid4()
        mock_orm.tenant_id = uuid.uuid4()
        mock_orm.name = "Mock Corp"
        mock_orm.ticker = "MCK"
        mock_orm.cik = None
        mock_orm.exchange = None
        mock_orm.sector = None
        mock_orm.industry = None
        mock_orm.description = None
        mock_orm.website = None
        mock_orm.is_active = True
        mock_orm.created_at = _NOW
        mock_orm.updated_at = _NOW
        r = CompanyResponse.model_validate(mock_orm)
        assert r.name == "Mock Corp"
        assert r.ticker == "MCK"

    def test_serialises_to_json(self) -> None:
        r = CompanyResponse(**_company_response_data())
        data = r.model_dump()
        assert "id" in data
        assert "tenant_id" in data
        assert isinstance(data["id"], uuid.UUID)

    def test_all_response_fields_present(self) -> None:
        r = CompanyResponse(**_company_response_data(
            cik="0000320193", exchange="NYSE", sector="Finance",
            industry="Banking", description="A bank.", website="https://bank.com",
        ))
        assert r.cik == "0000320193"
        assert r.exchange == "NYSE"


# ===========================================================================
# CompanyListResponse
# ===========================================================================


class TestCompanyListResponse:
    def _make_item(self) -> CompanyResponse:
        return CompanyResponse(**_company_response_data())

    def test_pages_auto_computed(self) -> None:
        r = CompanyListResponse(
            items=[self._make_item()],
            total=45,
            page=1,
            page_size=20,
        )
        assert r.pages == 3  # ceil(45/20)

    def test_pages_exact_division(self) -> None:
        r = CompanyListResponse(items=[], total=40, page=2, page_size=20)
        assert r.pages == 2

    def test_pages_zero_total(self) -> None:
        r = CompanyListResponse(items=[], total=0, page=1, page_size=20)
        assert r.pages == 0

    def test_pages_explicit_override(self) -> None:
        r = CompanyListResponse(items=[], total=10, page=1, page_size=5, pages=99)
        assert r.pages == 99

    def test_page_size_max_100(self) -> None:
        with pytest.raises(ValidationError):
            CompanyListResponse(items=[], total=0, page=1, page_size=101)

    def test_page_min_1(self) -> None:
        with pytest.raises(ValidationError):
            CompanyListResponse(items=[], total=0, page=0, page_size=20)


# ===========================================================================
# JobCreate
# ===========================================================================


class TestJobCreate:
    def test_valid_minimal(self) -> None:
        j = JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_annual")
        assert j.job_type == "sec_10k_annual"
        assert j.fiscal_year is None

    def test_with_fiscal_year(self) -> None:
        j = JobCreate(
            company_id=uuid.uuid4(), job_type="sec_10k_annual", fiscal_year=2023
        )
        assert j.fiscal_year == 2023

    # ── job_type validation ───────────────────────────────────────────────────

    def test_job_type_snake_case_valid(self) -> None:
        # min_length=3 enforced by Field; regex enforces snake_case structure
        valid = ["sec_10k_annual", "sec_10q_quarterly", "abc", "a1b"]
        for jt in valid:
            j = JobCreate(company_id=uuid.uuid4(), job_type=jt)
            assert j.job_type == jt

    def test_job_type_uppercase_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            JobCreate(company_id=uuid.uuid4(), job_type="SEC_10K")
        assert "snake_case" in str(exc.value).lower()

    def test_job_type_starting_with_digit_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobCreate(company_id=uuid.uuid4(), job_type="10k_annual")

    def test_job_type_ending_with_underscore_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_")

    def test_job_type_max_length(self) -> None:
        with pytest.raises(ValidationError):
            JobCreate(company_id=uuid.uuid4(), job_type="a" * 101)

    def test_job_type_stripped(self) -> None:
        j = JobCreate(company_id=uuid.uuid4(), job_type="  sec_10k_annual  ")
        assert j.job_type == "sec_10k_annual"

    # ── fiscal_year range ─────────────────────────────────────────────────────

    def test_fiscal_year_min(self) -> None:
        with pytest.raises(ValidationError):
            JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_annual", fiscal_year=1899)

    def test_fiscal_year_max(self) -> None:
        with pytest.raises(ValidationError):
            JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_annual", fiscal_year=2101)

    def test_fiscal_year_boundary_valid(self) -> None:
        j = JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_annual", fiscal_year=1900)
        assert j.fiscal_year == 1900
        j2 = JobCreate(company_id=uuid.uuid4(), job_type="sec_10k_annual", fiscal_year=2100)
        assert j2.fiscal_year == 2100


# ===========================================================================
# JobUpdate
# ===========================================================================


class TestJobUpdate:
    def test_valid_status_update(self) -> None:
        u = JobUpdate(status="running")
        assert u.status == "running"

    def test_all_job_statuses_valid(self) -> None:
        for s in JobStatus:
            u = JobUpdate(status=s.value)
            assert u.status == s.value

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            JobUpdate(status="unknown_status")
        assert "invalid job status" in str(exc.value).lower()

    def test_none_status_allowed(self) -> None:
        u = JobUpdate(status=None, error_message="something went wrong")
        assert u.status is None
        assert u.error_message == "something went wrong"

    def test_celery_task_id_max_length(self) -> None:
        with pytest.raises(ValidationError):
            JobUpdate(celery_task_id="x" * 256)


# ===========================================================================
# JobResponse
# ===========================================================================


class TestJobResponse:
    def test_from_dict(self) -> None:
        r = JobResponse(**_job_response_data())
        assert r.status == "pending"

    def test_from_orm_object(self) -> None:
        mock = MagicMock()
        mock.id = uuid.uuid4()
        mock.tenant_id = uuid.uuid4()
        mock.company_id = uuid.uuid4()
        mock.created_by = None
        mock.status = "running"
        mock.job_type = "sec_10k_annual"
        mock.fiscal_year = 2023
        mock.document_url = None
        mock.result_url = None
        mock.error_message = None
        mock.celery_task_id = "abc-123"
        mock.started_at = _NOW
        mock.completed_at = None
        mock.created_at = _NOW
        mock.updated_at = _NOW
        r = JobResponse.model_validate(mock)
        assert r.status == "running"
        assert r.fiscal_year == 2023

    def test_is_terminal_completed(self) -> None:
        r = JobResponse(**_job_response_data(status="completed"))
        assert r.is_terminal is True
        assert r.is_cancellable is False

    def test_is_terminal_failed(self) -> None:
        r = JobResponse(**_job_response_data(status="failed"))
        assert r.is_terminal is True

    def test_is_terminal_cancelled(self) -> None:
        r = JobResponse(**_job_response_data(status="cancelled"))
        assert r.is_terminal is True

    def test_is_cancellable_pending(self) -> None:
        r = JobResponse(**_job_response_data(status="pending"))
        assert r.is_cancellable is True
        assert r.is_terminal is False

    def test_is_cancellable_queued(self) -> None:
        r = JobResponse(**_job_response_data(status="queued"))
        assert r.is_cancellable is True

    def test_is_cancellable_running(self) -> None:
        r = JobResponse(**_job_response_data(status="running"))
        assert r.is_cancellable is True

    def test_terminal_and_cancellable_mutually_exclusive(self) -> None:
        for status in JobStatus:
            r = JobResponse(**_job_response_data(status=status.value))
            assert not (r.is_terminal and r.is_cancellable), (
                f"status={status!r} is both terminal and cancellable"
            )


# ===========================================================================
# JobListResponse
# ===========================================================================


class TestJobListResponse:
    def test_pages_computed(self) -> None:
        r = JobListResponse(items=[], total=55, page=1, page_size=20)
        assert r.pages == 3

    def test_empty_result(self) -> None:
        r = JobListResponse(items=[], total=0, page=1, page_size=20)
        assert r.pages == 0
        assert r.items == []


# ===========================================================================
# JobStatusResponse
# ===========================================================================


class TestJobStatusResponse:
    def test_minimal_pending(self) -> None:
        r = JobStatusResponse(
            id=uuid.uuid4(),
            status="pending",
            started_at=None,
            completed_at=None,
            error_message=None,
        )
        assert r.status == "pending"

    def test_completed_state(self) -> None:
        r = JobStatusResponse(
            id=uuid.uuid4(),
            status="completed",
            started_at=_NOW,
            completed_at=_NOW,
            error_message=None,
        )
        assert r.completed_at is not None

    def test_from_orm(self) -> None:
        mock = MagicMock()
        mock.id = uuid.uuid4()
        mock.status = "failed"
        mock.started_at = _NOW
        mock.completed_at = _NOW
        mock.error_message = "Worker crashed"
        r = JobStatusResponse.model_validate(mock)
        assert r.status == "failed"
        assert r.error_message == "Worker crashed"


# ===========================================================================
# InvitationCreate
# ===========================================================================


class TestInvitationCreate:
    def test_valid_invitation(self) -> None:
        i = InvitationCreate(email="bob@example.com", role=UserRole.ANALYST)
        assert i.email == "bob@example.com"
        assert i.role == UserRole.ANALYST

    def test_email_normalised_to_lowercase(self) -> None:
        i = InvitationCreate(email="BOB@EXAMPLE.COM", role=UserRole.VIEWER)
        assert i.email == "bob@example.com"

    def test_invalid_email_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InvitationCreate(email="not-an-email", role=UserRole.ANALYST)

    def test_owner_role_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            InvitationCreate(email="bob@example.com", role=UserRole.OWNER)
        assert "owner" in str(exc.value).lower()

    def test_admin_role_allowed(self) -> None:
        i = InvitationCreate(email="bob@example.com", role=UserRole.ADMIN)
        assert i.role == UserRole.ADMIN

    def test_viewer_role_allowed(self) -> None:
        i = InvitationCreate(email="bob@example.com", role=UserRole.VIEWER)
        assert i.role == UserRole.VIEWER

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InvitationCreate(email="bob@example.com", role="superadmin")  # type: ignore[arg-type]


# ===========================================================================
# InvitationResponse
# ===========================================================================


class TestInvitationResponse:
    def test_from_dict(self) -> None:
        r = InvitationResponse(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            invitee_email="bob@example.com",
            role="analyst",
            status="pending",
            invited_by_id=uuid.uuid4(),
            expires_at=_NOW,
            accepted_at=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
        assert r.invitee_email == "bob@example.com"
        assert r.role == "analyst"

    def test_serialises_to_dict(self) -> None:
        r = InvitationResponse(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            invitee_email="alice@example.com",
            role="viewer",
            status="pending",
            invited_by_id=uuid.uuid4(),
            expires_at=_NOW,
            accepted_at=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
        data = r.model_dump()
        assert "invitee_email" in data
        assert "expires_at" in data


# ===========================================================================
# schemas/__init__.py — __all__ completeness
# ===========================================================================


class TestSchemaModuleExports:
    def test_m2_company_schemas_in_all(self) -> None:
        from apps.api.schemas import __all__ as sa  # noqa: PLC0415
        for name in ("CompanyCreate", "CompanyUpdate", "CompanyResponse", "CompanyListResponse"):
            assert name in sa, f"{name!r} missing from schemas.__all__"

    def test_m2_job_schemas_in_all(self) -> None:
        from apps.api.schemas import __all__ as sa  # noqa: PLC0415
        job_schemas = (
            "JobCreate", "JobUpdate", "JobResponse", "JobListResponse", "JobStatusResponse",
        )
        for name in job_schemas:
            assert name in sa

    def test_m2_invitation_schemas_in_all(self) -> None:
        from apps.api.schemas import __all__ as sa  # noqa: PLC0415
        for name in ("InvitationCreate", "InvitationResponse"):
            assert name in sa

    def test_m1_schemas_still_present(self) -> None:
        from apps.api.schemas import __all__ as sa  # noqa: PLC0415
        m1 = ("AuthResponse", "ForgotPasswordRequest", "LoginRequest",
              "MessageResponse", "RegisterRequest", "ResetPasswordRequest")
        for name in m1:
            assert name in sa, f"M1 schema {name!r} missing from __all__"

    def test_all_schemas_importable(self) -> None:
        from apps.api.schemas import (  # noqa: PLC0415
            CompanyCreate,
            CompanyListResponse,
            CompanyResponse,
            CompanyUpdate,
            InvitationCreate,
            InvitationResponse,
            JobCreate,
            JobListResponse,
            JobResponse,
            JobStatusResponse,
            JobUpdate,
        )
        assert CompanyCreate is not None
        assert JobCreate is not None
        assert InvitationCreate is not None
        assert CompanyListResponse is not None
        assert JobListResponse is not None
        assert JobStatusResponse is not None
        assert CompanyResponse is not None
        assert CompanyUpdate is not None
        assert JobResponse is not None
        assert JobUpdate is not None
        assert InvitationResponse is not None
