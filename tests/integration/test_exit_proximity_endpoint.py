"""Integration tests for ``GET /api/today/exit-proximity`` (PR-B).

PR-B of ``Docs/exit-proximity-design.md`` (SIGNED OFF 2026-05-30). Verifies the
endpoint:

  1. Returns latest-per-position rows.
  2. Orders rows closest-to-closing — TRIGGERED first, then NEAR ascending by
     headroom, then HOLDING (design §3.3).
  3. Renders the empty state (``{as_of_cycle_ts_utc: null, positions: []}``).
  4. Enforces the same auth as the other Today endpoints (``Depends(
     get_session_context)`` fails closed in ``live-small``).
  5. Flattens repo rows into the wire shape (per-trigger sub-objects,
     Decimal-as-string per A05).

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

from services.api.repos.exit_proximity import insert_rows
from services.api.schemas.lean import ExitTriggerItem, PositionExitEvaluationItem

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
        conn.execute(text("TRUNCATE TABLE exit_proximity RESTART IDENTITY"))
    yield


def _trigger(state: str, headroom: str | None = None, detail: str | None = None) -> ExitTriggerItem:
    return ExitTriggerItem(
        state=state,  # type: ignore[arg-type]
        headroom=Decimal(headroom) if headroom is not None else None,
        detail=detail,
    )


def _exit_item(
    market: str,
    *,
    direction: str = "long",
    held_days: int | None = 20,
    last_close: str | None = "100",
    trend_flip: tuple[str, str | None] = ("holding", "0.05"),
) -> PositionExitEvaluationItem:
    """Build a LEAN evaluation; no stop is ever seeded here, so the overall
    state is driven by ``trend_flip`` (the stop dimension is NULL-state HOLDING).
    """
    return PositionExitEvaluationItem(
        market=market,
        direction=direction,  # type: ignore[arg-type]
        held_days=held_days,
        trend_flip=_trigger(trend_flip[0], trend_flip[1]),
        stop=_trigger("holding", None, "no stop price on record"),
        reversal=_trigger("holding"),
        decommission=_trigger("holding"),
        last_close=Decimal(last_close) if last_close is not None else None,
        stop_price=None,
        overall_state="holding",  # type: ignore[arg-type]
        closest_exit="trend_flip",  # type: ignore[arg-type]
        gate_status="active",
    )


def _make_app(monkeypatch: pytest.MonkeyPatch, pg_url: str, *, environment: str) -> FastAPI:
    async_url = pg_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
    monkeypatch.setenv("API_DATABASE_URL", async_url)
    monkeypatch.setenv("API_ENVIRONMENT", environment)
    monkeypatch.setenv("API_VERSION", "test")
    monkeypatch.setenv("API_LOG_LEVEL", "INFO")
    monkeypatch.setenv("API_DISCORD_BOT_BEARER_TOKEN", "bot-bearer-secret-test-32-bytes")

    from services.api import config as api_config

    api_config.get_settings.cache_clear()
    main_mod = importlib.import_module("services.api.main")
    return main_mod.create_app()  # type: ignore[no-any-return]


@pytest_asyncio.fixture
async def app_dev(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    """``dev`` env — Phase-0 stub session injects (requests succeed)."""
    app = _make_app(monkeypatch, pg_url, environment="dev")
    from services.api import config as api_config
    from services.api import db as api_db

    await api_db.init_pool(api_config.get_settings())
    try:
        yield app
    finally:
        await api_db.close_pool()


@pytest_asyncio.fixture
async def app_production(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    """``live-small`` env — Phase-0 stub session FAILS CLOSED."""
    app = _make_app(monkeypatch, pg_url, environment="live-small")
    from services.api import config as api_config
    from services.api import db as api_db

    await api_db.init_pool(api_config.get_settings())
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
async def test_endpoint_returns_latest_per_position(
    client_dev: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two cycles → endpoint returns only the latest row per market."""
    earlier = datetime(2026, 5, 30, 21, 30, tzinfo=UTC)
    later = datetime(2026, 5, 31, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=earlier,
            session_date_et="2026-05-30",
            evaluations=[_exit_item("/MES", trend_flip=("holding", "0.05"))],
        )
        await insert_rows(
            session,
            cycle_ts_utc=later,
            session_date_et="2026-05-31",
            evaluations=[_exit_item("/MES", trend_flip=("near", "0.003"))],
        )
        await session.commit()

    resp = await client_dev.get("/api/today/exit-proximity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["as_of_cycle_ts_utc"].startswith("2026-05-31T21:30:00")
    assert len(body["positions"]) == 1
    pos = body["positions"][0]
    assert pos["market"] == "/MES"
    assert pos["overall_state"] == "near"  # the later cycle's value
    assert pos["trend_flip"] == {"state": "near", "headroom": "0.003"}


@pytest.mark.asyncio
async def test_endpoint_orders_closest_to_closing(
    client_dev: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """TRIGGERED first, then NEAR ascending by headroom, then HOLDING (§3.3)."""
    cycle_ts = datetime(2026, 5, 31, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-31",
            evaluations=[
                _exit_item("/M2K", trend_flip=("holding", "0.05")),  # HOLDING
                _exit_item("/MYM", trend_flip=("near", "0.004")),  # NEAR, farther
                _exit_item("/MES", trend_flip=("triggered", "-0.01")),  # TRIGGERED
                _exit_item("/MNQ", trend_flip=("near", "0.002")),  # NEAR, closer
            ],
        )
        await session.commit()

    resp = await client_dev.get("/api/today/exit-proximity")
    assert resp.status_code == 200
    order = [p["market"] for p in resp.json()["positions"]]
    assert order == ["/MES", "/MNQ", "/MYM", "/M2K"]


@pytest.mark.asyncio
async def test_endpoint_empty_state(client_dev: AsyncClient) -> None:
    """No rows → ``{as_of_cycle_ts_utc: null, positions: []}``."""
    resp = await client_dev.get("/api/today/exit-proximity")
    assert resp.status_code == 200
    assert resp.json() == {"as_of_cycle_ts_utc": None, "positions": []}


@pytest.mark.asyncio
async def test_endpoint_response_shape_matches_design(
    client_dev: AsyncClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per-trigger sub-objects + Decimal-as-string + derived held_days_headroom."""
    cycle_ts = datetime(2026, 5, 31, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-31",
            evaluations=[_exit_item("/MES", held_days=20, last_close="100")],
        )
        await session.commit()

    resp = await client_dev.get("/api/today/exit-proximity")
    assert resp.status_code == 200
    pos = resp.json()["positions"][0]
    assert pos["market"] == "/MES"
    assert pos["direction"] == "long"
    assert pos["held_days"] == 20
    assert pos["held_days_headroom"] == -6  # 14 (MIN_HOLDING, locked) - 20
    assert pos["session_date_et"] == "2026-05-31"
    assert pos["last_close"] == "100"  # Decimal-as-string per A05
    assert pos["stop_price"] is None  # no working stop on record
    assert pos["trend_flip"] == {"state": "holding", "headroom": "0.05"}
    assert pos["stop"] == {"state": "holding", "headroom": None}
    assert pos["reversal"] == {"state": "holding", "headroom": None}
    assert pos["decommission"] == {"state": "holding", "headroom": None}
    assert pos["overall_state"] == "holding"
    assert pos["closest_exit"] == "trend_flip"
    assert pos["gate_status"] == "active"


# ---------------------------------------------------------------------------
# Auth — same gate as the other Today endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_unauthenticated_in_production_returns_401(
    client_production: AsyncClient,
) -> None:
    """In ``live-small`` env the Phase-0 stub session fails closed."""
    resp = await client_production.get("/api/today/exit-proximity")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_exit_proximity_and_health_score_share_auth_gate(
    client_production: AsyncClient,
) -> None:
    """``/api/today/exit-proximity`` + ``/api/health-score`` BOTH 401 in production.

    The endpoint must enforce the same ``Depends(get_session_context)`` gate as
    the other Today endpoints; any divergence is a regression.
    """
    exit_resp = await client_production.get("/api/today/exit-proximity")
    health_resp = await client_production.get("/api/health-score")
    assert exit_resp.status_code == 401
    assert health_resp.status_code == 401
    assert exit_resp.json()["error_code"] == health_resp.json()["error_code"]
