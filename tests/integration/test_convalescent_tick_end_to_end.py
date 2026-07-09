"""Integration tests for ``apply_convalescent_clean_day_tick`` (2026-07-09 amendment).

Same harness as ``test_kill_switch_end_to_end.py``: postgres:16
testcontainer, ``alembic upgrade head``, real audit chain + risk_state
schema. Exercises the production graduation journey the 00:15 UTC recon
cycle drives:

  * halt → resume → 3 clean-day ticks on distinct counted days: the two
    non-graduating ticks are plain counter UPDATEs with ZERO new audit
    rows (documented A04 deferral) and an unbroken hash chain; the 3rd
    tick graduates audit-first (``state_transition_convalescent_to_normal``
    row BEFORE the NORMAL risk_state row references it);
  * same-day re-fire idempotency via the ``convalescent_last_counted_
    day_utc`` marker;
  * the breach-day guard: a same-UTC-day halt+resume never counts the
    breach day, even though the resume-day-counts rule would otherwise
    admit it;
  * HALT_NEW no-op;
  * ``is_current`` uniqueness preserved through graduation.

Timeline control: ``apply_state_transition`` stamps ``entered_at_utc``
with the real clock, so tests re-anchor the halt/CONVALESCENT rows'
``entered_at_utc`` to fixed past dates via direct UPDATE (test-side
fixture surgery only — production rows are never hand-dated) and drive
the tick with explicit ``now_utc`` values.

A22 satisfied (real Postgres). A01 binds (audit via append_audit_event
inside the dispatcher). A02 binds (module under test is services/risk/).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
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
from services.risk.dispatch import apply_convalescent_clean_day_tick, apply_state_transition
from services.risk.state_machine import (
    RiskState,
    TransitionTrigger,
    plan_invoke_kill_switch,
    plan_resume_from_halt,
)

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer


REPO_ROOT = Path(__file__).resolve().parents[2]


def _require_docker() -> None:
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except Exception as exc:  # testcontainers raises varied types
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
async def session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def fresh_account_id(sync_engine: Engine) -> UUID:
    ext_id = f"CONV_TICK_TEST_{uuid.uuid4().hex[:12]}"
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
# Helpers
# ---------------------------------------------------------------------------


async def _halt_then_resume(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: UUID,
) -> None:
    """Drive NORMAL→HALT_NEW→CONVALESCENT via the production dispatcher."""
    async with session_factory() as session:
        halt_plan = plan_invoke_kill_switch(
            current_state=RiskState.NORMAL,
            current_severity=None,
            convalescent_counter=0,
            trigger=TransitionTrigger.DAILY_LOSS_BREACH,
            triggered_by="integration_test",
            timestamp_utc="2026-08-10T10:00:00Z",
        )
        await apply_state_transition(
            plan=halt_plan, db=session, account_id=account_id, env="paper", phase_at_emit=1
        )
    async with session_factory() as session:
        resume_plan = plan_resume_from_halt(
            current_state=RiskState.HALT_NEW,
            current_severity=halt_plan.new_severity,
            operator_session_id="sess_integration_test",
            incident_review_id=None,
            timestamp_utc="2026-08-10T20:00:00Z",
        )
        await apply_state_transition(
            plan=resume_plan, db=session, account_id=account_id, env="paper", phase_at_emit=1
        )


def _re_anchor(
    sync_engine: Engine,
    account_id: UUID,
    *,
    halt_entered: datetime,
    convalescent_entered: datetime,
) -> None:
    """Re-date the halt + CONVALESCENT rows for timeline control (docstring)."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE risk_state SET entered_at_utc = :ts "
                "WHERE account_id = :acc AND state = 'HALT_NEW'"
            ),
            {"acc": account_id, "ts": halt_entered},
        )
        conn.execute(
            text(
                "UPDATE risk_state SET entered_at_utc = :ts "
                "WHERE account_id = :acc AND state = 'CONVALESCENT' AND is_current = TRUE"
            ),
            {"acc": account_id, "ts": convalescent_entered},
        )


def _current_row(sync_engine: Engine, account_id: UUID) -> Any:
    with sync_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT state, convalescent_session_count, "
                "       convalescent_last_counted_day_utc, is_current "
                "FROM risk_state WHERE account_id = :acc AND is_current = TRUE"
            ),
            {"acc": account_id},
        ).one()


def _audit_count(sync_engine: Engine, account_id: UUID) -> int:
    with sync_engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE account_id = :acc"),
                {"acc": account_id},
            ).scalar_one()
        )


def _tick_kwargs(
    session_factory: async_sessionmaker[AsyncSession], account_id: UUID
) -> dict[str, Any]:
    return {
        "session_factory": session_factory,
        "account_id": account_id,
        "env": "paper",
        "phase_at_emit": 1,
    }


