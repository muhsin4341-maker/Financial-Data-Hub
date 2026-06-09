"""
Notification Tasks — Celery task definitions for email dispatch.

Engineering Specification references:
  M2 Execution Plan, Section 2.4   — team invitation flow
  Part 1, Section 9.2, Decision 2  — all tasks must be idempotent

Current milestone (M2):
  send_invitation_email — stub registered so the task name is reservable.
  The router calls the email backend directly (synchronous, in-process).
  In M8, replace the router's direct backend call with:
    celery_app.send_task(
        "workers.tasks.notification_tasks.send_invitation_email",
        args=[invitee_email, raw_token, role],
    )

Full implementation (M8):
  - Connect to SES / Resend backend.
  - Add retry logic with exponential back-off.
  - Add idempotency key on (invitee_email, token_hash) to prevent
    duplicate deliveries on retry.
"""

from __future__ import annotations

import structlog

from workers.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(
    name="workers.tasks.notification_tasks.send_invitation_email",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    ignore_result=True,
)  # type: ignore[misc]
def send_invitation_email(
    self: object,  # noqa: ARG001  — Celery bound task instance
    invitee_email: str,
    raw_token: str,
    role: str,
) -> None:
    """
    Dispatch a team invitation email.

    M2: stub — logs the intent.  Real delivery wired in M8.

    Args:
        invitee_email: Email address of the person being invited.
        raw_token:     Raw 288-bit URL-safe base64 invitation token.
        role:          RBAC role string (viewer/analyst/admin).
    """
    log.info(
        "notification_tasks.send_invitation_email.stub",
        invitee_email=invitee_email,
        role=role,
        note="M8: implement real email dispatch here",
    )
