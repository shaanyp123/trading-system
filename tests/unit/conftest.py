"""Test fixtures for `tests/unit/test_api_*.py`.

Lightweight FastAPI app fixture that bypasses the lifespan (no real Postgres
required). Tests override DB-touching deps explicitly via `app.dependency_overrides`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api import config as api_config


def _stub_env() -> dict[str, str]:
    return {
        "API_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        "API_ENVIRONMENT": "dev",
        "API_VERSION": "test",
        "API_LOG_LEVEL": "INFO",
    }


@pytest.fixture
def api_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a fresh FastAPI app for each test (clears settings cache)."""
    for k, v in _stub_env().items():
        monkeypatch.setenv(k, v)
    api_config.get_settings.cache_clear()

    # Import here so the env vars above are visible to module load.
    from services.api.main import create_app

    app = create_app()
    yield_app = app
    return yield_app


@pytest_asyncio.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """httpx AsyncClient over an in-memory ASGI transport.

    Lifespan is intentionally NOT triggered: tests that need DB-touching
    behavior override the relevant FastAPI deps directly.
    """
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def override_dep(api_app: FastAPI) -> Any:
    """Helper: returns a function that wires a FastAPI dep override."""

    def _override(target: Any, replacement: Any) -> None:
        api_app.dependency_overrides[target] = replacement

    return _override
