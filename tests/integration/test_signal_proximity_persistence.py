"""Integration tests for ``signal_proximity`` heartbeat persistence (PR-B).

PR-B of ``Docs/signal-proximity-design.md`` (SIGNED OFF 2026-05-28).
Spins up a ``postgres:16`` testcontainer per module, runs
``alembic upgrade head``, and exercises ``insert_signal_proximity_rows``
directly + the heartbeat-handler path end-to-end via the FastAPI
test client.

Coverage:

  * Direct repo path — ``insert_rows`` writes one row per market with
    every column populated correctly, including the warming-up branch
    (state='fail', headroom=None, gate_status='warming_up').
  * Heartbeat-handler path — POST a heartbeat with 10 evaluations →
    10 rows land in ``signal_proximity``. Two consecutive heartbeats
    → 20 rows total (no UPSERT-clobbering — the design retains every
    row).
  * Best-effort write contract — when the DB is unavailable mid-
    heartbeat, the endpoint still returns 202 (does NOT 5xx the
    liveness signal). This is the carry-over watch-out from PR-A's
    risk-review.

If Docker is unreachable, the module skips cleanly (matches
``test_signal_dispatch_end_to_end.py``).
"""

from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

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

from services.api.repos.signal_proximity import (
    fetch_latest_per_market,
    insert_rows,
)
from services.api.schemas.lean import (
    GateProximityItem,
    MarketEvaluationItem,
)

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]

_LEAN_BEARER = "lean-bearer-secret-test-32-bytes"


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
    """Clean signal_proximity between tests so row-count assertions are stable."""
    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE signal_proximity RESTART IDENTITY"))
    yield


# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------


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
    last_close: str | None = "5234.50",
) -> MarketEvaluationItem:
    """Build a representative MarketEvaluationItem for /MES-style values."""
    return MarketEvaluationItem(
        market=market,
        long_donchian=_gate("close", "0.008"),
        short_donchian=_gate("fail", "0.085"),
        long_trend=_gate("pass", "0.015"),
        short_trend=_gate("fail", "-0.015"),
        hurst=_gate("pass", "0.03"),
        last_close=Decimal(last_close) if last_close is not None else None,
        overall_state=overall,  # type: ignore[arg-type]
        closest_gate=closest,  # type: ignore[arg-type]
        gate_status=status,  # type: ignore[arg-type]
    )


def _warming_up_evaluation(market: str) -> MarketEvaluationItem:
    """Build the canonical warming-up shape — every gate FAIL, no numerics."""
    sentinel = GateProximityItem(state="fail", headroom=None, detail="warming_up")
    return MarketEvaluationItem(
        market=market,
        long_donchian=sentinel,
        short_donchian=sentinel,
        long_trend=sentinel,
        short_trend=sentinel,
        hurst=sentinel,
        last_close=None,
        overall_state="fail",
        closest_gate="history",
        gate_status="warming_up",
    )


def _heartbeat_body(
    *,
    evaluations: list[dict[str, object]] | None,
    ts_utc: str = "2026-05-28T21:30:00+00:00",
) -> dict[str, object]:
    """Wire-shape heartbeat body — what LEAN actually POSTs."""
    body: dict[str, object] = {
        "event_type": "lean_cycle_heartbeat",
        "ts_utc": ts_utc,
        "algorithm_id": "v1_trend_following",
        "session_date_et": "2026-05-28",
        "equity_usd": "100000.00",
        "live_mode": True,
        "signals_emitted_count": 0,
        "rejections_count": 0,
    }
    if evaluations is not None:
        body["market_evaluations"] = evaluations
    return body


def _wire_evaluation(market: str = "/MES") -> dict[str, object]:
    return {
        "market": market,
        "long_donchian": {"state": "close", "headroom": "0.008", "detail": None},
        "short_donchian": {"state": "fail", "headroom": "0.085", "detail": None},
        "long_trend": {"state": "pass", "headroom": "0.015", "detail": None},
        "short_trend": {"state": "fail", "headroom": "-0.015", "detail": None},
        "hurst": {"state": "pass", "headroom": "0.03", "detail": None},
        "last_close": "5234.50",
        "overall_state": "close",
        "closest_gate": "donchian",
        "gate_status": "ok",
    }


