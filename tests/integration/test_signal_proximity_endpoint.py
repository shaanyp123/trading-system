"""Integration tests for ``GET /api/signals/proximity`` (PR-B).

PR-B of ``Docs/signal-proximity-design.md`` (SIGNED OFF 2026-05-28).
Verifies the endpoint:

  1. Returns latest-per-market rows ordered by the underlying repo's
     DISTINCT ON walk + the correct ``as_of_cycle_ts_utc``.
  2. Renders the empty state (``{as_of_cycle_ts_utc: null, markets: []}``)
     when the table has no rows.
  3. Enforces the same auth as ``GET /api/signals`` — i.e., the
     ``Depends(get_session_context)`` dependency fails closed in
     ``live-small``/``live-scale`` environments (where the Phase-0 stub
     session middleware does NOT inject a stub session).
  4. Flattens repo rows into the design §6.4 wire shape (per-gate
     sub-objects, Decimal-as-string per A05).

If Docker is unreachable, the module skips cleanly.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from services.api.repos.signal_proximity import insert_rows
from services.api.schemas.lean import GateProximityItem, MarketEvaluationItem

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]


def _require_docker() -> None:
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon unavailable: {exc}")


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    _require_docker()
    with PostgresContainer("postgres:16") as pg:
        sync_url = pg.get_connection_url()
        prev_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = sync_url
        try:
            from alembic.config import Config

            from alembic import command

            cfg = Config(str(REPO_ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
            command.upgrade(cfg, "head")
        finally:
            if prev_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev_url
        yield sync_url


@pytest.fixture(scope="module")
def sync_engine(pg_url: str) -> Iterator[Engine]:
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def async_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    async_url = pg_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
    engine = create_async_engine(async_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def async_session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _truncate_proximity(sync_engine: Engine) -> Iterator[None]:
    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE signal_proximity RESTART IDENTITY"))
    yield


def _gate(state: str, headroom: str | None) -> GateProximityItem:
    return GateProximityItem(
        state=state,  # type: ignore[arg-type]
        headroom=Decimal(headroom) if headroom is not None else None,
        detail=None,
    )


def _evaluation(
    market: str,
    *,
    overall: str = "close",
    closest: str = "donchian",
    status: str = "ok",
) -> MarketEvaluationItem:
    return MarketEvaluationItem(
        market=market,
        long_donchian=_gate("close", "0.008"),
        short_donchian=_gate("fail", "0.085"),
        long_trend=_gate("pass", "0.015"),
        short_trend=_gate("fail", "-0.015"),
        efficiency=_gate("pass", "0.23"),
        last_close=Decimal("5234.50"),
        efficiency_ratio_value=Decimal("0.43"),
        efficiency_ratio_threshold=Decimal("0.20"),
        overall_state=overall,  # type: ignore[arg-type]
        closest_gate=closest,  # type: ignore[arg-type]
        gate_status=status,  # type: ignore[arg-type]
    )


@pytest_asyncio.fixture
async def app_dev(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build a FastAPI app in ``dev`` env — Phase-0 stub session injects."""
    async_url = pg_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
    monkeypatch.setenv("API_DATABASE_URL", async_url)
    monkeypatch.setenv("API_ENVIRONMENT", "dev")
    monkeypatch.setenv("API_VERSION", "test")
    monkeypatch.setenv("API_LOG_LEVEL", "INFO")
    monkeypatch.setenv("API_LEAN_LOCAL_BEARER_TOKEN", "lean-bearer-secret-test-32-bytes")
    monkeypatch.setenv("API_DISCORD_BOT_BEARER_TOKEN", "bot-bearer-secret-test-32-bytes")

    from services.api import config as api_config

    api_config.get_settings.cache_clear()
    main_mod = importlib.import_module("services.api.main")
    app = main_mod.create_app()

    from services.api import db as api_db

    settings = api_config.get_settings()
    await api_db.init_pool(settings)
    try:
        yield app
    finally:
        await api_db.close_pool()


