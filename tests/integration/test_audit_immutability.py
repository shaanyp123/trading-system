"""Integration tests for audit_log immutability + attribution expected_* lock.

Verifies the triggers from migration 0005 (BEFORE UPDATE/DELETE row trigger
+ per-table BEFORE TRUNCATE statement triggers) and the role-permission
landscape from migration 0006 actually block UPDATE / DELETE / TRUNCATE
against audit_log, plus the attribution expected_* immutability rule.

Spins up a fresh `postgres:16` testcontainer per module, runs
`alembic upgrade head`, and asserts that mutation attempts raise the expected
DB error. If Docker is unreachable on the runner, the module is skipped (the
default behavior of testcontainers).

The fault-injection block (Day 13 / Week 4 Wed) layers role-aware coverage on
top of the original 6 default-superuser tests:

* As ``app_service`` (INSERT/SELECT only on audit_log): UPDATE / DELETE /
  TRUNCATE all hit the **permission layer** first — SQLSTATE 42501
  (insufficient_privilege). The trigger never fires; that's by design and
  documents the first-line defense.
* As ``app_owner`` (full DML; only TRUNCATE explicitly REVOKEd on the
  parent partitioned table): UPDATE / DELETE on the parent get past
  permission and **hit the trigger** — SQLSTATE P0001 with the
  ``audit_log is append-only`` message. TRUNCATE on the parent is
  permission-denied (REVOKE TRUNCATE FROM app_owner in 0006); TRUNCATE
  on a yearly partition reaches the **per-partition trigger** because
  the wildcard GRANT ALL never gets re-revoked at the partition level
  — SQLSTATE P0001 with the ``TRUNCATE forbidden on audit_log``
  message.
* SQLSTATE P0001 is asserted explicitly on the trigger paths so a future
  refactor that downgrades RAISE EXCEPTION to e.g. RAISE WARNING fails
  loudly here instead of silently letting a mutation through.

The fourth role tier — ``dba_breakglass`` — is created NOLOGIN in 0006 and
is intentionally NOT exercised; per spec §8.2.1 it's the manual operator
escape hatch and the test would have to run as superuser to ``SET ROLE``
to it anyway, which defeats the purpose. Coverage of its TRUNCATE GRANT
stays at the migration level (0006).

Backend-spec sections under test: §2.10.2 (audit_log append-only), §3.7
(attribution expected_* immutable post-emit), §8.2 (role hierarchy).
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


# ---------------------------------------------------------------------------
# Day 13 / Week 4 Wed — fault-injection coverage
#
# The seven tests below add role-permission + SQLSTATE assertion coverage on
# top of the defaults-as-superuser tests. Two defense layers are exercised:
#
#   Layer 1 — REVOKE / GRANT (migration 0006). Permission denial fires
#             BEFORE the row hits the table; SQLSTATE 42501.
#   Layer 2 — Triggers (migration 0005). Fire AFTER permission allows the
#             write attempt; SQLSTATE P0001 + the spec's RAISE EXCEPTION
#             message.
#
# Both layers must hold for the audit_log immutability guarantee to be
# real. ``app_service`` exercises layer 1; ``app_owner`` exercises layer 2
# for UPDATE/DELETE on the parent and partitions, AND layer 1 for TRUNCATE
# on the parent (TRUNCATE was REVOKEd from app_owner specifically in 0006).
# ---------------------------------------------------------------------------


def _pgcode(excinfo: pytest.ExceptionInfo[Any]) -> str | None:
    """Best-effort SQLSTATE accessor for a SQLAlchemy DB error.

    psycopg2's ``Error`` exposes ``pgcode``; SQLAlchemy wraps it in
    ``DBAPIError`` and stashes the original on ``.orig``. The chain is
    ``excinfo.value.orig.pgcode``; both legs are guarded with ``getattr``
    so a future driver swap (or a synthetic exception in a unit test)
    surfaces a clean ``None`` instead of an ``AttributeError`` distractor.
    """
    orig = getattr(excinfo.value, "orig", None)
    return getattr(orig, "pgcode", None) if orig is not None else None


def test_audit_log_update_as_app_service_blocked_by_permission(pg_engine: Engine) -> None:
    """``app_service`` UPDATE on audit_log → permission layer (SQLSTATE 42501).

    Defense layer 1: migration 0006 grants ``app_service`` only INSERT/SELECT
    on audit_log and REVOKEs UPDATE/DELETE/TRUNCATE. The UPDATE never reaches
    the BEFORE UPDATE trigger.
    """
    keys = _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError, ProgrammingError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE app_service"))
            conn.execute(
                text("UPDATE audit_log SET event_type = 'spoofed' WHERE sequence_no = :seq"),
                {"seq": keys["sequence_no"]},
            )
    msg = str(excinfo.value).lower()
    assert "permission denied" in msg, f"expected permission denial, got: {msg}"
    assert _pgcode(excinfo) == "42501", (
        f"expected SQLSTATE 42501 (insufficient_privilege), got {_pgcode(excinfo)!r}"
    )


def test_audit_log_truncate_as_app_service_blocked_by_permission(pg_engine: Engine) -> None:
    """``app_service`` TRUNCATE on audit_log → permission layer (SQLSTATE 42501).

    Defense layer 1: REVOKE TRUNCATE FROM app_service blocks before the
    BEFORE TRUNCATE trigger has a chance to fire.
    """
    _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError, ProgrammingError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE app_service"))
            conn.execute(text("TRUNCATE TABLE audit_log"))
    msg = str(excinfo.value).lower()
    assert "permission denied" in msg, f"expected permission denial, got: {msg}"
    assert _pgcode(excinfo) == "42501", (
        f"expected SQLSTATE 42501 (insufficient_privilege), got {_pgcode(excinfo)!r}"
    )


def test_audit_log_update_as_app_owner_blocked_by_trigger(pg_engine: Engine) -> None:
    """``app_owner`` UPDATE on audit_log → trigger layer (SQLSTATE P0001).

    Defense layer 2: migration 0006 grants ``app_owner`` ALL privileges,
    so the UPDATE passes the permission gate and hits the BEFORE UPDATE
    row trigger from 0005. The trigger raises with the spec's exact
    message ``audit_log is append-only; UPDATE/DELETE forbidden (TG_OP=%)``.
    """
    keys = _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE app_owner"))
            conn.execute(
                text("UPDATE audit_log SET event_type = 'spoofed' WHERE sequence_no = :seq"),
                {"seq": keys["sequence_no"]},
            )
    msg = str(excinfo.value).lower()
    assert "append-only" in msg, f"expected append-only message, got: {msg}"
    assert "tg_op=update" in msg, f"expected TG_OP=UPDATE, got: {msg}"
    assert _pgcode(excinfo) == "P0001", (
        f"expected SQLSTATE P0001 (raise_exception), got {_pgcode(excinfo)!r}"
    )


def test_audit_log_delete_as_app_owner_blocked_by_trigger(pg_engine: Engine) -> None:
    """``app_owner`` DELETE on audit_log → trigger layer (SQLSTATE P0001).

    Same path as UPDATE-as-app_owner but exercises the BEFORE DELETE leg
    of the row trigger; TG_OP differs (DELETE vs UPDATE).
    """
    keys = _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE app_owner"))
            conn.execute(
                text("DELETE FROM audit_log WHERE sequence_no = :seq"),
                {"seq": keys["sequence_no"]},
            )
    msg = str(excinfo.value).lower()
    assert "append-only" in msg, f"expected append-only message, got: {msg}"
    assert "tg_op=delete" in msg, f"expected TG_OP=DELETE, got: {msg}"
    assert _pgcode(excinfo) == "P0001", (
        f"expected SQLSTATE P0001 (raise_exception), got {_pgcode(excinfo)!r}"
    )


def test_audit_log_truncate_parent_as_app_owner_blocked_by_permission(
    pg_engine: Engine,
) -> None:
    """``app_owner`` TRUNCATE on parent → permission layer (SQLSTATE 42501).

    ``app_owner`` has GRANT ALL on every table EXCEPT TRUNCATE on the
    parent partitioned table (REVOKE TRUNCATE ON audit_log FROM app_owner
    in 0006 — the only TRUNCATE retainer is ``dba_breakglass``). So the
    parent-table TRUNCATE is permission-denied; the trigger doesn't fire.
    """
    _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError, ProgrammingError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE app_owner"))
            conn.execute(text("TRUNCATE TABLE audit_log"))
    msg = str(excinfo.value).lower()
    assert "permission denied" in msg, f"expected permission denial, got: {msg}"
    assert _pgcode(excinfo) == "42501", (
        f"expected SQLSTATE 42501 (insufficient_privilege), got {_pgcode(excinfo)!r}"
    )


def test_audit_log_truncate_partition_as_app_owner_blocked_by_trigger(
    pg_engine: Engine,
) -> None:
    """``app_owner`` TRUNCATE on a yearly partition → trigger layer (SQLSTATE P0001).

    Postgres treats a partitioned-table REVOKE as parent-only — the
    GRANT ALL ON ALL TABLES wildcard from 0006 leaves ``app_owner`` with
    TRUNCATE on each yearly partition (``audit_log_y2026`` etc.). That
    means TRUNCATE on a partition gets past the permission gate and hits
    the per-partition ``audit_log_y2026_no_truncate`` BEFORE TRUNCATE
    trigger registered in 0005 (the spec's EVENT TRIGGER pattern; the
    migration uses statement-level triggers because Postgres doesn't
    accept TRUNCATE TABLE as an event trigger tag — see the comment block
    in 0005 lines 56-69).
    """
    _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError, ProgrammingError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE app_owner"))
            conn.execute(text("TRUNCATE TABLE audit_log_y2026"))
    msg = str(excinfo.value).lower()
    assert "truncate forbidden" in msg, f"expected trigger message, got: {msg}"
    assert _pgcode(excinfo) == "P0001", (
        f"expected SQLSTATE P0001 (raise_exception), got {_pgcode(excinfo)!r}"
    )


def test_audit_log_update_default_role_sqlstate_is_p0001(pg_engine: Engine) -> None:
    """Locks SQLSTATE P0001 on the trigger path under the default superuser.

    Complements ``test_audit_log_update_raises_immutability_exception`` by
    asserting the SQLSTATE in addition to the message substring. Without
    this, a future migration could downgrade RAISE EXCEPTION → RAISE
    WARNING (SQLSTATE 01000) and the substring assertion alone would
    keep passing while the trigger silently let the mutation through.
    """
    keys = _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(
                text("UPDATE audit_log SET event_type = 'spoofed' WHERE sequence_no = :seq"),
                {"seq": keys["sequence_no"]},
            )
    assert _pgcode(excinfo) == "P0001", (
        f"expected SQLSTATE P0001 (raise_exception), got {_pgcode(excinfo)!r}"
    )


def test_audit_log_truncate_partition_default_role_sqlstate_is_p0001(
    pg_engine: Engine,
) -> None:
    """Locks SQLSTATE P0001 on the per-partition TRUNCATE trigger path.

    Complements ``test_audit_log_truncate_partition_also_raises``. Same
    rationale as the UPDATE SQLSTATE lock above — guards against silent
    severity downgrades on the per-partition triggers from 0005.
    """
    _seed_account_and_audit_row(pg_engine)
    with pytest.raises((DatabaseError, InternalError, ProgrammingError)) as excinfo:
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE audit_log_y2026"))
    assert _pgcode(excinfo) == "P0001", (
        f"expected SQLSTATE P0001 (raise_exception), got {_pgcode(excinfo)!r}"
    )
