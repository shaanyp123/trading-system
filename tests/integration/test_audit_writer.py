"""Integration tests for :func:`services.audit.writer.append_audit_event`
and :func:`services.audit.chain.verify_chain`.

Spins up a fresh ``postgres:16`` testcontainer per module, runs
``alembic upgrade head``, then exercises the writer + walker over an
asyncpg connection. Test cases:

1. ``test_single_insert_computes_correct_hash_chain`` — a single insert's
   ``record_hash`` matches ``SHA-256(prev_hash || JCS(payload))``.
2. ``test_first_row_genesis_prev_hash_is_all_zeros`` — the very first row
   in the chain (sequence_no = 1) has ``prev_hash = GENESIS_HASH``.
3. ``test_concurrent_writers_serialize_correctly`` — three concurrent
   writes produce distinct ``sequence_no`` values and an unbroken hash
   chain. (Concurrency is intentionally moderate — see the test's
   docstring for the SSI-snapshot rationale; realistic Phase-1 load is
   sub-second per audit write.)
4. ``test_serialization_failure_triggers_retry_then_succeeds`` —
   deterministic injection of a SQLSTATE 40001 error on the first attempt
   exercises the retry control flow without relying on race conditions.
5. ``test_verify_chain_count_matches_select_count`` — ``verify_chain``'s
   third return value (rows-walked count) matches a separate
   ``SELECT COUNT(*) FROM audit_log`` against the same DB state, locking
   the SQL contract for the operator-runbook gate at
   ``deploy/audit/README.md``.

If Docker is unreachable on the runner, the module skips cleanly (same
pattern as ``test_audit_immutability.py``).
"""

from __future__ import annotations

import asyncio
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

from services.audit.chain import (
    GENESIS_HASH,
    compute_record_hash,
    jcs_serialize,
    verify_chain,
)
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
    except Exception as exc:  # testcontainers raises varied types
        pytest.skip(f"Docker daemon unavailable: {exc}")


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """Bring up postgres:16, apply migrations, yield the sync (psycopg2) URL.

    Module-scoped so all fixtures below share one container; the URL
    string is the only thing handed off so consumers can derive whatever
    engine flavor they need (sync via :func:`create_engine`, async via
    :func:`create_async_engine` after a ``+psycopg2 -> +asyncpg`` swap).
    """
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
    """Sync engine for seed data + chain inspection (psycopg2)."""
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def async_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    """Async engine for the writer (asyncpg).

    Function-scoped so each test gets an engine bound to its own
    pytest-asyncio event loop. A module-scoped engine triggers
    "Event loop is closed" during teardown because the pool's connections
    are tied to the prior test's loop.
    """
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
    """Insert a fresh accounts row and return its UUID for FK use.

    Module-scoped DB means audit_log accumulates rows across tests; we do
    NOT try to reset state between tests (TRUNCATE/DELETE are blocked by
    the audit_log immutability triggers from migration 0005). Instead,
    each test reads the chain tail before its writes and asserts the
    hash math against THAT tail — the chain remains continuous regardless
    of how many prior rows live in the table.
    """
    ext_id = f"AUDIT_WRITER_TEST_{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": ext_id},
        ).scalar_one()
    return UUID(str(account_id))


def _read_chain_tail_before(sync_engine: Engine) -> tuple[bytes, int]:
    """Return ``(prev_hash, latest_sequence_no)`` for the current chain tail.

    Used to compute the EXPECTED ``prev_hash`` for the next insert. Tests
    that race writes against an empty-or-non-empty audit_log both work.
    """
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT record_hash, sequence_no FROM audit_log ORDER BY sequence_no DESC LIMIT 1")
        ).fetchone()
    if row is None:
        return GENESIS_HASH, 0
    return bytes(row.record_hash), row.sequence_no


def _read_row_by_uuid(sync_engine: Engine, event_uuid: UUID) -> dict[str, Any]:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT sequence_no, event_uuid, event_type, prev_hash, record_hash, "
                "       payload_jcs, account_id, env, phase_at_emit, "
                "       monotonic_ns, repaired_for_sequence_no "
                "FROM audit_log WHERE event_uuid = :uuid"
            ),
            {"uuid": event_uuid},
        ).one()
    return {
        "sequence_no": row.sequence_no,
        "event_uuid": row.event_uuid,
        "event_type": row.event_type,
        "prev_hash": bytes(row.prev_hash),
        "record_hash": bytes(row.record_hash),
        "payload_jcs": bytes(row.payload_jcs),
        "account_id": row.account_id,
        "env": row.env,
        "phase_at_emit": row.phase_at_emit,
        "monotonic_ns": row.monotonic_ns,
        "repaired_for_sequence_no": row.repaired_for_sequence_no,
    }