def _at_0015(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 0, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGraduationJourney:
    async def test_three_clean_days_graduate_audit_first(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: UUID,
    ) -> None:
        """Halt Aug 10, resume Aug 11 14:00. Ticks: Aug 12 counts Aug 11
        (resume day — amendment), Aug 13 counts Aug 12, Aug 14 counts
        Aug 13 -> graduation. Non-graduating ticks add ZERO audit rows."""
        await _halt_then_resume(session_factory, fresh_account_id)
        _re_anchor(
            sync_engine,
            fresh_account_id,
            halt_entered=datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
            convalescent_entered=datetime(2026, 8, 11, 14, 0, tzinfo=UTC),
        )
        audit_before = _audit_count(sync_engine, fresh_account_id)

        # Tick 1 (Aug 12 00:15 counts Aug 11 — the resume day).
        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=_at_0015(date(2026, 8, 12)),
        )
        assert applied is None
        row = _current_row(sync_engine, fresh_account_id)
        assert row.state == "CONVALESCENT"
        assert row.convalescent_session_count == 1
        assert row.convalescent_last_counted_day_utc == date(2026, 8, 11)
        assert _audit_count(sync_engine, fresh_account_id) == audit_before

        # Same-day re-fire: idempotent no-op via the marker.
        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=datetime(2026, 8, 12, 3, 0, tzinfo=UTC),
        )
        assert applied is None
        assert _current_row(sync_engine, fresh_account_id).convalescent_session_count == 1

        # Tick 2 (counts Aug 12).
        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=_at_0015(date(2026, 8, 13)),
        )
        assert applied is None
        assert _current_row(sync_engine, fresh_account_id).convalescent_session_count == 2
        assert _audit_count(sync_engine, fresh_account_id) == audit_before

        # Tick 3 (counts Aug 13) -> graduation, audit-first.
        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=_at_0015(date(2026, 8, 14)),
        )
        assert applied is not None
        assert applied.new_state == "NORMAL"
        row = _current_row(sync_engine, fresh_account_id)
        assert row.state == "NORMAL"
        assert row.convalescent_session_count == 0
        assert row.convalescent_last_counted_day_utc is None
        assert _audit_count(sync_engine, fresh_account_id) == audit_before + 1

        # Audit-first linkage: the NORMAL row references the graduation event.
        with sync_engine.connect() as conn:
            audit_row = conn.execute(
                text(
                    "SELECT event_type, payload_jcs FROM audit_log "
                    "WHERE event_uuid = ("
                    "  SELECT audit_event_uuid FROM risk_state "
                    "  WHERE account_id = :acc AND is_current = TRUE)"
                ),
                {"acc": fresh_account_id},
            ).one()
        assert audit_row.event_type == "state_transition_convalescent_to_normal"
        assert b'"convalescent_sessions_completed":3' in bytes(audit_row.payload_jcs)

        # is_current uniqueness held throughout.
        with sync_engine.connect() as conn:
            current = conn.execute(
                text(
                    "SELECT COUNT(*) FROM risk_state WHERE account_id = :acc AND is_current = TRUE"
                ),
                {"acc": fresh_account_id},
            ).scalar_one()
        assert current == 1

        # Hash chain intact end-to-end.
        async with session_factory() as session:
            ok, broken_at, rows_walked = await verify_chain(session)
        assert ok, f"hash chain broken at sequence_no={broken_at} after {rows_walked} rows"
        assert rows_walked >= audit_before + 1


class TestGuards:
    async def test_breach_day_never_counts_with_same_day_resume(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: UUID,
    ) -> None:
        """Halt AND resume on Aug 10: the Aug-11-00:15 tick (counting
        Aug 10) must skip (breach_day); the Aug-12 tick counts Aug 11."""
        await _halt_then_resume(session_factory, fresh_account_id)
        _re_anchor(
            sync_engine,
            fresh_account_id,
            halt_entered=datetime(2026, 8, 10, 0, 30, tzinfo=UTC),
            convalescent_entered=datetime(2026, 8, 10, 20, 0, tzinfo=UTC),
        )

        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=_at_0015(date(2026, 8, 11)),
        )
        assert applied is None
        row = _current_row(sync_engine, fresh_account_id)
        assert row.convalescent_session_count == 0
        assert row.convalescent_last_counted_day_utc is None

        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=_at_0015(date(2026, 8, 12)),
        )
        assert applied is None
        assert _current_row(sync_engine, fresh_account_id).convalescent_session_count == 1

    async def test_halted_account_never_ticks(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: UUID,
    ) -> None:
        """State HALT_NEW at tick time -> no-op, counter untouched."""
        async with session_factory() as session:
            halt_plan = plan_invoke_kill_switch(
                current_state=RiskState.NORMAL,
                current_severity=None,
                convalescent_counter=0,
                trigger=TransitionTrigger.DAILY_LOSS_BREACH,
                triggered_by="integration_test",
                timestamp_utc="2026-08-10T10:00:00Z",
            )
            await apply_state_transition(
                plan=halt_plan,
                db=session,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=1,
            )
        audit_before = _audit_count(sync_engine, fresh_account_id)

        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=_at_0015(date(2026, 8, 12)),
        )
        assert applied is None
        row = _current_row(sync_engine, fresh_account_id)
        assert row.state == "HALT_NEW"
        assert row.convalescent_session_count == 0
        assert _audit_count(sync_engine, fresh_account_id) == audit_before

    async def test_no_current_row_is_noop(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        applied = await apply_convalescent_clean_day_tick(
            **_tick_kwargs(session_factory, fresh_account_id),
            now_utc=_at_0015(date(2026, 8, 12)),
        )
        assert applied is None


class TestSchema:
    def test_marker_column_is_nullable_date(self, sync_engine: Engine) -> None:
        """The 20260710_conv_clean_day migration's column contract."""
        with sync_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'risk_state' "
                    "AND column_name = 'convalescent_last_counted_day_utc'"
                )
            ).one()
        assert row.data_type == "date"
        assert row.is_nullable == "YES"
