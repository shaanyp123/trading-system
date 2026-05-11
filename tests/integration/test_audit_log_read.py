"""Integration tests for :meth:`PostgresPhase1QueryRepo.fetch_audit_log_page`.

Day 27 (Week 7 Wed): the read-side of the ``/system`` audit explorer. The
unit tests in ``tests/unit/test_api_phase1_routes.py::TestAuditLogTable``
exercise the route + mapper against a stub repo; this module locks the
SQL contract of the production Postgres repo against a real database.

Why this is A22 territory:

  * Postgres-side partition pruning on ``ingest_clock_ts`` only fires when
    the WHERE clause is bound at plan time — a unit test with a stub repo
    can't catch a regression where someone writes the SQL such that the
    partition manager scans every yearly partition. The integration test
    inserts a row + runs an EXPLAIN-equivalent assertion (we assert the
    row count + ordering, which exercises the SQL end-to-end).
  * BYTEA → ``bytes`` coercion in SQLAlchemy depends on the driver
    (asyncpg returns ``memoryview`` while psycopg2 returns ``bytes``);
    locking this in the repo via ``bytes(r.prev_hash)`` is integration-
    tested here so the routing layer's ``.hex()`` call can't crash on a
    surprise type.
  * Cursor pagination has a ``has_more`` boundary the unit test can fake
    but real Postgres reveals the off-by-one (we fetch ``limit + 1``
    rows; if that math regresses, the integration test fails).

Skip behavior matches ``tests/integration/test_audit_writer.py``: if
Docker is unreachable, the module skips cleanly. Tests are sequential
(class-scoped fixtures) because the audit_log accumulates rows across
inserts and each test reads the chain tail before asserting against
its own writes.
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

from services.api.repos.phase1 import PostgresPhase1QueryRepo
from services.audit.event_types import AuditEventType
from services.audit.writer import append_audit_event

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
    ext_id = f"AUDIT_READ_TEST_{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": ext_id},
        ).scalar_one()
    return UUID(str(account_id))


async def _seed_three_rows(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: UUID,
) -> list[int]:
    """Append three rows of different event_types; return their sequence_nos."""
    seqs: list[int] = []
    payloads = [
        ("system_started", {"version": "test-1", "phase": 0}),
        (
            "state_transition_normal_to_halt",
            {"reason": "trailing_dd_breach", "triggered_by": "operator"},
        ),
        ("reconciliation_check_passed", {"check_id": "rec-001", "open_breaks": 0}),
    ]
    event_types = [
        AuditEventType.SYSTEM_STARTED,
        AuditEventType.STATE_TRANSITION_NORMAL_TO_HALT,
        AuditEventType.RECONCILIATION_CHECK_PASSED,
    ]
    async with session_factory() as session:
        for event_type, (_, payload) in zip(event_types, payloads, strict=True):
            record = await append_audit_event(
                session,
                event_type,
                payload,
                account_id=account_id,
                env="paper",
                phase_at_emit=0,
            )
            seqs.append(record.sequence_no)
    return seqs


class TestEmptyAndDataShape:
    """Smoke tests over the empty + single-row shape."""

    @pytest.mark.asyncio
    async def test_no_filter_returns_recent_rows_desc(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # Seed three rows; verify they come back in DESC sequence_no order.
        seqs = await _seed_three_rows(async_session_factory, fresh_account_id)
        assert sorted(seqs) == seqs  # appended monotonically

        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, _next_cursor, _has_more = await repo.fetch_audit_log_page(
                event_type=None,
                env=None,
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=100,
            )

        # Should include AT LEAST our three (other tests may have written more
        # against this module-scoped DB). Assert our three are present in
        # DESC order relative to each other.
        seq_set = {r.sequence_no for r in rows}
        for s in seqs:
            assert s in seq_set, f"seeded seq {s} missing from page"
        # DESC ordering: locate ours and verify the relative order.
        positions = {r.sequence_no: i for i, r in enumerate(rows)}
        assert positions[seqs[0]] > positions[seqs[1]] > positions[seqs[2]]

    @pytest.mark.asyncio
    async def test_hashes_are_bytes_not_memoryview(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # Driver-level: asyncpg returns memoryview for BYTEA by default; the
        # repo wraps with ``bytes(...)`` so the route's ``.hex()`` call
        # works. This locks in that conversion.
        await _seed_three_rows(async_session_factory, fresh_account_id)
        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, _, _ = await repo.fetch_audit_log_page(
                event_type=None,
                env=None,
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=1,
            )
        assert len(rows) == 1
        assert isinstance(rows[0].prev_hash, bytes)
        assert isinstance(rows[0].record_hash, bytes)
        assert isinstance(rows[0].payload_jcs, bytes)
        assert len(rows[0].prev_hash) == 32
        assert len(rows[0].record_hash) == 32
        # JCS payload should round-trip through .hex() without crashing
        _ = rows[0].record_hash.hex()


class TestFilterPassthrough:
    """Each WHERE branch is exercised end-to-end against real Postgres."""

    @pytest.mark.asyncio
    async def test_event_type_filter(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        seqs = await _seed_three_rows(async_session_factory, fresh_account_id)

        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, _, _ = await repo.fetch_audit_log_page(
                event_type="reconciliation_check_passed",
                env=None,
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=100,
            )

        # Every row in the page must match the filter — proves the WHERE
        # branch is actually applied (regression: forgetting ``AND
        # event_type = :et`` would silently return everything).
        assert len(rows) >= 1
        for r in rows:
            assert r.event_type == "reconciliation_check_passed"
        # And our seeded recon row is in there.
        assert seqs[2] in {r.sequence_no for r in rows}

    @pytest.mark.asyncio
    async def test_env_filter(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        await _seed_three_rows(async_session_factory, fresh_account_id)
        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, _, _ = await repo.fetch_audit_log_page(
                event_type=None,
                env="paper",
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=100,
            )
        assert len(rows) >= 1
        for r in rows:
            assert r.env == "paper"

    @pytest.mark.asyncio
    async def test_env_filter_excludes_other_envs(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # Phase-0 paper env only writes 'paper'; asking for live-small must
        # return zero seeded rows from this module-scoped DB.
        await _seed_three_rows(async_session_factory, fresh_account_id)
        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, _, _ = await repo.fetch_audit_log_page(
                event_type=None,
                env="live-small",
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=100,
            )
        assert rows == []


class TestPartitionPruning:
    """``ingest_clock_ts`` partition key — from/to bound at plan time prunes
    the scan to the 2026 partition. We assert the SQL accepts tz-aware
    datetimes + returns the expected rows."""

    @pytest.mark.asyncio
    async def test_from_in_2026_window_returns_rows(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        from datetime import UTC, datetime, timedelta

        seqs = await _seed_three_rows(async_session_factory, fresh_account_id)

        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            # All inserts above happened within the last minute; a window
            # starting an hour ago must include them.
            window_start = datetime.now(tz=UTC) - timedelta(hours=1)
            rows, _, _ = await repo.fetch_audit_log_page(
                event_type=None,
                env=None,
                from_ingest_utc=window_start,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=100,
            )

        seq_set = {r.sequence_no for r in rows}
        for s in seqs:
            assert s in seq_set

    @pytest.mark.asyncio
    async def test_future_window_returns_empty(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        from datetime import UTC, datetime

        await _seed_three_rows(async_session_factory, fresh_account_id)
        # A 2031 window targets the audit_log_y2031 partition which is empty.
        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, _, has_more = await repo.fetch_audit_log_page(
                event_type=None,
                env=None,
                from_ingest_utc=datetime(2031, 1, 1, tzinfo=UTC),
                to_ingest_utc=datetime(2031, 12, 31, tzinfo=UTC),
                before_sequence_no=None,
                limit=100,
            )
        assert rows == []
        assert has_more is False


class TestCursorPagination:
    """``before_sequence_no`` + ``has_more`` math against real Postgres."""

    @pytest.mark.asyncio
    async def test_limit_smaller_than_available_emits_has_more(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # Seed three rows; ask for two → has_more=True, next_cursor=seq of
        # the second-to-last row.
        await _seed_three_rows(async_session_factory, fresh_account_id)

        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, next_cursor, has_more = await repo.fetch_audit_log_page(
                event_type=None,
                env=None,
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=2,
            )
        assert len(rows) == 2
        assert has_more is True
        assert next_cursor is not None
        assert next_cursor == rows[-1].sequence_no

    @pytest.mark.asyncio
    async def test_cursor_continuation_returns_next_page(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        seqs = await _seed_three_rows(async_session_factory, fresh_account_id)

        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            page1, cursor1, _ = await repo.fetch_audit_log_page(
                event_type=None,
                env=None,
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=1,
            )
            assert page1 and cursor1 is not None
            page2, _, _ = await repo.fetch_audit_log_page(
                event_type=None,
                env=None,
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=cursor1,
                limit=10,
            )

        # Continuation page does NOT include the page1 tail
        assert page1[0].sequence_no not in {r.sequence_no for r in page2}
        # Continuation page MUST include earlier seeded rows (strict less
        # than the cursor in DESC order).
        for r in page2:
            assert r.sequence_no < cursor1
        # And at least one of our seeded rows shows up in the continuation.
        assert any(s in {r.sequence_no for r in page2} for s in seqs)

    @pytest.mark.asyncio
    async def test_limit_exactly_matches_available_has_more_false(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        fresh_account_id: UUID,
    ) -> None:
        # Filter to a unique event_type to isolate from other tests' writes;
        # request limit=1 against exactly 1 matching row.
        async with async_session_factory() as session:
            await append_audit_event(
                session,
                AuditEventType.SLIPPAGE_CALIBRATION_RECALIBRATED,
                {
                    "calibration_version_id": str(uuid.uuid4()),
                    "becomes_head": True,
                    "trigger": "manual",
                },
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )

        async with async_session_factory() as session:
            repo = PostgresPhase1QueryRepo(session)
            rows, next_cursor, has_more = await repo.fetch_audit_log_page(
                event_type="slippage_calibration_recalibrated",
                env=None,
                from_ingest_utc=None,
                to_ingest_utc=None,
                before_sequence_no=None,
                limit=1,
            )

        # Exactly one match in the entire table → has_more must be False
        # because the limit + 1 fetch returns no extra row.
        assert len(rows) == 1
        assert has_more is False
        assert next_cursor is None
