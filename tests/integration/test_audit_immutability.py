"""Integration tests for audit_log immutability + attribution expected_* lock.

Verifies the triggers and EVENT TRIGGER from migration 0005 actually block
UPDATE / DELETE / TRUNCATE against audit_log, plus the attribution
expected_* immutability rule.

Spins up a fresh `postgres:16` testcontainer per module, runs
`alembic upgrade head`, and asserts that mutation attempts raise the expected
DB error. If Docker is unreachable on the runner, the module is skipped (the
default behavior of testcontainers).

Backend-spec sections under test: §2.10.2 (audit_log append-only), §3.7
(attribution expected_* immutable post-emit).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DatabaseError, InternalError, ProgrammingError

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer  # type: ignore[attr-defined]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _require_docker() -> None:
    """Skip the module if the Docker daemon is unreachable.

    testcontainers needs Docker; on a runner without it (CI sans `docker:dind`,
    or a laptop with Docker Desktop closed) the module skips with a clear
    message instead of producing 6 confusing connection-refused errors.
    """
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except Exception as exc:  # testcontainers/docker raise varied types
        pytest.skip(f"Docker daemon unavailable: {exc}")


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Engine]:
    """Bring up postgres:16, apply all migrations, yield a sync engine.

    The engine is psycopg2-based (testcontainers default). asyncpg is not used
    here because Alembic itself is synchronous — async would force a parallel
    sync connection just for the migration step.
    """
    _require_docker()
    with PostgresContainer("postgres:16") as pg:
        sync_url = pg.get_connection_url()  # postgresql+psycopg2://...

        # Alembic env.py reads DATABASE_URL; set it for the migration window.
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

        engine = create_engine(sync_url)
        yield engine
        engine.dispose()


def _seed_account_and_audit_row(engine: Engine) -> dict[str, Any]:
    """Insert one accounts row + one audit_log row; return the audit row's keys.

    Uses a fresh UUID-suffixed `external_account_id` per call so that this
    helper can be invoked from multiple module-scoped tests without hitting
    the UNIQUE constraint on `accounts.external_account_id`.
    """
    ext_id = f"TEST_ACCOUNT_{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        account_id = conn.execute(
            text(
                """
                INSERT INTO accounts
                    (external_account_id, account_type, active_from)
                VALUES (:ext, 'individual', now())
                RETURNING id
                """
            ),
            {"ext": ext_id},
        ).scalar_one()

        # 32-byte hashes, all-zero prev_hash for first row in chain.
        zero32 = bytes(32)
        record_hash = bytes(range(32))
        result = conn.execute(
            text(
                """
                INSERT INTO audit_log (
                    event_type, account_id, env, phase_at_emit,
                    source_clock_ts, prev_hash, record_hash, payload_jcs
                ) VALUES (
                    'system_started', :acct, 'paper', 0,
                    now(), :prev, :rec, :pl
                )
                RETURNING sequence_no, ingest_clock_ts
                """
            ),
            {
                "acct": account_id,
                "prev": zero32,
                "rec": record_hash,
                "pl": b'{"hello":"world"}',
            },
        ).one()

    return {"sequence_no": result.sequence_no, "ingest_clock_ts": result.ingest_clock_ts}


def test_audit_log_insert_succeeds(pg_engine: Engine) -> None:
    """Sanity check: triggers don't block the legitimate INSERT path."""
    keys = _seed_account_and_audit_row(pg_engine)
    assert keys["sequence_no"] >= 1
    with pg_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one()
    assert count >= 1


