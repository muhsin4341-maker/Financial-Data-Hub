"""
Shared fixtures for integration tests.

The ``client`` fixture manually triggers the FastAPI lifespan context manager
so that ``init_db()`` runs before any request and ``dispose_db()`` runs on
teardown. Without this, ``ASGITransport`` does not fire ASGI lifespan events,
causing ``get_db()`` to raise 'Database not initialised'.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
async def client() -> AsyncClient:  # type: ignore[override]
    """
    httpx AsyncClient backed by the real FastAPI app, with the full lifespan
    (init_db + dispose_db) triggered around each test.
    """
    from apps.api.main import app, lifespan

    async with lifespan(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c  # type: ignore[misc]
