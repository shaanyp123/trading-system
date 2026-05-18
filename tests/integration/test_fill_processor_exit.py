"""Integration test for the fill_processor EXIT_FULL_CLOSE path (PR-G exit extension).

Spins up a ``postgres:16`` testcontainer per module, runs
``alembic upgrade head``, then exercises
:func:`services.risk.fill_processor.process_fill_event` against a real
schema pre-populated with an open trade + position + entry order + exit
order.

**What this test proves end-to-end:**

  1. Audit-first per spec §2.10.1 — 4 audit rows land (ORDER_FILLED →
     POSITION_CLOSED → BALANCE_SNAPSHOT_RECORDED → TRADE_CLOSED) BEFORE
     any table mutation.
  2. ``positions_current`` row is DELETEd.
  3. ``trades`` row UPDATEd: ``state='closed'`` + ``closed_at_utc`` +
     ``exit_order_id`` + ``avg_exit_price`` + ``realized_pnl_usd`` +
     accumulated ``realized_commission_usd``.
  4. ``fills`` gains a new row tagged to the exit order_id.
  5. ``balances`` gains a new snapshot with cash credited by the
     proceeds minus commission.
  6. Audit chain still verifies clean across the new 4 rows.

**Bracket-order contract (Option B, verified here):** the exit (stop)
order shares ``signal_id`` with the entry order. ``fetch_fill_context``
joining ``orders`` + ``signals`` returns the entry signal's direction
which then triggers the EXIT_FULL_CLOSE classification.

If Docker is unreachable, the module skips cleanly.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from services.audit.chain import verify_chain
from services.risk.fill_processor import (
    FillIngestPayload,
    process_fill_event,
)

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


@pytest.fixture
def fresh_account_id(sync_engine: Engine) -> UUID:
    ext_id = f"FILLEXIT_TEST_{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": ext_id},
        ).scalar_one()
    return UUID(str(account_id))


@pytest.fixture
def slippage_head_version_id(sync_engine: Engine) -> UUID:
    """Insert + return a bootstrap slippage_calibration_versions row.

    The slippage_head partial unique index requires only ONE is_head=TRUE
    row globally; we flip prior heads off first.
    """
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE slippage_calibration_versions SET is_head = FALSE WHERE is_head = TRUE")
        )
        next_version_no = conn.execute(
            text("SELECT COALESCE(MAX(version_no), 0) + 1 FROM slippage_calibration_versions")
        ).scalar_one()
        slip_id = conn.execute(
            text(
                "INSERT INTO slippage_calibration_versions ("
                "    version_no, is_head, calibrated_at_utc, trigger,"
                "    per_market_coefficients, data_window_start, data_window_end,"
                "    audit_event_uuid, notes"
                ") VALUES ("
                "    :ver, TRUE, now(), 'bootstrap',"
                "    '{}'::jsonb, NULL, NULL,"
                "    :audit, 'fill_processor_exit fixture'"
                ") RETURNING id"
            ),
            {"ver": next_version_no, "audit": uuid.uuid4()},
        ).scalar_one()
    return UUID(str(slip_id))


def _seed_open_trade_state(
    sync_engine: Engine,
    *,
    account_id: UUID,
    slip_id: UUID,
    market: str = "TLT",
    entry_price: str = "85.00",
    entry_commission: str = "0.50",
    quantity: int = 1,
) -> dict[str, Any]:
    """Pre-load a complete open-trade state: signal + entry order + exit
    order + positions_current row + trades row. Both orders share the same
    signal_id per the bracket-order Option B contract.

    Returns a dict with the minted UUIDs + client_order_ids the test
    needs to drive process_fill_event.
    """
    entry_cid = "strat001-paramset-sig00001-0"
    exit_cid = "strat001-paramset-sig00001-0-stop"
    with sync_engine.begin() as conn:
        signal_id = conn.execute(
            text(
                "INSERT INTO signals ("
                "    account_id, env, market, emitted_at_utc, session_date,"
                "    direction, signal_type, strategy_hash, parameter_set_hash,"
                "    slippage_calibration_version_id, decision_price, target_contracts,"
                "    sizing_trace, status"
                ") VALUES ("
                "    :acct, 'paper', :market, now(), CURRENT_DATE,"
                "    'long', 'donchian_breakout',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001',"
                "    :slip, 85.00, :qty,"
                "    '{}'::jsonb, 'working'"
                ") RETURNING id"
            ),
            {"acct": account_id, "market": market, "slip": slip_id, "qty": quantity},
        ).scalar_one()
        entry_order_id = conn.execute(
            text(
                "INSERT INTO orders ("
                "    account_id, env, signal_id, client_order_id, broker_order_id,"
                "    market, direction, order_type, quantity, limit_price, placed_at_utc,"
                "    status, retry_n, strategy_hash, parameter_set_hash"
                ") VALUES ("
                "    :acct, 'paper', :sig, :cid, '1',"
                "    :market, 'buy', 'limit_marketable', :qty, 85.00, now(),"
                "    'filled', 0,"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001'"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "sig": signal_id,
                "cid": entry_cid,
                "market": market,
                "qty": quantity,
            },
        ).scalar_one()
        exit_order_id = conn.execute(
            text(
                "INSERT INTO orders ("
                "    account_id, env, signal_id, client_order_id, broker_order_id,"
                "    market, direction, order_type, quantity, stop_price, placed_at_utc,"
                "    status, retry_n, strategy_hash, parameter_set_hash"
                ") VALUES ("
                "    :acct, 'paper', :sig, :cid, '2',"
                "    :market, 'sell', 'stop_market', :qty, 82.50, now(),"
                "    'pending', 0,"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001'"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "sig": signal_id,
                "cid": exit_cid,
                "market": market,
                "qty": quantity,
            },
        ).scalar_one()
        # Open trade (state=open_position) and position rows. They reflect
        # the post-entry-fill state — the entry fill itself isn't in the
        # fills table for this test; we only care about the EXIT fill.
        trade_id = conn.execute(
            text(
                "INSERT INTO trades ("
                "    account_id, env, market, entry_signal_id, entry_order_id,"
                "    direction, opened_at_utc,"
                "    total_quantity, avg_entry_price, realized_commission_usd, state,"
                "    managed_by_version, strategy_hash, parameter_set_hash,"
                "    slippage_calibration_version_id"
                ") VALUES ("
                "    :acct, 'paper', :market, :sig, :ord,"
                "    'long', now(),"
                "    :qty, :entry_price, :entry_comm, 'open_position',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001',"
                "    :slip"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "market": market,
                "sig": signal_id,
                "ord": entry_order_id,
                "qty": quantity,
                "entry_price": Decimal(entry_price),
                "entry_comm": Decimal(entry_commission),
                "slip": slip_id,
            },
        ).scalar_one()
        position_id = conn.execute(
            text(
                "INSERT INTO positions_current ("
                "    account_id, market, quantity, avg_cost, last_mark_ts,"
                "    managed_by_version"
                ") VALUES ("
                "    :acct, :market, :qty, :avg, now(),"
                "    '0000000000000000000000000000000000000001'"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "market": market,
                "qty": quantity,
                "avg": Decimal(entry_price),
            },
        ).scalar_one()
        # Seed a prior balances row representing post-entry state. cash =
        # 100000 - (85 * 1) - 0.50 = 99914.50
        #
        # snapshot_ts must be strictly BEFORE the exit fill's filled_at_utc
        # (`2026-05-17 18:00 UTC` in test_exit_full_close_end_to_end) because
        # production code AND the test's post-apply assertion both use
        # `ORDER BY snapshot_ts DESC LIMIT 1` to fetch the latest balance.
        # Using `now()` here is wall-clock-dependent: it produces a timestamp
        # *later* than the hard-coded exit time whenever the test runs after
        # 2026-05-17 18:00 UTC, which caused the latest-balance SELECT to
        # return THIS seeded row instead of the new exit-fill row. Hard-coded
        # 1 hour before the exit makes the relative ordering deterministic
        # regardless of when the test runs.
        balance_id = conn.execute(
            text(
                "INSERT INTO balances ("
                "    account_id, snapshot_ts, net_liquidation,"
                "    cash_usd, excess_liquidity, used_margin_pct, source"
                ") VALUES ("
                "    :acct, '2026-05-17 17:00:00+00'::timestamptz, 99999.50,"
                "    99914.50, 99914.50, 0, 'tws_api'"
                ") RETURNING id"
            ),
            {"acct": account_id},
        ).scalar_one()
    return {
        "signal_id": UUID(str(signal_id)),
        "entry_order_id": UUID(str(entry_order_id)),
        "exit_order_id": UUID(str(exit_order_id)),
        "trade_id": UUID(str(trade_id)),
        "position_id": UUID(str(position_id)),
        "balance_id": UUID(str(balance_id)),
        "entry_cid": entry_cid,
        "exit_cid": exit_cid,
        "entry_price": Decimal(entry_price),
        "entry_commission": Decimal(entry_commission),
        "quantity": quantity,
    }


@pytest.mark.asyncio
async def test_exit_full_close_end_to_end(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """Drive a complete EXIT_FULL_CLOSE through the I/O orchestrator.

    Pre-state: 1 TLT long open trade (entry 85.00, commission 0.50).
    Action: process exit fill at 86.50 with commission 0.50.
    Post-state: position DELETEd, trade UPDATEd to closed with realized
    P&L 0.50 (= 1.50 gross - 1.00 total commission), 4 new audit rows,
    fills + balances each gain one row, chain still verifies.
    """
    state = _seed_open_trade_state(
        sync_engine,
        account_id=fresh_account_id,
        slip_id=slippage_head_version_id,
    )

    # Snapshot audit_log size BEFORE the apply so we can assert +4.
    with sync_engine.connect() as conn:
        audit_count_before = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()

    exit_filled_at = datetime(2026, 5, 17, 18, 0, 0, tzinfo=UTC)
    payload = FillIngestPayload(
        broker_fill_id="2:agg",
        cumulative_filled_quantity=1,
        fill_quantity=1,
        fill_price=Decimal("86.50"),
        commission_usd=Decimal("0.50"),
        filled_at_utc=exit_filled_at,
    )
    result = await process_fill_event(
        session_factory=async_session_factory,
        client_order_id=state["exit_cid"],
        payload=payload,
        env="paper",
    )
    assert result is not None
    assert result.new_order_status == "filled"
    assert result.signal_id == state["signal_id"]
    assert result.trade_id == state["trade_id"]
    assert len(result.audit_event_uuids) == 4

    # ----- Audit chain landed +4 rows -----
    with sync_engine.connect() as conn:
        audit_count_after = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
    assert audit_count_after == audit_count_before + 4

    # Event types in order.
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type FROM audit_log WHERE account_id = :acct "
                "ORDER BY sequence_no DESC LIMIT 4"
            ),
            {"acct": fresh_account_id},
        ).fetchall()
    last_four_types_desc = [r.event_type for r in rows]
    # Last four landed in order: ORDER_FILLED → POSITION_CLOSED →
    # BALANCE_SNAPSHOT_RECORDED → TRADE_CLOSED. Reverse-chronological
    # DESC means TRADE_CLOSED comes first in the SELECT.
    assert last_four_types_desc == [
        "trade_closed",
        "balance_snapshot_recorded",
        "position_closed",
        "order_filled",
    ]

    # ----- positions_current row DELETEd -----
    with sync_engine.connect() as conn:
        remaining_positions = conn.execute(
            text("SELECT COUNT(*) FROM positions_current WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
    assert remaining_positions == 0

    # ----- trades row UPDATEd -----
    with sync_engine.connect() as conn:
        trade_row = conn.execute(
            text(
                "SELECT state, closed_at_utc, exit_order_id, avg_exit_price, "
                "       realized_pnl_usd, realized_commission_usd "
                "FROM trades WHERE id = :tid"
            ),
            {"tid": state["trade_id"]},
        ).one()
    assert trade_row.state == "closed"
    assert trade_row.closed_at_utc is not None
    assert trade_row.exit_order_id == state["exit_order_id"]
    assert trade_row.avg_exit_price == Decimal("86.50000000")
    # realized_pnl = (86.50 - 85.00) * 1 - (0.50 entry + 0.50 exit) = 0.50
    assert trade_row.realized_pnl_usd == Decimal("0.5000")
    # cumulative commission = 0.50 (seeded) + 0.50 (exit fill) = 1.00
    assert trade_row.realized_commission_usd == Decimal("1.0000")

    # ----- fills row INSERTed -----
    with sync_engine.connect() as conn:
        fills_row = conn.execute(
            text(
                "SELECT fill_price, fill_quantity, commission, broker_fill_id "
                "FROM fills WHERE order_id = :oid"
            ),
            {"oid": state["exit_order_id"]},
        ).one()
    assert fills_row.fill_price == Decimal("86.50000000")
    assert fills_row.fill_quantity == 1
    assert fills_row.commission == Decimal("0.50000000")
    assert fills_row.broker_fill_id == "2:agg"

    # ----- balances snapshot INSERTed -----
    with sync_engine.connect() as conn:
        latest_balance = conn.execute(
            text(
                "SELECT cash_usd, net_liquidation, source FROM balances "
                "WHERE account_id = :acct ORDER BY snapshot_ts DESC LIMIT 1"
            ),
            {"acct": fresh_account_id},
        ).one()
    # Prior cash 99914.50 + (86.50 - 0.50) = 100000.50
    assert latest_balance.cash_usd == Decimal("100000.5000")
    assert latest_balance.net_liquidation == Decimal("100000.5000")
    assert latest_balance.source == "tws_api"

    # ----- orders.status flipped to 'filled' -----
    with sync_engine.connect() as conn:
        exit_order = conn.execute(
            text("SELECT status, acknowledged_at_utc FROM orders WHERE id = :oid"),
            {"oid": state["exit_order_id"]},
        ).one()
    assert exit_order.status == "filled"
    assert exit_order.acknowledged_at_utc is not None

    # ----- signals.status flipped to 'closed' (drill 5 follow-up #3) -----
    # The cosmetic cleanup that closes drill 5 follow-up #3 from PR #176.
    # Pre-PR: the entry signal stayed at 'working' even after the trade
    # lifecycle completed, leaving stale rows in /api/signals?status=working.
    # The UPDATE happens inside the same _apply_trade_mutation session.begin()
    # block as the UPDATE trades, so the two are atomic.
    with sync_engine.connect() as conn:
        signal_row = conn.execute(
            text("SELECT status FROM signals WHERE id = :sig"),
            {"sig": state["signal_id"]},
        ).one()
    assert signal_row.status == "closed"

    # ----- chain still verifies clean -----
    async with async_session_factory() as session:
        verify_result = await verify_chain(session)
    assert verify_result[0] is True, (
        f"Chain broken at sequence_no={verify_result[1]} after {verify_result[2]} rows"
    )
    # We just wrote 4 audit rows in this test (the fixture's
    # slippage-bootstrap audit is not emitted by the fixture path, so
    # rows_walked equals the count of audit_log rows in the testcontainer).
    assert verify_result[2] >= 4
