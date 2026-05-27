"""Integration test for the PR-C exit-pipeline dispatch path.

Spins up a ``postgres:16`` testcontainer per module + ``alembic upgrade
head``, then exercises
:meth:`services.risk.order_placement_worker.OrderPlacementWorker._dispatch_exit_signal`
against a real schema pre-populated with an open trade + a working
bracket stop + an APPROVED exit signal. A fake :class:`IbkrClient`
simulates the broker round-trip.

**What this module proves end-to-end:**

* Happy path: audit-first ORDER_CANCELLED (bracket-stop intent) lands
  BEFORE the cancel call to IBKR. After IBKR ack, the bracket-stop
  ``orders`` row flips to ``status='cancelled'``. The close ``orders``
  row is pre-INSERTed (PR-ζ pattern) BEFORE the ``place_order`` call.
  On place ack, a second audit row (ORDER_PLACED) lands + the close
  orders row UPDATEs with the broker_order_id + ``signals.status`` for
  the exit row flips to ``'working'``.

* POSITION_UNPROTECTED branch: when ``cancel_order`` succeeds but
  ``place_order`` raises :class:`IbkrPlacementError` (broker
  unavailable / async-reject), the worker writes a POSITION_UNPROTECTED
  audit row + invokes the ``alert_dispatch_hook`` with the descriptor
  the api lifespan wires to the P0 Discord/email fan-out. The
  pre-placed close orders row stays in ``status='pending'`` for
  operator reconciliation.

* HALT_NEW bypass per design L5 + backend-spec §2.5: a risk_state row
  is seeded as HALT_NEW and ``worker.run_once()`` is invoked (rather
  than ``_dispatch_exit_signal`` directly), proving the worker's
  per-signal-type filter dispatches the exit even when entries would
  be blocked. The audit chain still verifies clean afterwards.

**Not covered here** (separate test files):

* The fill-processing side (``process_fill_event`` →
  ORDER_FILLED → POSITION_CLOSED → TRADE_CLOSED) is covered by
  ``tests/integration/test_fill_processor_exit.py``.
* The entry-side filter under HALT_NEW (entry signal stays at
  ``status='approved'`` + ``_dispatch_entry_signal`` is not invoked)
  is covered by the unit-level ``TestHaltGuard`` cases in
  ``tests/unit/test_order_placement_worker.py`` which monkeypatch
  the dispatch helpers directly.

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
from unittest.mock import AsyncMock, MagicMock
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
from services.execution.types import (
    IbkrCancelOrderResult,
    IbkrContractRef,
    IbkrPlacementError,
    IbkrPlaceOrderResult,
)
from services.risk.order_placement_worker import (
    ApprovedSignalRow,
    OrderPlacementWorker,
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
    ext_id = f"EXITE2E_TEST_{uuid.uuid4().hex[:12]}"
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

    Same pattern as ``tests/integration/test_fill_processor_exit.py``.
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
                "    :audit, 'exit_end_to_end fixture'"
                ") RETURNING id"
            ),
            {"ver": next_version_no, "audit": uuid.uuid4()},
        ).scalar_one()
    return UUID(str(slip_id))


def _seed_open_trade_with_exit_signal(
    sync_engine: Engine,
    *,
    account_id: UUID,
    slip_id: UUID,
    market: str = "TLT",
    entry_price: str = "85.00",
    stop_price: str = "82.50",
    quantity: int = 1,
) -> dict[str, Any]:
    """Pre-load complete pre-dispatch state: entry signal (status='closed'
    so it's done) + entry order (filled) + bracket-stop order (working) +
    open trade + position + balance + an APPROVED exit signal that the
    worker should pick up.

    The exit signal carries ``sizing_trace`` with the stop_price the
    planner reads via the bracket-order Option B contract.

    Returns the minted UUIDs + client_order_ids for the test.
    """
    entry_cid = "strat001-paramset-sig00001-0"
    stop_cid = "strat001-paramset-sig00001-0-stop"
    exit_cid_prefix = "strat001-paramset-sig00002-0"
    with sync_engine.begin() as conn:
        entry_signal_id = conn.execute(
            text(
                "INSERT INTO signals ("
                "    account_id, env, market, emitted_at_utc, session_date,"
                "    direction, signal_type, strategy_hash, parameter_set_hash,"
                "    slippage_calibration_version_id, decision_price, target_contracts,"
                "    sizing_trace, status"
                ") VALUES ("
                "    :acct, 'paper', :market, now() - interval '1 day', CURRENT_DATE - 1,"
                "    'long', 'donchian_breakout',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001',"
                "    :slip, :ep, :qty,"
                "    :sizing, 'closed'"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "market": market,
                "slip": slip_id,
                "ep": Decimal(entry_price),
                "qty": quantity,
                "sizing": (
                    '{"stage_0_universe": {"strategy_inputs": {"'
                    + market
                    + '": {"stop_price": "'
                    + stop_price
                    + '"}}}}'
                ),
            },
        ).scalar_one()
        entry_order_id = conn.execute(
            text(
                "INSERT INTO orders ("
                "    account_id, env, signal_id, client_order_id, broker_order_id,"
                "    market, direction, order_type, quantity, limit_price, placed_at_utc,"
                "    status, retry_n, strategy_hash, parameter_set_hash"
                ") VALUES ("
                "    :acct, 'paper', :sig, :cid, '1',"
                "    :market, 'buy', 'limit_marketable', :qty, :ep, now() - interval '1 day',"
                "    'filled', 0,"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001'"
                ") RETURNING id, created_at"
            ),
            {
                "acct": account_id,
                "sig": entry_signal_id,
                "cid": entry_cid,
                "market": market,
                "ep": Decimal(entry_price),
                "qty": quantity,
            },
        ).one()
        # Bracket-stop orders row — status='working' so the exit
        # dispatcher's fetch_open_bracket_stop_for_entry finds it.
        # parent_order_id wires it to the entry per PR-C contract.
        stop_order_id = conn.execute(
            text(
                "INSERT INTO orders ("
                "    account_id, env, signal_id, client_order_id, broker_order_id,"
                "    market, direction, order_type, quantity, stop_price, placed_at_utc,"
                "    status, retry_n, parent_order_id,"
                "    strategy_hash, parameter_set_hash"
                ") VALUES ("
                "    :acct, 'paper', :sig, :cid, '2',"
                "    :market, 'sell', 'stop_market', :qty, :sp, now() - interval '1 day',"
                "    'working', 0, :parent,"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001'"
                ") RETURNING id, created_at"
            ),
            {
                "acct": account_id,
                "sig": entry_signal_id,
                "cid": stop_cid,
                "market": market,
                "qty": quantity,
                "sp": Decimal(stop_price),
                "parent": entry_order_id.id,
            },
        ).one()
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
                "    'long', now() - interval '1 day',"
                "    :qty, :ep, 0.50, 'open_position',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001',"
                "    :slip"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "market": market,
                "sig": entry_signal_id,
                "ord": entry_order_id.id,
                "qty": quantity,
                "ep": Decimal(entry_price),
                "slip": slip_id,
            },
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO positions_current ("
                "    account_id, market, quantity, avg_cost, last_mark_ts,"
                "    managed_by_version"
                ") VALUES ("
                "    :acct, :market, :qty, :avg, now(),"
                "    '0000000000000000000000000000000000000001'"
                ")"
            ),
            {
                "acct": account_id,
                "market": market,
                "qty": quantity,
                "avg": Decimal(entry_price),
            },
        )
        conn.execute(
            text(
                "INSERT INTO balances ("
                "    account_id, snapshot_ts, net_liquidation,"
                "    cash_usd, excess_liquidity, used_margin_pct, source"
                ") VALUES ("
                "    :acct, now() - interval '1 hour', 99999.50,"
                "    99914.50, 99914.50, 0, 'tws_api'"
                ")"
            ),
            {"acct": account_id},
        )
        # The APPROVED EXIT signal — what the worker will pick up.
        # decision_price is the exit-time mark; signal_type='exit'
        # triggers the exit dispatch branch.
        exit_signal_id = conn.execute(
            text(
                "INSERT INTO signals ("
                "    account_id, env, market, emitted_at_utc, session_date,"
                "    direction, signal_type, strategy_hash, parameter_set_hash,"
                "    slippage_calibration_version_id, decision_price, target_contracts,"
                "    sizing_trace, status"
                ") VALUES ("
                "    :acct, 'paper', :market, now(), CURRENT_DATE,"
                "    'flat', 'exit',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001',"
                "    :slip, 86.50, 0,"
                "    '{}'::jsonb, 'approved'"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "market": market,
                "slip": slip_id,
            },
        ).scalar_one()
    return {
        "entry_signal_id": UUID(str(entry_signal_id)),
        "entry_order_id": UUID(str(entry_order_id.id)),
        "stop_order_id": UUID(str(stop_order_id.id)),
        "stop_order_created_at": stop_order_id.created_at,
        "trade_id": UUID(str(trade_id)),
        "exit_signal_id": UUID(str(exit_signal_id)),
        "entry_cid": entry_cid,
        "stop_cid": stop_cid,
        "exit_cid_prefix": exit_cid_prefix,
        "entry_price": Decimal(entry_price),
        "stop_price": Decimal(stop_price),
        "quantity": quantity,
        "market": market,
    }


def _build_exit_approved_signal_row(
    *, account_id: UUID, market: str, exit_signal_id: UUID, decision_price: Decimal
) -> ApprovedSignalRow:
    """Build the in-memory ApprovedSignalRow that mirrors what
    ``fetch_approved_signals`` would return for the seeded exit signal.

    We pass this directly to ``_dispatch_exit_signal`` rather than
    invoking ``run_once`` so the test surface stays focused on the
    dispatch logic itself (the HALT_NEW gate at run_once is a separate
    concern documented in the module docstring).
    """
    return ApprovedSignalRow(
        signal_id=exit_signal_id,
        account_id=account_id,
        env="paper",
        market=market,
        direction="flat",
        target_contracts=0,
        decision_price=decision_price,
        strategy_hash="0000000000000000000000000000000000000001",
        parameter_set_hash="0000000000000000000000000000000000000000000000000000000000000001",
        sizing_trace={},
        signal_type="exit",
    )


def _fake_ibkr_client(
    *,
    cancel_returns: IbkrCancelOrderResult | None = None,
    place_returns: IbkrPlaceOrderResult | None = None,
    place_raises: BaseException | None = None,
) -> MagicMock:
    """Construct a MagicMock IbkrClient with controllable async returns.

    ``resolve_contract`` returns a canned TLT-shape IbkrContractRef
    (multiplier=1 matches what ``services.api.exposure`` and the
    DB ``contracts.multiplier`` column would expect for ETFs).
    """
    contract = IbkrContractRef(
        market="TLT",
        ibkr_local_symbol="TLT",
        ibkr_con_id=None,
        multiplier=Decimal("1"),
        exchange="SMART",
    )
    client = MagicMock()
    client.resolve_contract = AsyncMock(return_value=contract)
    if cancel_returns is None:
        cancel_returns = IbkrCancelOrderResult(
            client_order_id="strat001-paramset-sig00001-0-stop",
            broker_order_id=2,
            cancel_submitted_at_utc=datetime.now(tz=UTC),
        )
    client.cancel_order = AsyncMock(return_value=cancel_returns)
    if place_raises is not None:
        client.place_order = AsyncMock(side_effect=place_raises)
    else:
        if place_returns is None:
            place_returns = IbkrPlaceOrderResult(
                client_order_id="strat001-paramset-sig00002-0-close",
                broker_order_id=3,
                status="submitted",
                submitted_at_utc=datetime.now(tz=UTC),
                rejection_category=None,
                rejection_detail=None,
            )
        client.place_order = AsyncMock(return_value=place_returns)
    return client


@pytest.mark.asyncio
async def test_exit_dispatch_happy_path_writes_two_audit_rows_and_state_transitions(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """End-to-end happy path: dispatch → cancel bracket → pre-INSERT
    close → place close → ack.

    Asserts:
      * Audit rows landed in order: ORDER_CANCELLED → ORDER_PLACED.
      * Bracket-stop ``orders`` row flipped to ``status='cancelled'``
        + rejection_reason='bracket_stop_replaced_by_exit_signal'.
      * Close orders row INSERTed with broker_order_id + status mapped
        from the broker's 'submitted' → 'submitted' (DB enum).
      * Exit signal flipped to ``status='working'``.
      * IbkrClient cancel_order + place_order each called exactly once.
      * Audit chain still verifies clean.
    """
    state = _seed_open_trade_with_exit_signal(
        sync_engine,
        account_id=fresh_account_id,
        slip_id=slippage_head_version_id,
    )

    with sync_engine.connect() as conn:
        audit_count_before = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()

    ibkr = _fake_ibkr_client()
    worker = OrderPlacementWorker(
        session_factory=async_session_factory,
        ibkr_client=ibkr,
        account_id=fresh_account_id,
        env="paper",
    )
    approved = _build_exit_approved_signal_row(
        account_id=fresh_account_id,
        market=state["market"],
        exit_signal_id=state["exit_signal_id"],
        decision_price=Decimal("86.50"),
    )

    placed_count = await worker._dispatch_exit_signal(approved)
    assert placed_count == 1

    # ----- IBKR client interactions -----
    ibkr.cancel_order.assert_awaited_once_with(state["stop_cid"])
    ibkr.place_order.assert_awaited_once()
    ibkr.resolve_contract.assert_awaited_once_with(state["market"])

    # ----- Audit chain landed +2 rows in ORDER_CANCELLED → ORDER_PLACED order -----
    with sync_engine.connect() as conn:
        audit_count_after = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
    assert audit_count_after == audit_count_before + 2

    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type FROM audit_log WHERE account_id = :acct "
                "ORDER BY sequence_no DESC LIMIT 2"
            ),
            {"acct": fresh_account_id},
        ).fetchall()
    # DESC ordering puts the most-recent ORDER_PLACED first.
    assert [r.event_type for r in rows] == ["order_placed", "order_cancelled"]

    # ----- Bracket-stop orders row flipped to cancelled -----
    with sync_engine.connect() as conn:
        stop_row = conn.execute(
            text("SELECT status, rejection_reason FROM orders WHERE id = :oid"),
            {"oid": state["stop_order_id"]},
        ).one()
    assert stop_row.status == "cancelled"
    assert stop_row.rejection_reason == "bracket_stop_replaced_by_exit_signal"

    # ----- Close orders row INSERTed with broker_order_id -----
    with sync_engine.connect() as conn:
        close_row = conn.execute(
            text(
                "SELECT broker_order_id, status, parent_order_id, "
                "       order_type, direction FROM orders "
                "WHERE signal_id = :sig AND order_type = 'limit_marketable'"
            ),
            {"sig": state["exit_signal_id"]},
        ).one()
    assert close_row.broker_order_id == "3"
    # IbkrPlaceOrderResult.status="submitted" maps to orders.status="working"
    # per services.risk.order_placement_worker._broker_status_to_orders_status
    # (the IBKR-side enum is richer than the orders.status CHECK constraint).
    assert close_row.status == "working"
    # parent_order_id wires the close back to the ORIGINAL entry per
    # PR-C contract — NOT the bracket stop.
    assert close_row.parent_order_id == state["entry_order_id"]
    assert close_row.direction == "sell"  # closing a long

    # ----- Exit signal flipped to status='working' -----
    with sync_engine.connect() as conn:
        sig_row = conn.execute(
            text("SELECT status FROM signals WHERE id = :sid"),
            {"sid": state["exit_signal_id"]},
        ).one()
    assert sig_row.status == "working"

    # ----- Chain verifies clean across the +2 rows -----
    async with async_session_factory() as session:
        verify_result = await verify_chain(session)
    assert verify_result[0] is True, (
        f"Chain broken at sequence_no={verify_result[1]} after {verify_result[2]} rows"
    )


@pytest.mark.asyncio
async def test_exit_dispatch_position_unprotected_branch_invokes_alert_hook(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """POSITION_UNPROTECTED branch: cancel succeeds + place_order raises
    IbkrPlacementError → the worker writes a POSITION_UNPROTECTED audit
    row, invokes the alert_dispatch_hook with a populated descriptor,
    and the pre-placed close orders row stays in 'pending'.

    The bracket-stop is now OFF (cancel landed at step 2) but the close
    didn't establish — the position is NAKED until the operator
    intervenes manually. This is the loud-failure mode the design
    requires (per backend-spec POSITION_UNPROTECTED semantics).

    Asserts:
      * Audit rows landed in order: ORDER_CANCELLED → POSITION_UNPROTECTED.
      * ``alert_dispatch_hook`` invoked once with descriptor carrying
        signal_id + market + prior_position_direction + the
        triggering_audit_event_uuid.
      * Bracket-stop orders row IS cancelled (step 3 ran before the
        place failure at step 5).
      * Pre-placed close orders row exists in status='pending'.
      * Exit signal stays at ``status='approved'`` (no UPDATE).
      * Dispatcher returns 0 (recoverable failure).
    """
    state = _seed_open_trade_with_exit_signal(
        sync_engine,
        account_id=fresh_account_id,
        slip_id=slippage_head_version_id,
    )

    ibkr = _fake_ibkr_client(
        place_raises=IbkrPlacementError(
            operation="placeOrder.exit_close",
            detail="simulated broker rejection",
            underlying_exception_class="ConnectionError",
            occurred_at_utc=datetime.now(tz=UTC),
        )
    )

    hook = AsyncMock()
    worker = OrderPlacementWorker(
        session_factory=async_session_factory,
        ibkr_client=ibkr,
        account_id=fresh_account_id,
        env="paper",
        position_unprotected_alert_hook=hook,
    )
    approved = _build_exit_approved_signal_row(
        account_id=fresh_account_id,
        market=state["market"],
        exit_signal_id=state["exit_signal_id"],
        decision_price=Decimal("86.50"),
    )

    placed_count = await worker._dispatch_exit_signal(approved)
    # Recoverable failure — dispatcher logs + returns 0.
    assert placed_count == 0

    # ----- Audit rows: ORDER_CANCELLED → POSITION_UNPROTECTED -----
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type FROM audit_log WHERE account_id = :acct "
                "ORDER BY sequence_no DESC LIMIT 2"
            ),
            {"acct": fresh_account_id},
        ).fetchall()
    assert [r.event_type for r in rows] == ["position_unprotected", "order_cancelled"]

    # ----- Alert dispatch hook invoked exactly once with descriptor -----
    hook.assert_awaited_once()
    assert hook.await_args is not None  # narrow for mypy strict
    descriptor = hook.await_args.args[0]
    assert descriptor.signal_id == state["exit_signal_id"]
    assert descriptor.market == state["market"]
    assert descriptor.account_id == fresh_account_id
    assert descriptor.prior_position_direction == "long"
    assert descriptor.prior_position_quantity == state["quantity"]
    assert descriptor.last_known_stop_price == state["stop_price"]
    # The descriptor links to the POSITION_UNPROTECTED audit row that
    # just landed; the route handler reads this to bind the alerts row
    # back to the audit chain.
    assert descriptor.triggering_audit_event_uuid is not None

    # ----- Bracket-stop orders row IS cancelled (cancel succeeded) -----
    with sync_engine.connect() as conn:
        stop_row = conn.execute(
            text("SELECT status FROM orders WHERE id = :oid"),
            {"oid": state["stop_order_id"]},
        ).one()
    assert stop_row.status == "cancelled"

    # ----- Close orders row pre-INSERTed at status='pending' -----
    with sync_engine.connect() as conn:
        close_row = conn.execute(
            text(
                "SELECT broker_order_id, status FROM orders "
                "WHERE signal_id = :sig AND order_type = 'limit_marketable'"
            ),
            {"sig": state["exit_signal_id"]},
        ).one()
    # PR-ζ pre-INSERT keeps the row durable for operator reconciliation;
    # broker_order_id stays NULL because the place call never succeeded.
    assert close_row.status == "pending"
    assert close_row.broker_order_id is None

    # ----- Exit signal stays at status='approved' (no UPDATE) -----
    with sync_engine.connect() as conn:
        sig_row = conn.execute(
            text("SELECT status FROM signals WHERE id = :sid"),
            {"sid": state["exit_signal_id"]},
        ).one()
    assert sig_row.status == "approved"

    # ----- Chain still verifies clean across +2 rows -----
    async with async_session_factory() as session:
        verify_result = await verify_chain(session)
    assert verify_result[0] is True


def _seed_halt_new_risk_state(sync_engine: Engine, *, account_id: UUID) -> None:
    """Seed a HALT_NEW risk_state row for the account, matching the
    pattern used by ``tests/integration/test_kill_switch_end_to_end.py``.

    ``audit_event_uuid`` is FK-less in the schema; a random UUID seeds
    the slot without needing an upstream state-transition audit row.
    """
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO risk_state ("
                "    account_id, state, severity, reason, entered_at_utc, "
                "    convalescent_session_count, vacation_active, "
                "    audit_event_uuid, is_current"
                ") VALUES ("
                "    :acc, 'HALT_NEW', 'routine', 'trailing_dd_breach', now(), "
                "    0, FALSE, :seed, TRUE"
                ")"
            ),
            {"acc": account_id, "seed": uuid.uuid4()},
        )


@pytest.mark.asyncio
async def test_run_once_dispatches_exit_under_halt_new_bypass(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """HALT_NEW bypass for exits (design L5 + backend-spec §2.5).

    Seeds a HALT_NEW risk_state row + the canonical exit-pipeline state
    (open trade, working bracket, approved exit signal) and invokes
    :meth:`OrderPlacementWorker.run_once` (not ``_dispatch_exit_signal``
    directly) so the per-signal-type filter at the run_once layer is
    exercised end-to-end.

    Asserts:
      * Worker returns 1 (the exit dispatched).
      * The canonical 2 audit rows landed (ORDER_CANCELLED → ORDER_PLACED).
      * Bracket-stop flipped to cancelled + close row at status='working'.
      * Exit signal flipped to status='working'.
      * Chain verifies clean across the +2 rows.

    Pre-PR-#264 + pre-this-PR, the run_once HALT_NEW gate short-circuited
    on ANY non-permitting risk_state and returned 0; with the gate split,
    the exit signal's signal_type='exit' carries it through. Entries
    under HALT_NEW are covered by the unit-level TestHaltGuard cases.
    """
    state = _seed_open_trade_with_exit_signal(
        sync_engine,
        account_id=fresh_account_id,
        slip_id=slippage_head_version_id,
    )
    _seed_halt_new_risk_state(sync_engine, account_id=fresh_account_id)

    ibkr = _fake_ibkr_client()
    worker = OrderPlacementWorker(
        session_factory=async_session_factory,
        ibkr_client=ibkr,
        account_id=fresh_account_id,
        env="paper",
    )

    placed = await worker.run_once()
    assert placed == 1

    ibkr.cancel_order.assert_awaited_once_with(state["stop_cid"])
    ibkr.place_order.assert_awaited_once()

    with sync_engine.connect() as conn:
        audit_event_types = conn.execute(
            text(
                "SELECT event_type FROM audit_log WHERE account_id = :acct "
                "ORDER BY sequence_no DESC LIMIT 2"
            ),
            {"acct": fresh_account_id},
        ).fetchall()
    assert [r.event_type for r in audit_event_types] == [
        "order_placed",
        "order_cancelled",
    ]

    with sync_engine.connect() as conn:
        stop_row = conn.execute(
            text("SELECT status FROM orders WHERE id = :oid"),
            {"oid": state["stop_order_id"]},
        ).one()
    assert stop_row.status == "cancelled"

    with sync_engine.connect() as conn:
        close_row = conn.execute(
            text(
                "SELECT status, broker_order_id FROM orders "
                "WHERE signal_id = :sig AND order_type = 'limit_marketable'"
            ),
            {"sig": state["exit_signal_id"]},
        ).one()
    assert close_row.status == "working"
    assert close_row.broker_order_id == "3"

    with sync_engine.connect() as conn:
        sig_row = conn.execute(
            text("SELECT status FROM signals WHERE id = :sid"),
            {"sid": state["exit_signal_id"]},
        ).one()
    assert sig_row.status == "working"

    async with async_session_factory() as session:
        verify_result = await verify_chain(session)
    assert verify_result[0] is True
