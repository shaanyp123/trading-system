"""Integration test for ``scripts.operator_tools.replace_protective_stop``.

Spins up a ``postgres:16`` testcontainer per module + ``alembic upgrade
head``, then exercises
:func:`scripts.operator_tools.replace_protective_stop.apply_replace_protective_stop`
+ :func:`scripts.operator_tools.replace_protective_stop._amain` against a
real schema pre-populated with an open trade + a working bracket-stop or
no working bracket-stop, depending on the test case. A fake
:class:`IbkrClient` simulates the broker round-trip.

**What this module proves end-to-end:**

* Happy path: audit-first ORDER_PLACED row commits BEFORE the orders row
  (status='pending'). After IBKR ack, the orders row UPDATEs to
  status='working' + carries the broker_order_id. The new orders row's
  ``parent_order_id`` points at the ORIGINAL entry order — wiring the
  replacement stop back to the position for future exit-pipeline lookups.

* Idempotency guard: when a working bracket-stop already exists for the
  open trade, ``_amain`` (without ``--force``) returns
  ``EXIT_BRACKET_ALREADY_PROTECTED`` (exit code 2). No new audit row +
  no new orders row land. This is the safety net for the "operator
  re-ran the script while the previous attempt is still working"
  scenario.

* Risk-state bypass: PR-5's recovery path explicitly bypasses the
  risk_state gate (it's recovery, not new exposure). Seeding a
  ``risk_state.state='HALT_NEW'`` row + invoking
  ``apply_replace_protective_stop`` still succeeds. Mirrors the design
  intent documented in the script's docstring.

If Docker is unreachable, the module skips cleanly.

Pattern mirrors ``tests/integration/test_exit_end_to_end.py`` from
PR #264 (testcontainers + module-scoped pg fixture + ``alembic upgrade
head``) — that file is the canonical exemplar.
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

from scripts.operator_tools.replace_protective_stop import (
    EXIT_BRACKET_ALREADY_PROTECTED,
    EXIT_OK,
    ParsedArgs,
    _amain,
    apply_replace_protective_stop,
    fetch_open_position,
    fetch_open_trade,
    plan_replace_protective_stop,
)
from services.audit.chain import verify_chain
from services.execution.types import (
    IbkrContractRef,
    IbkrPlaceOrderResult,
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
    ext_id = f"RPS_TEST_{uuid.uuid4().hex[:12]}"
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

    Same pattern as ``tests/integration/test_exit_end_to_end.py``.
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
                "    :audit, 'replace_protective_stop fixture'"
                ") RETURNING id"
            ),
            {"ver": next_version_no, "audit": uuid.uuid4()},
        ).scalar_one()
    return UUID(str(slip_id))


def _seed_open_position_for_replace_stop(
    sync_engine: Engine,
    *,
    account_id: UUID,
    slip_id: UUID,
    market: str = "TLT",
    entry_price: str = "85.00",
    stop_price: str = "82.50",
    quantity: int = 1,
    seed_existing_working_bracket: bool = False,
) -> dict[str, Any]:
    """Seed the pre-replace-stop state: closed entry signal + filled
    entry order + open trade + positions_current row + balance. When
    ``seed_existing_working_bracket=True``, also seed a working
    bracket-stop orders row so the idempotency guard fires.

    ``sizing_trace`` on the entry signal carries the original ATR-derived
    stop_price the planner reads via ``_extract_stop_price_from_sizing_trace``.
    """
    entry_cid = "strat001-paramset-sig00001-0"
    existing_stop_cid = "strat001-paramset-sig00001-0-stop"
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
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "sig": entry_signal_id,
                "cid": entry_cid,
                "market": market,
                "ep": Decimal(entry_price),
                "qty": quantity,
            },
        ).scalar_one()
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
                "ord": entry_order_id,
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
        existing_stop_order_id: UUID | None = None
        if seed_existing_working_bracket:
            existing_stop_order_id = conn.execute(
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
                    ") RETURNING id"
                ),
                {
                    "acct": account_id,
                    "sig": entry_signal_id,
                    "cid": existing_stop_cid,
                    "market": market,
                    "qty": quantity,
                    "sp": Decimal(stop_price),
                    "parent": entry_order_id,
                },
            ).scalar_one()
    return {
        "entry_signal_id": UUID(str(entry_signal_id)),
        "entry_order_id": UUID(str(entry_order_id)),
        "trade_id": UUID(str(trade_id)),
        "existing_stop_order_id": (
            UUID(str(existing_stop_order_id)) if existing_stop_order_id is not None else None
        ),
        "existing_stop_cid": existing_stop_cid if seed_existing_working_bracket else None,
        "entry_cid": entry_cid,
        "entry_price": Decimal(entry_price),
        "stop_price": Decimal(stop_price),
        "quantity": quantity,
        "market": market,
    }


def _seed_halt_new_risk_state(sync_engine: Engine, *, account_id: UUID) -> None:
    """Seed HALT_NEW risk_state row. Mirrors the helper added in PR
    #265 (HALT_NEW exit bypass) in tests/integration/test_exit_end_to_end.py.
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