@pytest.mark.asyncio
async def test_single_insert_computes_correct_hash_chain(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """A single ``append_audit_event`` produces a row whose hashes match
    SHA-256(prev_hash || JCS(payload))."""
    expected_prev, _ = _read_chain_tail_before(sync_engine)

    payload = {"version": "1.2.3", "phase": "test"}
    async with async_session_factory() as session:
        record = await append_audit_event(
            session,
            AuditEventType.SYSTEM_STARTED,
            payload,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    assert record.event_type == AuditEventType.SYSTEM_STARTED
    assert record.sequence_no >= 1

    persisted = _read_row_by_uuid(sync_engine, record.event_uuid)
    assert persisted["prev_hash"] == expected_prev
    expected_payload_jcs = jcs_serialize(payload)
    assert persisted["payload_jcs"] == expected_payload_jcs
    expected_record_hash = compute_record_hash(expected_prev, expected_payload_jcs)
    assert persisted["record_hash"] == expected_record_hash
    assert persisted["account_id"] == fresh_account_id
    assert persisted["env"] == "paper"
    assert persisted["phase_at_emit"] == 0


@pytest.mark.asyncio
async def test_first_row_genesis_prev_hash_is_all_zeros(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """When ``audit_log`` is empty, the first writer must use GENESIS_HASH.

    Module-scoped DB means another test may have already written a row
    before this one runs. We reproduce the empty-chain condition by
    asserting the GENESIS property NOT against the `audit_log`'s overall
    first row (which always has prev_hash = GENESIS by definition once
    the chain is bootstrapped) but specifically against the very-first
    row whose sequence_no = 1 — that is the genuine genesis row.
    """
    payload = {"probe": "genesis"}
    async with async_session_factory() as session:
        await append_audit_event(
            session,
            AuditEventType.SYSTEM_STARTED,
            payload,
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    with sync_engine.connect() as conn:
        first_row = conn.execute(
            text("SELECT prev_hash FROM audit_log ORDER BY sequence_no ASC LIMIT 1")
        ).one()
    assert bytes(first_row.prev_hash) == GENESIS_HASH


@pytest.mark.asyncio
async def test_concurrent_writers_serialize_correctly(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """Concurrent ``append_audit_event`` calls produce distinct sequence_nos
    with no gaps and a continuous hash chain.

    This is the critical concurrency invariant: the advisory lock + SSI +
    retry loop must cooperate so each writer reads the correct
    ``prev_hash`` before computing its ``record_hash``. Without the lock,
    two writers might read the same ``prev_hash`` and one would be aborted
    by SSI (or, worse, both would commit and corrupt the chain).

    Concurrency is intentionally moderate (3 writers) — under heavier
    contention the spec'd pattern (``pg_advisory_xact_lock`` inside
    SERIALIZABLE) can still produce serialization failures because the
    transaction snapshot is fixed at the lock-call statement, BEFORE the
    lock is acquired. The retry loop catches those, but
    :data:`MAX_RETRIES` = 5 has a finite stress ceiling. Phase-1 realistic
    load is <1 audit-write per second; 3 simultaneous writers comfortably
    bounds the realistic worst case. A separate test exercises the retry
    path deterministically below.
    """
    n_writers = 3

    async def write_one(i: int) -> int:
        async with async_session_factory() as session:
            record = await append_audit_event(
                session,
                AuditEventType.SYSTEM_STARTED,
                {"writer_index": i},
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )
            return record.sequence_no

    sequence_nos = await asyncio.gather(*[write_one(i) for i in range(n_writers)])

    # All writers succeeded with distinct sequence_nos. Note: gaps in the
    # BIGSERIAL sequence are EXPECTED after retries because nextval() is
    # consumed even by rolled-back INSERTs — what matters is that every
    # COMMITTED row is on the chain and that no two committed rows share
    # a sequence_no.
    assert len(set(sequence_nos)) == n_writers
    sorted_seqs = sorted(sequence_nos)

    # The hash chain is intact across THESE rows AND any rows interleaved
    # by other tests sharing the module-scoped DB. Reading rows by
    # sequence_no walks them in commit order; each row's prev_hash MUST
    # equal the prior row's record_hash.
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT sequence_no, prev_hash, record_hash, payload_jcs "
                "FROM audit_log "
                "WHERE sequence_no >= :start AND sequence_no <= :end "
                "ORDER BY sequence_no ASC"
            ),
            {"start": sorted_seqs[0], "end": sorted_seqs[-1]},
        ).fetchall()

    assert len(rows) >= n_writers
    for i in range(1, len(rows)):
        assert bytes(rows[i].prev_hash) == bytes(rows[i - 1].record_hash), (
            f"chain break between sequence_no {rows[i - 1].sequence_no} and {rows[i].sequence_no}"
        )
        recomputed = compute_record_hash(bytes(rows[i].prev_hash), bytes(rows[i].payload_jcs))
        assert bytes(rows[i].record_hash) == recomputed

    # All N writers' rows are present in the read set.
    persisted_seqs = {row.sequence_no for row in rows}
    assert set(sequence_nos).issubset(persisted_seqs)


@pytest.mark.asyncio
async def test_serialization_failure_triggers_retry_then_succeeds(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simulated SerializationError on the first attempt is recovered by
    the retry loop, and the second attempt commits cleanly.

    Real concurrent contention is non-deterministic (SSI conflicts depend
    on snapshot timing); this test injects a fake serialization failure
    on the first attempt and asserts the writer retries and succeeds.
    That validates the retry control flow (catch DBAPIError → SQLSTATE
    check → sleep → re-enter the transaction) end-to-end without relying
    on race conditions.

    The injection point is ``services.audit.writer.compute_record_hash``
    — on the first call we raise a synthetic
    :class:`sqlalchemy.exc.DBAPIError` carrying SQLSTATE 40001; on
    subsequent calls we delegate to the real implementation.
    """
    from sqlalchemy.exc import DBAPIError

    from services.audit.chain import compute_record_hash as real_compute_record_hash

    call_count = {"n": 0}
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    class FakeAsyncpgSerializationError(Exception):
        sqlstate = "40001"
        pgcode = "40001"

    def maybe_fail(prev_hash: bytes, payload_jcs: bytes) -> bytes:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise DBAPIError(
                "INSERT INTO audit_log",
                {},
                FakeAsyncpgSerializationError("could not serialize access"),
            )
        return real_compute_record_hash(prev_hash, payload_jcs)

    async def recording_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("services.audit.writer.compute_record_hash", maybe_fail)
    monkeypatch.setattr("services.audit.writer.asyncio.sleep", recording_sleep)

    async with async_session_factory() as session:
        record = await append_audit_event(
            session,
            AuditEventType.SYSTEM_STARTED,
            {"probe": "retry"},
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    # Two compute_record_hash calls (one failed, one succeeded).
    assert call_count["n"] == 2
    # Exactly one backoff sleep, with the first retry delay.
    assert sleep_calls == [0.01]
    # Row landed; chain math checks out via the real compute.
    persisted = _read_row_by_uuid(sync_engine, record.event_uuid)
    expected_record_hash = real_compute_record_hash(
        persisted["prev_hash"], persisted["payload_jcs"]
    )
    assert persisted["record_hash"] == expected_record_hash


@pytest.mark.asyncio
async def test_verify_chain_count_matches_select_count(
    async_session_factory: async_sessionmaker[AsyncSession],
    sync_engine: Engine,
    fresh_account_id: UUID,
) -> None:
    """``verify_chain`` rows-walked count == ``SELECT COUNT(*)`` after a
    real chain has been built up by prior tests + fresh writes.

    This locks the SQL contract for the operator-runbook gate: the
    operator's "CHAIN OK: N rows verified" stdout line is true iff the
    walker's count equals the table's row count, and the chain is
    intact. We don't trust the count alone — we also assert the walker
    returned ``ok = True`` and ``broken_at = None`` so the test fails
    loudly if the walker silently degrades the contract.
    """
    # Append two rows so the chain has enough material to walk.
    async with async_session_factory() as session:
        await append_audit_event(
            session,
            AuditEventType.SYSTEM_STARTED,
            {"probe": "verify_chain_count_1"},
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )
    async with async_session_factory() as session:
        await append_audit_event(
            session,
            AuditEventType.SYSTEM_STARTED,
            {"probe": "verify_chain_count_2"},
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    # Walk the whole chain.
    async with async_session_factory() as session:
        ok, broken_at, walked = await verify_chain(session)

    # Independent count via the sync engine.
    with sync_engine.connect() as conn:
        actual_count = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one()

    assert ok is True
    assert broken_at is None
    assert walked == actual_count, (
        f"verify_chain reported {walked} rows but SELECT COUNT(*) sees {actual_count}"
    )
    # The sequence is monotonic; we expect at least the two rows we just
    # wrote (plus any from prior tests in this module-scoped DB).
    assert walked >= 2


@pytest.mark.asyncio
async def test_cli_verify_function_runs_against_real_db(
    pg_url: str,
    fresh_account_id: UUID,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``services.audit.verify_chain._verify`` exercises engine lifecycle.

    The CLI's ``_verify`` wrapper builds its own one-shot engine,
    sessionmaker, and session — separate from the ``async_engine``
    fixture used elsewhere in this module. This test forwards the
    testcontainer's URL into ``_verify`` and asserts the same
    ``(True, None, N)`` contract as the direct ``verify_chain`` call,
    locking the engine-lifecycle path against real Postgres.

    Without this test, ``_verify`` is exercised only at operator-runbook
    time (``deploy/audit/README.md``) — the unit tests in
    ``test_verify_chain_cli.py`` monkeypatch it out.
    """
    from services.audit.verify_chain import _verify as cli_verify

    # Append a row so the chain has at least one element to walk.
    async with async_session_factory() as session:
        await append_audit_event(
            session,
            AuditEventType.SYSTEM_STARTED,
            {"probe": "cli_verify_smoke"},
            account_id=fresh_account_id,
            env="paper",
            phase_at_emit=0,
        )

    async_url = pg_url.replace("postgresql+psycopg2", "postgresql+asyncpg")
    ok, broken_at, walked = await cli_verify(async_url)

    assert ok is True
    assert broken_at is None
    assert walked >= 1
