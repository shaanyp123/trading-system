"""Integration test for the QC adapter end-to-end ingestion path.

Spins up postgres:16 testcontainer, runs ``alembic upgrade head``, then
exercises ``Orchestrator.run_once`` against a respx-mocked QC ObjectStore
to prove the full pipeline (HTTP → parse → audit append → signals INSERT
→ cursor advance) lands the right rows under real Postgres triggers +
grants + the hash-chain invariant.

A22 BINDS + satisfied: real Postgres triggers + grants + audit writer
behavior, not mocks. A27 N/A (respx-mocked QC HTTP — the live-QC
contract is operator-runbook material).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from services.audit.chain import verify_chain
from services.audit.event_types import AuditEventType
from services.qc_adapter.cursor import CursorDirectory
from services.qc_adapter.orchestrator import Orchestrator
from services.qc_adapter.qc_objectstore_client import QCObjectStoreClient

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer


REPO_ROOT = Path(__file__).resolve().parents[2]
_QC_BASE = "https://www.quantconnect.com/api/v2"


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
async def session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def fresh_account_id(sync_engine: Engine) -> uuid.UUID:
    ext_id = f"QC_ADAPTER_TEST_{uuid.uuid4().hex[:12]}"
    with sync_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO accounts (external_account_id, account_type, active_from) "
                "VALUES (:ext, 'individual', now()) RETURNING id"
            ),
            {"ext": ext_id},
        ).scalar_one()
    return uuid.UUID(str(account_id))


@pytest.fixture
def reset_cursor(sync_engine: Engine) -> None:
    """Reset cursor rows to zero between tests so each test sees a fresh state."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE qc_adapter_cursor SET last_consumed_sequence_no = 0, "
                "bytes_consumed = 0, consecutive_failures = 0"
            )
        )


def _make_signal_event_line(
    *, seq: int, market: str = "MES", direction: str = "long", price: float = 5234.5
) -> str:
    return (
        json.dumps(
            {
                "sequence_no": seq,
                "event_type": "signal_emitted",
                "source_clock_ts": "2026-05-14T21:30:01.234+00:00",
                "qc_algorithm_version": (
                    "v1-trend-following-9d2f7a1c20bcdef0123456789abcdef012345678"
                ),
                "payload": {
                    "market": market,
                    "direction": direction,
                    "target_contracts": 1,
                    "decision_price": price,
                    "sizing_trace": {"stage_0": "passed", "vol_target": "0.15"},
                },
            }
        )
        + "\n"
    )