def test_audit_log_update_raises_immutability_exception(pg_engine: Engine) -> None:
    """UPDATE on audit_log must raise via the BEFORE UPDATE trigger."""
    keys = _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE audit_log SET event_type = 'spoofed'
                    WHERE sequence_no = :seq
                    """
                ),
                {"seq": keys["sequence_no"]},
            )
    assert "append-only" in str(excinfo.value).lower() or "forbidden" in str(excinfo.value).lower()


def test_audit_log_delete_raises_immutability_exception(pg_engine: Engine) -> None:
    """DELETE on audit_log must raise via the BEFORE DELETE trigger."""
    keys = _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM audit_log WHERE sequence_no = :seq"),
                {"seq": keys["sequence_no"]},
            )
    assert "append-only" in str(excinfo.value).lower() or "forbidden" in str(excinfo.value).lower()


def test_audit_log_truncate_raises_immutability_exception(pg_engine: Engine) -> None:
    """TRUNCATE on audit_log must raise via the EVENT TRIGGER."""
    _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError, ProgrammingError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE audit_log"))
    assert "truncate forbidden" in str(excinfo.value).lower()


def test_audit_log_truncate_partition_also_raises(pg_engine: Engine) -> None:
    """TRUNCATE on a yearly partition must also be blocked (LIKE 'audit_log%' match)."""
    _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError, ProgrammingError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE audit_log_y2026"))
    assert "truncate forbidden" in str(excinfo.value).lower()


def test_attribution_expected_columns_immutable(pg_engine: Engine) -> None:
    """attribution.expected_* columns may not change post-insert; realized_* may."""
    ext_id = f"TEST_ACCOUNT_ATTR_{uuid.uuid4().hex[:12]}"
    with pg_engine.begin() as conn:
        account_id = conn.execute(
            text(
                """
                INSERT INTO accounts (external_account_id, account_type, active_from)
                VALUES (:ext, 'individual', now())
                RETURNING id
                """
            ),
            {"ext": ext_id},
        ).scalar_one()
        slip_id = conn.execute(
            text(
                """
                INSERT INTO slippage_calibration_versions
                    (version_no, calibrated_at_utc, trigger,
                     per_market_coefficients, audit_event_uuid)
                VALUES
                    (1, now(), 'bootstrap', '{}'::jsonb, gen_random_uuid())
                RETURNING id
                """
            )
        ).scalar_one()
        signal_id = conn.execute(
            text(
                """
                INSERT INTO signals (
                    account_id, env, market, emitted_at_utc, session_date,
                    direction, signal_type, strategy_hash, parameter_set_hash,
                    slippage_calibration_version_id, decision_price,
                    target_contracts, sizing_trace
                ) VALUES (
                    :acct, 'paper', '/MES', now(), CURRENT_DATE,
                    'long', 'donchian_breakout',
                    '0000000000000000000000000000000000000000',
                    repeat('0', 64),
                    :slip, 5234.50, 1, '{}'::jsonb
                )
                RETURNING id
                """
            ),
            {"acct": account_id, "slip": slip_id},
        ).scalar_one()
        order_id = conn.execute(
            text(
                """
                INSERT INTO orders (
                    account_id, env, signal_id, client_order_id, market,
                    direction, order_type, quantity, placed_at_utc,
                    strategy_hash, parameter_set_hash
                ) VALUES (
                    :acct, 'paper', :sig, 'COID-ATTR-1', '/MES',
                    'buy', 'limit_marketable', 1, now(),
                    '0000000000000000000000000000000000000000',
                    repeat('0', 64)
                )
                RETURNING id
                """
            ),
            {"acct": account_id, "sig": signal_id},
        ).scalar_one()
        trade_id = conn.execute(
            text(
                """
                INSERT INTO trades (
                    account_id, env, market, entry_signal_id, entry_order_id,
                    direction, opened_at_utc, total_quantity, avg_entry_price,
                    state, managed_by_version, strategy_hash, parameter_set_hash,
                    slippage_calibration_version_id
                ) VALUES (
                    :acct, 'paper', '/MES', :sig, :ord,
                    'long', now(), 1, 5234.50,
                    'open_position',
                    '0000000000000000000000000000000000000000',
                    '0000000000000000000000000000000000000000',
                    repeat('0', 64), :slip
                )
                RETURNING id
                """
            ),
            {"acct": account_id, "sig": signal_id, "ord": order_id, "slip": slip_id},
        ).scalar_one()
        attribution_id = conn.execute(
            text(
                """
                INSERT INTO attribution (
                    trade_id, account_id, env, expected_entry_price,
                    expected_pnl_usd, expected_slippage_bps,
                    expected_holding_days, expected_at_utc
                ) VALUES (
                    :trade, :acct, 'paper', 5234.50, 100.00, 5.0, 7, now()
                )
                RETURNING id
                """
            ),
            {"trade": trade_id, "acct": account_id},
        ).scalar_one()

    # Attempting to mutate expected_* must fail.
    with pytest.raises((DatabaseError, InternalError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE attribution SET expected_pnl_usd = 999
                    WHERE id = :id
                    """
                ),
                {"id": attribution_id},
            )
    assert "expected_*" in str(excinfo.value) or "immutable" in str(excinfo.value).lower()

    # Mutating realized_* on the same row must succeed.
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE attribution
                SET realized_pnl_usd = 95.50, realized_at_utc = now()
                WHERE id = :id
                """
            ),
            {"id": attribution_id},
        )
