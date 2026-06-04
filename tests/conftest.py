"""
Pytest configuration and shared fixtures.

Unit tests:        use mocks, no database required.
Integration tests: require real PostgreSQL + Redis (Docker Compose).

Environment loading
-------------------
The .env file is loaded at session start so DATABASE_URL and JWT_SECRET are
available to the FastAPI app's Pydantic Settings when integration tests run.

Integration test gating
-----------------------
Integration tests are SKIPPED by default. Set RUN_INTEGRATION_TESTS=1 in the
shell to enable them (requires Docker Compose services to be healthy):

    docker compose up -d db redis
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/ -v

The DATABASE_URL env var (from .env) is used by the FastAPI app; the
RUN_INTEGRATION_TESTS flag is used by this conftest to gate test execution.
This separation means the .env file loading does not accidentally run
integration tests when a developer runs the full test suite locally.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

# Load .env before fixtures or skip markers are evaluated.
load_dotenv()

# ---------------------------------------------------------------------------
# Integration test gate
# ---------------------------------------------------------------------------

_RUN_INTEGRATION = os.getenv("RUN_INTEGRATION_TESTS", "").lower() in (
    "1", "true", "yes"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """
    Skip all tests in tests/integration/ unless RUN_INTEGRATION_TESTS is set.

    This hook runs after collection and adds a skip marker to every integration
    test when the flag is absent, regardless of any pytestmark defined in the
    individual test modules. This prevents the .env-loaded DATABASE_URL from
    accidentally enabling integration tests during normal unit test runs.
    """
    if _RUN_INTEGRATION:
        return  # integration tests explicitly requested — let them run

    skip_marker = pytest.mark.skip(
        reason=(
            "Integration tests are disabled by default. "
            "Run: docker compose up -d db redis && "
            "RUN_INTEGRATION_TESTS=1 pytest tests/integration/ -v"
        )
    )
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

# TODO M2-Step24: Add company fixtures
