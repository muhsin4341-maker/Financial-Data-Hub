"""
Standard error response schema and exception handlers.

Engineering Spec Part 1, Section 2.2 Decision 4.
All exceptions map to { "error": { "code": ..., "message": ..., "details": {}, "request_id": ... } }

Milestone: M1-Step13
"""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
    request_id: str = ""


class APIErrorResponse(BaseModel):
    error: ErrorDetail


class APIError(Exception):
    """Base application exception — maps to standard error response."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


# ── Standard error codes ─────────────────────────────────────────────────────
class NotFoundError(APIError):
    def __init__(self, resource: str, resource_id: str = "") -> None:
        super().__init__(
            code=f"{resource.upper()}_NOT_FOUND",
            message=f"{resource} not found{f': {resource_id}' if resource_id else ''}",
            status_code=404,
        )


class UnauthorizedError(APIError):
    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(code="UNAUTHORIZED", message=message, status_code=401)


class ForbiddenError(APIError):
    def __init__(self, message: str = "Insufficient permissions") -> None:
        super().__init__(code="FORBIDDEN", message=message, status_code=403)


class ValidationError(APIError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code="VALIDATION_ERROR", message=message, status_code=422, details=details)


class ConflictError(APIError):
    def __init__(self, message: str) -> None:
        super().__init__(code="CONFLICT", message=message, status_code=409)


class RateLimitError(APIError):
    def __init__(self) -> None:
        super().__init__(code="RATE_LIMIT_EXCEEDED", message="Too many requests", status_code=429)


# ── Exception handler (register on FastAPI app in M1-Step10) ─────────────────
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "request_id": request_id,
            }
        },
    )
