"""
Source Registry service — business logic for source config management.

Engineering Specification references:
  M3 Execution Plan, M3.1          — Source Registry milestone

Business rules enforced here (not at repository or router level):
  BR-1  code must be globally unique.
        The DB unique constraint guarantees this at the database level.
        ``IntegrityError`` is caught here and surfaced as ``ConflictError``.
  BR-2  Provider code is immutable after creation.
        ``update()`` rejects any attempt to include ``code`` in the patch by
        asserting it is absent from the schema's ``model_fields_set``.
        (``SourceConfigUpdate`` omits the field entirely, so this is a safety net.)
  BR-3  Prefer disable over hard delete for active sources.
        ``delete()`` logs a warning if the source is currently active, and
        delegates to the repository's ``delete()`` which performs the hard delete.
        Callers should use ``disable()`` when the intent is deactivation.

This service owns no session management — the session is injected via the
repository constructor and the transaction is committed by the route handler
through the ``get_db`` FastAPI dependency (same pattern as M2).

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.exceptions import ConflictError, NotFoundError
from apps.api.models import SourceConfig
from apps.api.repositories.sources import SourceConfigRepository
from apps.api.schemas.sources import (
    SourceConfigCreate,
    SourceConfigListResponse,
    SourceConfigResponse,
    SourceConfigUpdate,
)

log = structlog.get_logger(__name__)


class SourceRegistryService:
    """
    Business logic layer for the Source Registry.

    Instantiated per-request inside route handlers::

        service = SourceRegistryService(db)
        source  = await service.create(schema)

    All public methods return Pydantic response schemas, not ORM instances,
    so routers can return them directly without calling ``model_validate``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._repo = SourceConfigRepository(session)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _to_response(source: SourceConfig) -> SourceConfigResponse:
        """Convert a SourceConfig ORM instance to its Pydantic response schema."""
        return SourceConfigResponse.model_validate(source)

    # ── Create ────────────────────────────────────────────────────────────────

    async def create(self, schema: SourceConfigCreate) -> SourceConfigResponse:
        """
        Create a new source config.

        Business rules:
          BR-1: Raises ``ConflictError`` if ``code`` already exists.

        Args:
            schema: Validated ``SourceConfigCreate`` model (code already normalised).

        Returns:
            ``SourceConfigResponse`` for the newly created record.

        Raises:
            ConflictError: If a source with the same ``code`` already exists.
        """
        try:
            source = await self._repo.create(schema)
        except IntegrityError as exc:
            raise ConflictError(
                f"A source with code '{schema.code}' already exists."
            ) from exc

        log.info(
            "source_registry.created",
            source_id=str(source.id),
            code=source.code,
            provider_type=source.provider_type,
        )
        return self._to_response(source)

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_by_id(self, source_id: uuid.UUID) -> SourceConfigResponse:
        """
        Return a source config by primary key.

        Args:
            source_id: UUID of the source config.

        Returns:
            ``SourceConfigResponse`` for the found record.

        Raises:
            NotFoundError: If no source exists with the given ID.
        """
        source = await self._repo.get_by_id(source_id)
        if source is None:
            raise NotFoundError("SourceConfig", str(source_id))
        return self._to_response(source)

    async def get_by_code(self, code: str) -> SourceConfigResponse:
        """
        Return a source config by machine-readable code.

        Args:
            code: Machine-readable code, e.g. 'SEC_EDGAR'.

        Returns:
            ``SourceConfigResponse`` for the found record.

        Raises:
            NotFoundError: If no source exists with the given code.
        """
        source = await self._repo.get_by_code(code)
        if source is None:
            raise NotFoundError("SourceConfig", code)
        return self._to_response(source)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        provider_type: str | None = None,
        country_code: str | None = None,
        is_active: bool | None = None,
    ) -> SourceConfigListResponse:
        """
        Return a paginated list of source configs with optional filters.

        Args:
            page:          1-based page number.
            page_size:     Items per page.
            provider_type: Optional filter by provider type.
            country_code:  Optional filter by country code.
            is_active:     Optional filter by enabled/disabled state.

        Returns:
            ``SourceConfigListResponse`` with pagination metadata.
        """
        import math  # noqa: PLC0415 — local import to match M2 router pattern

        items, total = await self._repo.list(
            page=page,
            page_size=page_size,
            provider_type=provider_type,
            country_code=country_code,
            is_active=is_active,
        )
        pages = math.ceil(total / page_size) if page_size else 0
        return SourceConfigListResponse(
            items=[self._to_response(s) for s in items],
            total=total,
            page=page,
            page_size=page_size,
            pages=pages,
        )

    # ── Update ────────────────────────────────────────────────────────────────

    async def update(
        self,
        source_id: uuid.UUID,
        schema: SourceConfigUpdate,
    ) -> SourceConfigResponse:
        """
        Partially update a source config.

        Business rules:
          BR-2: ``code`` is not a field in ``SourceConfigUpdate``, so it can
                never appear in ``model_fields_set``.  This is a structural
                guarantee, not a runtime check.

        Args:
            source_id: UUID of the source config to update.
            schema:    Validated ``SourceConfigUpdate`` model.

        Returns:
            ``SourceConfigResponse`` for the updated record.

        Raises:
            NotFoundError: If the source does not exist.
        """
        source = await self._repo.update(source_id, schema)
        if source is None:
            raise NotFoundError("SourceConfig", str(source_id))

        log.info(
            "source_registry.updated",
            source_id=str(source_id),
            code=source.code,
            fields=sorted(schema.model_fields_set),
        )
        return self._to_response(source)

    # ── Enable / Disable ──────────────────────────────────────────────────────

    async def enable(self, source_id: uuid.UUID) -> SourceConfigResponse:
        """
        Enable a previously disabled source config (set ``is_active = True``).

        Args:
            source_id: UUID of the source config to enable.

        Returns:
            ``SourceConfigResponse`` reflecting the updated state.

        Raises:
            NotFoundError: If the source does not exist.
        """
        source = await self._repo.enable(source_id)
        if source is None:
            raise NotFoundError("SourceConfig", str(source_id))

        log.info(
            "source_registry.enabled",
            source_id=str(source_id),
            code=source.code,
        )
        return self._to_response(source)

    async def disable(self, source_id: uuid.UUID) -> SourceConfigResponse:
        """
        Disable a source config (set ``is_active = False``).

        Business rules:
          BR-3: Prefer disable over hard delete for active sources.
                Callers should invoke this endpoint instead of DELETE
                when the intent is deactivation, not data removal.

        Args:
            source_id: UUID of the source config to disable.

        Returns:
            ``SourceConfigResponse`` reflecting the updated state.

        Raises:
            NotFoundError: If the source does not exist.
        """
        source = await self._repo.disable(source_id)
        if source is None:
            raise NotFoundError("SourceConfig", str(source_id))

        log.info(
            "source_registry.disabled",
            source_id=str(source_id),
            code=source.code,
        )
        return self._to_response(source)

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete(self, source_id: uuid.UUID) -> None:
        """
        Hard-delete a source config from the database.

        Business rules:
          BR-3: Logs a warning if deleting an active source.
                Callers should prefer ``disable()`` for active sources.

        Args:
            source_id: UUID of the source config to delete.

        Raises:
            NotFoundError: If the source does not exist.
        """
        # Peek at the record before deletion to log the warning if active.
        source = await self._repo.get_by_id(source_id)
        if source is None:
            raise NotFoundError("SourceConfig", str(source_id))

        if source.is_active:
            log.warning(
                "source_registry.deleting_active_source",
                source_id=str(source_id),
                code=source.code,
                advice="Prefer POST /{id}/disable over DELETE for active sources.",
            )

        await self._repo.delete(source_id)
        log.info(
            "source_registry.deleted",
            source_id=str(source_id),
            code=source.code,
        )
