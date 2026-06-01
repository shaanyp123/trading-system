"""Integration tests for ``GET /api/internal/lean/positions`` + the server-side
exit-dedup guard (PR-A2), against a real ``postgres:16`` testcontainer.

These exercise the parts only a real DB can verify:

  * the endpoint's real SQL — ``positions_current`` LEFT JOIN the open ``trades``
    row for ``opened_at_utc`` → ET session date, and the ``exits_in_flight`` query
  * the duplicate-exit round trip — POSTing an exit for a market that already has
    a non-terminal exit hits the real ``_exit_in_flight_exists`` SQL and the route
    maps ``DuplicateInFlightExitError`` → a benign accepted=False success (no
    second signals row, no audit row)

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

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, text

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]

_LEAN_BEARER = "lean-bearer-secret-test-32-bytes"
_AUTH = {"Authorization": f"Bearer {_LEAN_BEARER}"}
_POSITIONS_PATH = "/api/internal/lean/positions"
_SIGNALS_PATH = "/api/internal/lean/signals"
_STRATEGY_HASH = "0000000000000000000000000000000000000001"  # CHAR(40)
_PARAM_HASH = "0000000000000000000000000000000000000000000000000000000000000001"  # CHAR(64)
_MANAGED_BY = "v1_trend_following@abc1234".ljust(40, "0")[:40]


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
        prev = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = sync_url
        try:
            from alembic.config import Config

            from alembic import command

            cfg = Config(str(REPO_ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
            command.upgrade(cfg, "head")
        finally:
            if prev is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev
        yield sync_url


@pytest.fixture(scope="module")
def sync_engine(pg_url: str) -> Iterator[Engine]:
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _isolate_accounts(sync_engine: Engine) -> Iterator[None]:
    """Deactivate any prior account so ``fetch_active_account_id`` (active_to IS
    NULL) only ever resolves the account THIS test seeds.

    We cannot TRUNCATE — ``audit_log`` has an immutability trigger that blocks
    TRUNCATE (dev-guide §7.4), and a CASCADE from ``accounts`` would hit it.
    Deactivating instead leaves prior rows in place (belonging to now-inactive
    accounts); every assertion below scopes to the current test's account_id.
    """
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE accounts SET active_to = now() WHERE active_to IS NULL"))
    yield


@pytest_asyncio.fixture
async def app(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
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
    application = main_mod.create_app()

    from services.api import db as api_db

    await api_db.init_pool(api_config.get_settings())
    try:
        yield application
    finally:
        await api_db.close_pool()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_slippage_head(conn: Any) -> uuid.UUID:
    conn.execute(
        text("UPDATE slippage_calibration_versions SET is_head = FALSE WHERE is_head = TRUE")
    )
    ver = conn.execute(
        text("SELECT COALESCE(MAX(version_no), 0) + 1 FROM slippage_calibration_versions")
    ).scalar_one()
    return conn.execute(
        text(
            "INSERT INTO slippage_calibration_versions ("
            "  version_no, is_head, calibrated_at_utc, trigger, per_market_coefficients,"
            "  audit_event_uuid, notes"
            ") VALUES (:ver, TRUE, now(), 'bootstrap', '{}'::jsonb, :audit, 'pr-a2 test') RETURNING id"
        ),
        {"ver": ver, "audit": uuid.uuid4()},
    ).scalar_one()


def _seed_account(conn: Any) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO accounts (external_account_id, account_type, active_from) "
            "VALUES (:ext, 'individual', now()) RETURNING id"
        ),
        {"ext": f"PRA2_{uuid.uuid4().hex[:12]}"},
    ).scalar_one()


def _seed_signal(
    conn: Any,
    *,
    account_id: uuid.UUID,
    slip: uuid.UUID,
    market: str,
    signal_type: str,
    direction: str,
    status: str,
) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO signals ("
            "  account_id, env, market, emitted_at_utc, session_date, direction, signal_type,"
            "  strategy_hash, parameter_set_hash, slippage_calibration_version_id,"
            "  decision_price, target_contracts, sizing_trace, status, created_at"
            ") VALUES ("
            "  :acct, 'paper', :market, now(), CURRENT_DATE, :dir, :stype,"
            "  :strat, :param, :slip, 100.00, 1, '{}'::jsonb, :status, now()"
            ") RETURNING id"
        ),
        {
            "acct": account_id,
            "market": market,
            "dir": direction,
            "stype": signal_type,
            "strat": _STRATEGY_HASH,
            "param": _PARAM_HASH,
            "slip": slip,
            "status": status,
        },
    ).scalar_one()


def _seed_held_position(
    conn: Any,
    *,
    account_id: uuid.UUID,
    slip: uuid.UUID,
    market: str,
    quantity: int,
    avg_cost: str,
    opened_at_utc: datetime,
) -> None:
    """positions_current row + the open trade that carries the open date."""
    entry_signal = _seed_signal(
        conn,
        account_id=account_id,
        slip=slip,
        market=market,
        signal_type="donchian_breakout",
        direction="long" if quantity > 0 else "short",
        status="filled",
    )
    conn.execute(
        text(
            "INSERT INTO trades ("
            "  account_id, env, market, entry_signal_id, entry_order_id, direction,"
            "  opened_at_utc, total_quantity, avg_entry_price, state, managed_by_version,"
            "  strategy_hash, parameter_set_hash, slippage_calibration_version_id, created_at"
            ") VALUES ("
            "  :acct, 'paper', :market, :sig, :ord, :dir, :opened, :qty, :avg,"
            "  'open_position', :mbv, :strat, :param, :slip, now()"
            ")"
        ),
        {
            "acct": account_id,
            "market": market,
            "sig": entry_signal,
            "ord": uuid.uuid4(),
            "dir": "long" if quantity > 0 else "short",
            "opened": opened_at_utc,
            "qty": abs(quantity),
            "avg": Decimal(avg_cost),
            "mbv": _MANAGED_BY,
            "strat": _STRATEGY_HASH,
            "param": _PARAM_HASH,
            "slip": slip,
        },
    )
    conn.execute(
        text(
            "INSERT INTO positions_current ("
            "  account_id, market, quantity, avg_cost, margin_held, last_mark_ts, managed_by_version"
            ") VALUES (:acct, :market, :qty, :avg, 0, now(), :mbv)"
        ),
        {
            "acct": account_id,
            "market": market,
            "qty": quantity,
            "avg": Decimal(avg_cost),
            "mbv": _MANAGED_BY,
        },
    )


def _exit_post_body(market: str = "/MES") -> dict[str, object]:
    return {
        "event_type": "signal_emitted",
        "ts_utc": "2026-05-31T21:30:00.000+00:00",
        "algorithm_id": "v1_trend_following",
        "session_date_et": "2026-05-31",
        "equity_usd": "100000.00",
        "live_mode": True,
        "market": market,
        "direction": "flat",
        "target_contracts": 0,
        "decision_price": "5234.75",
        "sizing_trace": {"schema_version": 1},
        "strategy_version": "v1_trend_following@abc1234",
        "signal_type": "exit",
        "exit_reason": "trend_flip",
        "prior_position_direction": "long",
        "prior_position_quantity": 3,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_active_account_returns_empty(client: AsyncClient) -> None:
    resp = await client.get(_POSITIONS_PATH, headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["positions"] == []
    assert body["exits_in_flight"] == []


@pytest.mark.asyncio
async def test_endpoint_maps_position_and_inflight_exit(
    client: AsyncClient, sync_engine: Engine
) -> None:
    with sync_engine.begin() as conn:
        slip = _seed_slippage_head(conn)
        account_id = _seed_account(conn)
        # Opened 2026-05-20 14:00Z → 2026-05-20 10:00 EDT → session date 2026-05-20.
        _seed_held_position(
            conn,
            account_id=account_id,
            slip=slip,
            market="/MES",
            quantity=3,
            avg_cost="5123.25",
            opened_at_utc=datetime(2026, 5, 20, 14, 0, tzinfo=UTC),
        )
        # An in-flight (pending) exit for /MES → should appear in exits_in_flight.
        _seed_signal(
            conn,
            account_id=account_id,
            slip=slip,
            market="/MES",
            signal_type="exit",
            direction="flat",
            status="pending",
        )

    resp = await client.get(_POSITIONS_PATH, headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["positions"]) == 1
    pos = body["positions"][0]
    assert pos["market"] == "/MES"
    assert pos["quantity"] == 3
    assert pos["avg_cost"] == "5123.25000000"  # NUMERIC(20,8) string form
    assert pos["opened_at_session_date"] == "2026-05-20"
    assert body["exits_in_flight"] == ["/MES"]


@pytest.mark.asyncio
async def test_duplicate_exit_post_is_suppressed(client: AsyncClient, sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        slip = _seed_slippage_head(conn)
        account_id = _seed_account(conn)
        # A non-terminal exit already in flight for /MES.
        _seed_signal(
            conn,
            account_id=account_id,
            slip=slip,
            market="/MES",
            signal_type="exit",
            direction="flat",
            status="pending",
        )
    with sync_engine.connect() as conn:
        audit_before = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one()

    resp = await client.post(_SIGNALS_PATH, headers=_AUTH, json=_exit_post_body("/MES"))
    # The server-side guard rejects the duplicate → benign success (the endpoint's
    # 202), accepted=False — NOT a 422. The LEAN client treats any <400 as success.
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is False
    assert body["signal_id"] is None
    assert "in flight" in body["note"]

    with sync_engine.connect() as conn:
        exit_rows = conn.execute(
            text(
                "SELECT COUNT(*) FROM signals WHERE account_id = :acct "
                "AND market = '/MES' AND signal_type = 'exit'"
            ),
            {"acct": account_id},
        ).scalar_one()
        audit_after = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one()
    assert exit_rows == 1  # the POST inserted NO second exit row
    assert audit_after == audit_before  # the duplicate wrote NO audit row (guard runs first)


@pytest.mark.asyncio
async def test_first_exit_for_market_is_accepted(client: AsyncClient, sync_engine: Engine) -> None:
    """Sanity: with NO in-flight exit, the same POST is accepted + lands a row."""
    with sync_engine.begin() as conn:
        _seed_slippage_head(conn)
        account_id = _seed_account(conn)

    resp = await client.post(_SIGNALS_PATH, headers=_AUTH, json=_exit_post_body("/MES"))
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert body["signal_id"] is not None

    with sync_engine.connect() as conn:
        exit_rows = conn.execute(
            text(
                "SELECT COUNT(*) FROM signals WHERE account_id = :acct "
                "AND market = '/MES' AND signal_type = 'exit'"
            ),
            {"acct": account_id},
        ).scalar_one()
    assert exit_rows == 1


def _seed_raw_position(
    conn: Any, *, account_id: uuid.UUID, market: str, quantity: int, avg_cost: str
) -> None:
    """A bare positions_current row (no trade/contract) — for the multi-contract
    aggregation test. contract_id stays NULL (NULLs are distinct in the
    UNIQUE(account_id, market, contract_id) index, so two NULL-contract rows for
    the same market coexist — the same shape a roll produces)."""
    conn.execute(
        text(
            "INSERT INTO positions_current ("
            "  account_id, market, quantity, avg_cost, margin_held, last_mark_ts, managed_by_version"
            ") VALUES (:acct, :market, :qty, :avg, 0, now(), :mbv)"
        ),
        {
            "acct": account_id,
            "market": market,
            "qty": quantity,
            "avg": Decimal(avg_cost),
            "mbv": _MANAGED_BY,
        },
    )


@pytest.mark.asyncio
async def test_completed_exit_does_not_block_reentry(
    client: AsyncClient, sync_engine: Engine
) -> None:
    """A RESOLVED exit (closed/filled/stopped_out) must NOT keep its market in
    ``exits_in_flight`` or suppress a fresh exit on a RE-OPENED position. Regresses
    the bug where the non-session-scoped filter blocked re-exits forever."""
    with sync_engine.begin() as conn:
        slip = _seed_slippage_head(conn)
        account_id = _seed_account(conn)
        # The prior position's exit already COMPLETED.
        _seed_signal(
            conn,
            account_id=account_id,
            slip=slip,
            market="/MES",
            signal_type="exit",
            direction="flat",
            status="closed",
        )
        # A freshly re-opened /MES position (entry + trade + positions_current).
        _seed_held_position(
            conn,
            account_id=account_id,
            slip=slip,
            market="/MES",
            quantity=2,
            avg_cost="5200.00",
            opened_at_utc=datetime(2026, 5, 30, 14, 0, tzinfo=UTC),
        )

    # GET: the completed exit is NOT in flight → /MES is exit-eligible again.
    resp = await client.get(_POSITIONS_PATH, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["exits_in_flight"] == []

    # POST: a fresh exit for the re-opened position is ACCEPTED (not suppressed).
    resp = await client.post(_SIGNALS_PATH, headers=_AUTH, json=_exit_post_body("/MES"))
    assert resp.status_code == 202
    assert resp.json()["accepted"] is True
    assert resp.json()["signal_id"] is not None


@pytest.mark.asyncio
async def test_multi_contract_market_is_aggregated(
    client: AsyncClient, sync_engine: Engine
) -> None:
    """Two positions_current rows for one market (a roll) net to ONE Position with
    the summed quantity — mirrors the recon SUM(quantity) GROUP BY market pattern
    so LEAN never silently drops a leg."""
    with sync_engine.begin() as conn:
        account_id = _seed_account(conn)
        _seed_raw_position(
            conn, account_id=account_id, market="/MES", quantity=2, avg_cost="5000.00"
        )
        _seed_raw_position(
            conn, account_id=account_id, market="/MES", quantity=1, avg_cost="5100.00"
        )

    resp = await client.get(_POSITIONS_PATH, headers=_AUTH)
    assert resp.status_code == 200
    positions = resp.json()["positions"]
    assert len(positions) == 1  # one netted row, not two
    assert positions[0]["market"] == "/MES"
    assert positions[0]["quantity"] == 3  # 2 + 1 summed, not silently overwritten