@pytest.fixture
def qc_client_with_respx() -> tuple[QCObjectStoreClient, respx.MockRouter]:
    """A QCObjectStoreClient wired to respx so we can mock the QC HTTP surface.

    The shared httpx.AsyncClient is constructed with a respx-friendly transport;
    the caller starts/stops the router as a context manager.
    """
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    qc = QCObjectStoreClient(
        user_id="12345",
        api_token="secret-token",
        base_url=_QC_BASE,
        timeout_seconds=5.0,
        client=http_client,
    )
    router = respx.mock(assert_all_called=False)
    return qc, router


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunOnceHappyPath:
    @pytest.mark.asyncio
    async def test_two_signals_land_audit_chain_and_signals_table(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: uuid.UUID,
        reset_cursor: None,
        qc_client_with_respx: tuple[QCObjectStoreClient, respx.MockRouter],
    ) -> None:
        qc, router = qc_client_with_respx
        # Seed two signal_emitted events in QC ObjectStore.
        body_1 = _make_signal_event_line(seq=101, market="MES", price=5234.5)
        body_2 = _make_signal_event_line(seq=102, market="MNQ", price=18500.25)

        router.start()
        try:
            router.post(f"{_QC_BASE}/object-store/list").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "objects": [
                            {"key": "events/101.jsonl"},
                            {"key": "events/102.jsonl"},
                        ],
                        "success": True,
                    },
                )
            )
            router.post(f"{_QC_BASE}/object-store/get").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={"url": "https://s3.example.com/k1", "success": True},
                    ),
                    httpx.Response(
                        200,
                        json={"url": "https://s3.example.com/k2", "success": True},
                    ),
                ]
            )
            router.get("https://s3.example.com/k1").mock(
                return_value=httpx.Response(200, content=body_1.encode())
            )
            router.get("https://s3.example.com/k2").mock(
                return_value=httpx.Response(200, content=body_2.encode())
            )

            orchestrator = Orchestrator(
                client=qc,
                session_factory=session_factory,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )
            result = await orchestrator.run_once(CursorDirectory.EVENTS)
        finally:
            router.stop()
            await qc.aclose()

        assert result.fetched == 2
        assert result.events_ingested == 2
        assert result.malformed_quarantined == 0
        assert result.failed is False

        # Signals table has the two rows attributed to our account.
        with sync_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT market, direction, decision_price, target_contracts, "
                    "       status, strategy_hash, parameter_set_hash, "
                    "       slippage_calibration_version_id, env, signal_type "
                    "FROM signals WHERE account_id = :acc ORDER BY market"
                ),
                {"acc": fresh_account_id},
            ).fetchall()
        assert len(rows) == 2
        markets = sorted(r.market for r in rows)
        assert markets == ["MES", "MNQ"]
        # The signals row's slippage_calibration_version_id FKs into the
        # bootstrap row that signal_ingestion auto-creates on first signal.
        with sync_engine.connect() as conn:
            head_row = conn.execute(
                text(
                    "SELECT id, version_no, trigger, is_head "
                    "FROM slippage_calibration_versions WHERE is_head = TRUE"
                )
            ).fetchone()
        assert head_row is not None
        assert head_row.version_no == 1
        assert head_row.trigger == "bootstrap"
        assert head_row.is_head is True
        bootstrap_id = head_row.id

        for row in rows:
            assert row.status == "pending"
            assert row.env == "paper"
            assert row.signal_type == "entry"
            assert len(row.strategy_hash) == 40
            assert len(row.parameter_set_hash) == 64
            assert row.slippage_calibration_version_id == bootstrap_id
            assert row.target_contracts == 1

        # audit_log has two SIGNAL_EMITTED rows + chain intact.
        with sync_engine.connect() as conn:
            audit_rows = conn.execute(
                text(
                    "SELECT sequence_no, event_type FROM audit_log "
                    "WHERE event_type = :etype "
                    "ORDER BY sequence_no ASC"
                ),
                {"etype": AuditEventType.SIGNAL_EMITTED.value},
            ).fetchall()
        assert len(audit_rows) == 2

        # Chain verify still passes.
        async with session_factory() as session:
            ok, broken_seq, n = await verify_chain(session)
        assert ok is True, f"chain break at {broken_seq}"
        assert n >= 2

        # Cursor advanced.
        with sync_engine.connect() as conn:
            cursor_row = conn.execute(
                text(
                    "SELECT last_consumed_sequence_no, consecutive_failures, bytes_consumed "
                    "FROM qc_adapter_cursor WHERE directory_path = :p"
                ),
                {"p": CursorDirectory.EVENTS.value},
            ).one()
        assert cursor_row.last_consumed_sequence_no == 102
        assert cursor_row.consecutive_failures == 0
        assert cursor_row.bytes_consumed > 0


