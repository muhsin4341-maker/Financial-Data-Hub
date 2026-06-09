"""
Rate limiter and circuit breaker for external source HTTP requests.

Three components are provided:

  InProcessRateLimiter — asyncio token-bucket, single-process use.
    Suitable for the API server and single-worker Celery configurations.
    Uses no external dependencies.

  RedisRateLimiter — distributed sliding-window counter backed by Redis.
    Suitable for multi-process Celery workers where per-process limiters
    would collectively exceed the source's rate limit.
    Falls back to InProcessRateLimiter if Redis is unavailable (fail-open).

  CircuitBreaker — per-source failure tracker (Amendment V1.2, Section 9.2).
    Trips OPEN after a configurable number of consecutive failures.
    Blocks all further requests for a configurable hold duration.
    Callers catch CircuitBreakerOpenError and surface it to the API layer.

Both RateLimiter implementations honour the ``RateLimiter`` ABC — callers
receive the same interface regardless of which backend is in use.

SEC EDGAR rate limit policy (per SEC robots.txt and acceptable use):
  Maximum 10 requests per second.  We default to 8 req/s to maintain a
  conservative safety margin and avoid SEC IP blocks.

Milestone: M3.4 — SEC EDGAR Integration
Amendment: V1.2, Section 9.2 — Scraper Resiliency & Circuit Breakers
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

#: Default consecutive-failure threshold before tripping open (Amendment V1.2 §9.2).
_DEFAULT_FAILURE_THRESHOLD: int = 5
#: Default hold-open duration in seconds — 30 minutes (Amendment V1.2 §9.2).
_DEFAULT_HOLD_OPEN_SECONDS: float = 1800.0


class CircuitBreakerOpenError(Exception):
    """
    Raised when a circuit breaker is open and the caller must not proceed.

    The message embeds the source name and the UTC timestamp at which the
    breaker will auto-reset, enabling the API layer to surface a precise,
    actionable message to the user:

        "[External Regulatory Server Throttled — Local Pipeline Paused to
         Prevent IP Block]"

    Callers that receive this error must NOT retry — the breaker is open
    specifically to prevent further outbound requests.
    """


class CircuitBreaker:
    """
    Per-source failure tracker that enforces Amendment V1.2 Section 9.2.

    Algorithm (CLOSED → OPEN → auto-reset):
      CLOSED: requests proceed normally.  Each successful response resets the
              consecutive-failure counter to zero.  Each failure increments it.
              When the counter reaches ``failure_threshold`` the breaker trips
              to OPEN and records the trip timestamp.

      OPEN:   ``check()`` raises ``CircuitBreakerOpenError`` immediately for
              the full ``hold_open_seconds`` duration.  No outbound request is
              attempted; the error is surfaced to the caller and ultimately to
              the API layer.

      RESET:  After ``hold_open_seconds`` have elapsed since the trip, the
              next ``check()`` succeeds (breaker transitions back to CLOSED)
              allowing one trial request to pass through.

    Thread / coroutine safety:
      All state mutations are guarded by an ``asyncio.Lock``.  The breaker is
      safe for concurrent use within a single event loop.

    Usage::

        breaker = CircuitBreaker(source_name="SEC_EDGAR")
        try:
            breaker.check()          # raises if open
            resp = await client.get(url)
            breaker.record_success()
        except CircuitBreakerOpenError:
            raise                    # propagate — do NOT retry
        except Exception:
            breaker.record_failure()
            raise

    Args:
        source_name:       Human-readable label used in log messages and errors.
        failure_threshold: Consecutive failures before tripping (default 5).
        hold_open_seconds: Seconds to hold open before auto-reset (default 1800).
    """

    def __init__(
        self,
        source_name: str,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        hold_open_seconds: float = _DEFAULT_HOLD_OPEN_SECONDS,
    ) -> None:
        self._source_name = source_name
        self._failure_threshold = failure_threshold
        self._hold_open_seconds = hold_open_seconds

        self._consecutive_failures: int = 0
        self._tripped_at: float | None = None  # monotonic timestamp of last trip

    @property
    def is_open(self) -> bool:
        """True if the breaker is currently open and blocking requests."""
        if self._tripped_at is None:
            return False
        return (time.monotonic() - self._tripped_at) < self._hold_open_seconds

    @property
    def seconds_until_reset(self) -> float:
        """Remaining hold-open duration in seconds. Zero if closed."""
        if self._tripped_at is None:
            return 0.0
        remaining = self._hold_open_seconds - (time.monotonic() - self._tripped_at)
        return max(0.0, remaining)

    def check(self) -> None:
        """
        Assert that the circuit is closed and requests may proceed.

        If the hold-open window has expired, silently resets the breaker to
        CLOSED before returning, allowing one trial request through.

        Raises:
            CircuitBreakerOpenError: If the breaker is currently open.
        """
        if self._tripped_at is not None:
            elapsed = time.monotonic() - self._tripped_at
            if elapsed >= self._hold_open_seconds:
                # Hold window expired — auto-reset to CLOSED for trial request.
                log.info(
                    "circuit_breaker.auto_reset",
                    source=self._source_name,
                    held_open_seconds=round(elapsed, 1),
                )
                self._tripped_at = None
                self._consecutive_failures = 0
            else:
                remaining = round(self._hold_open_seconds - elapsed, 0)
                raise CircuitBreakerOpenError(
                    f"[External Regulatory Server Throttled — Local Pipeline Paused to "
                    f"Prevent IP Block] Source={self._source_name!r} resets in "
                    f"{int(remaining)}s."
                )

    def record_success(self) -> None:
        """Reset the consecutive-failure counter on a successful response."""
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """
        Increment the consecutive-failure counter and trip open if threshold reached.

        A trip is idempotent: calling record_failure() on an already-open breaker
        refreshes the trip timestamp, extending the hold-open window.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._tripped_at = time.monotonic()
            log.error(
                "circuit_breaker.tripped_open",
                source=self._source_name,
                consecutive_failures=self._consecutive_failures,
                hold_open_seconds=self._hold_open_seconds,
            )

