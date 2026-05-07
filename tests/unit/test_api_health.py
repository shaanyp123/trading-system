"""Unit tests for `services/api/routes/health.py`.

Health route is exercised against a mocked `db.ping` — we don't spin up a
real Postgres for unit tests. Integration tests against the actual
migrations + `/api/health` shape land alongside the recon service later
(testcontainers per dev-guide §6.3).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from services.api import db as api_db


@pytest.mark.asyncio
async def test_health_returns_ok_when_db_reachable_and_fast(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ping(timeout_seconds: float = 0.2) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(api_db, "ping", fake_ping)

    response = await api_client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db_connected"] is True
    assert body["environment"] == "dev"
    assert body["version"] == "test"
    assert isinstance(body["checks"], list)
    assert body["checks"][0]["name"] == "postgres"
    assert body["checks"][0]["ok"] is True
    assert body["checks"][0]["detail"] is None


@pytest.mark.asyncio
async def test_health_returns_503_when_db_unreachable(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ping(timeout_seconds: float = 0.2) -> tuple[bool, str | None]:
        return False, "ConnectionRefusedError: cannot connect"

    monkeypatch.setattr(api_db, "ping", fake_ping)

    response = await api_client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db_connected"] is False
    assert body["checks"][0]["ok"] is False
    assert "ConnectionRefusedError" in (body["checks"][0]["detail"] or "")


@pytest.mark.asyncio
async def test_health_response_includes_trace_id_header(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ping(timeout_seconds: float = 0.2) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(api_db, "ping", fake_ping)

    response = await api_client.get("/api/health")

    assert response.status_code == 200
    assert "x-trace-id" in {k.lower() for k in response.headers.keys()}
    assert len(response.headers["x-trace-id"]) >= 8


@pytest.mark.asyncio
async def test_health_propagates_inbound_trace_id(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ping(timeout_seconds: float = 0.2) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(api_db, "ping", fake_ping)

    response = await api_client.get(
        "/api/health",
        headers={"x-trace-id": "abc123def456"},
    )

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == "abc123def456"
