"""Integration tests for ``exit_proximity`` heartbeat persistence (PR-B).

PR-B of ``Docs/exit-proximity-design.md`` (SIGNED OFF 2026-05-30). Spins up a
``postgres:16`` testcontainer per module, runs ``alembic upgrade head``, and
exercises ``exit_proximity.insert_rows`` directly + the heartbeat-handler path
end-to-end via the FastAPI test client.

Coverage:

  * Direct repo path — ``insert_rows`` writes one row per open position with
    every column populated, including the api-derived ``held_days_headroom``
    (MIN_HOLDING_DAYS - held_days), the warming-up branch, and the
    no-working-stop branch (stop_price NULL + stop_state HOLDING).
  * **The Q1 = (B) stop-enrichment join** — seed a working ``stop_market``
    order, then assert ``insert_rows`` joins its ``stop_price``, re-classifies
    the stop dimension (a breached stop → TRIGGERED), and RE-DERIVES
    ``overall_state``/``closest_exit`` so a row LEAN sent as HOLDING/trend_flip
    flips to TRIGGERED/stop.
  * Heartbeat-handler path — POST a 2-position heartbeat → 2 rows.
  * Best-effort write contract — a DB hiccup mid-heartbeat still returns 202
    (does NOT 5xx the liveness signal).

If Docker is unreachable, the module skips cleanly.
"""

from __future__ import annotations

import importlib
import os
import uuid
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

from services.api.repos.exit_proximity import fetch_latest_per_market, insert_rows
from services.api.schemas.lean import ExitTriggerItem, PositionExitEvaluationItem

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]

_LEAN_BEARER = "lean-bearer-secret-test-32-bytes"
_STRATEGY_HASH = "0000000000000000000000000000000000000001"  # CHAR(40)
_PARAM_HASH = "0000000000000000000000000000000000000000000000000000000000000001"  # CHAR(64)


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
    """Clean exit_proximity between tests so row-count assertions are stable.

    The FK chain (orders/signals/accounts) is NOT truncated — only the
    stop-enrichment test seeds it, and it uses a market ('/MGC') no other test
    inserts a working stop for, so there is no cross-test contamination.
    """
    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE exit_proximity RESTART IDENTITY"))
    yield


# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------


def _trigger(state: str, headroom: str | None = None, detail: str | None = None) -> ExitTriggerItem:
    return ExitTriggerItem(
        state=state,  # type: ignore[arg-type]
        headroom=Decimal(headroom) if headroom is not None else None,
        detail=detail,
    )


def _exit_item(
    market: str = "/MES",
    *,
    direction: str = "long",
    held_days: int | None = 20,
    last_close: str | None = "100",
    trend_flip: tuple[str, str | None] = ("holding", "0.05"),
    gate_status: str = "active",
) -> PositionExitEvaluationItem:
    """Build a LEAN-emitted evaluation. ``stop`` is the Q1 = (B) NULL-state
    placeholder (LEAN does not know the working stop); ``overall_state`` /
    ``closest_exit`` are LEAN's stop-blind values the api will re-derive."""
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
        gate_status=gate_status,  # type: ignore[arg-type]
    )


def _wire_exit_eval(
    market: str = "/MES",
    *,
    direction: str = "long",
    held_days: int | None = 20,
    last_close: str | None = "100",
) -> dict[str, object]:
    """Wire-shape (JSON) exit evaluation — what LEAN actually POSTs."""
    return {
        "market": market,
        "direction": direction,
        "held_days": held_days,
        "trend_flip": {"state": "holding", "headroom": "0.05", "detail": None},
        "stop": {"state": "holding", "headroom": None, "detail": "no stop price on record"},
        "reversal": {"state": "holding", "headroom": None, "detail": None},
        "decommission": {"state": "holding", "headroom": None, "detail": None},
        "last_close": last_close,
        "stop_price": None,
        "overall_state": "holding",
        "closest_exit": "trend_flip",
        "gate_status": "active",
    }


def _heartbeat_body(
    *,
    exit_evaluations: list[dict[str, object]] | None,
    ts_utc: str = "2026-05-31T21:30:00+00:00",
) -> dict[str, object]:
    body: dict[str, object] = {
        "event_type": "lean_cycle_heartbeat",
        "ts_utc": ts_utc,
        "algorithm_id": "v1_trend_following",
        "session_date_et": "2026-05-31",
        "equity_usd": "100000.00",
        "live_mode": True,
        "signals_emitted_count": 0,
        "rejections_count": 0,
    }
    if exit_evaluations is not None:
        body["position_exit_evaluations"] = exit_evaluations
    return body