# ---------------------------------------------------------------------------
# Direct repo tests — insert_rows + fetch_latest_per_market
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_rows_writes_every_column(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """Single-market insert — every column matches what was supplied."""
    cycle_ts = datetime(2026, 5, 28, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        n = await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-28",
            evaluations=[_evaluation("/MES")],
        )
        await session.commit()
    assert n == 1

    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT cycle_ts_utc, session_date_et, market, last_close, "
                "long_donchian_state, long_donchian_headroom_pct, "
                "short_donchian_state, short_donchian_headroom_pct, "
                "long_trend_state, long_trend_gap_pct, "
                "short_trend_state, short_trend_gap_pct, "
                "hurst_state, hurst_headroom, "
                "overall_state, closest_gate, gate_status "
                "FROM signal_proximity"
            )
        ).one()

    assert row.market == "/MES"
    assert row.session_date_et.isoformat() == "2026-05-28"
    assert row.cycle_ts_utc.astimezone(UTC) == cycle_ts
    assert row.last_close == Decimal("5234.50")
    assert row.long_donchian_state == "close"
    assert row.long_donchian_headroom_pct == Decimal("0.008")
    assert row.short_donchian_state == "fail"
    assert row.short_donchian_headroom_pct == Decimal("0.085")
    assert row.long_trend_state == "pass"
    assert row.long_trend_gap_pct == Decimal("0.015")
    assert row.short_trend_state == "fail"
    assert row.short_trend_gap_pct == Decimal("-0.015")
    assert row.hurst_state == "pass"
    assert row.hurst_headroom == Decimal("0.03")
    assert row.overall_state == "close"
    assert row.closest_gate == "donchian"
    assert row.gate_status == "ok"


@pytest.mark.asyncio
async def test_insert_rows_warming_up_writes_nulls(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """Warming-up evaluations land with NULL numerics + gate_status='warming_up'."""
    cycle_ts = datetime(2026, 5, 28, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-28",
            evaluations=[_warming_up_evaluation("/MGC")],
        )
        await session.commit()

    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT last_close, long_donchian_headroom_pct, hurst_headroom, "
                "long_donchian_state, gate_status, closest_gate "
                "FROM signal_proximity"
            )
        ).one()
    assert row.last_close is None
    assert row.long_donchian_headroom_pct is None
    assert row.hurst_headroom is None
    assert row.long_donchian_state == "fail"
    assert row.gate_status == "warming_up"
    assert row.closest_gate == "history"


@pytest.mark.asyncio
async def test_insert_rows_batch_inserts_all_markets(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """A 10-market heartbeat lands 10 rows in one call."""
    cycle_ts = datetime(2026, 5, 28, 21, 30, tzinfo=UTC)
    markets = [f"/M{i:02d}" for i in range(10)]
    async with async_session_factory() as session:
        n = await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-28",
            evaluations=[_evaluation(m) for m in markets],
        )
        await session.commit()
    assert n == 10

    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM signal_proximity")).scalar_one()
    assert count == 10


@pytest.mark.asyncio
async def test_insert_rows_empty_evaluations_is_noop(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """An empty evaluations list returns 0 + writes nothing."""
    cycle_ts = datetime(2026, 5, 28, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        n = await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-28",
            evaluations=[],
        )
        await session.commit()
    assert n == 0
    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM signal_proximity")).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_fetch_latest_per_market_returns_most_recent_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two cycles → latest per market wins; as_of = MAX(cycle_ts_utc)."""
    earlier = datetime(2026, 5, 27, 21, 30, tzinfo=UTC)
    later = datetime(2026, 5, 28, 21, 30, tzinfo=UTC)

    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=earlier,
            session_date_et="2026-05-27",
            evaluations=[
                _evaluation("/MES", overall="fail", closest="hurst"),
                _evaluation("/MNQ", overall="close"),
            ],
        )
        await insert_rows(
            session,
            cycle_ts_utc=later,
            session_date_et="2026-05-28",
            evaluations=[
                _evaluation("/MES", overall="pass", closest="trend"),
                _evaluation("/MNQ", overall="fail", closest="hurst"),
            ],
        )
        await session.commit()

    async with async_session_factory() as read_session:
        as_of, rows = await fetch_latest_per_market(read_session)

    assert as_of == later
    by_market = {r.market: r for r in rows}
    assert by_market["/MES"].overall_state == "pass"
    assert by_market["/MES"].closest_gate == "trend"
    assert by_market["/MES"].cycle_ts_utc == later
    assert by_market["/MNQ"].overall_state == "fail"
    assert by_market["/MNQ"].cycle_ts_utc == later


