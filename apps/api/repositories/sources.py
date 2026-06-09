"""
Source Registry repository — all database operations for source config management.

Engineering Specification references:
  M3 Execution Plan, Section 6.1   — source_configs table design
  M3 Execution Plan, M3.1          — Source Registry milestone

Repository contract (matches CompanyRepository conventions from M2):
  - No tenant_id argument — source configs are global system records.
    All queries are platform-wide; there is no per-tenant isolation layer.
  - All write methods call ``session.flush([obj])`` after adding/modifying
    objects so that database-generated values are populated before the
    caller's transaction is committed.
  - The session is NEVER committed here; the route handler + ``get_db``
    dependency owns the transaction boundary.
  - No soft delete: ``delete`` performs a hard delete.  The service layer
    should prefer calling ``disable()`` over ``delete()`` for active sources.
  - ``enable`` / ``disable`` set ``is_active`` and flush immediately.

Pagination:
  ``list`` returns a ``(items, total)`` tuple where ``total`` is the count of
  all matching rows across all pages (not just the current page), and ``items``
  is the current page's data.  Two queries are executed: a COUNT and a SELECT.

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import SourceConfig
from apps.api.schemas.sources import SourceConfigCreate, SourceConfigUpdate

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Column allowlist for partial updates
# ---------------------------------------------------------------------------

#: Columns on ``SourceConfig`` that may be modified by ``SourceConfigUpdate``.
#: ``code`` is deliberately excluded — it is immutable after creation.
#: ``id``, ``created_at``, and server-managed fields are never writable via update.
_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "name",
    "description",
    "provider_type",
    "country_code",
    "base_url",
    "rate_limit_per_minute",
    "is_active",
    "config",
})


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SourceConfigRepository:
    """
    Database access layer for source registry operations.

    Instantiated per-request inside route handlers, receiving the
    ``AsyncSession`` from the ``get_db`` FastAPI dependency::

        repo = SourceConfigRepository(db)
        source = await repo.get_by_code("SEC_EDGAR")
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(self, schema: SourceConfigCreate) -> SourceConfig:
        """
        Persist a new SourceConfig.

        All fields from the validated schema are written.  The caller (service
        layer) must catch ``IntegrityError`` for duplicate ``code`` violations
        and raise an appropriate ``ConflictError``.

        Args:
            schema: Validated ``SourceConfigCreate`` Pydantic model.
                    The ``code`` field will already be normalised (uppercased).

        Returns:
            Persisted ``SourceConfig`` instance with ``id`` and timestamps set.
        """
        source = SourceConfig(
            code=schema.code,
            name=schema.name,
            description=schema.description,
            provider_type=schema.provider_type,
            country_code=schema.country_code,
            base_url=schema.base_url,
            rate_limit_per_minute=schema.rate_limit_per_minute,
            is_active=schema.is_active,
            config=schema.config,
        )
        self._session.add(source)
        await self._session.flush([source])
        log.debug(
            "source_config.repository.created",
            source_id=str(source.id),
            code=source.code,
            provider_type=source.provider_type,
        )
        return source

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(
        self,
        source_id: uuid.UUID,
    ) -> SourceConfig | None:
        """
        Fetch a single source config by its primary key.

        Returns ``None`` if no source exists with the given ID.
        Callers should raise HTTP 404 on ``None``.

        Args:
            source_id: UUID primary key of the source config.

        Returns:
            ``SourceConfig`` ORM instance or ``None``.
        """
        result = await self._session.execute(
            select(SourceConfig).where(SourceConfig.id == source_id)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> SourceConfig | None:
        """
        Fetch a single source config by its machine-readable code.

        Performs a case-sensitive match on the stored (uppercased) code value.
        Callers should normalise the code to uppercase before calling.

        Args:
            code: Machine-readable code, e.g. 'SEC_EDGAR'.

        Returns:
            ``SourceConfig`` ORM instance or ``None``.
        """
        result = await self._session.execute(
            select(SourceConfig).where(SourceConfig.code == code.upper())
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        provider_type: str | None = None,
        country_code: str | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[SourceConfig], int]:
        """
        Return a paginated, optionally filtered list of source configs.

        Two database queries are executed:
          1. A COUNT to determine the total number of matching rows.
          2. A SELECT with LIMIT / OFFSET to fetch the current page.

        Results are ordered by ``created_at`` descending (newest first),
        consistent with the companies / jobs list endpoints from M2.

        Args:
            page:          1-based page number (default 1).
            page_size:     Rows per page (default 20; max 100 enforced by schema).
            provider_type: When set, filter by provider type.
            country_code:  When set, filter by country (case-insensitive).
            is_active:     When True, return only enabled sources.
                           When False, return only disabled sources.
                           When None, return all.

        Returns:
            ``(items, total)`` tuple.
        """
        conditions: list[Any] = []

        if provider_type is not None:
            conditions.append(SourceConfig.provider_type == provider_type.lower())
        if country_code is not None:
            conditions.append(SourceConfig.country_code == country_code.upper())
        if is_active is not None:
            conditions.append(SourceConfig.is_active == is_active)

        # ── Count query ───────────────────────────────────────────────────────
        count_q = select(func.count()).select_from(SourceConfig)
        if conditions:
            count_q = count_q.where(*conditions)
        count_result = await self._session.execute(count_q)
        total: int = count_result.scalar_one()

        # ── Data query ────────────────────────────────────────────────────────
        offset = (page - 1) * page_size
        data_q = (
            select(SourceConfig)
            .order_by(SourceConfig.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        if conditions:
            data_q = data_q.where(*conditions)
        data_result = await self._session.execute(data_q)
        items = list(data_result.scalars().all())

        return items, total

    # ── Update ────────────────────────────────────────────────────────────────

    async def update(
        self,
        source_id: uuid.UUID,
        schema: SourceConfigUpdate,
    ) -> SourceConfig | None:
        """
        Apply a partial update to a source config.

        Only the fields explicitly set in the ``SourceConfigUpdate`` schema are
        written to the database (uses ``schema.model_fields_set``).  ``code`` is
        never updated — it is excluded from ``_UPDATABLE_FIELDS``.

        The service layer is responsible for enforcing the business rule that
        ``code`` may not be changed; this repository simply ignores it.

        Args:
            source_id: UUID of the source config to update.
            schema:    Validated ``SourceConfigUpdate`` Pydantic model.

        Returns:
            Updated ``SourceConfig`` ORM instance, or ``None`` if not found.
        """
        source = await self.get_by_id(source_id)
        if source is None:
            return None

        changed = False
        for field in schema.model_fields_set & _UPDATABLE_FIELDS:
            new_value = getattr(schema, field)
            if getattr(source, field) != new_value:
                setattr(source, field, new_value)
                changed = True

        if changed:
            source.updated_at = datetime.now(UTC)
            await self._session.flush([source])
            log.debug(
                "source_config.repository.updated",
                source_id=str(source_id),
                code=source.code,
                fields=sorted(schema.model_fields_set & _UPDATABLE_FIELDS),
            )

        return source

    # ── Enable / Disable ──────────────────────────────────────────────────────

    async def enable(self, source_id: uuid.UUID) -> SourceConfig | None:
        """
        Set ``is_active = True`` on the source config.

        Args:
            source_id: UUID of the source config to enable.

        Returns:
            Updated ``SourceConfig`` instance, or ``None`` if not found.
        """
        source = await self.get_by_id(source_id)
        if source is None:
            return None

        if not source.is_active:
            source.is_active = True
            source.updated_at = datetime.now(UTC)
            await self._session.flush([source])
            log.debug(
                "source_config.repository.enabled",
                source_id=str(source_id),
                code=source.code,
            )

        return source

    async def disable(self, source_id: uuid.UUID) -> SourceConfig | None:
        """
        Set ``is_active = False`` on the source config.

        Preferred over hard deletion — disabled sources preserve history and
        prevent FK breakage once filing_records reference source_config_id.

        Args:
            source_id: UUID of the source config to disable.

        Returns:
            Updated ``SourceConfig`` instance, or ``None`` if not found.
        """
        source = await self.get_by_id(source_id)
        if source is None:
            return None

        if source.is_active:
            source.is_active = False
            source.updated_at = datetime.now(UTC)
            await self._session.flush([source])
            log.debug(
                "source_config.repository.disabled",
                source_id=str(source_id),
                code=source.code,
            )

        return source

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete(self, source_id: uuid.UUID) -> bool:
        """
        Hard-delete a source config from the database.

        This permanently removes the row.  The service layer should prefer
        ``disable()`` for active sources to preserve referential integrity
        once filing_records starts referencing source_config_id (migration 005).

        Args:
            source_id: UUID of the source config to delete.

        Returns:
            True if the source was found and deleted.
            False if the source was not found.
        """
        source = await self.get_by_id(source_id)
        if source is None:
            return False

        await self._session.delete(source)
        await self._session.flush()
        log.debug(
            "source_config.repository.deleted",
            source_id=str(source_id),
            code=source.code,
        )
        return True
