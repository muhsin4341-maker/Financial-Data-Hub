"""
Admin router — M6/B3.

Exposes system-level administrative endpoints that are restricted to users
with the ADMIN or OWNER role (``require_admin`` dependency).

Endpoints
─────────
POST /api/v1/admin/fx-rates/sync
    Dispatch a background FX rate ingestion job via Celery.
    Accepts an optional JSON body with ``days_back`` and ``currencies``.
    Returns HTTP 202 Accepted with the Celery task ID for status tracking.

Security
────────
All routes are protected by ``require_admin``, which validates:
  - A valid JWT token in the request (via JWTAuthMiddleware state).
  - The authenticated user's role is ADMIN or OWNER.
  - The user belongs to a valid, non-expired tenant.

Milestone: B3 — Admin FX Sync Endpoint
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.middleware.auth import AuthRequestContext, require_admin

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class FXSyncRequest(BaseModel):
    """
    Optional body for POST /api/v1/admin/fx-rates/sync.

    All fields have defaults so the request body may be omitted entirely.
    """

    days_back: int = Field(
        default=90,
        ge=1,
        le=3650,
        description=(
            "Number of calendar days back from today to fetch and seed. "
            "Default 90 covers one full fiscal quarter plus look-back buffer. "
            "Use 365–3650 for a historical backfill."
        ),
        examples=[90, 365, 1825],
    )
    currencies: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of ISO 4217 currency codes to seed. "
            "When omitted the full default list of ~30 major currencies is used. "
            "USD is always included as the anchor and is never stored (identity rate)."
        ),
        examples=[["EUR", "GBP", "INR", "JPY"]],
    )


class FXSyncResponse(BaseModel):
    """
    Response body for POST /api/v1/admin/fx-rates/sync.

    Returned with HTTP 202 Accepted once the Celery task has been enqueued.
    The caller may use ``task_id`` with a Celery result back-end or the job
    monitoring dashboard to track progress.
    """

    task_id: str = Field(
        description="Celery task UUID; use this to poll the Celery result back-end."
    )
    status: str = Field(
        default="queued",
        description="Always 'queued' — indicates the task has been dispatched.",
    )
    message: str = Field(description="Human-readable confirmation message.")
    days_back: int = Field(description="Number of days back that will be seeded.")
    currencies: list[str] | None = Field(
        description="Currency codes to be seeded, or null for the default list."
    )
    queue: str = Field(description="Celery queue the task was dispatched to.")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/fx-rates/sync",
    response_model=FXSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger FX rate ingestion",
    description=(
        "Dispatch a background Celery task that fetches daily closing exchange "
        "rates from the ECB Statistical Data Warehouse and bulk-upserts them into "
        "the *daily_fx_rates* table.\n\n"
        "The operation is fully idempotent — re-running for the same date range "
        "will update any revised ECB rates (ON CONFLICT DO UPDATE) and skip "
        "unchanged rows.\n\n"
        "**Requires ADMIN or OWNER role.**"
    ),
    responses={
        202: {"description": "Task accepted and queued."},
        401: {"description": "Missing or invalid authentication token."},
        403: {"description": "Insufficient role — ADMIN or OWNER required."},
        503: {
            "description": "Celery broker unreachable — task could not be dispatched."
        },
    },
)
async def trigger_fx_sync(
    auth: Annotated[AuthRequestContext, Depends(require_admin)],
    body: Annotated[FXSyncRequest, Body()] = FXSyncRequest(),  # type: ignore[assignment]
) -> Any:
    """
    Enqueue an FX rate seeding task on the ``fetch`` Celery queue.

    The endpoint returns HTTP 202 immediately once the task message has been
    sent to the broker.  The actual rate ingestion runs asynchronously in the
    background Celery worker.

    Args:
        auth:  Injected auth context — used for structured logging only.
        body:  Optional request body controlling the sync scope.

    Returns:
        FXSyncResponse with the Celery task ID and dispatch metadata.

    Raises:
        HTTPException 503:  When the Celery broker is unreachable and the
                            task message cannot be enqueued.
    """
    from workers.queues import QUEUE_FETCH  # noqa: PLC0415
    from workers.tasks.fx_tasks import sync_fx_rates_task  # noqa: PLC0415

    bound_log = log.bind(
        user_id=str(auth.user_id),
        tenant_id=str(auth.tenant_id),
        days_back=body.days_back,
        currencies=body.currencies,
    )
    bound_log.info("admin.fx_sync.dispatch_requested")

    # Build apply_async kwargs — only pass currencies when explicitly provided.
    task_kwargs: dict[str, Any] = {"days_back": body.days_back}
    if body.currencies is not None:
        task_kwargs["currencies"] = [c.upper() for c in body.currencies]

    try:
        task_result = sync_fx_rates_task.apply_async(
            kwargs=task_kwargs,
            queue=QUEUE_FETCH,
        )
    except Exception as exc:  # noqa: BLE001
        bound_log.error(
            "admin.fx_sync.dispatch_failed",
            error=str(exc)[:300],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Failed to dispatch FX sync task — the Celery broker may be "
                f"unreachable. Error: {str(exc)[:200]}"
            ),
        ) from exc

    task_id: str = str(task_result.id)
    bound_log.info(
        "admin.fx_sync.dispatched",
        task_id=task_id,
        queue=QUEUE_FETCH,
    )

    currencies_label = (
        f"{len(body.currencies)} currencies"
        if body.currencies
        else "default currency list"
    )
    return FXSyncResponse(
        task_id=task_id,
        status="queued",
        message=(
            f"FX rate sync task dispatched. "
            f"Seeding {body.days_back} days of rates ({currencies_label})."
        ),
        days_back=body.days_back,
        currencies=body.currencies,
        queue=QUEUE_FETCH,
    )
