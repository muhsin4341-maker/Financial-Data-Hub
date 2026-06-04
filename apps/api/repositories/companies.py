"""
Company repository — all database operations for company management.

Engineering Specification references:
  Part 1, Section 1.2, Decision 3  — tenant_id on all user-data tables
  Part 1, Section 1.2, Decision 4  — soft delete via deleted_at
  M2 Execution Plan, Section 6.5   — tenant isolation: enforced at repository layer

Repository contract (matches M1 AuthRepository conventions):
  - All public methods accept ``tenant_id`` as the first argument after ``self``.
    This is the hard tenant-isolation boundary: no query ever runs without a
    tenant_id filter, so a misconfigured route handler cannot leak cross-tenant data.
  - All write methods call ``session.flush([obj])`` after adding/modifying objects
    so that database-generated values (UUIDs, server defaults) are populated before
    the caller's transaction is committed.
  - The session is NEVER committed here; the route handler + ``get_db`` dependency
    owns the transaction boundary.
  - Soft-delete: ``soft_delete`` sets ``deleted_at = NOW()``.  No hard-delete
    operation is exposed.  List queries default to excluding soft-deleted rows.

Pagination:
  ``list`` returns a ``(items, total)`` tuple where ``total`` is the count of
  all matching rows across all pages (not just the current page), and ``items``
  is the current page's data.  Two queries are executed: a COUNT and a SELECT.

Search:
  ``list`` accepts an optional ``search`` string that is applied as an ILIKE
  match against ``Company.name``.  The GIN trigram index ``gin_companies_name``
  (installed by migration 002) accelerates these queries.

Milestone: M2-Step 5
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import Company
from apps.api.schemas.companies import CompanyCreate, CompanyUpdate

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Column set for partial updates
# ---------------------------------------------------------------------------

#: Columns on ``Company`` that may be modified by ``CompanyUpdate``.
#: Explicit allowlist prevents mass-assignment vulnerabilities and ensures
#: read-only fields (id, tenant_id, created_at) are never overwritten.
_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "name",
    "ticker",
    "cik",
    "exchange",
    "sector",
    "industry",
    "description",
    "website",
    "is_active",
})


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class CompanyRepository:
    """
    Database access layer for company management operations.

    Instantiated per-request inside route handlers, receiving the
    ``AsyncSession`` from the ``get_db`` FastAPI dependency::

        repo = CompanyRepository(db)
        company = await repo.get_by_id(ctx.tenant_id, company_id)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(
        self,
        tenant_id: uuid.UUID,
        schema: CompanyCreate,
    ) -> Company:
        """
        Persist a new Company in the given tenant workspace.

        All fields from the validated schema are written.  ``tenant_id`` is
        injected from the authenticated request context, not from the request
        body, to prevent tenant spoofing.

        Args:
            tenant_id: UUID of the owning tenant (from JWT payload).
            schema:    Validated ``CompanyCreate`` Pydantic model.

        Returns:
            Persisted ``Company`` instance with ``id`` and timestamps populated.
        """
        company = Company(
            tenant_id=tenant_id,
            name=schema.name,
            ticker=schema.ticker,
            cik=schema.cik,
            exchange=schema.exchange,
            sector=schema.sector,
            industry=schema.industry,
            description=schema.description,
            website=schema.website,
        )
        self._session.add(company)
        await self._session.flush([company])
        log.debug(
            "company.repository.created",
            company_id=str(company.id),
            tenant_id=str(tenant_id),
            ticker=company.ticker,
        )
        return company

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(
        self,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> Company | None:
        """
        Fetch a single company by its primary key, scoped to the tenant.

        Returns ``None`` if the company does not exist, belongs to a different
        tenant, or has been soft-deleted (unless ``include_deleted=True``).
        Callers should raise HTTP 404 on ``None`` — do NOT raise 403 for
        wrong-tenant rows, as that would leak existence information.

        Args:
            tenant_id:       Tenant scope from the authenticated request.
            company_id:      UUID of the company to fetch.
            include_deleted: If True, soft-deleted companies are also returned.

        Returns:
            ``Company`` ORM instance or ``None``.
        """
        conditions: list[Any] = [
            Company.id == company_id,
            Company.tenant_id == tenant_id,
        ]
        if not include_deleted:
            conditions.append(Company.deleted_at.is_(None))

        result = await self._session.execute(
            select(Company).where(*conditions)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        tenant_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
        is_active: bool | None = None,
        include_deleted: bool = False,
    ) -> tuple[list[Company], int]:
        """
        Return a paginated, optionally filtered list of companies.

        Two database queries are executed:
          1. A COUNT to determine the total number of matching rows.
          2. A SELECT with LIMIT / OFFSET to fetch the current page.

        Both queries use identical WHERE conditions to guarantee consistency.

        Args:
            tenant_id:       Tenant scope.
            page:            1-based page number.
            page_size:       Rows per page (default 20; max 100 enforced by schema).
            search:          Case-insensitive substring match on ``Company.name``.
                             The GIN trigram index makes this efficient.
            is_active:       When True, only active companies are returned.
                             When False, only inactive companies are returned.
                             When None, both are included.
            include_deleted: When True, soft-deleted companies are also returned.

        Returns:
            ``(items, total)`` tuple.
        """
        conditions: list[Any] = [Company.tenant_id == tenant_id]

        if not include_deleted:
            conditions.append(Company.deleted_at.is_(None))
        if is_active is not None:
            conditions.append(Company.is_active == is_active)
        if search:
            conditions.append(Company.name.ilike(f"%{search}%"))

        # ── Count query ───────────────────────────────────────────────────────
        count_result = await self._session.execute(
            select(func.count()).select_from(Company).where(*conditions)
        )
        total: int = count_result.scalar_one()

        # ── Data query ────────────────────────────────────────────────────────
        offset = (page - 1) * page_size
        data_result = await self._session.execute(
            select(Company)
            .where(*conditions)
            .order_by(Company.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list(data_result.scalars().all())

        return items, total

    # ── Update ────────────────────────────────────────────────────────────────

    async def update(
        self,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        schema: CompanyUpdate,
    ) -> Company | None:
        """
        Apply a partial update to a company.

        Only the fields explicitly set in the ``CompanyUpdate`` schema are
        written to the database.  ``schema.model_fields_set`` tracks which
        fields the client actually provided — this ensures that a PATCH body
        of ``{"name": "New Name"}`` does not clear other fields.

        IMPORTANT: This method must NOT use ``model_dump(exclude_unset=True)``
        blindly, because ``None`` values in ``model_fields_set`` represent an
        explicit "clear this field" intent (e.g. clearing the CIK), while
        unset fields in the default dict would also be ``None``.  The
        distinction is that ``model_fields_set`` contains only the keys the
        client provided.

        Args:
            tenant_id:  Tenant scope — company must belong to this tenant.
            company_id: UUID of the company to update.
            schema:     Validated ``CompanyUpdate`` Pydantic model.

        Returns:
            Updated ``Company`` ORM instance, or ``None`` if not found.
        """
        company = await self.get_by_id(tenant_id, company_id)
        if company is None:
            return None

        # Apply only the fields explicitly set in the PATCH body.
        # This respects the semantic difference between:
        #   {"cik": null}  → explicit clear (in model_fields_set, value=None)
        #   field omitted  → no change (not in model_fields_set)
        changed = False
        for field in schema.model_fields_set & _UPDATABLE_FIELDS:
            new_value = getattr(schema, field)
            if getattr(company, field) != new_value:
                setattr(company, field, new_value)
                changed = True

        if changed:
            company.updated_at = datetime.now(UTC)
            await self._session.flush([company])
            log.debug(
                "company.repository.updated",
                company_id=str(company_id),
                tenant_id=str(tenant_id),
                fields=sorted(schema.model_fields_set & _UPDATABLE_FIELDS),
            )

        return company

    # ── Soft delete ───────────────────────────────────────────────────────────

    async def soft_delete(
        self,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
    ) -> bool:
        """
        Soft-delete a company by setting ``deleted_at = NOW()``.

        Soft-deleted companies are excluded from normal ``list`` and
        ``get_by_id`` queries but their job history is retained for audit
        and compliance purposes.

        Args:
            tenant_id:  Tenant scope.
            company_id: UUID of the company to soft-delete.

        Returns:
            True if the company was found and soft-deleted.
            False if the company was not found or already deleted.
        """
        company = await self.get_by_id(tenant_id, company_id)
        if company is None:
            return False

        company.deleted_at = datetime.now(UTC)
        await self._session.flush([company])
        log.debug(
            "company.repository.soft_deleted",
            company_id=str(company_id),
            tenant_id=str(tenant_id),
        )
        return True
