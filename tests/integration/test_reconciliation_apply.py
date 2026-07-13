"""Integration tests for ``services.reconciliation.apply.apply_reconciliation_plan``.

Worker-PR-3b (post-pivot 2026-05-12). Exercises the real Postgres
schema (alembic 0004 ``reconciliation_breaks`` table + audit_log
SERIALIZABLE write path) via testcontainers. The unit tests in
tests/unit/test_reconciliation_apply.py cover the orchestration
semantics; this file covers schema-shape correctness.

A22 BINDS + satisfied — real Postgres triggers + CHECK constraints +
audit_log grant matrix exercised. A01 binds — every break detection
routes its audit write through ``append_audit_event``.

Skips cleanly if Docker is unreachable (same pattern as
tests/integration/test_kill_switch_end_to_end.py).
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

from services.reconciliation.apply import (
    ReconciliationKillSwitchContext,
    apply_reconciliation_plan,
)
from services.reconciliation.recon import (
    BrokerSource,
    PendingAuditEvent,
    ReconciliationBreak,
    ReconciliationMetric,
    ReconciliationPlan,
    ResolvedPriorBreak,
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
    ext_id = f"RECON_TEST_{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": ext_id},
        ).scalar_one()
    return UUID(str(account_id))


def _detected_at() -> datetime:
    return datetime(2026, 5, 12, 18, 30, tzinfo=UTC)


def _make_break(
    *,
    market: str | None = "/MES",
    delta: Decimal = Decimal("1"),
    within_grace_period: bool = False,
) -> ReconciliationBreak:
    return ReconciliationBreak(
        metric=ReconciliationMetric.POSITION_QTY,
        market=market,
        expected=Decimal("2"),
        actual=Decimal("1"),
        delta=delta,
        tolerance=Decimal("0"),
        source=BrokerSource.TWS_API,
        detected_at_utc=_detected_at(),
        within_grace_period=within_grace_period,
        resolution_path=None,
    )


def _pending(
    event_type: str = "reconciliation_break_detected",
    **payload_overrides: Any,
) -> PendingAuditEvent:
    payload: dict[str, Any] = {"detected_at_utc": _detected_at().isoformat()}
    payload.update(payload_overrides)
    return PendingAuditEvent(event_type=event_type, payload=payload)  # type: ignore[arg-type]


def _read_breaks(sync_engine: Engine, account_id: UUID) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, account_id, detected_at_utc, metric, market, "
                "       expected, actual, delta, tolerance, source, "
                "       resolved_at_utc, resolution_path, audit_event_uuid "
                "FROM reconciliation_breaks WHERE account_id = :acc "
                "ORDER BY detected_at_utc, market"
            ),
            {"acc": account_id},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _count_audit_rows(sync_engine: Engine, account_id: UUID, event_type: str) -> int:
    with sync_engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acc AND event_type = :et"),
            {"acc": account_id, "et": event_type},
        ).scalar_one()
    return int(n)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_one_break_writes_audit_and_break_row(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """End-to-end: plan → audit row + reconciliation_breaks row, both linked."""
    plan = ReconciliationPlan(
        breaks_detected=(_make_break(),),
        breaks_resolved=(),
        audit_events=(_pending("reconciliation_break_detected", market="/MES"),),
        actionable_break_count=1,
        should_invoke_kill_switch=False,
    )
    result = await apply_reconciliation_plan(
        plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
        phase_at_emit=1,
    )
    assert len(result.audit_event_uuids) == 1
    assert len(result.inserted_break_ids) == 1

    rows = _read_breaks(sync_engine, fresh_account_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["metric"] == "position_qty"
    assert row["market"] == "/MES"
    assert row["source"] == "tws_api"
    assert row["audit_event_uuid"] == result.audit_event_uuids[0]
    assert row["resolved_at_utc"] is None  # not yet resolved
    assert _count_audit_rows(sync_engine, fresh_account_id, "reconciliation_break_detected") == 1


@pytest.mark.asyncio
async def test_apply_check_passed_writes_audit_no_break_row(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """No breaks → one ``reconciliation_check_passed`` audit row; no break rows."""
    plan = ReconciliationPlan(
        breaks_detected=(),
        breaks_resolved=(),
        audit_events=(_pending("reconciliation_check_passed"),),
        actionable_break_count=0,
        should_invoke_kill_switch=False,
    )
    await apply_reconciliation_plan(
        plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
        phase_at_emit=1,
    )
    assert _read_breaks(sync_engine, fresh_account_id) == []
    assert _count_audit_rows(sync_engine, fresh_account_id, "reconciliation_check_passed") == 1


@pytest.mark.asyncio
async def test_apply_resolved_break_updates_prior_row(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """Insert a break first, then apply a plan that resolves it; verify the UPDATE landed."""
    # Step 1: seed an unresolved break via an apply pass.
    seed_plan = ReconciliationPlan(
        breaks_detected=(_make_break(market="/MES", delta=Decimal("1")),),
        breaks_resolved=(),
        audit_events=(_pending("reconciliation_break_detected", market="/MES"),),
        actionable_break_count=1,
        should_invoke_kill_switch=False,
    )
    await apply_reconciliation_plan(
        seed_plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
        phase_at_emit=1,
    )

    # Step 2: resolve it.
    resolve_plan = ReconciliationPlan(
        breaks_detected=(),
        breaks_resolved=(
            ResolvedPriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="/MES",
                delta=Decimal("1"),
            ),
        ),
        audit_events=(_pending("reconciliation_break_resolved", market="/MES"),),
        actionable_break_count=0,
        should_invoke_kill_switch=False,
    )
    result = await apply_reconciliation_plan(
        resolve_plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
        phase_at_emit=1,
    )
    assert result.resolved_break_count == 1

    rows = _read_breaks(sync_engine, fresh_account_id)
    assert len(rows) == 1
    assert rows[0]["resolved_at_utc"] is not None
    assert rows[0]["resolution_path"] == "manual"


@pytest.mark.asyncio
async def test_apply_resolved_break_stamps_auto_rereconciled(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """The 2026-07-13 CHECK extension: 'auto_rereconciled' lands against
    the REAL constraint (migration ``20260713_recon_resolution_auto``) —
    the path ``run_eod_cycle`` stamps for machine re-resolutions."""
    seed_plan = ReconciliationPlan(
        breaks_detected=(_make_break(market="/MES", delta=Decimal("1")),),
        breaks_resolved=(),
        audit_events=(_pending("reconciliation_break_detected", market="/MES"),),
        actionable_break_count=1,
        should_invoke_kill_switch=False,
    )
    await apply_reconciliation_plan(
        seed_plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
        phase_at_emit=1,
    )
    resolve_plan = ReconciliationPlan(
        breaks_detected=(),
        breaks_resolved=(
            ResolvedPriorBreak(
                metric=ReconciliationMetric.POSITION_QTY,
                market="/MES",
                delta=Decimal("1"),
            ),
        ),
        audit_events=(_pending("reconciliation_break_resolved", market="/MES"),),
        actionable_break_count=0,
        should_invoke_kill_switch=False,
    )
    result = await apply_reconciliation_plan(
        resolve_plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
        phase_at_emit=1,
        resolution_path="auto_rereconciled",
    )
    assert result.resolved_break_count == 1

    rows = _read_breaks(sync_engine, fresh_account_id)
    assert len(rows) == 1
    assert rows[0]["resolved_at_utc"] is not None
    assert rows[0]["resolution_path"] == "auto_rereconciled"


@pytest.mark.asyncio
async def test_apply_kill_switch_hook_receives_primary_audit_uuid(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """Hook is invoked with the first detected-break audit UUID."""
    captured: list[ReconciliationKillSwitchContext] = []

    async def hook(ctx: ReconciliationKillSwitchContext) -> None:
        captured.append(ctx)

    plan = ReconciliationPlan(
        breaks_detected=(_make_break(market="/MES"),),
        breaks_resolved=(),
        audit_events=(_pending("reconciliation_break_detected", market="/MES"),),
        actionable_break_count=1,
        should_invoke_kill_switch=True,
    )
    result = await apply_reconciliation_plan(
        plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
        state_transition_hook=hook,
    )
    assert result.kill_switch_invoked is True
    assert len(captured) == 1
    assert captured[0].account_id == fresh_account_id
    assert captured[0].primary_audit_event_uuid == result.audit_event_uuids[0]


@pytest.mark.asyncio
async def test_apply_within_grace_period_stamps_resolution_path_grace(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """A break with within_grace_period=True gets resolution_path='grace_period' at insert."""
    plan = ReconciliationPlan(
        breaks_detected=(_make_break(within_grace_period=True),),
        breaks_resolved=(),
        audit_events=(_pending("reconciliation_break_detected"),),
        actionable_break_count=0,  # grace = not actionable
        should_invoke_kill_switch=False,
    )
    await apply_reconciliation_plan(
        plan,
        session_factory=async_session_factory,
        account_id=fresh_account_id,
        env="paper",
    )
    rows = _read_breaks(sync_engine, fresh_account_id)
    assert len(rows) == 1
    assert rows[0]["resolution_path"] == "grace_period"
