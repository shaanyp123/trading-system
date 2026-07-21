"""Integration tests: strategy-worker migration + store against real Postgres.

Risk-review F3 for the C1 strategy-worker PR. Two surfaces:

1. Alembic up → down → up for ``20260709_strategy_worker``
   (``strategy_decisions`` + ``strategy_worker_status``), canonical
   smoke contract per ``test_alembic_crypto_pivot_tables.py`` (targets
   the explicit revision, not ``head``).
2. Real-SQL round-trip of :class:`StrategyWorkerStore`: heartbeat/status
   upsert (tick-count accumulation, JSONB engine state), decision
   insert-dedupe (``ON CONFLICT DO NOTHING``) + update + fetch, equity
   history reads, contracts-row idempotency, audit-first signal/order
   inserts (real ``append_audit_event`` — A22 permits it in
   integration), the fill fallback path, and the alerts insert
   (``alert_category`` enum cast).

Skips cleanly when Docker is unreachable.
A22 enforced: real Postgres via testcontainers, not mocks.
A02 binding: exercises ``alembic/**`` + ``services/signal/**``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from services.execution.types import PerpProductRef
from services.risk.fill_processor import FillIngestPayload
from services.signal.strategy_worker import (
    AssetRuntime,
    StrategyWorkerStore,
    serialize_engine_state,
)

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]
_PRIOR_REVISION = (
    "20260709_coinbase_recon_src"  # re-parented by the post-#354/#355 re-chain (PR #357)
)
_WORKER_REVISION = "20260709_strategy_worker"
_WORKER_TABLES: tuple[str, ...] = ("strategy_decisions", "strategy_worker_status")
NOW = datetime(2026, 7, 9, 0, 6, tzinfo=UTC)
TODAY = NOW.date()


def _require_docker() -> None:
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon unavailable: {exc}")


def _run_alembic(sync_url: str, cmd: str, target: str) -> None:
    from alembic.config import Config

    from alembic import command

    prev_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sync_url
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", sync_url)
        getattr(command, cmd)(cfg, target)
    finally:
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url


def _table_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": name},
        ).first()
    return row is not None


@pytest.fixture(scope="module")
def pg() -> Iterator[tuple[Engine, str]]:
    _require_docker()
    with PostgresContainer("postgres:16") as container:
        sync_url = container.get_connection_url()
        engine = create_engine(sync_url)
        yield engine, sync_url
        engine.dispose()


class TestMigrationUpDownUp:
    def test_upgrade_creates_worker_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run_alembic(sync_url, "upgrade", _WORKER_REVISION)
        for tname in _WORKER_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after upgrade"

    def test_downgrade_one_step_drops_worker_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run_alembic(sync_url, "downgrade", "-1")
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == _PRIOR_REVISION
        for tname in _WORKER_TABLES:
            assert not _table_exists(engine, tname), f"{tname} survived downgrade -1"

    def test_second_upgrade_recreates_worker_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run_alembic(sync_url, "upgrade", _WORKER_REVISION)
        for tname in _WORKER_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after re-upgrade"


# ---------------------------------------------------------------------------
# Store round-trip (runs after the migration class re-upgraded to head)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def session_factory(
    pg: tuple[Engine, str],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _, sync_url = pg
    # The STORE always speaks the current (head) schema — pinning this
    # fixture to _WORKER_REVISION let the store's SQL drift past the
    # fixture DB unnoticed (surfaced on PR #406 CI: upsert referenced
    # the 20260719 day_start_captured_at_utc column the pinned schema
    # lacked). The migration class above keeps its explicit-revision
    # pin (DP-022); the round-trip runs at head, as its section comment
    # always said.
    _run_alembic(sync_url, "upgrade", "head")
    async_url = sync_url.replace("+psycopg2", "+asyncpg")
    engine: AsyncEngine = create_async_engine(async_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_account_and_slippage(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID]:
    """accounts row + slippage_calibration_versions head (signals FK)."""
    async with session_factory() as session:
        async with session.begin():
            acct_row = (
                await session.execute(
                    text(
                        "INSERT INTO accounts ("
                        "    external_account_id, account_type, base_currency,"
                        "    role, active_from"
                        ") VALUES ("
                        "    :ext, 'individual', 'USD', 'owner', :now"
                        ") ON CONFLICT (external_account_id) WHERE active_to IS NULL "
                        "DO NOTHING RETURNING id"
                    ),
                    {"ext": "CBTEST123", "now": NOW},
                )
            ).fetchone()
            if acct_row is None:
                acct_row = (
                    await session.execute(
                        text(
                            "SELECT id FROM accounts "
                            "WHERE external_account_id = 'CBTEST123' LIMIT 1"
                        )
                    )
                ).fetchone()
            assert acct_row is not None
            account_id = UUID(str(acct_row.id))
            slip_row = (
                await session.execute(
                    text(
                        "INSERT INTO slippage_calibration_versions ("
                        "    version_no, is_head, calibrated_at_utc, trigger,"
                        "    per_market_coefficients, audit_event_uuid"
                        ") VALUES ("
                        "    1, TRUE, :now, 'bootstrap', '{}'::jsonb, :uuid"
                        ") ON CONFLICT (version_no) DO UPDATE SET is_head = TRUE "
                        "RETURNING id"
                    ),
                    {"now": NOW, "uuid": uuid4()},
                )
            ).fetchone()
            assert slip_row is not None
            slippage_id = UUID(str(slip_row.id))
    return account_id, slippage_id


def _btc_product() -> PerpProductRef:
    return PerpProductRef(
        product_id="BIP-20DEC30-CDE",
        base_asset="BTC",
        contract_size=Decimal("0.01"),
        tick_size=Decimal("1"),
        trading_disabled=False,
        raw={"future_product_details": {"contract_expiry": "2030-12-20T16:00:00Z"}},
    )


class TestStoreRoundTrip:
    async def test_worker_status_upsert_and_ticks(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        account_id, _ = await _seed_account_and_slippage(session_factory)
        store = StrategyWorkerStore(session_factory=session_factory, env="paper")
        state = serialize_engine_state(
            {"BTC": AssetRuntime(contracts=2, entry_vwap="78471.41"), "ETH": AssetRuntime()}
        )
        kwargs: dict[str, Any] = {
            "now_utc": NOW,
            "tick_increment": True,
            "marks_stale": False,
            "day_start_date": TODAY,
            "day_start_equity_usd": Decimal("6000.12345678"),
            "day_start_captured_at_utc": NOW,
            "weekly_halved_until": None,
            "flatten_seq": 3,
            "engine_state": state,
            "last_decision_date": TODAY,
        }
        await store.upsert_worker_status(account_id, **kwargs)
        await store.upsert_worker_status(account_id, **kwargs)  # second tick
        row = await store.fetch_worker_status(account_id)
        assert row is not None
        assert row.day_start_equity_usd == Decimal("6000.12345678")  # [A05] NUMERIC
        assert row.flatten_seq == 3
        assert row.engine_state["BTC"]["contracts"] == 2
        assert row.engine_state["BTC"]["entry_vwap"] == "78471.41"
        async with session_factory() as session:
            ticks = (
                await session.execute(
                    text(
                        "SELECT risk_loop_tick_count FROM strategy_worker_status "
                        "WHERE account_id = :acct"
                    ),
                    {"acct": account_id},
                )
            ).scalar()
        assert ticks == 2  # insert(1) + upsert(+1)

    async def test_decision_insert_dedupe_update_fetch(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        account_id, _ = await _seed_account_and_slippage(session_factory)
        store = StrategyWorkerStore(session_factory=session_factory, env="paper")
        outcome = {"schema_version": "strategy_decision_v1", "assets": {"BTC": {"final_target": 1}}}
        common: dict[str, Any] = {
            "decision_date": TODAY,
            "now_utc": NOW,
            "equity_usd": Decimal("6000"),
            "equity_for_sizing_usd": Decimal("1500"),
            "v_target": Decimal("0.8"),
            "m_combined_value": Decimal("1"),
            "weekly_halved": False,
            "parameter_set_hash": "a" * 64,
            "engine_state": serialize_engine_state({"BTC": AssetRuntime(), "ETH": AssetRuntime()}),
        }
        await store.insert_decision(account_id, status="dispatching", outcome=outcome, **common)
        # Dedupe: ON CONFLICT DO NOTHING — the second insert must not clobber.
        await store.insert_decision(
            account_id,
            status="completed",
            outcome={"schema_version": "x", "clobbered": True},
            **common,
        )
        row = await store.fetch_decision(account_id, TODAY)
        assert row is not None
        assert row.status == "dispatching"
        assert row.outcome["assets"]["BTC"]["final_target"] == 1
        await store.update_decision(
            account_id,
            decision_date=TODAY,
            now_utc=NOW,
            status="completed",
            outcome=outcome,
            engine_state=common["engine_state"],
        )
        row2 = await store.fetch_decision(account_id, TODAY)
        assert row2 is not None and row2.status == "completed"
        # Equity-history reads used by the weekly / monthly-dd windows.
        # (C1→C2 PR 2: fetch_equity_on_or_before returns EquityObservation —
        # equity + the created_at sweep-netting window anchor.)
        obs = await store.fetch_equity_on_or_before(account_id, TODAY + timedelta(days=1))
        assert obs is not None and obs.equity_usd == Decimal("6000")
        assert obs.observed_at_utc is not None
        assert await store.fetch_month_max_equity(
            account_id, date(TODAY.year, TODAY.month, 1)
        ) == Decimal("6000")

    async def test_fetch_last_threshold_met_capital_event_date(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Real-SQL contract for the m_capital_event count source:
        threshold_met filter, MAX (latest event restarts the clock), and
        the ``AT TIME ZONE 'UTC'`` date truncation."""
        account_id, _ = await _seed_account_and_slippage(session_factory)
        store = StrategyWorkerStore(session_factory=session_factory, env="paper")
        assert await store.fetch_last_threshold_met_capital_event_date(account_id) is None

        async def _insert(effective_at: str, threshold_met: bool) -> None:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO capital_events ("
                            "    account_id, event_type, amount_usd,"
                            "    pre_event_equity, post_event_equity,"
                            "    pct_of_pre_equity, threshold_met,"
                            "    effective_at_utc, capital_event_mode_session_start,"
                            "    audit_event_uuid"
                            ") VALUES ("
                            "    :acct, 'deposit', 500, 1500, 2000, 0.33333333,"
                            "    :thr, :eff, :sess, :audit"
                            ")"
                        ),
                        {
                            "acct": account_id,
                            "thr": threshold_met,
                            "eff": datetime.fromisoformat(effective_at),
                            "sess": 0 if threshold_met else None,
                            "audit": uuid4(),
                        },
                    )

        # Older threshold-met event, then a later one stored with a +02:00
        # offset that is still 23:30 UTC the SAME UTC day — the fetch must
        # normalize to the UTC calendar date, not the local one.
        await _insert("2026-07-01T12:00:00+00:00", True)
        await _insert("2026-07-06T01:30:00+02:00", True)  # = 2026-07-05T23:30Z
        # Newest row is below threshold: never starts the mode, must not win.
        await _insert("2026-07-08T00:00:00+00:00", False)

        got = await store.fetch_last_threshold_met_capital_event_date(account_id)
        assert got == date(2026, 7, 5)

    async def test_signal_order_fill_and_alert_round_trip(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        account_id, slippage_id = await _seed_account_and_slippage(session_factory)
        store = StrategyWorkerStore(session_factory=session_factory, env="paper")
        product = _btc_product()

        contract_id = await store.ensure_contract_row(asset="BTC", product=product)
        assert await store.ensure_contract_row(asset="BTC", product=product) == contract_id

        signal_id = await store.insert_signal_row(
            account_id,
            market="BTC",
            contract_id=contract_id,
            now_utc=NOW,
            session_date=TODAY,
            direction="long",
            signal_type="entry",
            decision_price=Decimal("78471.41"),
            target_contracts=1,
            sizing_trace={"source": "test"},
            strategy_hash="x" * 40,
            parameter_set_hash="a" * 64,
            slippage_calibration_version_id=slippage_id,
            rationale={"kind": "open"},
        )
        cid = "cde-test-entry-cid"
        assert await store.order_row_exists(cid) is None
        order_id = await store.insert_order_row(
            account_id,
            signal_id=signal_id,
            client_order_id=cid,
            broker_order_id="venue-1",
            market="BTC",
            contract_id=contract_id,
            direction="buy",
            order_type="limit_marketable",
            quantity=1,
            limit_price=None,
            stop_price=None,
            now_utc=NOW,
            strategy_hash="x" * 40,
            parameter_set_hash="a" * 64,
            audit_payload={"stages": []},
        )
        assert await store.order_row_exists(cid) == "pending"
        # Audit-first (risk-review F9): the ORDER_PLACED row carries the
        # pre-minted order id and precedes the orders INSERT.
        async with session_factory() as session:
            audit_row = (
                await session.execute(
                    text(
                        "SELECT convert_from(payload_jcs, 'UTF8') AS payload "
                        "FROM audit_log WHERE event_type = 'order_placed' "
                        "ORDER BY sequence_no DESC LIMIT 1"
                    )
                )
            ).fetchone()
        assert audit_row is not None
        import json as _json

        assert UUID(str(_json.loads(str(audit_row.payload))["order_id"])) == order_id

        # Fill fallback path: fills INSERT + orders status flip.
        await store.minimal_fill_fallback(
            account_id,
            client_order_id=cid,
            payload=FillIngestPayload(
                broker_fill_id=f"{cid}:agg",
                cumulative_filled_quantity=1,
                fill_quantity=1,
                fill_price=Decimal("78471.41"),
                commission_usd=Decimal("0.40"),
                filled_at_utc=NOW,
            ),
            market="BTC",
            fully_filled=True,
        )
        assert await store.order_row_exists(cid) == "filled"
        await store.update_signal_status(signal_id, status="filled")

        # Alerts insert (F1a): alert_category enum cast round-trips.
        await store.insert_alert(
            account_id,
            severity="P1",
            category="kill_switch_invoked",
            message="integration smoke",
            detail={"trigger": "daily_loss_breach"},
            now_utc=NOW,
        )
        async with session_factory() as session:
            alert = (
                await session.execute(
                    text(
                        "SELECT severity, category::text AS cat, detail FROM alerts "
                        "ORDER BY fired_at_utc DESC LIMIT 1"
                    )
                )
            ).fetchone()
        assert alert is not None
        assert alert.severity == "P1"
        assert alert.cat == "kill_switch_invoked"
