"""
Source Registry — runtime registry of available financial data sources.

The registry is the single point of contact for all acquisition pipeline
components that need a configured, ready-to-use data source client.

Responsibilities
----------------
* Load ``SourceConfig`` rows from the database (once, on first use).
* Cache the results in-process for ``_CACHE_TTL_SECONDS`` (5 minutes).
* Instantiate and return the correct ``BaseSource`` implementation for a
  given source code (e.g. ``"SEC_EDGAR"``).
* Expose a health-check that pings each active source.

Design decisions
----------------
* **Lazy loading** — the registry does NOT open a DB connection on import.
  It fetches configs on the first ``get_source`` / ``list_sources`` call so
  that importing the module in a test or Celery worker does not require a
  live database.

* **In-process cache** — a simple ``asyncio.Lock``-protected dict with a
  timestamp-based TTL.  Across Celery workers each process has its own
  cache; the TTL prevents stale configs lingering longer than 5 minutes
  after an admin enables/disables a source.

* **Source factory** — the mapping from ``SourceConfig.code`` to a
  ``BaseSource`` subclass is defined in ``_SOURCE_FACTORIES``.  Adding a
  new source (e.g. NSE) requires only one entry in that dict; no other code
  changes are needed.

* **No singleton** — the registry is instantiated per-context (FastAPI
  dependency or Celery task).  A shared module-level instance is provided
  as ``default_registry`` for convenience.

Usage (FastAPI dependency)::

    from services.acquisition.source_registry.registry import SourceRegistry

    async def get_registry(db: AsyncSession = Depends(get_db)) -> SourceRegistry:
        registry = SourceRegistry(session=db)
        return registry

    @router.get("/sources")
    async def list(registry: SourceRegistry = Depends(get_registry)):
        return await registry.list_sources()

Usage (Celery task — no DB session available)::

    from services.acquisition.source_registry.registry import default_registry

    source = await default_registry.get_source("SEC_EDGAR")

Milestone: M3.1 — Source Registry
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models import SourceConfig
from services.acquisition.source_registry.sources import BaseSource, SECEdgarSource

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Cache constants
# ---------------------------------------------------------------------------

#: Seconds before the in-process config cache is considered stale and
#: re-fetched from the database.  5 minutes matches the M3 spec (Section 9.1).
_CACHE_TTL_SECONDS: float = 300.0

# ---------------------------------------------------------------------------
# Source factory map
# ---------------------------------------------------------------------------

#: Maps SourceConfig.code → factory callable(config: SourceConfig, **kwargs) → BaseSource.
#:
#: Each factory receives the full ``SourceConfig`` ORM object so it can read
#: ``rate_limit_per_minute``, ``base_url``, and the ``config`` JSONB blob.
#: Additional keyword arguments (user_agent, redis_client) are forwarded from
#: ``SourceRegistry.__init__``.
#:
#: To add a new source:
#:   1. Add a factory function below.
#:   2. Register it under the source's ``code`` value.
#:   3. Insert a row in ``source_configs`` (via seed or migration).
#:   No other code changes are required.

def _make_sec_edgar(cfg: SourceConfig, **kwargs: Any) -> SECEdgarSource:
    """Instantiate SECEdgarSource from a SourceConfig row."""
    from services.acquisition.source_registry.rate_limiter import (
        InProcessRateLimiter,
        RedisRateLimiter,
    )

    user_agent: str = kwargs.get("user_agent") or "FinancialDataHub contact@example.com"
    redis_client = kwargs.get("redis_client")

    # Convert rate_limit_per_minute → requests_per_second for the token bucket.
    # SEC EDGAR's official limit is 10 req/s; we apply an 80% cap for safety margin.
    rps = min((cfg.rate_limit_per_minute / 60) * 0.8, 8.0)

    # Use distributed Redis limiter when a Redis client is available (multi-worker
    # Celery deployments); fall back to in-process limiter for single-process use.
    if redis_client is not None:
        rate_limiter = RedisRateLimiter(
            redis_client=redis_client,
            key=f"fdh:rate_limit:{cfg.code.lower()}",
            rate=int(rps),
        )
    else:
        rate_limiter = InProcessRateLimiter(rate=rps)

    return SECEdgarSource(
        user_agent=user_agent,
        rate_limiter=rate_limiter,
    )


_SOURCE_FACTORIES: dict[str, Any] = {
    "SEC_EDGAR": _make_sec_edgar,
    # Future sources:
    # "NSE":       _make_nse,
    # "BSE":       _make_bse,
    # "MANUAL_UPLOAD": _make_manual_upload,
}

# ---------------------------------------------------------------------------
# Cache state (module-level, shared within a process)
# ---------------------------------------------------------------------------

_cache_lock = asyncio.Lock()
_cached_configs: list[SourceConfig] | None = None
_cache_loaded_at: float = 0.0


def _cache_is_fresh() -> bool:
    """Return True if the in-process cache is within the TTL window."""
    return (
        _cached_configs is not None
        and (time.monotonic() - _cache_loaded_at) < _CACHE_TTL_SECONDS
    )


# ---------------------------------------------------------------------------
# SourceRegistry
# ---------------------------------------------------------------------------


class SourceRegistry:
    """
    Runtime registry that vends configured ``BaseSource`` instances.

    Parameters
    ----------
    session:
        SQLAlchemy async session used to load ``SourceConfig`` rows.
        When ``None``, the registry can only serve sources already in the
        in-process cache from a previous call.
    user_agent:
        Value forwarded to each source factory as the HTTP User-Agent.
        Defaults to the project's SEC fair-access identifier when not set.
    redis_client:
        Optional shared Redis client forwarded to sources that support
        distributed rate limiting.
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        user_agent: str | None = None,
        redis_client: object | None = None,
    ) -> None:
        self._session = session
        self._user_agent = user_agent
        self._redis = redis_client

    # ── Public API ───────────��───────────────────────���─────────────────────────

    async def get_source(self, code: str) -> BaseSource:
        """
        Return a configured ``BaseSource`` instance for the given source code.

        Args:
            code: Machine-readable source identifier (e.g. ``"SEC_EDGAR"``).
                  Case-insensitive — normalized to uppercase internally.

        Returns:
            A ``BaseSource`` instance ready for use.

        Raises:
            KeyError: The source code is not registered in ``_SOURCE_FACTORIES``.
            ValueError: The source exists in the DB but is marked ``is_active=False``.
            LookupError: The source code is not found in the ``source_configs`` table.
        """
        code = code.upper()
        configs = await self._load_configs()

        cfg = next((c for c in configs if c.code == code), None)
        if cfg is None:
            raise LookupError(
                f"Source {code!r} not found in source_configs table. "
                "Ensure the seed data has been applied."
            )
        if not cfg.is_active:
            raise ValueError(
                f"Source {code!r} is disabled (is_active=False). "
                "Enable it via the admin API before use."
            )

        factory = _SOURCE_FACTORIES.get(code)
        if factory is None:
            raise KeyError(
                f"No factory registered for source code {code!r}. "
                "Add an entry to _SOURCE_FACTORIES in registry.py."
            )

        source: BaseSource = factory(
            cfg,
            user_agent=self._user_agent,
            redis_client=self._redis,
        )
        log.debug(
            "source_registry.source_vended",
            code=code,
            source_type=type(source).__name__,
        )
        return source

    async def list_sources(
        self,
        *,
        country_code: str | None = None,
        active_only: bool = True,
    ) -> list[SourceConfig]:
        """
        Return source config records, optionally filtered.

        Args:
            country_code: When set, only return sources whose ``country_code``
                          matches (case-insensitive).  ``None`` = all countries.
            active_only:  When ``True`` (default), only return ``is_active=True``
                          records.

        Returns:
            List of ``SourceConfig`` ORM objects ordered by ``code``.
        """
        configs = await self._load_configs()

        result = list(configs)
        if active_only:
            result = [c for c in result if c.is_active]
        if country_code is not None:
            cc = country_code.upper()
            result = [
                c for c in result
                if c.country_code is not None and c.country_code.upper() == cc
            ]
        return sorted(result, key=lambda c: c.code)

    async def health_check(self) -> dict[str, bool]:
        """
        Return a mapping of source code → reachability for all active sources.

        Each source that has a registered factory is instantiated and a
        lightweight check is performed (currently: factory instantiation
        succeeds and ``isinstance(src, BaseSource)`` passes).  No live HTTP
        call is made so this is safe to call frequently.

        Returns:
            Dict mapping source code to ``True`` (healthy) / ``False`` (unhealthy).
        """
        configs = await self._load_configs()
        results: dict[str, bool] = {}

        for cfg in configs:
            if not cfg.is_active:
                continue
            factory = _SOURCE_FACTORIES.get(cfg.code)
            if factory is None:
                results[cfg.code] = False
                log.warning(
                    "source_registry.health_check.no_factory",
                    code=cfg.code,
                )
                continue
            try:
                src = factory(cfg, user_agent=self._user_agent)
                results[cfg.code] = isinstance(src, BaseSource)
            except Exception as exc:
                results[cfg.code] = False
                log.warning(
                    "source_registry.health_check.factory_error",
                    code=cfg.code,
                    error=str(exc),
                )

        return results

    @classmethod
    def invalidate_cache(cls) -> None:
        """
        Force the next call to re-fetch configs from the database.

        Useful after admin operations that update ``source_configs`` rows,
        so that workers pick up changes without waiting for the TTL to expire.
        """
        global _cached_configs, _cache_loaded_at
        _cached_configs = None
        _cache_loaded_at = 0.0
        log.info("source_registry.cache_invalidated")

    # ── Internal helpers ───────────────────────────────────────────��───────────

    async def _load_configs(self) -> list[SourceConfig]:
        """
        Return the list of all ``SourceConfig`` rows, using the in-process cache.

        Thread / async safety: guarded by ``_cache_lock`` so that concurrent
        coroutines in the same event loop do not each issue a redundant DB query.

        Falls back to the stale cache if no session is available and the cache
        exists (even if past TTL) — better to serve slightly stale data than
        raise an exception during a Celery task that has no DB session injected.
        """
        global _cached_configs, _cache_loaded_at

        if _cache_is_fresh():
            return _cached_configs  # type: ignore[return-value]

        async with _cache_lock:
            # Re-check under lock — another coroutine may have refreshed the
            # cache while we were waiting.
            if _cache_is_fresh():
                return _cached_configs  # type: ignore[return-value]

            if self._session is None:
                if _cached_configs is not None:
                    # Stale cache — tolerate rather than raise in worker context.
                    log.warning(
                        "source_registry.stale_cache_served",
                        age_seconds=round(time.monotonic() - _cache_loaded_at, 1),
                    )
                    return _cached_configs
                raise RuntimeError(
                    "SourceRegistry has no DB session and no cached configs. "
                    "Pass a live AsyncSession to SourceRegistry(session=...) "
                    "before calling get_source() for the first time."
                )

            stmt = select(SourceConfig).order_by(SourceConfig.code)
            rows = (await self._session.execute(stmt)).scalars().all()
            _cached_configs = list(rows)
            _cache_loaded_at = time.monotonic()

            log.info(
                "source_registry.configs_loaded",
                count=len(_cached_configs),
                codes=[c.code for c in _cached_configs],
            )
            return _cached_configs


# ---------------------------------------------------------------------------
# Module-level default instance (convenience for Celery tasks and tests)
# ---------------------------------------------------------------------------

#: Shared registry instance with no DB session.
#: Suitable for Celery tasks that need a source after the in-process cache
#: has been primed by at least one FastAPI request.
#:
#: For first-time use (e.g. in tests or cold workers), construct a
#: ``SourceRegistry(session=db_session)`` with a live session instead.
default_registry = SourceRegistry()