def _seed_working_stop(
    sync_engine: Engine,
    *,
    market: str,
    stop_price: Decimal,
    created_at: datetime,
) -> None:
    """Seed the FK chain for ONE working ``stop_market`` order so the repo's
    stop join has a row to find. Mirrors the seed pattern in
    ``test_replace_protective_stop_end_to_end.py``."""
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE slippage_calibration_versions SET is_head = FALSE WHERE is_head = TRUE")
        )
        next_ver = conn.execute(
            text("SELECT COALESCE(MAX(version_no), 0) + 1 FROM slippage_calibration_versions")
        ).scalar_one()
        slip_id = conn.execute(
            text(
                "INSERT INTO slippage_calibration_versions ("
                "  version_no, is_head, calibrated_at_utc, trigger,"
                "  per_market_coefficients, audit_event_uuid, notes"
                ") VALUES ("
                "  :ver, TRUE, now(), 'bootstrap', '{}'::jsonb, :audit, 'exit_proximity test'"
                ") RETURNING id"
            ),
            {"ver": next_ver, "audit": uuid.uuid4()},
        ).scalar_one()
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": f"EXITPROX_{uuid.uuid4().hex[:12]}"},
        ).scalar_one()
        signal_id = conn.execute(
            text(
                "INSERT INTO signals ("
                "  account_id, env, market, emitted_at_utc, session_date,"
                "  direction, signal_type, strategy_hash, parameter_set_hash,"
                "  slippage_calibration_version_id, decision_price, target_contracts,"
                "  sizing_trace, status, created_at"
                ") VALUES ("
                "  :acct, 'paper', :market, :ts, CURRENT_DATE,"
                "  'long', 'test_signal', :strat, :param,"
                "  :slip, 100.00, 1, '{}'::jsonb, 'filled', :ts"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "market": market,
                "ts": created_at,
                "strat": _STRATEGY_HASH,
                "param": _PARAM_HASH,
                "slip": slip_id,
            },
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO orders ("
                "  account_id, env, signal_id, client_order_id, market, direction,"
                "  order_type, quantity, stop_price, placed_at_utc, status,"
                "  retry_n, strategy_hash, parameter_set_hash, created_at"
                ") VALUES ("
                "  :acct, 'paper', :sig, :cid, :market, 'sell',"
                "  'stop_market', 1, :stop, :ts, 'working',"
                "  0, :strat, :param, :ts"
                ")"
            ),
            {
                "acct": account_id,
                "sig": signal_id,
                "cid": f"stop_{uuid.uuid4().hex[:12]}",
                "market": market,
                "stop": stop_price,
                "ts": created_at,
                "strat": _STRATEGY_HASH,
                "param": _PARAM_HASH,
            },
        )


# ---------------------------------------------------------------------------
# Direct repo tests — insert_rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_rows_two_positions_lands_two_rows(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """Two open positions → two rows, with the api-derived held_days_headroom."""
    cycle_ts = datetime(2026, 5, 31, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        n = await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-31",
            evaluations=[
                _exit_item("/MES", held_days=20),
                _exit_item("/MNQ", held_days=10),
            ],
        )
        await session.commit()
    assert n == 2

    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT market, direction, held_days, held_days_headroom, last_close, "
                "stop_price, trend_flip_state, trend_flip_headroom, stop_state, "
                "overall_state, closest_exit, gate_status "
                "FROM exit_proximity ORDER BY market"
            )
        ).fetchall()
    by_market = {r.market: r for r in rows}

    mes = by_market["/MES"]
    assert mes.direction == "long"
    assert mes.held_days == 20
    # MIN_HOLDING_DAYS (14, LOCKED) - 20 held = -6 (floor long cleared).
    assert mes.held_days_headroom == -6
    assert mes.last_close == Decimal("100")
    assert mes.trend_flip_state == "holding"
    assert mes.trend_flip_headroom == Decimal("0.05")
    # No working stop seeded for /MES → NULL-state stop.
    assert mes.stop_price is None
    assert mes.stop_state == "holding"
    assert mes.overall_state == "holding"
    assert mes.gate_status == "active"

    # /MNQ held 10 → 14 - 10 = 4 days left under the MIN_HOLDING floor.
    assert by_market["/MNQ"].held_days_headroom == 4


@pytest.mark.asyncio
async def test_insert_rows_no_working_stop_leaves_stop_null(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """With no working stop on record the stop dimension persists NULL/HOLDING."""
    cycle_ts = datetime(2026, 5, 31, 21, 30, tzinfo=UTC)
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-31",
            evaluations=[_exit_item("/MNQ")],
        )
        await session.commit()

    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT stop_price, stop_state, stop_headroom_pct FROM exit_proximity")
        ).one()
    assert row.stop_price is None
    assert row.stop_state == "holding"
    assert row.stop_headroom_pct is None


