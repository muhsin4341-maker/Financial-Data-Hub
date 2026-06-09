"""
S3 client factory for the Financial Data Hub API.

Engineering Specification references:
  M2 Execution Plan, Section 2.5 — S3 Document Upload / Pre-Signed URLs
  apps/api/core/config.py         — aws_* and s3_* settings

In development / CI, ``settings.aws_endpoint_url`` points to LocalStack.
In production, it is ``None`` and boto3 connects directly to AWS S3.

The client is created per-request via the ``get_s3_client`` FastAPI
dependency so that settings changes (e.g. in tests) are picked up without
restarting the process.

Milestone: M2-Step 8
"""

from __future__ import annotations

import re
from collections.abc import Generator
from typing import Any

import boto3
import structlog

from apps.api.core.config import get_settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def make_s3_client() -> Any:
    """
    Build a configured boto3 S3 client.

    Uses settings for region, credentials, and optional LocalStack
    endpoint_url.  Credentials are omitted when the env vars are empty so
    that IAM instance roles (production) are used automatically.
    """
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "region_name": settings.aws_region,
    }
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url

    log.debug(
        "s3.client.created",
        region=settings.aws_region,
        localstack=bool(settings.aws_endpoint_url),
    )
    return boto3.client("s3", **kwargs)


def get_s3_client() -> Generator[Any, None, None]:
    """
    FastAPI dependency — yield a configured boto3 S3 client.

    Usage in route handlers::

        from apps.api.core.s3 import get_s3_client

        @router.post("/upload-url")
        async def upload_url(s3: Any = Depends(get_s3_client)) -> ...:
            ...
    """
    client = make_s3_client()
    yield client


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r"[^\w\-.]")
_LEADING_UNSAFE = re.compile(r"^[._]+")


def make_safe_filename(filename: str) -> str:
    """
    Sanitise a user-supplied filename for use as an S3 key component.

    Steps:
      1. Strip any directory components (prevents path traversal).
      2. Replace characters that are unsafe in S3 keys or HTTP URLs.
      3. Strip leading dots / underscores (avoids hidden-file naming).
      4. Fall back to ``"document"`` if the result is empty.
      5. Truncate to 255 characters.

    This does NOT guarantee uniqueness — callers should embed the job UUID
    in the key to prevent collisions between concurrent uploads.
    """
    # 1. Basename only
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    # 2. Replace unsafe characters
    name = _UNSAFE_CHARS.sub("_", name)
    # 3. Strip leading unsafe chars
    name = _LEADING_UNSAFE.sub("", name)
    # 4. Fallback
    name = name or "document"
    # 5. Truncate
    return name[:255]
