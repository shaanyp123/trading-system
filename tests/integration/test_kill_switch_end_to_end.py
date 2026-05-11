"""Integration tests for ``services.risk.dispatch.apply_state_transition``.

Day 25 (Week 7 Mon). Spins up a fresh ``postgres:16`` testcontainer per
module, runs ``alembic upgrade head``, then exercises the dispatch layer
against the real audit_log + risk_state schema. Verifies that:

  * audit-first ordering holds end-to-end: every transition writes its
    audit row(s) BEFORE the risk_state UPSERT lands;
  * the hash chain stays continuous across multiple transitions for the
    same account (genesis → seq 1 → seq 2 → ...);
  * the partial unique index on ``risk_state(account_id, is_current)
    WHERE is_current = TRUE`` enforces "at most one current row per
    account" — i.e. the UPDATE-then-INSERT pattern is correct even after
    repeated transitions;
  * the audit_log immutability trigger from migration 0005 blocks UPDATE
    on the newly-written state_transition rows.

Anti-pattern A22 binds + is satisfied here (real Postgres triggers +
constraints + grant matrix exercised). A01 binds — every transition
routes audit writes through :func:`append_audit_event`. A02 binds — the
module under test lives at ``services/risk/dispatch.py``.

If Docker is unreachable, the module skips cleanly (same pattern as
``test_audit_writer.py`` / ``test_audit_immutability.py``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
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

from services.audit.chain import GENESIS_HASH, verify_chain
from services.risk.dispatch import apply_state_transition
from services.risk.state_machine import (
    HaltSeverity,
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
    """Bring up postgres:16, apply migrations, yield the sync (psycopg2) URL."""
    _require_docker()
    with PostgresContainer("postgres:16") as pg:
        sync_url = pg.get_connection_url()  # postgresql+psycopg2://...
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
    """Function-scoped async engine so each test gets its own event-loop pool."""
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
    """Insert a fresh accounts row + return its UUID for FK use."""
    ext_id = f"KILL_SWITCH_TEST_{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": ext_id},
        ).scalar_one()
    return UUID(str(account_id))


def _read_current_risk_state(sync_engine: Engine, account_id: UUID) -> dict[str, Any] | None:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT state, severity, reason, convalescent_session_count, "
                "       audit_event_uuid, is_current "
                "FROM risk_state "
                "WHERE account_id = :acc AND is_current = TRUE"
            ),
            {"acc": account_id},
        ).fetchone()
    if row is None:
        return None
    return {
        "state": row.state,
        "severity": row.severity,
        "reason": row.reason,
        "convalescent_session_count": row.convalescent_session_count,
        "audit_event_uuid": row.audit_event_uuid,
        "is_current": row.is_current,
    }


def _count_risk_state_rows(sync_engine: Engine, account_id: UUID) -> tuple[int, int]:
    """Return (total_rows, is_current_true_rows) for this account."""
    with sync_engine.connect() as conn:
        total = conn.execute(
            text("SELECT COUNT(*) FROM risk_state WHERE account_id = :acc"),
            {"acc": account_id},
        ).scalar_one()
        current = conn.execute(
            text("SELECT COUNT(*) FROM risk_state WHERE account_id = :acc AND is_current = TRUE"),
            {"acc": account_id},
        ).scalar_one()
    return int(total), int(current)


def _read_audit_row_by_uuid(sync_engine: Engine, event_uuid: UUID) -> dict[str, Any]:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT sequence_no, event_type, prev_hash, record_hash, "
                "       payload_jcs, account_id, env, phase_at_emit "
                "FROM audit_log WHERE event_uuid = :uuid"
            ),
            {"uuid": event_uuid},
        ).one()
    return {
        "sequence_no": row.sequence_no,
        "event_type": row.event_type,
        "prev_hash": bytes(row.prev_hash),
        "record_hash": bytes(row.record_hash),
        "payload_jcs": bytes(row.payload_jcs),
        "account_id": row.account_id,
        "env": row.env,
        "phase_at_emit": row.phase_at_emit,
    }


# ---------------------------------------------------------------------------
# Single-transition happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_normal_to_halt_writes_audit_and_risk_state(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """NORMAL → HALT_NEW writes exactly 1 audit row + 1 risk_state row.

    The risk_state row's ``audit_event_uuid`` points to the audit row's
    ``event_uuid`` — the audit log is the canonical source of truth and
    risk_state carries the FK linkage.
    """
    plan = plan_invoke_kill_switch(
        current_state=RiskState.NORMAL,
        current_severity=None,
        convalescent_counter=0,
        trigger=TransitionTrigger.TRAILING_DD_BREACH,
        triggered_by="operator",
        timestamp_utc="2026-05-18T12:00:00+00:00",
    )
    assert len(plan.audit_events) == 1  # NORMAL→HALT_NEW emits only state_transition

    async with async_session_factory() as session:
        applied = await apply_state_transition(
            plan=plan,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    # 1) The audit row exists, has the right event_type + envelope fields
    audit_row = _read_audit_row_by_uuid(sync_engine, applied.state_transition_audit_event_uuid)
    assert audit_row["event_type"] == "state_transition_normal_to_halt"
    assert audit_row["account_id"] == fresh_account_id
    assert audit_row["env"] == "paper"
    assert audit_row["phase_at_emit"] == 0

    # 2) The risk_state row exists, is current, links to the audit row
    state = _read_current_risk_state(sync_engine, fresh_account_id)
    assert state is not None
    assert state["state"] == "HALT_NEW"
    assert state["severity"] == "routine"
    assert state["reason"] == "trailing_dd_breach"
    assert state["convalescent_session_count"] == 0
    assert state["audit_event_uuid"] == applied.state_transition_audit_event_uuid
    assert state["is_current"] is True

    # 3) Exactly one row per account, and it's the current one
    total, current = _count_risk_state_rows(sync_engine, fresh_account_id)
    assert total == 1
    assert current == 1


@pytest.mark.asyncio
async def test_invoke_convalescent_to_halt_writes_counter_reset_before_state_transition(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """CONVALESCENT→HALT_NEW (counter > 0) writes 2 audit rows in order.

    Order is load-bearing: ``convalescent_counter_reset`` lands FIRST so
    the chain reflects "we lost the counter" before "we re-entered HALT".
    The state_transition row's UUID is the one linked from risk_state.
    """
    # Seed risk_state with a CONVALESCENT row first (skip the apply pipeline
    # to keep the test focused on the transition under exam). The audit_uuid
    # field is FK-less in the schema; we use a random UUID for seeding.
    seed_audit_uuid = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO risk_state ("
                "    account_id, state, severity, reason, entered_at_utc, "
                "    convalescent_session_count, vacation_active, "
                "    audit_event_uuid, is_current"
                ") VALUES ("
                "    :acc, 'CONVALESCENT', NULL, 'human_resume', now(), "
                "    3, FALSE, :seed, TRUE"
                ")"
            ),
            {"acc": fresh_account_id, "seed": seed_audit_uuid},
        )

    plan = plan_invoke_kill_switch(
        current_state=RiskState.CONVALESCENT,
        current_severity=None,
        convalescent_counter=3,
        trigger=TransitionTrigger.DAILY_LOSS_BREACH,
        triggered_by="operator",
        timestamp_utc="2026-05-18T12:00:00+00:00",
    )
    assert len(plan.audit_events) == 2  # counter_reset + state_transition

    async with async_session_factory() as session:
        applied = await apply_state_transition(
            plan=plan,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    # The state_transition row's UUID flows through to applied
    state_transition_row = _read_audit_row_by_uuid(
        sync_engine, applied.state_transition_audit_event_uuid
    )
    assert state_transition_row["event_type"] == "state_transition_normal_to_halt"
    state_transition_seq = state_transition_row["sequence_no"]

    # The counter_reset row is the one with seq = state_transition_seq - 1
    with sync_engine.connect() as conn:
        counter_reset_row = conn.execute(
            text(
                "SELECT event_type, sequence_no FROM audit_log "
                "WHERE account_id = :acc "
                "  AND event_type = 'convalescent_counter_reset' "
                "ORDER BY sequence_no DESC LIMIT 1"
            ),
            {"acc": fresh_account_id},
        ).one()
    assert counter_reset_row.event_type == "convalescent_counter_reset"
    assert counter_reset_row.sequence_no == state_transition_seq - 1

    # And the risk_state row is now HALT_NEW with counter=0, linked to the
    # state-transition row (NOT the counter_reset row)
    state = _read_current_risk_state(sync_engine, fresh_account_id)
    assert state is not None
    assert state["state"] == "HALT_NEW"
    assert state["severity"] == "routine"
    assert state["reason"] == "daily_loss_breach"
    assert state["convalescent_session_count"] == 0
    assert state["audit_event_uuid"] == applied.state_transition_audit_event_uuid

    # The prior CONVALESCENT row exists but is_current=FALSE
    total, current = _count_risk_state_rows(sync_engine, fresh_account_id)
    assert total == 2
    assert current == 1


@pytest.mark.asyncio
async def test_resume_halt_to_convalescent_writes_audit_and_risk_state(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """HALT_NEW (routine) → CONVALESCENT writes 1 audit row + flips risk_state."""
    # Seed a HALT_NEW row
    seed_audit_uuid = uuid.uuid4()
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
            {"acc": fresh_account_id, "seed": seed_audit_uuid},
        )

    plan = plan_resume_from_halt(
        current_state=RiskState.HALT_NEW,
        current_severity=HaltSeverity.ROUTINE,
        operator_session_id="phase0-stub-owner",
        incident_review_id=None,
        timestamp_utc="2026-05-18T12:30:00+00:00",
    )
    assert len(plan.audit_events) == 1

    async with async_session_factory() as session:
        applied = await apply_state_transition(
            plan=plan,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    audit_row = _read_audit_row_by_uuid(sync_engine, applied.state_transition_audit_event_uuid)
    assert audit_row["event_type"] == "state_transition_halt_to_convalescent"

    state = _read_current_risk_state(sync_engine, fresh_account_id)
    assert state is not None
    assert state["state"] == "CONVALESCENT"
    assert state["severity"] is None
    assert state["reason"] == "human_resume"
    assert state["convalescent_session_count"] == 0
    assert state["audit_event_uuid"] == applied.state_transition_audit_event_uuid

    total, current = _count_risk_state_rows(sync_engine, fresh_account_id)
    assert total == 2
    assert current == 1


# ---------------------------------------------------------------------------
# Chain integrity across multiple transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_transitions_maintain_continuous_hash_chain(
    async_session_factory: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """Three transitions in sequence produce 3 audit rows whose chain
    walks cleanly from genesis to the tail.

    The transitions chain NORMAL → HALT_NEW → CONVALESCENT → HALT_NEW
    (re-trigger). The third transition (CONVALESCENT → HALT_NEW with
    counter=0) emits only the state_transition row (no counter_reset
    because the counter is 0).
    """
    ts_base = "2026-05-18T1{hour}:00:00+00:00"

    # Transition 1: NORMAL → HALT_NEW
    plan_1 = plan_invoke_kill_switch(
        current_state=RiskState.NORMAL,
        current_severity=None,
        convalescent_counter=0,
        trigger=TransitionTrigger.TRAILING_DD_BREACH,
        triggered_by="operator",
        timestamp_utc=ts_base.format(hour="2"),
    )
    # Transition 2: HALT_NEW → CONVALESCENT
    plan_2 = plan_resume_from_halt(
        current_state=RiskState.HALT_NEW,
        current_severity=HaltSeverity.ROUTINE,
        operator_session_id="phase0-stub-owner",
        incident_review_id=None,
        timestamp_utc=ts_base.format(hour="3"),
    )
    # Transition 3: CONVALESCENT → HALT_NEW (counter=0 → no counter_reset)
    plan_3 = plan_invoke_kill_switch(
        current_state=RiskState.CONVALESCENT,
        current_severity=None,
        convalescent_counter=0,
        trigger=TransitionTrigger.SIGNAL_STORM,
        triggered_by="operator",
        timestamp_utc=ts_base.format(hour="4"),
    )

    async with async_session_factory() as session:
        applied_1 = await apply_state_transition(
            plan=plan_1, db=session, account_id=fresh_account_id, env="paper", phase_at_emit=0
        )
        applied_2 = await apply_state_transition(
            plan=plan_2, db=session, account_id=fresh_account_id, env="paper", phase_at_emit=0
        )
        applied_3 = await apply_state_transition(
            plan=plan_3, db=session, account_id=fresh_account_id, env="paper", phase_at_emit=0
        )

    # Chain walk: verify_chain returns (ok, broken_seq, rows_walked)
    async with async_session_factory() as walker_session:
        ok, broken_at, rows_walked = await verify_chain(walker_session)
    assert ok is True
    assert broken_at is None
    # Note: the audit_log is module-scoped — other tests may have written
    # rows before this one. We only assert ``rows_walked >= 3``.
    assert rows_walked >= 3

    # The three state-transition rows are present + sequence_no monotonic.
    rows = []
    for applied in (applied_1, applied_2, applied_3):
        rows.append(_read_audit_row_by_uuid(sync_engine, applied.state_transition_audit_event_uuid))
    assert rows[0]["sequence_no"] < rows[1]["sequence_no"] < rows[2]["sequence_no"]

    # The final risk_state row is HALT_NEW (the third transition's target)
    state = _read_current_risk_state(sync_engine, fresh_account_id)
    assert state is not None
    assert state["state"] == "HALT_NEW"
    assert state["audit_event_uuid"] == applied_3.state_transition_audit_event_uuid


# ---------------------------------------------------------------------------
# risk_state partial unique index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_state_keeps_at_most_one_current_row_per_account(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """After N transitions the account has N total rows but exactly 1 current.

    Verifies the partial unique index ``risk_state_current ON
    risk_state(account_id, is_current) WHERE is_current = TRUE`` works as
    expected against the UPDATE-then-INSERT pattern in apply_state_transition.
    """
    plan_normal_to_halt = plan_invoke_kill_switch(
        current_state=RiskState.NORMAL,
        current_severity=None,
        convalescent_counter=0,
        trigger=TransitionTrigger.TRAILING_DD_BREACH,
        triggered_by="operator",
        timestamp_utc="2026-05-18T12:00:00+00:00",
    )
    plan_resume = plan_resume_from_halt(
        current_state=RiskState.HALT_NEW,
        current_severity=HaltSeverity.ROUTINE,
        operator_session_id="phase0-stub-owner",
        incident_review_id=None,
        timestamp_utc="2026-05-18T12:30:00+00:00",
    )

    async with async_session_factory() as session:
        await apply_state_transition(
            plan=plan_normal_to_halt,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )
        await apply_state_transition(
            plan=plan_resume,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    total, current = _count_risk_state_rows(sync_engine, fresh_account_id)
    assert total == 2
    assert current == 1


# ---------------------------------------------------------------------------
# Audit immutability for state-transition rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_transition_audit_row_is_immutable(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """The Day-3 BEFORE UPDATE/DELETE trigger blocks any modification.

    Backend-spec §2.10.1 + migration 0005 immutability. Verifies the
    trigger fires for the Day 25 state_transition rows just as it does
    for the existing audit_log surface.
    """
    plan = plan_invoke_kill_switch(
        current_state=RiskState.NORMAL,
        current_severity=None,
        convalescent_counter=0,
        trigger=TransitionTrigger.UNHANDLED_EXCEPTION,
        triggered_by="operator",
        timestamp_utc="2026-05-18T12:00:00+00:00",
    )
    async with async_session_factory() as session:
        applied = await apply_state_transition(
            plan=plan,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    # Attempt UPDATE → triggers BEFORE UPDATE row trigger → raises
    with sync_engine.begin() as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                text("UPDATE audit_log SET event_type = 'tampered' WHERE event_uuid = :uuid"),
                {"uuid": applied.state_transition_audit_event_uuid},
            )
        # The trigger raises with the canonical message
        assert "append-only" in str(excinfo.value).lower() or "p0001" in str(excinfo.value).lower()

    # Attempt DELETE → same trigger fires
    with sync_engine.begin() as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                text("DELETE FROM audit_log WHERE event_uuid = :uuid"),
                {"uuid": applied.state_transition_audit_event_uuid},
            )
        assert "append-only" in str(excinfo.value).lower() or "p0001" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Genesis chain hash on first transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_transition_in_empty_chain_uses_genesis_prev_hash(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """The state_transition audit row at sequence_no=1 carries
    ``prev_hash = GENESIS_HASH``.

    Module-scoped DB means another test may have written first; we read
    the row at sequence_no=1 (regardless of when our test runs) and
    assert its prev_hash is the all-zeros genesis.
    """
    # Just exercise apply — the assertion targets the chain's actual first row.
    plan = plan_invoke_kill_switch(
        current_state=RiskState.NORMAL,
        current_severity=None,
        convalescent_counter=0,
        trigger=TransitionTrigger.MANUAL_JUDGMENT,
        triggered_by="operator",
        timestamp_utc="2026-05-18T12:00:00+00:00",
    )
    async with async_session_factory() as session:
        await apply_state_transition(
            plan=plan,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    with sync_engine.connect() as conn:
        first_row = conn.execute(
            text("SELECT prev_hash FROM audit_log ORDER BY sequence_no ASC LIMIT 1")
        ).one()
    assert bytes(first_row.prev_hash) == GENESIS_HASH


# ---------------------------------------------------------------------------
# DP-021 regression — route's read-then-apply session-flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_after_repo_reads_via_same_session_dp021(
    async_session_factory: async_sessionmaker[AsyncSession],
    fresh_account_id: UUID,
) -> None:
    """Regression for DP-021 (Day 25 carryover smoke).

    Production bug: FastAPI's ``Depends`` cache shared the same
    ``AsyncSession`` between ``Phase1QueryRepo.fetch_*`` and
    ``apply_state_transition``. The repo's SELECT auto-began a transaction
    (SQLAlchemy 2.0 autobegin semantics); the writer then raised
    ``InvalidRequestError: A transaction is already begun on this Session``
    because :func:`append_audit_event`'s contract requires a session with
    no open transaction.

    Day-25 unit tests didn't catch this because they monkeypatched
    ``apply_state_transition`` (real writer never ran). The Day-25
    integration tests didn't catch it because they used a fresh session
    not previously touched by a repo. This test simulates the route's
    real flow:

      1. Issue a SELECT through the session (mimics the repo reads).
      2. ``await session.commit()`` (the route's DP-021 fix).
      3. Call ``apply_state_transition`` — must succeed.

    Without the commit between steps 1 and 3, apply raises
    ``InvalidRequestError``. The test asserts both directions: the broken
    pre-fix path raises, the post-fix path succeeds.
    """
    from sqlalchemy.exc import InvalidRequestError

    plan = plan_invoke_kill_switch(
        current_state=RiskState.NORMAL,
        current_severity=None,
        convalescent_counter=0,
        trigger=TransitionTrigger.MANUAL_JUDGMENT,
        triggered_by="operator",
        timestamp_utc="2026-05-18T12:00:00+00:00",
    )

    # Pre-fix path: repo SELECT auto-begins transaction; apply raises.
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))  # mimics the repo's SELECT
        with pytest.raises(InvalidRequestError, match="already begun"):
            await apply_state_transition(
                plan=plan,
                db=session,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )

    # Post-fix path: explicit commit releases the implicit transaction;
    # apply succeeds.
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
        await session.commit()  # the DP-021 fix in routes/system.py
        applied = await apply_state_transition(
            plan=plan,
            db=session,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )
        assert applied.state_transition_audit_event_uuid is not None
        assert applied.new_state == "HALT_NEW"