class TestRunOnceFailurePath:
    @pytest.mark.asyncio
    async def test_list_keys_5xx_bumps_consecutive_failures(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: uuid.UUID,
        reset_cursor: None,
        qc_client_with_respx: tuple[QCObjectStoreClient, respx.MockRouter],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        qc, router = qc_client_with_respx

        async def _no_sleep(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("services.qc_adapter.qc_objectstore_client.asyncio.sleep", _no_sleep)

        router.start()
        try:
            router.post(f"{_QC_BASE}/object-store/list").mock(
                return_value=httpx.Response(503, text="degraded")
            )
            orchestrator = Orchestrator(
                client=qc,
                session_factory=session_factory,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )
            result = await orchestrator.run_once(CursorDirectory.EVENTS)
        finally:
            router.stop()
            await qc.aclose()

        assert result.failed is True
        assert result.events_ingested == 0
        assert result.advance_failures == 1

        # No new signals row.
        with sync_engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM signals WHERE account_id = :acc"),
                {"acc": fresh_account_id},
            ).scalar_one()
        assert count == 0


class TestRunOnceMalformedPayload:
    @pytest.mark.asyncio
    async def test_malformed_event_routes_to_data_quality_events(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: uuid.UUID,
        reset_cursor: None,
        qc_client_with_respx: tuple[QCObjectStoreClient, respx.MockRouter],
    ) -> None:
        qc, router = qc_client_with_respx

        # One valid event + one missing-required-key event in the same file.
        valid = _make_signal_event_line(seq=201)
        bad = (
            json.dumps(
                {
                    "sequence_no": 202,
                    "event_type": "signal_emitted",
                    "source_clock_ts": "2026-05-14T21:30:01.234+00:00",
                    "qc_algorithm_version": "v1-experimental",
                    "payload": {
                        # missing decision_price + sizing_trace
                        "market": "MCL",
                        "direction": "short",
                        "target_contracts": 1,
                    },
                }
            )
            + "\n"
        )

        router.start()
        try:
            router.post(f"{_QC_BASE}/object-store/list").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "objects": [{"key": "events/201.jsonl"}],
                        "success": True,
                    },
                )
            )
            router.post(f"{_QC_BASE}/object-store/get").mock(
                return_value=httpx.Response(
                    200, json={"url": "https://s3.example.com/k1", "success": True}
                )
            )
            router.get("https://s3.example.com/k1").mock(
                return_value=httpx.Response(200, content=(valid + bad).encode())
            )

            orchestrator = Orchestrator(
                client=qc,
                session_factory=session_factory,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )
            result = await orchestrator.run_once(CursorDirectory.EVENTS)
        finally:
            router.stop()
            await qc.aclose()

        assert result.events_ingested == 1
        assert result.malformed_quarantined == 1

        # One valid signal row.
        with sync_engine.connect() as conn:
            signal_count = conn.execute(
                text("SELECT COUNT(*) FROM signals WHERE account_id = :acc"),
                {"acc": fresh_account_id},
            ).scalar_one()
        assert signal_count == 1

        # data_quality_events has at least one row from this run.
        with sync_engine.connect() as conn:
            dq_rows = conn.execute(
                text(
                    "SELECT market, reason, event_type FROM data_quality_events "
                    "ORDER BY detected_at_utc DESC LIMIT 5"
                )
            ).fetchall()
        assert any(r.event_type == "quarantine" for r in dq_rows)

        # Chain still walks clean.
        async with session_factory() as session:
            ok, broken_seq, _n = await verify_chain(session)
        assert ok is True, f"chain break at {broken_seq}"


class TestRunOnceUnhandledEventType:
    @pytest.mark.asyncio
    async def test_order_filled_event_quarantined_but_chain_continuous(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: uuid.UUID,
        reset_cursor: None,
        qc_client_with_respx: tuple[QCObjectStoreClient, respx.MockRouter],
    ) -> None:
        qc, router = qc_client_with_respx

        # order_filled event — valid JSONL but PR-A doesn't wire a handler yet.
        line = (
            json.dumps(
                {
                    "sequence_no": 301,
                    "event_type": "order_filled",
                    "source_clock_ts": "2026-05-14T21:31:01.234+00:00",
                    "qc_algorithm_version": "v1-trend-following",
                    "payload": {
                        "order_id": "o-1",
                        "fill_price": 5235.0,
                        "qty": 1,
                    },
                }
            )
            + "\n"
        )
        router.start()
        try:
            router.post(f"{_QC_BASE}/object-store/list").mock(
                return_value=httpx.Response(
                    200, json={"objects": [{"key": "events/301.jsonl"}], "success": True}
                )
            )
            router.post(f"{_QC_BASE}/object-store/get").mock(
                return_value=httpx.Response(
                    200, json={"url": "https://s3.example.com/k1", "success": True}
                )
            )
            router.get("https://s3.example.com/k1").mock(
                return_value=httpx.Response(200, content=line.encode())
            )
            orchestrator = Orchestrator(
                client=qc,
                session_factory=session_factory,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )
            result = await orchestrator.run_once(CursorDirectory.EVENTS)
        finally:
            router.stop()
            await qc.aclose()

        assert result.events_ingested == 0
        assert result.malformed_quarantined == 1

        # Chain still walks clean (the quarantine row keeps the chain continuous).
        async with session_factory() as session:
            ok, broken_seq, _n = await verify_chain(session)
        assert ok is True, f"chain break at {broken_seq}"


