"""
Redis distributed lock for ingestion job serialisation.

Amendment V1.2, Section 8.2 — Distributed Idempotency Lock:
  Before any Celery ingestion task fires, a Redis lock must be acquired
  using the key pattern:

      lock:ingestion:{company_id}:{fiscal_year}

  The lock has a 10-minute TTL to prevent a crashed worker from
  permanently blocking subsequent runs for the same company+year.

  If the lock cannot be acquired (another worker is already processing
  the same company+year combination), the task raises
  ``IngestionLockError`` and is NOT retried — the in-progress worker
  owns that slot.

Usage::

    from services.acquisition.jobs.redis_lock import IngestionLock, IngestionLockError

    async with IngestionLock(redis_client, company_id="abc", fiscal_year=2023):
        await run_ingestion_pipeline(...)

Or as a callable check (without context manager)::

    lock = IngestionLock(redis_client, company_id="abc", fiscal_year=2023)
    if not await lock.try_acquire():
        raise IngestionLockError(lock.key)
    try:
        await run_ingestion_pipeline(...)
    finally:
        await lock.release()

Milestone: M4 (pre-provisioned by Amendment V1.2 compliance sweep)
"""

from __future__ import annotations

import uuid

import structlog

log = structlog.get_logger(__name__)

# Amendment V1.2 §8.2 — 10-minute TTL on the distributed lock.
_LOCK_TTL_SECONDS: int = 600


class IngestionLockError(Exception):
    """
    Raised when the distributed ingestion lock cannot be acquired.

    Indicates that another worker is already processing the same
    company+fiscal_year combination.  The caller must NOT retry
    immediately — wait for the lock to expire or the in-progress
    worker to release it.

    The exception message contains the lock key for logging:
        "Ingestion lock held: lock:ingestion:abc123:2023"
    """


class IngestionLock:
    """
    Async context manager for Redis-backed ingestion job deduplication.

    Implements the SET NX PX pattern for distributed mutual exclusion.
    A unique token is stored as the lock value to prevent accidental
    release by a different holder (e.g., after a TTL-forced expiry and
    re-acquisition by another worker).

    Args:
        redis_client:  An async Redis client (redis.asyncio or aioredis).
        company_id:    Company identifier (UUID or string) — part of the key.
        fiscal_year:   Fiscal year integer — part of the key.
        ttl_seconds:   Lock TTL in seconds (default 600 = 10 minutes).

    Key format (Amendment V1.2 §8.2):
        lock:ingestion:{company_id}:{fiscal_year}
    """

    def __init__(
        self,
        redis_client: object,
        *,
        company_id: str | uuid.UUID,
        fiscal_year: int,
        ttl_seconds: int = _LOCK_TTL_SECONDS,
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        # Normalise company_id to string for key construction.
        cid = str(company_id)
        self.key = f"lock:ingestion:{cid}:{fiscal_year}"
        # Unique token prevents accidental foreign release.
        self._token = str(uuid.uuid4())

    async def try_acquire(self) -> bool:
        """
        Attempt to acquire the lock without blocking.

        Returns:
            True if the lock was acquired.
            False if another holder currently holds the lock.
        """
        result = await self._redis.set(  # type: ignore[union-attr]
            self.key,
            self._token,
            nx=True,  # SET if Not eXists
            ex=self._ttl,  # expiry in seconds
        )
        acquired = result is not None
        if acquired:
            log.debug(
                "ingestion_lock.acquired",
                key=self.key,
                ttl_seconds=self._ttl,
            )
        else:
            log.info(
                "ingestion_lock.already_held",
                key=self.key,
            )
        return acquired

    async def release(self) -> None:
        """
        Release the lock only if this instance still holds it.

        Uses a Lua script to make the check-and-delete atomic.
        If the TTL expired and another worker acquired the lock,
        this method does nothing (safe no-op).
        """
        # Atomic: only delete if value matches our unique token.
        _LUA_RELEASE = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = await self._redis.eval(  # type: ignore[union-attr]
            _LUA_RELEASE, 1, self.key, self._token
        )
        if result:
            log.debug("ingestion_lock.released", key=self.key)
        else:
            log.warning(
                "ingestion_lock.release_skipped",
                key=self.key,
                reason="lock_expired_or_foreign_holder",
            )

    async def __aenter__(self) -> "IngestionLock":
        acquired = await self.try_acquire()
        if not acquired:
            raise IngestionLockError(
                f"Ingestion lock held: {self.key} — "
                "another worker is processing this company+year combination."
            )
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.release()
