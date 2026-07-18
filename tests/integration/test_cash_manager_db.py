"""Integration tests: cash-manager SQL surfaces against real Postgres.

Risk-review blocker 1 for the C1→C2 cash-manager PR: the fake-session
unit tests structurally cannot catch schema mismatches (the original
draft queried a non-existent ``accounts.active`` column), so the three
SQL readers + the ``cash_sweeps`` INSERT/UPDATE + the real audit-first
apply path are exercised here against the alembic-migrated schema
(A22: real Postgres via testcontainers, not mocks).

Coverage:

* ``CashManagerJob._fetch_account_id`` — canonical ``active_to IS NULL``
  predicate against the real ``accounts`` DDL
* ``_fetch_contract_sizes`` — latest ``product_metadata`` capture wins
* ``_fetch_cbi_usdc`` — fresh capture returned; stale (>48 h) → None
* :func:`apply_cash_sweep` end-to-end with a fake venue client: real
  ``append_audit_event`` (existing capital-event types + payload
  discriminator), ``cash_sweeps`` requested → completed/failed, and the
  baseline-pollution guard (zero ``capital_events`` rows)

Skips cleanly when Docker is unreachable.
A02 binding: exercises ``services/risk/**``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
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

from services.risk.cash_manager import (
    SWEEP_AUDIT_KIND,
    CashManagerJob,
    CashSweepError,
    CashSweepPlan,
    apply_cash_sweep,
)

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]
_HEAD_REVISION = "20260713_recon_resolution_auto"
NOW = datetime(2026, 7, 18, 0, 25, tzinfo=UTC)


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


@pytest.fixture(scope="module")
def pg() -> Iterator[tuple[Engine, str]]:
    _require_docker()
    with PostgresContainer("postgres:16") as container:
        sync_url = container.get_connection_url()
        engine = create_engine(sync_url)
        yield engine, sync_url
        engine.dispose()


@pytest_asyncio.fixture()
async def session_factory(
    pg: tuple[Engine, str],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _, sync_url = pg
    _run_alembic(sync_url, "upgrade", _HEAD_REVISION)
    async_url = sync_url.replace("+psycopg2", "+asyncpg")
    engine: AsyncEngine = create_async_engine(async_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_account(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "INSERT INTO accounts ("
                        "    external_account_id, account_type, active_from"
                        ") VALUES ('operator-cm-test', 'individual', now()) "
                        "ON CONFLICT (external_account_id) DO UPDATE "
                        "    SET active_from = accounts.active_from "
                        "RETURNING id"
                    )
                )
            ).fetchone()
    assert row is not None
    return UUID(str(row.id))


def _job(factory: async_sessionmaker[AsyncSession]) -> CashManagerJob:
    class _UnusedBroker:
        async def get_futures_balance_summary(self) -> Any:
            raise AssertionError("not used in these tests")

        async def list_positions(self) -> Any:
            raise AssertionError("not used in these tests")

    class _UnusedClient:
        async def convert_cash(self, **kwargs: Any) -> str:
            raise AssertionError("not used")

        async def schedule_futures_sweep(self, **kwargs: Any) -> None:
            raise AssertionError("not used")

    return CashManagerJob(
        broker=_UnusedBroker(),
        sweep_client=_UnusedClient(),
        session_factory=factory,
        env="paper",
        phase_at_emit=1,
    )


class _FakeVenueClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def convert_cash(
        self, *, from_currency: str, to_currency: str, amount_usd: Decimal
    ) -> str:
        if self.fail:
            raise CashSweepError(operation="create_convert_quote", detail="venue down")
        return "trade-int-1"

    async def schedule_futures_sweep(self, *, amount_usd: Decimal) -> None:
        return None


def _plan(direction: str = "to_yield") -> CashSweepPlan:
    return CashSweepPlan(
        direction=direction,  # type: ignore[arg-type]
        amount_usd=Decimal("1750.00"),
        visible_equity_usd=Decimal("4000"),
        cfm_target_usd=Decimal("2250"),
        floor_guard_usd=Decimal("2250"),
        initial_margin_usd=Decimal("0"),
        gross_notional_usd=Decimal("0"),
        cbi_usdc_usd=Decimal("3500"),
        reason="integration test",
    )


class TestSqlReaders:
    async def test_fetch_account_id_uses_real_columns(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        account_id = await _seed_account(session_factory)
        job = _job(session_factory)
        assert await job._fetch_account_id() == account_id

    async def test_fetch_contract_sizes_latest_capture_wins(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            async with session.begin():
                for captured, size in ((NOW - timedelta(days=1), "0.02"), (NOW, "0.01")):
                    await session.execute(
                        text(
                            "INSERT INTO product_metadata ("
                            "    product_id, captured_at_utc, contract_size, raw"
                            ") VALUES ('BIP-TEST-CDE', :cap, :size, '{}'::jsonb) "
                            "ON CONFLICT (product_id, captured_at_utc) DO NOTHING"
                        ),
                        {"cap": captured, "size": Decimal(size)},
                    )
        sizes = await _job(session_factory)._fetch_contract_sizes()
        assert sizes["BIP-TEST-CDE"] == Decimal("0.01")

    async def test_fetch_cbi_usdc_fresh_and_stale(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _job(session_factory)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO cash_balance_snapshots ("
                        "    snapshot_date_utc, captured_at_utc, cbi_usdc, raw"
                        ") VALUES (:d, :cap, :usdc, '{}'::jsonb) "
                        "ON CONFLICT (snapshot_date_utc) DO NOTHING"
                    ),
                    {
                        "d": NOW.date(),
                        "cap": NOW - timedelta(minutes=5),
                        "usdc": Decimal("3500"),
                    },
                )
        assert await job._fetch_cbi_usdc(now_utc=NOW) == Decimal("3500")
        # Same capture read 49 h later degrades to unknown.
        assert await job._fetch_cbi_usdc(now_utc=NOW + timedelta(hours=49)) is None


class TestApplyEndToEnd:
    async def test_completed_sweep_audit_row_ledger_no_capital_events(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        account_id = await _seed_account(session_factory)
        result = await apply_cash_sweep(
            session_factory,
            plan=_plan(),
            account_id=account_id,
            env="paper",
            phase_at_emit=1,
            client=_FakeVenueClient(),
            now_utc=NOW,
        )
        assert result.status == "completed"
        assert result.audit_event_uuid is not None
        async with session_factory() as session:
            sweep = (
                await session.execute(
                    text("SELECT * FROM cash_sweeps WHERE id = :id"),
                    {"id": result.sweep_row_id},
                )
            ).fetchone()
            assert sweep is not None
            assert sweep.status == "completed"
            assert sweep.direction == "to_yield"
            assert Decimal(str(sweep.amount_usd)) == Decimal("1750.00")
            assert sweep.completed_at_utc is not None
            audit = (
                await session.execute(
                    text(
                        "SELECT event_type, "
                        "       convert_from(payload_jcs, 'UTF8') AS payload_text "
                        "FROM audit_log WHERE event_uuid = :u"
                    ),
                    {"u": result.audit_event_uuid},
                )
            ).fetchone()
            assert audit is not None
            assert audit.event_type == "capital_event_withdrawal"
            payload = json.loads(audit.payload_text)
            assert payload["kind"] == SWEEP_AUDIT_KIND
            assert payload["internal_transfer"] is True
            # Baseline-pollution guard: NO capital_events row anywhere.
            count = (await session.execute(text("SELECT COUNT(*) FROM capital_events"))).scalar()
            assert count == 0

    async def test_venue_failure_lands_failed_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        account_id = await _seed_account(session_factory)
        result = await apply_cash_sweep(
            session_factory,
            plan=_plan("to_margin"),
            account_id=account_id,
            env="paper",
            phase_at_emit=1,
            client=_FakeVenueClient(fail=True),
            now_utc=NOW,
        )
        assert result.status == "failed"
        async with session_factory() as session:
            sweep = (
                await session.execute(
                    text(
                        "SELECT status, failure_detail, completed_at_utc "
                        "FROM cash_sweeps WHERE id = :id"
                    ),
                    {"id": result.sweep_row_id},
                )
            ).fetchone()
            assert sweep is not None
            assert sweep.status == "failed"
            assert sweep.completed_at_utc is None
            assert "venue down" in sweep.failure_detail