@pytest.mark.asyncio
async def test_fetch_latest_per_market_empty_table_returns_none(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-first-cycle: (None, []) so the endpoint can render the empty state."""
    async with async_session_factory() as session:
        as_of, rows = await fetch_latest_per_market(session)
    assert as_of is None
    assert rows == []


# ---------------------------------------------------------------------------
# Heartbeat-handler tests — full POST through the FastAPI app
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def heartbeat_app(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build a FastAPI app wired to the testcontainer DB + LEAN bearer.

    Using the real ``create_app`` flow exercises the full middleware
    chain (LeanAuth → CSRF bypass → route) so the heartbeat path is
    tested end-to-end including the persistence side effect.
    """
    async_url = pg_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
    monkeypatch.setenv("API_DATABASE_URL", async_url)
    monkeypatch.setenv("API_ENVIRONMENT", "dev")
    monkeypatch.setenv("API_VERSION", "test")
    monkeypatch.setenv("API_LOG_LEVEL", "INFO")
    monkeypatch.setenv("API_LEAN_LOCAL_BEARER_TOKEN", _LEAN_BEARER)
    monkeypatch.setenv("API_DISCORD_BOT_BEARER_TOKEN", "bot-bearer-secret-test-32-bytes")

    from services.api import config as api_config

    api_config.get_settings.cache_clear()
    main_mod = importlib.import_module("services.api.main")
    app = main_mod.create_app()

    # Force the api's db pool to use the testcontainer URL (the lifespan
    # hook reads settings; we initialize explicitly to bypass the
    # app's startup machinery which the TestClient may skip).
    from services.api import db as api_db

    settings = api_config.get_settings()
    await api_db.init_pool(settings)
    try:
        yield app
    finally:
        await api_db.close_pool()


@pytest_asyncio.fixture
async def heartbeat_client(heartbeat_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=heartbeat_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_heartbeat_with_10_evaluations_lands_10_rows(
    heartbeat_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    """POSTing a 10-market heartbeat lands 10 rows in signal_proximity."""
    markets = [f"/M{i:02d}" for i in range(10)]
    evaluations = [_wire_evaluation(m) for m in markets]
    resp = await heartbeat_client.post(
        "/api/internal/lean/signals",
        headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
        json=_heartbeat_body(evaluations=evaluations),
    )
    assert resp.status_code == 202

    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT market FROM signal_proximity ORDER BY market")).fetchall()
    assert sorted(r.market for r in rows) == sorted(markets)


@pytest.mark.asyncio
async def test_two_consecutive_heartbeats_keep_both_row_sets(
    heartbeat_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    """Two cycles → 20 rows total — no UPSERT-clobber, history is preserved."""
    evaluations = [_wire_evaluation("/MES"), _wire_evaluation("/MNQ")]

    resp1 = await heartbeat_client.post(
        "/api/internal/lean/signals",
        headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
        json=_heartbeat_body(evaluations=evaluations, ts_utc="2026-05-27T21:30:00+00:00"),
    )
    assert resp1.status_code == 202

    resp2 = await heartbeat_client.post(
        "/api/internal/lean/signals",
        headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
        json=_heartbeat_body(evaluations=evaluations, ts_utc="2026-05-28T21:30:00+00:00"),
    )
    assert resp2.status_code == 202

    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM signal_proximity")).scalar_one()
    assert count == 4


@pytest.mark.asyncio
async def test_heartbeat_without_market_evaluations_writes_nothing(
    heartbeat_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    """A legacy heartbeat (no market_evaluations) still 202s; table stays empty."""
    resp = await heartbeat_client.post(
        "/api/internal/lean/signals",
        headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
        json=_heartbeat_body(evaluations=None),
    )
    assert resp.status_code == 202

    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM signal_proximity")).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_heartbeat_db_failure_still_returns_202(
    heartbeat_client: AsyncClient,
) -> None:
    """A DB hiccup during proximity persistence MUST NOT 5xx the heartbeat.

    Per design §6.3 + the PR-A risk-review carry-over watch-out: the
    liveness signal is more important than the proximity snapshot. A
    missed snapshot is backfilled by the next cycle; a 5xx would spuriously
    trip the heartbeat watchdog. This test patches the repo's
    ``insert_rows`` to raise + asserts the response is still 202.
    """
    boom = RuntimeError("simulated DB outage")
    with patch(
        "services.api.routes.internal.lean.insert_signal_proximity_rows",
        side_effect=boom,
    ):
        resp = await heartbeat_client.post(
            "/api/internal/lean/signals",
            headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
            json=_heartbeat_body(evaluations=[_wire_evaluation("/MES")]),
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert body["event_type"] == "lean_cycle_heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_warming_up_market_lands_nulls(
    heartbeat_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    """An end-to-end warming-up evaluation persists with the canonical NULL shape."""
    warming_up_wire = {
        "market": "/MGC",
        "long_donchian": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "short_donchian": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "long_trend": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "short_trend": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "hurst": {"state": "fail", "headroom": None, "detail": "warming_up"},
        "last_close": None,
        "overall_state": "fail",
        "closest_gate": "history",
        "gate_status": "warming_up",
    }
    resp = await heartbeat_client.post(
        "/api/internal/lean/signals",
        headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
        json=_heartbeat_body(evaluations=[warming_up_wire]),
    )
    assert resp.status_code == 202

    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT last_close, hurst_headroom, gate_status, closest_gate FROM signal_proximity"
            )
        ).one()
    assert row.last_close is None
    assert row.hurst_headroom is None
    assert row.gate_status == "warming_up"
    assert row.closest_gate == "history"
