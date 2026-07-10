"""Alembic up -> down -> up smoke test for the USDC-interest capture tables.

Spins up postgres:16 via testcontainers, runs ``alembic upgrade
20260710_usdc_rewards_capture``, asserts the two new tables
(``usdc_reward_transactions`` / ``cash_balance_snapshots``) exist, runs
``alembic downgrade -1`` (back to ``20260710_conv_clean_day``), asserts
they are gone, then upgrades again and re-asserts. Canonical smoke
contract per ``test_alembic_crypto_pivot_tables.py`` (DP-022 form:
targets the explicit revision, not ``head``).

Additionally pins the RE-FIRE IDEMPOTENCY the capture job's restart
semantics rely on (risk-review 2026-07-10 concern 2): the writers'
``ON CONFLICT ... DO NOTHING`` keys must agree with the migration's
unique constraints on a REAL Postgres — a duplicate
``venue_transaction_id`` and a duplicate ``snapshot_date_utc`` must
insert nothing and raise nothing.

Skips cleanly when Docker is unreachable.

A22 enforced: real Postgres via testcontainers, not mocks.
A02 binding: exercises ``alembic/**`` (forbidden whitelist).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import CursorResult, Engine, create_engine, text
from sqlalchemy.sql.elements import TextClause

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer


REPO_ROOT = Path(__file__).resolve().parents[2]
_CAPTURE_TABLES: tuple[str, ...] = (
    "usdc_reward_transactions",
    "cash_balance_snapshots",
)
_PRIOR_REVISION = "20260710_conv_clean_day"
_CAPTURE_REVISION = "20260710_usdc_rewards_capture"


def _require_docker() -> None:
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon unavailable: {exc}")


def _alembic_config(sync_url: str) -> object:
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg


def _run(cmd: str, sync_url: str, target: str) -> None:
    from alembic import command

    prev_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sync_url
    try:
        cfg = _alembic_config(sync_url)
        getattr(command, cmd)(cfg, target)
    finally:
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url


def _table_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": name},
        ).first()
    return row is not None


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


@pytest.fixture(scope="module")
def pg() -> Iterator[tuple[Engine, str]]:
    _require_docker()
    with PostgresContainer("postgres:16") as container:
        sync_url = container.get_connection_url()
        engine = create_engine(sync_url)
        yield engine, sync_url
        engine.dispose()


class TestUpDownUp:
    def test_initial_upgrade_creates_both_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _CAPTURE_REVISION)
        assert _current_revision(engine) == _CAPTURE_REVISION
        for tname in _CAPTURE_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after upgrade"

    def test_downgrade_one_step_drops_both_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("downgrade", sync_url, "-1")
        assert _current_revision(engine) == _PRIOR_REVISION
        for tname in _CAPTURE_TABLES:
            assert not _table_exists(engine, tname), f"{tname} still present after downgrade -1"

    def test_second_upgrade_recreates_both_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _CAPTURE_REVISION)
        assert _current_revision(engine) == _CAPTURE_REVISION
        for tname in _CAPTURE_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after second upgrade"


class TestReFireIdempotency:
    """The writers' ON CONFLICT keys vs the migration's unique constraints."""

    @pytest.fixture(autouse=True)
    def _ensure_at_revision(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        if _current_revision(engine) != _CAPTURE_REVISION:
            _run("upgrade", sync_url, _CAPTURE_REVISION)

    def test_duplicate_venue_transaction_id_inserts_nothing(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        insert = text(
            "INSERT INTO usdc_reward_transactions "
            "(venue_transaction_id, account_currency, transaction_type, amount, "
            " currency, venue_created_at_utc, raw) "
            "VALUES ('tx-dup', 'USDC', 'interest', '0.48', "
            "        'USDC', '2026-07-10T00:00:00Z', '{}') "
            "ON CONFLICT (venue_transaction_id) DO NOTHING"
        )
        with engine.begin() as conn:
            first = conn.execute(insert)
            second = conn.execute(insert)  # daily re-poll re-sees the row
            count = conn.execute(
                text(
                    "SELECT count(*) FROM usdc_reward_transactions "
                    "WHERE venue_transaction_id = 'tx-dup'"
                )
            ).scalar_one()
        assert first.rowcount == 1
        assert second.rowcount == 0  # the job's ledger_rows_new counting contract
        assert count == 1

    def test_duplicate_snapshot_date_first_capture_wins(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg

        def _insert(spot: str) -> TextClause:
            return text(
                "INSERT INTO cash_balance_snapshots "
                "(snapshot_date_utc, captured_at_utc, spot_usd, cbi_usdc, raw) "
                f"VALUES ('2026-07-10', '2026-07-10T00:20:00Z', '{spot}', NULL, '{{}}') "
                "ON CONFLICT (snapshot_date_utc) DO NOTHING"
            )

        with engine.begin() as conn:
            first: CursorResult[Any] = conn.execute(_insert("1397.01"))
            second: CursorResult[Any] = conn.execute(_insert("999.99"))  # mid-day redeploy re-fire
            row = conn.execute(
                text(
                    "SELECT spot_usd FROM cash_balance_snapshots "
                    "WHERE snapshot_date_utc = '2026-07-10'"
                )
            ).one()
        assert first.rowcount == 1
        assert second.rowcount == 0
        assert str(row.spot_usd) == "1397.01"  # the 00:20 observation survived
