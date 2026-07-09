"""Alembic up -> down -> up smoke test for the ``coinbase_eod`` source CHECK.

Crypto-pivot C0 §3.5. Spins up postgres:16 via testcontainers, runs
``alembic upgrade 20260709_coinbase_recon_src``, asserts a ``balances``
row with ``source='coinbase_eod'`` INSERTs cleanly (and a garbage source
still violates the CHECK), runs ``alembic downgrade -1`` (back to
``20260708_crypto_pivot_tables``) and asserts ``coinbase_eod`` is
rejected again, then upgrades and re-asserts. Canonical smoke contract
per ``test_alembic_crypto_pivot_tables.py`` (DP-022 form: targets the
explicit revision, not ``head``).

Skips cleanly when Docker is unreachable.

A22 enforced: real Postgres via testcontainers, not mocks.
A02 binding: exercises ``alembic/**`` (forbidden whitelist).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer  # type: ignore[attr-defined]


REPO_ROOT = Path(__file__).resolve().parents[2]
_PRIOR_REVISION = "20260708_crypto_pivot_tables"
_RECON_SRC_REVISION = "20260709_coinbase_recon_src"


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


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def _insert_balance_with_source(engine: Engine, source: str) -> None:
    """INSERT (and immediately roll back) a balances row with ``source``.

    Raises on CHECK violation — exactly what the tests assert on.
    """
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            account_id = conn.execute(
                text(
                    "INSERT INTO accounts (external_account_id, account_type, active_from) "
                    "VALUES ('itest-recon-src', 'individual', now()) "
                    "RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO balances ("
                    "    account_id, snapshot_ts, net_liquidation, cash_usd, "
                    "    excess_liquidity, used_margin_pct, source"
                    ") VALUES ("
                    "    :acct, now(), 100, 100, 100, 0, :source"
                    ")"
                ),
                {"acct": account_id, "source": source},
            )
        finally:
            trans.rollback()


@pytest.fixture(scope="module")
def pg() -> Iterator[tuple[Engine, str]]:
    _require_docker()
    with PostgresContainer("postgres:16") as container:
        sync_url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        engine = create_engine(sync_url)
        try:
            yield engine, sync_url
        finally:
            engine.dispose()


class TestCoinbaseReconSourceMigration:
    def test_upgrade_allows_coinbase_eod(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _RECON_SRC_REVISION)
        assert _current_revision(engine) == _RECON_SRC_REVISION
        # coinbase_eod accepted...
        _insert_balance_with_source(engine, "coinbase_eod")
        # ...legacy sources still accepted...
        _insert_balance_with_source(engine, "tws_api")
        # ...garbage still rejected.
        with pytest.raises(Exception, match="balances_source_check"):
            _insert_balance_with_source(engine, "not_a_source")

    def test_downgrade_restores_pre_pivot_check(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _RECON_SRC_REVISION)
        _run("downgrade", sync_url, "-1")
        assert _current_revision(engine) == _PRIOR_REVISION
        with pytest.raises(Exception, match="balances_source_check"):
            _insert_balance_with_source(engine, "coinbase_eod")
        _insert_balance_with_source(engine, "flexquery_eod")

    def test_second_upgrade_reallows_coinbase_eod(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _RECON_SRC_REVISION)
        assert _current_revision(engine) == _RECON_SRC_REVISION
        _insert_balance_with_source(engine, "coinbase_eod")
