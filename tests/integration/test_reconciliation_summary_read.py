"""Integration tests for :meth:`PostgresPhase1QueryRepo.fetch_reconciliation_summary`.

Locks the SQL contract of the /system Reconciliation tile against real
Postgres. The unit tests in ``tests/unit/test_api_phase1_routes.py`` exercise
the route + mapper against a stub repo; they cannot catch the regression this
module targets:

  * ``last_check_utc`` is sourced from the latest EOD-recon balance snapshot
    (``balances.source = 'coinbase_eod'``), NOT MAX(detected_at_utc) over
    ``reconciliation_breaks``. A passing recon cycle writes a balance snapshot
    but NO break row, so a break-derived ``last_check_utc`` froze on the last
    detected break and the tile looked stale ("Last check" stuck in the past)
    after a clean run. These tests prove the timestamp tracks recon *runs*.
  * The fill processor also writes ``balances`` rows (``source = 'tws_api'``).
    A source-filter regression would let a fill masquerade as a recon check;
    ``test_fill_balance_does_not_count_as_recon_check`` guards that boundary.

Each test uses a fresh account so the module-scoped DB stays isolated (the
query filters by ``account_id``). Skip behavior mirrors
``tests/integration/test_audit_log_read.py``: if Docker is unreachable, the
module skips cleanly.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
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

from services.api.repos.phase1 import PostgresPhase1QueryRepo

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
    """Bring up postgres:16, apply migrations, yield the sync URL."""
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
    ext_id = f"RECON_SUMMARY_TEST_{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": ext_id},
        ).scalar_one()
    return UUID(str(account_id))


# ---------------------------------------------------------------------------
# Seed helpers (sync engine — commits on context exit)
# ---------------------------------------------------------------------------


def _insert_balance(
    engine: Engine,
    account_id: UUID,
    *,
    snapshot_ts: datetime,
    source: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO balances ("
                "  account_id, snapshot_ts, net_liquidation, cash_usd, "
                "  excess_liquidity, used_margin_pct, source"
                ") VALUES ("
                "  :acc, :ts, 100000, 100000, 100000, 0, :source"
                ")"
            ),
            {"acc": account_id, "ts": snapshot_ts, "source": source},
        )


def _insert_break(
    engine: Engine,
    account_id: UUID,
    *,
    detected_at_utc: datetime,
    resolved_at_utc: datetime | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO reconciliation_breaks ("
                "  account_id, detected_at_utc, metric, source, "
                "  audit_event_uuid, resolved_at_utc"
                ") VALUES ("
                "  :acc, :det, 'position_qty', 'coinbase_eod', :aud, :res"
                ")"
            ),
            {
                "acc": account_id,
                "det": detected_at_utc,
                "aud": uuid.uuid4(),
                "res": resolved_at_utc,
            },
        )


async def _fetch(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: UUID,
) -> Any:
    async with session_factory() as session:
        repo = PostgresPhase1QueryRepo(session)
        return await repo.fetch_reconciliation_summary(account_id)


def _assert_close(actual: datetime | None, expected: datetime) -> None:
    """TIMESTAMPTZ round-trips at microsecond precision; allow 1s slack."""
    assert actual is not None
    assert abs((actual - expected).total_seconds()) < 1.0, (actual, expected)


class TestLastCheckSource:
    @pytest.mark.asyncio
    async def test_last_check_utc_is_recon_snapshot_not_break_detection(
        self,
        sync_engine: Engine,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # An OPEN break detected 2h ago, plus a recon balance snapshot 1h ago.
        # last_check_utc must be the recon snapshot (the run), NOT the break.
        now = datetime.now(tz=UTC)
        break_ts = now - timedelta(hours=2)
        recon_ts = now - timedelta(hours=1)
        _insert_break(sync_engine, fresh_account_id, detected_at_utc=break_ts)
        _insert_balance(sync_engine, fresh_account_id, snapshot_ts=recon_ts, source="coinbase_eod")

        summary = await _fetch(async_session_factory, fresh_account_id)

        _assert_close(summary.last_check_utc, recon_ts)
        assert summary.open_breaks == 1
        assert summary.breaks_24h == 1
        assert summary.last_check_passed is False

    @pytest.mark.asyncio
    async def test_clean_run_after_resolved_break_advances_last_check(
        self,
        sync_engine: Engine,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # The regression case: a break was detected + later RESOLVED, then a
        # clean recon cycle ran. The clean cycle writes a balance snapshot but
        # no break row. last_check_utc must advance to the clean run, and the
        # tile must read as passing (0 open breaks).
        now = datetime.now(tz=UTC)
        _insert_break(
            sync_engine,
            fresh_account_id,
            detected_at_utc=now - timedelta(hours=2),
            resolved_at_utc=now - timedelta(minutes=90),
        )
        clean_run_ts = now - timedelta(minutes=30)
        _insert_balance(
            sync_engine,
            fresh_account_id,
            snapshot_ts=clean_run_ts,
            source="coinbase_eod",
        )

        summary = await _fetch(async_session_factory, fresh_account_id)

        _assert_close(summary.last_check_utc, clean_run_ts)
        assert summary.open_breaks == 0
        assert summary.last_check_passed is True

    @pytest.mark.asyncio
    async def test_latest_of_multiple_recon_snapshots_wins(
        self,
        sync_engine: Engine,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        now = datetime.now(tz=UTC)
        older = now - timedelta(days=1)
        newest = now - timedelta(minutes=5)
        _insert_balance(sync_engine, fresh_account_id, snapshot_ts=older, source="coinbase_eod")
        _insert_balance(sync_engine, fresh_account_id, snapshot_ts=newest, source="coinbase_eod")

        summary = await _fetch(async_session_factory, fresh_account_id)

        _assert_close(summary.last_check_utc, newest)


class TestSourceBoundary:
    @pytest.mark.asyncio
    async def test_fill_balance_does_not_count_as_recon_check(
        self,
        sync_engine: Engine,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # The fill processor writes balances rows with source='tws_api'. These
        # must NOT register as a recon check — recon has never run for this
        # account, so last_check_utc is None (caller substitutes the epoch
        # sentinel → tile shows "No checks yet").
        _insert_balance(
            sync_engine,
            fresh_account_id,
            snapshot_ts=datetime.now(tz=UTC),
            source="tws_api",
        )

        summary = await _fetch(async_session_factory, fresh_account_id)

        assert summary.last_check_utc is None
        assert summary.open_breaks == 0
        assert summary.breaks_24h == 0
        assert summary.last_check_passed is True

    @pytest.mark.asyncio
    async def test_no_data_returns_null_last_check(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        summary = await _fetch(async_session_factory, fresh_account_id)

        assert summary.last_check_utc is None
        assert summary.open_breaks == 0
        assert summary.breaks_24h == 0
        assert summary.last_check_passed is True