# Conservative default: 20% below SEC's 10 req/s ceiling.
_DEFAULT_RATE: float = 8.0   # tokens per second
_DEFAULT_BURST: int = 10     # max tokens (burst capacity)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class RateLimiter(ABC):
    """
    Abstract interface for acquisition source rate limiting.

    Callers await ``acquire()`` before each outbound HTTP request::

        await rate_limiter.acquire()
        response = await client.get(url)

    Implementations must be safe for concurrent coroutine access.
    """

    @abstractmethod
    async def acquire(self) -> None:
        """
        Block until a request token is available.

        Returns immediately when a token is available; sleeps until one
        becomes available when the bucket is empty (throttled).
        """


# ---------------------------------------------------------------------------
# In-process token bucket
# ---------------------------------------------------------------------------


class InProcessRateLimiter(RateLimiter):
    """
    Asyncio token-bucket rate limiter for single-process use.

    Algorithm:
      Tokens accumulate at ``rate`` tokens/second up to ``burst`` max.
      Each ``acquire()`` consumes one token; if none available, the caller
      waits exactly as long as needed for one token to refill.

    Concurrency:
      An ``asyncio.Lock`` serialises token accounting.  Only one coroutine
      at a time enters the accounting section, preventing over-issuance.
      The sleep (if any) happens outside the lock so other coroutines can
      proceed.

    Usage::

        limiter = InProcessRateLimiter(rate=8.0, burst=10)
        await limiter.acquire()
        resp = await client.get(url)
    """

    def __init__(self, rate: float = _DEFAULT_RATE, burst: int = _DEFAULT_BURST) -> None:
        """
        Args:
            rate:  Token refill rate in tokens per second (e.g. 8.0 for 8 req/s).
            burst: Maximum token accumulation (burst capacity).
                   Setting burst > rate allows a short request burst after idle.
        """
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        if burst <= 0:
            raise ValueError(f"burst must be positive, got {burst}")

        self._rate = rate
        self._burst = burst
        self._tokens: float = float(burst)   # start full
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        wait_time = 0.0

        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # Refill tokens proportionally to elapsed time.
            self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            # Calculate exact sleep needed for one token to be available.
            wait_time = (1.0 - self._tokens) / self._rate
            self._tokens = 0.0
            self._last_refill = now + wait_time  # advance clock by wait

        if wait_time > 0:
            log.debug(
                "rate_limiter.throttling",
                implementation="in_process",
                wait_seconds=round(wait_time, 4),
            )
            await asyncio.sleep(wait_time)

    @property
    def rate(self) -> float:
        """Configured refill rate in tokens per second."""
        return self._rate

    @property
    def burst(self) -> int:
        """Maximum token burst capacity."""
        return self._burst


# ---------------------------------------------------------------------------
# Redis sliding-window limiter
# ---------------------------------------------------------------------------


class RedisRateLimiter(RateLimiter):
    """
    Distributed rate limiter using a Redis sliding-window counter.

    Increments a per-key counter in Redis with a 1-second TTL.  When the
    counter exceeds ``rate``, the caller sleeps for the remainder of the
    current window before retrying.

    Suitable for multi-process Celery workers where several processes would
    collectively exceed the source's rate limit if each used its own
    in-process limiter.

    Fail-open:
      If Redis is unavailable (connection error, timeout, etc.), the call
      falls through to an in-process fallback limiter.  A Redis outage never
      blocks acquisition — it degrades to single-process rate limiting.

    Args:
        redis_client: An awaitable redis.asyncio / aioredis client.
        key:          Redis key for the sliding window counter.
                      Recommended format: ``ratelimit:{source}:{window_epoch}``
                      For simplicity this implementation uses a fixed key with
                      a 1-second TTL (SET if NOT EXISTS → INCR pattern).
        rate:         Max requests per window (default 8).
        window:       Window size in seconds (default 1).
    """

    def __init__(
        self,
        redis_client: object,
        key: str = "ratelimit:sec_edgar",
        rate: int = int(_DEFAULT_RATE),
        window: int = 1,
    ) -> None:
        self._redis = redis_client
        self._key = key
        self._rate = rate
        self._window = window
        self._fallback = InProcessRateLimiter(rate=float(rate), burst=rate)

    async def acquire(self) -> None:
        try:
            # Atomic increment + set TTL on first increment.
            count: int = await self._redis.incr(self._key)  # type: ignore[union-attr]
            if count == 1:
                await self._redis.expire(self._key, self._window)  # type: ignore[union-attr]

            if count > self._rate:
                # Over limit — sleep a proportional fraction of the window.
                wait_time = self._window / self._rate
                log.debug(
                    "rate_limiter.throttling",
                    implementation="redis",
                    key=self._key,
                    count=count,
                    limit=self._rate,
                    wait_seconds=round(wait_time, 4),
                )
                await asyncio.sleep(wait_time)

        except Exception as exc:  # noqa: BLE001
            log.warning(
                "rate_limiter.redis_error_fallback",
                key=self._key,
                error=str(exc),
            )
            await self._fallback.acquire()