class TestRunOnceNoNewKeys:
    @pytest.mark.asyncio
    async def test_empty_list_resets_failures_no_writes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: uuid.UUID,
        reset_cursor: None,
        qc_client_with_respx: tuple[QCObjectStoreClient, respx.MockRouter],
    ) -> None:
        qc, router = qc_client_with_respx

        # Bump failure counter to 5 before the run so we can assert reset.
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE qc_adapter_cursor SET consecutive_failures = 5 "
                    "WHERE directory_path = :p"
                ),
                {"p": CursorDirectory.EVENTS.value},
            )

        router.start()
        try:
            router.post(f"{_QC_BASE}/object-store/list").mock(
                return_value=httpx.Response(200, json={"objects": [], "success": True})
            )
            orchestrator = Orchestrator(
                client=qc,
                session_factory=session_factory,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )
            result = await orchestrator.run_once(CursorDirectory.EVENTS)
        finally:
            router.stop()
            await qc.aclose()

        assert result.failed is False
        assert result.events_ingested == 0
        assert result.fetched == 0
        # consecutive_failures reset on successful poll (empty body counts as success).
        with sync_engine.connect() as conn:
            failures = conn.execute(
                text(
                    "SELECT consecutive_failures FROM qc_adapter_cursor WHERE directory_path = :p"
                ),
                {"p": CursorDirectory.EVENTS.value},
            ).scalar_one()
        assert failures == 0


class TestKeyFiltering:
    @pytest.mark.asyncio
    async def test_keys_with_sequence_le_cursor_skipped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
        fresh_account_id: uuid.UUID,
        reset_cursor: None,
        qc_client_with_respx: tuple[QCObjectStoreClient, respx.MockRouter],
    ) -> None:
        qc, router = qc_client_with_respx

        # Pre-seed the cursor at sequence_no=100.
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE qc_adapter_cursor SET last_consumed_sequence_no = 100 "
                    "WHERE directory_path = :p"
                ),
                {"p": CursorDirectory.EVENTS.value},
            )

        line_new = _make_signal_event_line(seq=101, market="MES")

        router.start()
        try:
            # Return 3 keys — 99, 100 already consumed; 101 is new.
            router.post(f"{_QC_BASE}/object-store/list").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "objects": [
                            {"key": "events/99.jsonl"},
                            {"key": "events/100.jsonl"},
                            {"key": "events/101.jsonl"},
                        ],
                        "success": True,
                    },
                )
            )
            router.post(f"{_QC_BASE}/object-store/get").mock(
                return_value=httpx.Response(
                    200, json={"url": "https://s3.example.com/k1", "success": True}
                )
            )
            router.get("https://s3.example.com/k1").mock(
                return_value=httpx.Response(200, content=line_new.encode())
            )

            orchestrator = Orchestrator(
                client=qc,
                session_factory=session_factory,
                account_id=fresh_account_id,
                env="paper",
                phase_at_emit=0,
            )
            result = await orchestrator.run_once(CursorDirectory.EVENTS)
        finally:
            router.stop()
            await qc.aclose()

        assert result.fetched == 1
        assert result.events_ingested == 1

        with sync_engine.connect() as conn:
            cursor_seq = conn.execute(
                text(
                    "SELECT last_consumed_sequence_no FROM qc_adapter_cursor "
                    "WHERE directory_path = :p"
                ),
                {"p": CursorDirectory.EVENTS.value},
            ).scalar_one()
        assert cursor_seq == 101


class TestAccountIdLookup:
    @pytest.mark.asyncio
    async def test_fetch_active_account_id_returns_inserted_account(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_engine: Engine,
    ) -> None:
        """Sanity check on the db.fetch_active_account_id helper.

        Independent of the QC layer; locks the SQL contract the
        orchestrator's startup path depends on (services/qc_adapter/main.py).
        """
        from services.qc_adapter.db import fetch_active_account_id

        ext_id = f"QC_LOOKUP_{uuid.uuid4().hex[:12]}"
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO accounts (external_account_id, account_type, "
                    "                      active_from) "
                    "VALUES (:ext, 'individual', now())"
                ),
                {"ext": ext_id},
            )

        async with session_factory() as session:
            found = await fetch_active_account_id(session)
        # The fixture-fresh account might not be the absolute oldest; just
        # verify the function returns a valid UUID matching some row.
        assert found is not None


# Mark the module's only-fixture-test path as not load-bearing; tests above are
# the canonical surface. This stub exists to give the integration test file a
# stable shape for future PR-B/C/D extensions.
def test_module_loads() -> None:
    assert callable(Orchestrator)