@pytest_asyncio.fixture
async def app_production(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build a FastAPI app in ``live-small`` env — stub session FAILS CLOSED."""
    async_url = pg_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
    monkeypatch.setenv("API_DATABASE_URL", async_url)
    monkeypatch.setenv("API_ENVIRONMENT", "live-small")
    monkeypatch.setenv("API_VERSION", "test")
    monkeypatch.setenv("API_LOG_LEVEL", "INFO")
    monkeypatch.setenv("API_LEAN_LOCAL_BEARER_TOKEN", "lean-bearer-secret-test-32-bytes")
    monkeypatch.setenv("API_DISCORD_BOT_BEARER_TOKEN", "bot-bearer-secret-test-32-bytes")

    from services.api import config as api_config

    api_config.get_settings.cache_clear()
    main_mod = importlib.import_module("services.api.main")
    app = main_mod.create_app()

    from services.api import db as api_db

    settings = api_config.get_settings()
    await api_db.init_pool(settings)
    try:
        yield app
    finally:
        await api_db.close_pool()


@pytest_asyncio.fixture
async def client_dev(app_dev: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_dev)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def client_production(app_production: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_production)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_latest_per_market(
    client_dev: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two cycles → endpoint returns only the latest row per market."""
    earlier = datetime(2026, 5, 27, 21, 30, tzinfo=UTC)
    later = datetime(2026, 5, 28, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=earlier,
            session_date_et="2026-05-27",
            evaluations=[
                _evaluation("/MES", overall="fail", closest="efficiency"),
                _evaluation("/MNQ", overall="close"),
            ],
        )
        await insert_rows(
            session,
            cycle_ts_utc=later,
            session_date_et="2026-05-28",
            evaluations=[
                _evaluation("/MES", overall="pass", closest="trend"),
                _evaluation("/MNQ", overall="fail", closest="efficiency"),
            ],
        )
        await session.commit()

    resp = await client_dev.get("/api/signals/proximity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["as_of_cycle_ts_utc"].startswith("2026-05-28T21:30:00")
    by_market = {m["market"]: m for m in body["markets"]}
    assert by_market["/MES"]["overall_state"] == "pass"
    assert by_market["/MES"]["closest_gate"] == "trend"
    assert by_market["/MNQ"]["overall_state"] == "fail"
    assert by_market["/MNQ"]["closest_gate"] == "efficiency"


@pytest.mark.asyncio
async def test_endpoint_empty_state(
    client_dev: AsyncClient,
) -> None:
    """No rows → ``{as_of_cycle_ts_utc: null, markets: []}``."""
    resp = await client_dev.get("/api/signals/proximity")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"as_of_cycle_ts_utc": None, "markets": []}


@pytest.mark.asyncio
async def test_endpoint_response_shape_matches_design(
    client_dev: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per-gate sub-objects + Decimal-as-string match design §6.4."""
    cycle_ts = datetime(2026, 5, 28, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-28",
            evaluations=[_evaluation("/MES")],
        )
        await session.commit()

    resp = await client_dev.get("/api/signals/proximity")
    assert resp.status_code == 200
    body = resp.json()
    market = body["markets"][0]
    assert market["market"] == "/MES"
    assert market["overall_state"] == "close"
    assert market["closest_gate"] == "donchian"
    assert market["gate_status"] == "ok"
    assert market["session_date_et"] == "2026-05-28"
    # Decimal-as-string per A05.
    assert market["last_close"] == "5234.50"
    assert market["long_donchian"] == {"state": "close", "headroom": "0.008"}
    assert market["short_donchian"] == {"state": "fail", "headroom": "0.085"}
    assert market["long_trend"] == {"state": "pass", "headroom": "0.015"}
    assert market["short_trend"] == {"state": "fail", "headroom": "-0.015"}
    assert market["efficiency"] == {"state": "pass", "headroom": "0.23"}


@pytest.mark.asyncio
async def test_endpoint_warming_up_market_serializes_nulls(
    client_dev: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Warming-up rows surface as JSON null for every headroom + last_close."""
    sentinel = GateProximityItem(state="fail", headroom=None, detail="warming_up")
    warming = MarketEvaluationItem(
        market="/MGC",
        long_donchian=sentinel,
        short_donchian=sentinel,
        long_trend=sentinel,
        short_trend=sentinel,
        efficiency=sentinel,
        last_close=None,
        efficiency_ratio_value=None,
        efficiency_ratio_threshold=Decimal("0.20"),
        overall_state="fail",
        closest_gate="history",
        gate_status="warming_up",
    )
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=datetime(2026, 5, 28, 21, 30, tzinfo=UTC),
            session_date_et="2026-05-28",
            evaluations=[warming],
        )
        await session.commit()

    resp = await client_dev.get("/api/signals/proximity")
    body = resp.json()
    market = body["markets"][0]
    assert market["last_close"] is None
    assert market["gate_status"] == "warming_up"
    assert market["closest_gate"] == "history"
    assert market["long_donchian"]["headroom"] is None
    assert market["efficiency"]["headroom"] is None


# ---------------------------------------------------------------------------
# Auth — same gate as GET /api/signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_unauthenticated_in_production_returns_401(
    client_production: AsyncClient,
) -> None:
    """In ``live-small`` env, the Phase-0 stub session fails closed.

    The endpoint MUST enforce the same auth as ``GET /api/signals`` —
    which means relying on ``Depends(get_session_context)``. Without a
    real session middleware (Week 6+), the stub middleware returns 401
    AUTH_REQUIRED for any unauthenticated request in production envs.
    """
    resp = await client_production.get("/api/signals/proximity")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_get_signals_and_proximity_have_same_auth_gate(
    client_production: AsyncClient,
) -> None:
    """``GET /api/signals`` and ``GET /api/signals/proximity`` BOTH 401 in production.

    The carry-over watch-out from the design (§7 §9 PR-B "must enforce
    the same auth as ``GET /api/signals``"): the two endpoints share the
    Phase-0 stub-session fail-closed behavior. Any divergence between them
    is a regression — this test holds the line.
    """
    proximity_resp = await client_production.get("/api/signals/proximity")
    signals_resp = await client_production.get("/api/signals")
    assert proximity_resp.status_code == 401
    assert signals_resp.status_code == 401
    assert proximity_resp.json()["error_code"] == signals_resp.json()["error_code"]
