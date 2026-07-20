"""Alembic up -> down -> up smoke test for the ``market_bars`` table.

Agentic-refinement data capture (2026-07-20): durable OHLCV (incl.
volume) bar store written by the market-data worker + the operator
backfill script. Spins up postgres:16 via testcontainers, runs
``alembic upgrade 20260720_market_bars``, asserts an idempotent bar
INSERT lands (and the second identical INSERT is a no-op via the PK
``ON CONFLICT DO NOTHING`` contract), asserts the granularity CHECK
rejects garbage and NULL volume is accepted (price columns stay NOT
NULL), runs ``alembic downgrade -1`` (back to
``20260719_day_start_captured_at``) and asserts the table is gone, then
upgrades and re-asserts. Canonical smoke contract per
``test_alembic_coinbase_recon_source.py`` (DP-022 form: targets the
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
_PRIOR_REVISION = "20260719_day_start_captured_at"
_MARKET_BARS_REVISION = "20260720_market_bars"


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


_INSERT = text(
    "INSERT INTO market_bars ("
    "    product_id, granularity, bar_start_utc,"
    "    open, high, low, close, volume, source, captured_at_utc"
    ") VALUES ("
    "    :pid, :gran, :start, 100, 101, 99, 100.5, :volume,"
    "    'coinbase_advanced', now()"
    ") ON CONFLICT (product_id, granularity, bar_start_utc) DO NOTHING"
)


class TestMarketBarsMigration:
    def test_upgrade_creates_table_with_idempotent_inserts(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _MARKET_BARS_REVISION)
        assert _current_revision(engine) == _MARKET_BARS_REVISION
        with engine.connect() as conn:
            params = {
                "pid": "BTC-USD",
                "gran": "ONE_DAY",
                "start": "2026-07-19T00:00:00+00:00",
                "volume": "1234.5",
            }
            first = conn.execute(_INSERT, params)
            second = conn.execute(_INSERT, params)  # idempotent re-run
            conn.commit()
            assert first.rowcount == 1
            assert second.rowcount == 0
            count = conn.execute(text("SELECT COUNT(*) FROM market_bars")).scalar()
            assert count == 1

    def test_null_volume_accepted_but_granularity_check_enforced(
        self, pg: tuple[Engine, str]
    ) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _MARKET_BARS_REVISION)
        with engine.connect() as conn:
            conn.execute(
                _INSERT,
                {
                    "pid": "ETH-USD",
                    "gran": "ONE_HOUR",
                    "start": "2026-07-19T11:00:00+00:00",
                    "volume": None,
                },
            )
            conn.commit()
        with engine.connect() as conn, pytest.raises(Exception, match="granularity"):
            conn.execute(
                _INSERT,
                {
                    "pid": "ETH-USD",
                    "gran": "FIVE_MINUTE",
                    "start": "2026-07-19T11:05:00+00:00",
                    "volume": None,
                },
            )

    def test_downgrade_drops_table_and_second_upgrade_recreates(
        self, pg: tuple[Engine, str]
    ) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _MARKET_BARS_REVISION)
        _run("downgrade", sync_url, "-1")
        assert _current_revision(engine) == _PRIOR_REVISION
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('market_bars') IS NOT NULL")).scalar()
        assert exists is False
        _run("upgrade", sync_url, _MARKET_BARS_REVISION)
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('market_bars') IS NOT NULL")).scalar()
        assert exists is True


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