@pytest.mark.asyncio
async def test_insert_rows_warming_up_writes_nulls(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """A warming-up position (no snapshot, held_days unknown) lands NULL numerics."""
    cycle_ts = datetime(2026, 5, 31, 21, 30, tzinfo=UTC)
    warming = PositionExitEvaluationItem(
        market="/MGC",
        direction="long",
        held_days=None,
        trend_flip=_trigger("holding", None, "warming_up"),
        stop=_trigger("holding", None, "no stop price on record"),
        reversal=_trigger("holding"),
        decommission=_trigger("holding"),
        last_close=None,
        stop_price=None,
        overall_state="holding",
        closest_exit="trend_flip",
        gate_status="warming_up",
    )
    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-31",
            evaluations=[warming],
        )
        await session.commit()

    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT held_days, held_days_headroom, last_close, trend_flip_headroom, "
                "gate_status FROM exit_proximity"
            )
        ).one()
    assert row.held_days is None
    assert row.held_days_headroom is None  # None held_days → no headroom
    assert row.last_close is None
    assert row.trend_flip_headroom is None
    assert row.gate_status == "warming_up"


@pytest.mark.asyncio
async def test_insert_rows_enriches_stop_and_flips_overall(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """THE Q1 = (B) join: a seeded working stop below the mark flips the row.

    LEAN emits a NULL-state stop + an all-HOLDING summary (closest=trend_flip).
    The api joins the working stop (101) for a long marked at 100 — a breach —
    so the persisted row must show stop_price=101, stop_state=triggered, and a
    RE-DERIVED overall_state=triggered / closest_exit=stop.
    """
    cycle_ts = datetime(2026, 5, 31, 21, 30, tzinfo=UTC)
    _seed_working_stop(
        sync_engine,
        market="/MGC",
        stop_price=Decimal("101"),
        created_at=datetime(2026, 5, 31, 10, 0, tzinfo=UTC),
    )

    async with async_session_factory() as session:
        await insert_rows(
            session,
            cycle_ts_utc=cycle_ts,
            session_date_et="2026-05-31",
            # last_close 100 < stop 101 for a long → breached.
            evaluations=[_exit_item("/MGC", direction="long", last_close="100")],
        )
        await session.commit()

    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT stop_price, stop_state, stop_headroom_pct, overall_state, "
                "closest_exit FROM exit_proximity WHERE market = '/MGC'"
            )
        ).one()
    assert row.stop_price == Decimal("101")
    assert row.stop_state == "triggered"
    assert row.stop_headroom_pct == Decimal("-0.01")
    # Re-derived from the enriched stop — NOT LEAN's stop-blind 'holding'.
    assert row.overall_state == "triggered"
    assert row.closest_exit == "stop"


@pytest.mark.asyncio
async def test_insert_rows_empty_evaluations_is_noop(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
) -> None:
    """An empty evaluations list returns 0 + writes nothing."""
    async with async_session_factory() as session:
        n = await insert_rows(
            session,
            cycle_ts_utc=datetime(2026, 5, 31, 21, 30, tzinfo=UTC),
            session_date_et="2026-05-31",
            evaluations=[],
        )
        await session.commit()
    assert n == 0
    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM exit_proximity")).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_fetch_latest_per_market_empty_table_returns_none(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-first-cycle / no open positions: (None, []) for the empty state."""
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
async def test_heartbeat_two_positions_lands_two_rows(
    heartbeat_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    """POSTing a 2-position heartbeat lands 2 rows in exit_proximity."""
    resp = await heartbeat_client.post(
        "/api/internal/lean/signals",
        headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
        json=_heartbeat_body(exit_evaluations=[_wire_exit_eval("/MES"), _wire_exit_eval("/MNQ")]),
    )
    assert resp.status_code == 202

    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT market FROM exit_proximity ORDER BY market")).fetchall()
    assert sorted(r.market for r in rows) == ["/MES", "/MNQ"]


@pytest.mark.asyncio
async def test_heartbeat_without_exit_evaluations_writes_nothing(
    heartbeat_client: AsyncClient,
    sync_engine: Engine,
) -> None:
    """A legacy heartbeat (no position_exit_evaluations) still 202s; table empty."""
    resp = await heartbeat_client.post(
        "/api/internal/lean/signals",
        headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
        json=_heartbeat_body(exit_evaluations=None),
    )
    assert resp.status_code == 202
    with sync_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM exit_proximity")).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_heartbeat_db_failure_still_returns_202(
    heartbeat_client: AsyncClient,
) -> None:
    """A DB hiccup during exit-proximity persistence MUST NOT 5xx the heartbeat.

    Per design §6.3: the liveness signal outranks the proximity snapshot. This
    patches the route's ``insert_exit_proximity_rows`` to raise + asserts 202.
    """
    with patch(
        "services.api.routes.internal.lean.insert_exit_proximity_rows",
        side_effect=RuntimeError("simulated DB outage"),
    ):
        resp = await heartbeat_client.post(
            "/api/internal/lean/signals",
            headers={"Authorization": f"Bearer {_LEAN_BEARER}"},
            json=_heartbeat_body(exit_evaluations=[_wire_exit_eval("/MES")]),
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert body["event_type"] == "lean_cycle_heartbeat"
