"""
Invitations router — team invitation flow endpoints.

Engineering Specification references:
  M2 Execution Plan, Section 2.4   — team invitation flow
  M2 Execution Plan, Section 9.4   — invitation token security

Endpoints:
  POST   /api/v1/invitations              — Send invitation (role >= admin)
  GET    /api/v1/invitations/{token}      — Validate token (public)
  POST   /api/v1/invitations/{token}/accept   — Accept invitation (authenticated)
  POST   /api/v1/invitations/{token}/resend   — Resend invitation email (role >= admin)
  DELETE /api/v1/invitations/{token}      — Cancel invitation (role >= admin)

Authorization:
  - POST / resend / DELETE : require_admin (ADMIN or OWNER)
  - GET (validate)         : public — no JWT required
  - accept                 : require_authenticated (any valid JWT)

Tenant isolation:
  ``tenant_id`` is injected from the JWT payload on all authenticated routes.
  Token lookup is global (tokens are unique), but every mutating operation
  verifies ``invitation.tenant_id == ctx.tenant_id`` before proceeding.

Error codes:
  404 INVITATION_NOT_FOUND  — token unknown, expired, or not pending
  409 CONFLICT              — duplicate active invitation / already a member
  422 VALIDATION_ERROR      — invalid body / wrong token prefix
  403 FORBIDDEN             — invitation email does not match authenticated user
  401 UNAUTHORIZED          — missing or invalid JWT

Milestone: M2-Step 9
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import get_settings
from apps.api.core.database import get_db
from apps.api.core.email import EmailMessage, get_email_backend, render_email_template
from apps.api.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from apps.api.core.security import (
    generate_invitation_token,
    hash_invitation_token,
)
from apps.api.middleware.auth import (
    AuthRequestContext,
    require_admin,
    require_authenticated,
)
from apps.api.models import InvitationStatus
from apps.api.repositories.invitations import InvitationRepository
from apps.api.schemas.invitations import (
    InvitationAccept,
    InvitationCreate,
    InvitationResponse,
    InvitationValidateResponse,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/invitations", tags=["invitations"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_response(invitation: object) -> InvitationResponse:
    """Convert an Invitation ORM instance to its Pydantic response schema."""
    return InvitationResponse.model_validate(invitation)


def _to_validate_response(invitation: object) -> InvitationValidateResponse:
    """Convert to the lightweight public validate schema."""
    return InvitationValidateResponse.model_validate(invitation)


async def _dispatch_invitation_email(
    invitee_email: str,
    raw_token: str,
    role: str,
) -> None:
    """
    Render and dispatch the invitation email.

    Fire-and-forget: email failure is logged but never propagated to the
    caller.  This mirrors the pattern used by the password-reset flow.

    M8: replace the direct backend call with a Celery task dispatch:
      celery_app.send_task(
          "workers.tasks.notification_tasks.send_invitation_email",
          args=[invitee_email, raw_token, role],
      )
    """
    settings = get_settings()
    accept_link = (
        f"{settings.frontend_base_url}/invite/accept"
        f"?token={raw_token}"
    )
    template_ctx = {
        "invitee_email": invitee_email,
        "role": role,
        "accept_link": accept_link,
        "expiry_hours": 72,
        "app_name": settings.email_from_name or "Financial Data Hub",
    }

    try:
        text_body = render_email_template("user_invitation.txt", template_ctx)
        html_body = render_email_template("user_invitation.html", template_ctx)
    except Exception:  # noqa: BLE001
        log.error(
            "invitation.email.template_render_failed",
            invitee_email=invitee_email,
        )
        text_body = (
            f"You have been invited to join Financial Data Hub as {role}.\n"
            f"Accept your invitation: {accept_link}\n"
            f"This link expires in 72 hours."
        )
        html_body = ""

    from_address = (
        f"{settings.email_from_name} <{settings.email_from_address}>"
        if settings.email_from_name
        else settings.email_from_address
    )
    backend = get_email_backend(settings)
    try:
        await backend.send(
            EmailMessage(
                to=invitee_email,
                subject="You've been invited to Financial Data Hub",
                text_body=text_body,
                html_body=html_body,
                from_address=from_address,
                from_name=settings.email_from_name,
            )
        )
    except Exception:  # noqa: BLE001
        log.error(
            "invitation.email.send_failed",
            invitee_email=invitee_email,
            backend=settings.email_backend,
        )


# ---------------------------------------------------------------------------
# POST /api/v1/invitations
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=InvitationResponse,
    status_code=201,
    summary="Send a team invitation",
    description=(
        "Create a pending invitation and dispatch the invitation email.  "
        "Only one active invitation per email per tenant is permitted — "
        "creating a duplicate returns 409.  "
        "Requires ADMIN role or above."
    ),
)
async def create_invitation(
    payload: InvitationCreate,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> InvitationResponse:
    """
    Send a team invitation.

    Steps:
      1. Check for an existing active (pending, non-expired) invitation for
         this email in the tenant → 409 if found.
      2. Generate 288-bit raw token + SHA-256 hash.
      3. Persist Invitation row.
      4. Dispatch invitation email (fire-and-forget).
      5. Return 201 InvitationResponse (raw token NEVER in response).
    """
    repo = InvitationRepository(db)

    existing = await repo.get_active_by_email(ctx.tenant_id, payload.email)
    if existing is not None:
        raise ConflictError(
            f"An active invitation for '{payload.email}' already exists in "
            "this workspace.  Cancel it or wait for it to expire before "
            "sending another."
        )

    raw_token = generate_invitation_token()
    token_hash = hash_invitation_token(raw_token)

    invitation = await repo.create(
        tenant_id=ctx.tenant_id,
        invited_by_id=ctx.user_id,
        token_hash=token_hash,
        schema=payload,
    )

    log.info(
        "invitation.created",
        invitation_id=str(invitation.id),
        tenant_id=str(ctx.tenant_id),
        invitee_email=invitation.invitee_email,
        role=invitation.role,
    )

    # Dispatch email — failure does not roll back the invitation row.
    await _dispatch_invitation_email(
        invitee_email=invitation.invitee_email,
        raw_token=raw_token,
        role=invitation.role,
    )

    return _to_response(invitation)


# ---------------------------------------------------------------------------
# GET /api/v1/invitations/{token}
# ---------------------------------------------------------------------------


@router.get(
    "/{token}",
    response_model=InvitationValidateResponse,
    status_code=200,
    summary="Validate an invitation token",
    description=(
        "Public endpoint — no authentication required.  "
        "Returns invitation metadata so the frontend can display the "
        "workspace name, role, and inviter before the user accepts.  "
        "Returns the same 404 for unknown, already-used, and cancelled "
        "tokens to prevent token existence enumeration."
    ),
)
async def validate_invitation(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> InvitationValidateResponse:
    """
    Validate and return metadata for an invitation token.

    Returns InvitationValidateResponse for any pending invitation, whether
    usable or expired, so the frontend can render appropriate messaging.
    Returns 404 for cancelled or already-accepted tokens.
    """
    token_hash = hash_invitation_token(token)
    repo = InvitationRepository(db)
    invitation = await repo.get_by_token_hash(token_hash)

    if invitation is None:
        raise NotFoundError("Invitation", "")

    return _to_validate_response(invitation)


# ---------------------------------------------------------------------------
# POST /api/v1/invitations/{token}/accept
# ---------------------------------------------------------------------------


@router.post(
    "/{token}/accept",
    response_model=InvitationResponse,
    status_code=200,
    summary="Accept an invitation",
    description=(
        "Accept a pending invitation.  "
        "The authenticated user's email must match the invitation's invitee_email.  "
        "On success, a TenantMembership is created linking the user to the "
        "tenant with the invitation's role, and the invitation is marked accepted.  "
        "Returns 403 if the authenticated user's email does not match the invitation.  "
        "Returns 409 if the user is already a member of the tenant."
    ),
)
async def accept_invitation(
    token: str,
    payload: InvitationAccept = InvitationAccept(),  # noqa: B008
    ctx: AuthRequestContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> InvitationResponse:
    """
    Accept a pending invitation.

    Steps:
      1. Hash the token; look up the invitation.
      2. Verify invitation is usable (pending + not expired).
      3. Verify the authenticated user's email matches invitee_email.
      4. Check the user is not already a member.
      5. Accept the invitation + create TenantMembership atomically.
      6. Return 200 InvitationResponse.
    """
    token_hash = hash_invitation_token(token)
    repo = InvitationRepository(db)
    invitation = await repo.get_by_token_hash(token_hash)

    if invitation is None:
        raise NotFoundError("Invitation", "")

    if not invitation.is_usable:
        raise NotFoundError("Invitation", "")  # same shape as unknown token

    # Verify authenticated user's email matches the invitation.
    # Look up the user to get their email.
    user = await repo.get_user_by_email(invitation.invitee_email)
    if user is None or user.id != ctx.user_id:
        raise ForbiddenError(
            "This invitation was sent to a different email address.  "
            "Log in with the account that received the invitation."
        )

    # Check for existing membership (idempotency guard).
    existing_membership = await repo.get_membership(invitation.tenant_id, ctx.user_id)
    if existing_membership is not None:
        raise ConflictError(
            "You are already a member of this workspace."
        )

    # Accept invitation + create membership atomically (same DB transaction).
    await repo.accept(invitation, accepted_by_id=ctx.user_id)
    await repo.create_membership(
        tenant_id=invitation.tenant_id,
        user_id=ctx.user_id,
        role=invitation.role,
        invited_by_id=invitation.invited_by_id,
    )

    log.info(
        "invitation.accepted",
        invitation_id=str(invitation.id),
        tenant_id=str(invitation.tenant_id),
        accepted_by=str(ctx.user_id),
        role=invitation.role,
    )
    return _to_response(invitation)


# ---------------------------------------------------------------------------
# POST /api/v1/invitations/{token}/resend
# ---------------------------------------------------------------------------


@router.post(
    "/{token}/resend",
    response_model=InvitationResponse,
    status_code=200,
    summary="Resend an invitation",
    description=(
        "Generate a new token for an existing pending invitation and "
        "re-dispatch the invitation email.  "
        "The old token is immediately invalidated.  "
        "The invitation must be in PENDING state (accepted/cancelled cannot "
        "be resent).  "
        "Requires ADMIN role or above."
    ),
)
async def resend_invitation(
    token: str,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> InvitationResponse:
    """
    Resend an invitation with a fresh token.

    Validates tenant ownership: the invitation must belong to the
    requesting user's tenant.
    """
    token_hash = hash_invitation_token(token)
    repo = InvitationRepository(db)
    invitation = await repo.get_by_token_hash(token_hash)

    if invitation is None:
        raise NotFoundError("Invitation", "")

    # Tenant isolation — must own this invitation.
    if invitation.tenant_id != ctx.tenant_id:
        raise NotFoundError("Invitation", "")

    # Only pending invitations can be resent (expired ones can be resent too,
    # since they're still in PENDING status — the repo's partial index
    # includes expired rows).
    if invitation.status != InvitationStatus.PENDING.value:
        raise ConflictError(
            f"Invitation has status '{invitation.status}' and cannot be resent."
        )

    new_raw_token = generate_invitation_token()
    new_token_hash = hash_invitation_token(new_raw_token)

    await repo.refresh_token(invitation, new_token_hash)

    log.info(
        "invitation.resent",
        invitation_id=str(invitation.id),
        tenant_id=str(ctx.tenant_id),
    )

    await _dispatch_invitation_email(
        invitee_email=invitation.invitee_email,
        raw_token=new_raw_token,
        role=invitation.role,
    )

    return _to_response(invitation)


# ---------------------------------------------------------------------------
# DELETE /api/v1/invitations/{token}
# ---------------------------------------------------------------------------


@router.delete(
    "/{token}",
    status_code=204,
    summary="Cancel an invitation",
    description=(
        "Cancel a pending invitation.  "
        "The invitee will no longer be able to use the invitation link.  "
        "Accepted or already-cancelled invitations cannot be cancelled (409).  "
        "Requires ADMIN role or above.  "
        "Returns 204 with no body on success."
    ),
)
async def cancel_invitation(
    token: str,
    ctx: AuthRequestContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Cancel a pending invitation.

    Validates tenant ownership before cancelling.
    """
    token_hash = hash_invitation_token(token)
    repo = InvitationRepository(db)
    invitation = await repo.get_by_token_hash(token_hash)

    if invitation is None:
        raise NotFoundError("Invitation", "")

    # Tenant isolation.
    if invitation.tenant_id != ctx.tenant_id:
        raise NotFoundError("Invitation", "")

    if invitation.status != InvitationStatus.PENDING.value:
        raise ConflictError(
            f"Invitation has status '{invitation.status}' and cannot be cancelled."
        )

    await repo.cancel(invitation)

    log.info(
        "invitation.cancelled",
        invitation_id=str(invitation.id),
        tenant_id=str(ctx.tenant_id),
    )
    return Response(status_code=204)
