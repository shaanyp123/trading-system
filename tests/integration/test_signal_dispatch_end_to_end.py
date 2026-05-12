"""Integration tests for ``services.risk.signal_dispatch.apply_signal_dispatch``.

Pivot-PR-D (post-pivot 2026-05-12). Spins up a fresh ``postgres:16``
testcontainer per module, runs ``alembic upgrade head``, then exercises
the I/O orchestrator against the real ``signals`` + ``audit_log`` schema.

**Regression guard: column-name bug.** The first iteration of
``signal_dispatch.apply_signal_dispatch`` used ``WHERE signal_id = :sid``
against the ``signals`` table — the PK column is actually ``id``
(``signal_id`` is the FK column name on the ``orders`` table). Pure-policy
planner tests didn't catch this; the bug only surfaced when the I/O
orchestrator hit a real Postgres. This test exercises the full SQL update
path so the regression can't ship again silently.

**Schema mapping verified by this test** (alembic 0002 + the
Pivot-PR-D dispatcher mapping):

  * approve  →  status='approved'  AND approved_at_utc set
  * reject   →  status='rejected'  AND rejected_at_utc set
  * defer    →  status='deferred'  (no timestamp column today)

Anti-pattern A22 binds + is satisfied here (real Postgres triggers +
column types + grant matrix exercised). A01 binds — every dispatch
writes its audit event through :func:`append_audit_event`. A02 binds —
the module under test lives at ``services/risk/signal_dispatch.py``.

If Docker is unreachable, the module skips cleanly (same pattern as
``test_kill_switch_end_to_end.py``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
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

from services.risk.signal_dispatch import (
    DecisionDiaryEntryInput,
    SignalDispatchError,
    apply_signal_dispatch,
    plan_signal_approve,
    plan_signal_defer,
    plan_signal_reject,
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
    ext_id = f"SIGDISPATCH_TEST_{uuid.uuid4().hex[:12]}"
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
    """Insert a bootstrap slippage_calibration_versions row.

    Schema-aligned with alembic 0003: version_no UNIQUE / is_head + the
    `slippage_head` partial unique index. The fixture is per-test
    (function-scoped) so each test gets a fresh row — the slippage_head
    partial unique index would otherwise raise IntegrityError on the
    second is_head=TRUE insert.

    Mirror of `services.qc_adapter.db.insert_slippage_bootstrap_row` but
    sync (psycopg2 path) so it can run as a pytest fixture.
    """
    with sync_engine.begin() as conn:
        # Generate a fresh version_no per call (UNIQUE constraint) +
        # a fresh is_head row (partial unique index requires only one
        # is_head=TRUE row globally — flip prior heads off first).
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
                "    :audit, 'test fixture'"
                ") RETURNING id"
            ),
            {"ver": next_version_no, "audit": uuid.uuid4()},
        ).scalar_one()
    return UUID(str(slip_id))


def _insert_pending_signal(
    sync_engine: Engine,
    *,
    account_id: UUID,
    slippage_head_version_id: UUID,
    market: str = "/MES",
    direction: str = "long",
) -> UUID:
    """Insert a `pending` signals row + return its id."""
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
                "    :direction, 'donchian_breakout',"
                "    '0000000000000000000000000000000000000001',"
                "    '0000000000000000000000000000000000000000000000000000000000000001',"
                "    :slip, 5250.00, 1,"
                "    '{}'::jsonb, 'pending'"
                ") RETURNING id"
            ),
            {
                "acct": account_id,
                "market": market,
                "direction": direction,
                "slip": slippage_head_version_id,
            },
        ).scalar_one()
    return UUID(str(signal_id))


def _read_signal(sync_engine: Engine, signal_id: UUID) -> dict[str, Any]:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, approved_at_utc, rejected_at_utc FROM signals WHERE id = :sid"),
            {"sid": signal_id},
        ).one()
    return {
        "status": row.status,
        "approved_at_utc": row.approved_at_utc,
        "rejected_at_utc": row.rejected_at_utc,
    }


# ---------------------------------------------------------------------------
# Happy-path tests — exercise the full audit + UPDATE path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_signal_dispatch_approve_updates_status_and_approved_at(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """Approve path flips status='approved' AND populates approved_at_utc."""
    signal_id = _insert_pending_signal(
        sync_engine,
        account_id=fresh_account_id,
        slippage_head_version_id=slippage_head_version_id,
    )

    decided_at = datetime(2026, 5, 12, 18, 0, tzinfo=UTC)
    plan = plan_signal_approve(
        signal_id=signal_id,
        account_id=fresh_account_id,
        decided_by_user_id="operator",
        override_size=None,
        decided_at_utc=decided_at,
    )

    result = await apply_signal_dispatch(
        plan,
        session_factory=async_session_factory,
        env="paper",
        phase_at_emit=1,
    )

    assert result.new_status == "approved"
    assert result.signal_id == signal_id

    row = _read_signal(sync_engine, signal_id)
    assert row["status"] == "approved"
    assert row["approved_at_utc"] is not None
    # tz-aware compare — Postgres TIMESTAMPTZ round-trips with UTC offset
    assert row["approved_at_utc"].astimezone(UTC) == decided_at
    assert row["rejected_at_utc"] is None


@pytest.mark.asyncio
async def test_apply_signal_dispatch_reject_updates_status_and_rejected_at(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """Reject path flips status='rejected' AND populates rejected_at_utc."""
    signal_id = _insert_pending_signal(
        sync_engine,
        account_id=fresh_account_id,
        slippage_head_version_id=slippage_head_version_id,
    )

    decided_at = datetime(2026, 5, 12, 18, 5, tzinfo=UTC)
    plan = plan_signal_reject(
        signal_id=signal_id,
        account_id=fresh_account_id,
        decided_by_user_id="operator",
        diary_entry=DecisionDiaryEntryInput(
            tag="regime_concern",
            reasoning_text="elevated VIX, post-FOMC reaction unclear",
        ),
        decided_at_utc=decided_at,
    )

    result = await apply_signal_dispatch(
        plan,
        session_factory=async_session_factory,
        env="paper",
        phase_at_emit=1,
    )

    assert result.new_status == "rejected"

    row = _read_signal(sync_engine, signal_id)
    assert row["status"] == "rejected"
    assert row["rejected_at_utc"] is not None
    assert row["rejected_at_utc"].astimezone(UTC) == decided_at
    assert row["approved_at_utc"] is None


@pytest.mark.asyncio
async def test_apply_signal_dispatch_defer_updates_status_only(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """Defer path flips status='deferred'; no timestamp column exists for defer today."""
    signal_id = _insert_pending_signal(
        sync_engine,
        account_id=fresh_account_id,
        slippage_head_version_id=slippage_head_version_id,
    )

    plan = plan_signal_defer(
        signal_id=signal_id,
        account_id=fresh_account_id,
        decided_by_user_id="operator",
        diary_entry=DecisionDiaryEntryInput(
            tag="data_concern",
            reasoning_text="bar gap on /MES yesterday — defer until next session",
        ),
    )

    result = await apply_signal_dispatch(
        plan,
        session_factory=async_session_factory,
        env="paper",
        phase_at_emit=1,
    )

    assert result.new_status == "deferred"

    row = _read_signal(sync_engine, signal_id)
    assert row["status"] == "deferred"
    assert row["approved_at_utc"] is None
    assert row["rejected_at_utc"] is None


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_signal_dispatch_signal_not_found(
    async_session_factory: async_sessionmaker[AsyncSession],
    fresh_account_id: UUID,
) -> None:
    """Unknown signal_id surfaces SIGNAL_NOT_FOUND via the validation step."""
    ghost_id = uuid.uuid4()
    plan = plan_signal_approve(
        signal_id=ghost_id,
        account_id=fresh_account_id,
        decided_by_user_id="operator",
        override_size=None,
    )

    with pytest.raises(SignalDispatchError) as exc_info:
        await apply_signal_dispatch(
            plan,
            session_factory=async_session_factory,
            env="paper",
            phase_at_emit=1,
        )
    assert exc_info.value.error_code == "SIGNAL_NOT_FOUND"


@pytest.mark.asyncio
async def test_apply_signal_dispatch_signal_already_approved_blocks_re_dispatch(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    slippage_head_version_id: UUID,
) -> None:
    """Approving an already-approved signal raises SIGNAL_NOT_PENDING."""
    signal_id = _insert_pending_signal(
        sync_engine,
        account_id=fresh_account_id,
        slippage_head_version_id=slippage_head_version_id,
    )

    # First approve succeeds.
    plan_first = plan_signal_approve(
        signal_id=signal_id,
        account_id=fresh_account_id,
        decided_by_user_id="operator",
        override_size=None,
    )
    await apply_signal_dispatch(
        plan_first,
        session_factory=async_session_factory,
        env="paper",
        phase_at_emit=1,
    )

    # Second approve hits SIGNAL_NOT_PENDING.
    plan_second = plan_signal_approve(
        signal_id=signal_id,
        account_id=fresh_account_id,
        decided_by_user_id="operator",
        override_size=2,
    )
    with pytest.raises(SignalDispatchError) as exc_info:
        await apply_signal_dispatch(
            plan_second,
            session_factory=async_session_factory,
            env="paper",
            phase_at_emit=1,
        )
    assert exc_info.value.error_code == "SIGNAL_NOT_PENDING"
