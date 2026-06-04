"""
Email service abstraction — backend-agnostic email dispatch.

Engineering Specification references:
  Part 3, Section 13.4 — Email service design
  Part 1, Section 2.3  — Service layer: no direct external calls from route handlers

Architecture
------------
Three layers:

  1. ``EmailMessage`` dataclass — the canonical email payload. All senders
     construct one of these regardless of the delivery backend.

  2. ``EmailBackend`` abstract base class — one ``send()`` coroutine.
     Concrete implementations hide infrastructure details from callers.

  3. ``get_email_backend()`` factory — reads ``settings.email_backend`` and
     returns the appropriate concrete implementation. Route handlers always
     call this factory; they never instantiate a backend directly.

Backends
--------
  ConsoleEmailBackend  — M1  — logs to stdout via structlog. No external calls.
                               Safe for local development, CI, and tests.
  SESEmailBackend      — M8  — AWS SES via boto3 (production).
  ResendEmailBackend   — M8  — Resend API (dev/staging alternative to SES).

Jinja2 template rendering
--------------------------
``render_email_template()`` loads a ``*.txt`` or ``*.html`` file from the
``apps/api/email_templates/`` directory and renders it with the supplied
context dict. Route handlers build the context, call the render helper,
then call ``backend.send()``.

Milestone: M1-Step22 — ConsoleEmailBackend + template rendering
           M8         — SESEmailBackend + ResendEmailBackend
Status:    COMPLETE (M1 scope)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from apps.api.core.config import Settings

log = structlog.get_logger(__name__)

# Absolute path to the email templates directory.
_TEMPLATES_DIR: Path = Path(__file__).parent.parent / "email_templates"


# ---------------------------------------------------------------------------
# EmailMessage — canonical payload
# ---------------------------------------------------------------------------


@dataclass
class EmailMessage:
    """
    Immutable description of one outbound email.

    All email backends accept this type. Build it in the route handler or
    service layer; the backend decides how to deliver it.

    Attributes:
        to:           Recipient email address (single recipient per message).
        subject:      Email subject line.
        text_body:    Plain-text body (required — always provide a text version).
        html_body:    HTML body (optional; empty string disables HTML part).
        from_address: Sender address. Leave empty to use ``settings.email_from_address``.
        from_name:    Sender display name. Leave empty to use ``settings.email_from_name``.
    """

    to: str
    subject: str
    text_body: str
    html_body: str = field(default="")
    from_address: str = field(default="")
    from_name: str = field(default="")


# ---------------------------------------------------------------------------
# EmailBackend — abstract interface
# ---------------------------------------------------------------------------


class EmailBackend(ABC):
    """
    Abstract base for all email delivery backends.

    All methods are async so backends can perform I/O (HTTP calls, SMTP)
    without blocking the event loop.
    """

    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        """
        Deliver one email message.

        Implementations must not raise on delivery failure (they should log
        the error at WARNING or ERROR level and continue). The caller treats
        ``send()`` as fire-and-forget for transactional emails.

        Args:
            message: The email to deliver.
        """


# ---------------------------------------------------------------------------
# ConsoleEmailBackend — development / test backend
# ---------------------------------------------------------------------------


class ConsoleEmailBackend(EmailBackend):
    """
    Development email backend — logs the email to stdout via structlog.

    No external calls are made. This backend is safe for:
      - Local development (``EMAIL_BACKEND=console`` in ``.env``)
      - Automated tests (patch ``get_email_backend`` to return this)
      - CI pipelines

    The full email content (to, subject, text body) is logged at INFO level
    so developers can verify emails are triggered without inspecting a mail
    server.
    """

    async def send(self, message: EmailMessage) -> None:
        log.info(
            "email.console.sent",
            to=message.to,
            subject=message.subject,
            from_address=message.from_address,
            body_preview=message.text_body[:200],
        )


# ---------------------------------------------------------------------------
# Jinja2 template rendering
# ---------------------------------------------------------------------------


def _get_jinja_env() -> Environment:
    """Return a Jinja2 Environment pointed at the email templates directory."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_email_template(template_name: str, context: dict[str, Any]) -> str:
    """
    Render a Jinja2 email template from ``apps/api/email_templates/``.

    Args:
        template_name: Filename relative to the templates directory,
                       e.g. ``"password_reset.txt"`` or ``"password_reset.html"``.
        context:       Template variable mapping passed to Jinja2.

    Returns:
        Rendered string (plain text or HTML depending on the template).

    Raises:
        jinja2.TemplateNotFound: If the template file does not exist.
    """
    env = _get_jinja_env()
    template = env.get_template(template_name)
    return template.render(**context)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_email_backend(settings: Settings) -> EmailBackend:
    """
    Instantiate and return the email backend configured in ``settings``.

    Reads ``settings.email_backend`` (default: ``"console"``).
    Unknown backend names fall back to ``ConsoleEmailBackend`` with a warning
    so a misconfiguration never silently swallows emails.

    Args:
        settings: Application settings instance (typically from ``get_settings()``).

    Returns:
        A concrete ``EmailBackend`` instance ready to call ``.send()`` on.
    """
    backend_name = settings.email_backend.lower().strip()

    if backend_name == "console":
        return ConsoleEmailBackend()

    # SES and Resend backends are implemented in M8.
    # Until then, fall back to ConsoleEmailBackend with a warning so
    # misconfigured environments don't silently drop emails.
    log.warning(
        "email.unknown_backend_fallback",
        configured=backend_name,
        fallback="console",
        note="SES and Resend backends are implemented at M8.",
    )
    return ConsoleEmailBackend()
