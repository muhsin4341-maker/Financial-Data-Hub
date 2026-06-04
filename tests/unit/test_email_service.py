"""
Unit tests — Email service abstraction (apps/api/core/email.py).

Tests cover:
  - ConsoleEmailBackend.send() does not raise and logs
  - get_email_backend() returns ConsoleEmailBackend for "console"
  - get_email_backend() falls back to ConsoleEmailBackend for unknown names
  - render_email_template() renders Jinja2 variables correctly
  - EmailMessage dataclass construction

Milestone: M1-Step22
"""

from __future__ import annotations

import pytest

from apps.api.core.email import (
    ConsoleEmailBackend,
    EmailMessage,
    get_email_backend,
    render_email_template,
)


class TestEmailMessage:
    def test_required_fields(self) -> None:
        msg = EmailMessage(to="a@b.com", subject="Hi", text_body="Hello")
        assert msg.to == "a@b.com"
        assert msg.subject == "Hi"
        assert msg.text_body == "Hello"

    def test_optional_defaults(self) -> None:
        msg = EmailMessage(to="a@b.com", subject="Hi", text_body="Hello")
        assert msg.html_body == ""
        assert msg.from_address == ""
        assert msg.from_name == ""


class TestConsoleEmailBackend:
    async def test_send_does_not_raise(self) -> None:
        backend = ConsoleEmailBackend()
        msg = EmailMessage(to="dev@example.com", subject="Test", text_body="Hello")
        # Should complete without raising
        await backend.send(msg)

    async def test_send_long_body(self) -> None:
        backend = ConsoleEmailBackend()
        msg = EmailMessage(to="dev@example.com", subject="Test", text_body="x" * 10_000)
        await backend.send(msg)  # must not raise


class TestGetEmailBackend:
    def test_console_returns_console_backend(self) -> None:
        settings = _make_settings("console")
        backend = get_email_backend(settings)
        assert isinstance(backend, ConsoleEmailBackend)

    def test_unknown_backend_falls_back_to_console(self) -> None:
        settings = _make_settings("ses")  # M8 — not yet implemented
        backend = get_email_backend(settings)
        assert isinstance(backend, ConsoleEmailBackend)

    def test_case_insensitive(self) -> None:
        settings = _make_settings("CONSOLE")
        backend = get_email_backend(settings)
        assert isinstance(backend, ConsoleEmailBackend)


class TestRenderEmailTemplate:
    def test_password_reset_txt_renders_variables(self) -> None:
        rendered = render_email_template(
            "password_reset.txt",
            {
                "full_name": "Alice Smith",
                "email": "alice@example.com",
                "reset_link": "https://app.example.com/auth/reset?token=XYZ",
                "expires_in": "1 hour",
            },
        )
        assert "Alice Smith" in rendered
        assert "alice@example.com" in rendered
        assert "https://app.example.com/auth/reset?token=XYZ" in rendered
        assert "1 hour" in rendered

    def test_password_reset_html_renders_variables(self) -> None:
        rendered = render_email_template(
            "password_reset.html",
            {
                "full_name": "Bob Jones",
                "email": "bob@example.com",
                "reset_link": "https://app.example.com/auth/reset?token=ABC",
                "expires_in": "1 hour",
            },
        )
        assert "Bob Jones" in rendered
        assert "bob@example.com" in rendered
        assert "https://app.example.com/auth/reset?token=ABC" in rendered

    def test_missing_template_raises(self) -> None:
        from jinja2 import TemplateNotFound

        with pytest.raises(TemplateNotFound):
            render_email_template("nonexistent_template.txt", {})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(backend_name: str) -> object:
    """Create a minimal settings-like object with email_backend set."""
    from unittest.mock import MagicMock

    s = MagicMock()
    s.email_backend = backend_name
    return s