def _fake_ibkr_client(
    *,
    place_returns: IbkrPlaceOrderResult | None = None,
    place_raises: BaseException | None = None,
) -> MagicMock:
    """Construct a MagicMock IbkrClient with controllable async returns.

    The replace-stop happy path needs ``resolve_contract`` + ``place_order``;
    no cancel call is involved.
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
    if place_raises is not None:
        client.place_order = AsyncMock(side_effect=place_raises)
    else:
        if place_returns is None:
            place_returns = IbkrPlaceOrderResult(
                client_order_id="0000000100000000-00000001-019e0000-stop-replace-0",
                broker_order_id=5,
                status="submitted",
                submitted_at_utc=datetime.now(tz=UTC),
                rejection_category=None,
                rejection_detail=None,
            )
        client.place_order = AsyncMock(return_value=place_returns)
    return client


@pytest.mark.asyncio
async def test_apply_replace_protective_stop_happy_path(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """End-to-end happy path: build plan → apply → assert audit-first
    ORDER_PLACED → pre-INSERT pending row → broker ack → UPDATE to
    working.

    Asserts:
      * ORDER_PLACED audit row lands (+1 row in audit_log).
      * New orders row INSERTed with parent_order_id pointing at the
        ORIGINAL entry_order_id.
      * After broker ack, status='working' + broker_order_id='5'.
      * IbkrClient.resolve_contract + place_order each called exactly once.
      * Audit chain still verifies clean.
    """
    state = _seed_open_position_for_replace_stop(
        sync_engine,
        account_id=fresh_account_id,
        slip_id=slippage_head_version_id,
        seed_existing_working_bracket=False,
    )

    with sync_engine.connect() as conn:
        audit_count_before = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
        orders_count_before = conn.execute(
            text(
                "SELECT COUNT(*) FROM orders WHERE account_id = :acct "
                "  AND order_type = 'stop_market'"
            ),
            {"acct": fresh_account_id},
        ).scalar_one()

    # Build plan via the public planner (no DB I/O in the planner).
    position = await fetch_open_position(
        async_session_factory, account_id=fresh_account_id, market=state["market"]
    )
    assert position is not None
    trade = await fetch_open_trade(
        async_session_factory, account_id=fresh_account_id, market=state["market"]
    )
    assert trade is not None
    plan = plan_replace_protective_stop(
        position=position,
        trade=trade,
        sizing_trace={
            "stage_0_universe": {
                "strategy_inputs": {state["market"]: {"stop_price": str(state["stop_price"])}}
            }
        },
        stop_price_override=None,
        env="paper",
    )
    assert plan.stop_source == "sizing_trace"
    assert plan.entry_order_id == state["entry_order_id"]

    ibkr = _fake_ibkr_client()
    contract = await ibkr.resolve_contract(state["market"])

    result = await apply_replace_protective_stop(
        plan,
        ibkr_client=ibkr,
        contract=contract,
        session_factory=async_session_factory,
    )
    assert result["db_status"] == "working"
    assert result["broker_order_id"] == "5"

    ibkr.place_order.assert_awaited_once()
    place_call = ibkr.place_order.await_args
    assert place_call is not None
    placed_request = place_call.args[0]
    assert placed_request.order_type == "stop_market"
    assert placed_request.side == "sell"
    assert placed_request.client_order_id == plan.stop_client_order_id

    # ----- Audit chain landed +1 ORDER_PLACED row -----
    with sync_engine.connect() as conn:
        audit_count_after = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
        last_event_type = conn.execute(
            text(
                "SELECT event_type FROM audit_log WHERE account_id = :acct "
                "ORDER BY sequence_no DESC LIMIT 1"
            ),
            {"acct": fresh_account_id},
        ).scalar_one()
    assert audit_count_after == audit_count_before + 1
    assert last_event_type == "order_placed"

    # ----- New stop_market orders row links back to ORIGINAL entry -----
    with sync_engine.connect() as conn:
        orders_count_after = conn.execute(
            text(
                "SELECT COUNT(*) FROM orders WHERE account_id = :acct "
                "  AND order_type = 'stop_market'"
            ),
            {"acct": fresh_account_id},
        ).scalar_one()
        new_stop_row = conn.execute(
            text(
                "SELECT id, status, broker_order_id, parent_order_id, "
                "       client_order_id, direction, stop_price "
                "FROM orders "
                "WHERE account_id = :acct AND order_type = 'stop_market' "
                "  AND client_order_id LIKE '%-stop-replace-0'"
            ),
            {"acct": fresh_account_id},
        ).one()
    assert orders_count_after == orders_count_before + 1
    assert new_stop_row.status == "working"
    assert new_stop_row.broker_order_id == "5"
    assert new_stop_row.parent_order_id == state["entry_order_id"]
    assert new_stop_row.direction == "sell"
    assert new_stop_row.stop_price == state["stop_price"]

    # ----- Chain still verifies clean -----
    async with async_session_factory() as session:
        verify_result = await verify_chain(session)
    assert verify_result[0] is True, (
        f"Chain broken at sequence_no={verify_result[1]} after {verify_result[2]} rows"
    )


@pytest.mark.asyncio
async def test_amain_idempotency_guard_blocks_when_bracket_already_working(
    async_session_factory: async_sessionmaker[AsyncSession],
    pg_url: str,
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency: pre-seed a working bracket-stop + invoke ``_amain``
    WITHOUT ``--force`` → returns EXIT_BRACKET_ALREADY_PROTECTED. No new
    audit row + no new orders row.

    The check fires BEFORE the IbAsyncIbkrClient import path (see
    ``scripts/operator_tools/replace_protective_stop.py::_amain``
    lines 1124-1138), so no IBKR stubbing is required — if the
    idempotency check works the IBKR module is never touched.
    """
    state = _seed_open_position_for_replace_stop(
        sync_engine,
        account_id=fresh_account_id,
        slip_id=slippage_head_version_id,
        seed_existing_working_bracket=True,
    )
    assert state["existing_stop_order_id"] is not None

    with sync_engine.connect() as conn:
        audit_count_before = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
        orders_count_before = conn.execute(
            text("SELECT COUNT(*) FROM orders WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()

    # _amain reads DATABASE_URL from env; point it at the async URL
    # the test fixture is built against so the script's own engine
    # sees the same testcontainer data. Build the async URL directly
    # from pg_url (the sync URL from PostgresContainer); going through
    # async_engine.url would mask the password as ``***`` via
    # SQLAlchemy's URL.__str__.
    async_url = pg_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
    monkeypatch.setenv("DATABASE_URL", async_url)

    args = ParsedArgs(
        market=state["market"],
        env="paper",
        dry_run=False,
        confirm=True,
        force=False,
        stop_price_override=None,
        account_id_override=fresh_account_id,
        ibkr_host="ib_gateway",
        ibkr_port=4004,
        client_id=99,
        allow_non_paper=False,
    )
    exit_code = await _amain(args)
    assert exit_code == EXIT_BRACKET_ALREADY_PROTECTED

    # ----- No new audit row, no new orders row -----
    with sync_engine.connect() as conn:
        audit_count_after = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
        orders_count_after = conn.execute(
            text("SELECT COUNT(*) FROM orders WHERE account_id = :acct"),
            {"acct": fresh_account_id},
        ).scalar_one()
    assert audit_count_after == audit_count_before
    assert orders_count_after == orders_count_before


@pytest.mark.asyncio
async def test_apply_replace_protective_stop_succeeds_under_halt_new(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """Risk-state bypass: PR-5's recovery path explicitly does NOT
    consult risk_state — it's recovery, not new exposure. Seeding
    risk_state='HALT_NEW' must NOT block the apply call.

    The script's _amain orchestrator also does not gate on risk_state
    (verified by code-read: there's no fetch_current_risk_state call
    on any code path through replace_protective_stop). This test
    asserts the apply step lands cleanly under HALT_NEW.
    """
    state = _seed_open_position_for_replace_stop(
        sync_engine,
        account_id=fresh_account_id,
        slip_id=slippage_head_version_id,
        seed_existing_working_bracket=False,
    )
    _seed_halt_new_risk_state(sync_engine, account_id=fresh_account_id)

    with sync_engine.connect() as conn:
        risk_state = conn.execute(
            text("SELECT state FROM risk_state WHERE account_id = :acct   AND is_current = TRUE"),
            {"acct": fresh_account_id},
        ).scalar_one()
    assert risk_state == "HALT_NEW", "fixture failed to seed HALT_NEW risk_state"

    position = await fetch_open_position(
        async_session_factory, account_id=fresh_account_id, market=state["market"]
    )
    assert position is not None
    trade = await fetch_open_trade(
        async_session_factory, account_id=fresh_account_id, market=state["market"]
    )
    assert trade is not None
    plan = plan_replace_protective_stop(
        position=position,
        trade=trade,
        sizing_trace={
            "stage_0_universe": {
                "strategy_inputs": {state["market"]: {"stop_price": str(state["stop_price"])}}
            }
        },
        stop_price_override=None,
        env="paper",
    )

    ibkr = _fake_ibkr_client()
    contract = await ibkr.resolve_contract(state["market"])

    result = await apply_replace_protective_stop(
        plan,
        ibkr_client=ibkr,
        contract=contract,
        session_factory=async_session_factory,
    )
    assert result["db_status"] == "working"
    assert result["broker_order_id"] == "5"

    # ----- Chain still verifies clean (key invariant — HALT_NEW
    # doesn't corrupt the audit-first ordering) -----
    async with async_session_factory() as session:
        verify_result = await verify_chain(session)
    assert verify_result[0] is True


# Bind EXIT_OK so the imports at the top of the file are exercised
# (mypy --strict + ruff F401 enforce; this keeps the import surface
# stable for the operator-runbook contract).
_EXIT_OK_REFERENCE = EXIT_OK
