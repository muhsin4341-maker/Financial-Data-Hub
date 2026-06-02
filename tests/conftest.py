"""
Pytest configuration and shared fixtures.

Unit tests: use mocks, no database required.
Integration tests: use real PostgreSQL + Redis via Docker Compose.
"""
import pytest
from httpx import AsyncClient, ASGITransport
# TODO M1: from apps.api.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"

# TODO M1-Step17: Add auth fixtures (test_user, test_tenant, auth_headers)
# TODO M1-Step12: Add database fixtures (test_db_session)
# TODO M2-Step24: Add company fixtures
