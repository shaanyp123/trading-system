"""Alembic up -> down -> up smoke test for the ``exit_proximity`` migration (PR-B).

PR-B of ``Docs/exit-proximity-design.md``. Spins up postgres:16 via
testcontainers, runs ``alembic upgrade 20260531_exit_proximity`` (applying every
migration through this one), asserts the ``exit_proximity`` table exists, runs
``alembic downgrade -1`` (back to ``20260529_recon_src_degraded``), asserts the
table is gone, then ``alembic upgrade`` again and re-asserts — proving both
``upgrade()`` and ``downgrade()`` are reversible.

Targets the EXPLICIT revision (not ``head``) so a future migration sitting on
top of this one doesn't move the goalpost (DP-022 lesson, mirrored from
``test_alembic_auth_tables.py``).

Skips cleanly when Docker is unreachable.

A02 binding: this test directly exercises ``alembic/**`` (forbidden whitelist).
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
_TABLE = "exit_proximity"
_REVISION = "20260531_exit_proximity"
_PRIOR_REVISION = "20260529_recon_src_degraded"


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


def _index_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"),
            {"name": name},
        ).first()
    return row is not None


@pytest.fixture(scope="module")
def pg() -> Iterator[tuple[Engine, str]]:
    _require_docker()
    with PostgresContainer("postgres:16") as container:
        sync_url = container.get_connection_url()
        engine = create_engine(sync_url)
        yield engine, sync_url
        engine.dispose()


class TestUpDownUp:
    """alembic upgrade <rev> -> downgrade -1 -> upgrade <rev>."""

    def test_initial_upgrade_creates_table_and_indexes(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _REVISION)
        assert _current_revision(engine) == _REVISION
        assert _table_exists(engine, _TABLE)
        assert _index_exists(engine, "idx_exit_proximity_market_cycle")
        assert _index_exists(engine, "idx_exit_proximity_cycle_ts")

    def test_downgrade_one_step_drops_table(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("downgrade", sync_url, "-1")
        assert _current_revision(engine) == _PRIOR_REVISION
        assert not _table_exists(engine, _TABLE)

    def test_second_upgrade_recreates_table(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _REVISION)
        assert _current_revision(engine) == _REVISION
        assert _table_exists(engine, _TABLE)


class TestCheckConstraints:
    """The CHECK constraints reject values outside the ExitState / gate_status /
    closest_exit / direction alphabets — the migration is the schema's guard
    against drift from the Python enums + wire Literals.

    Module-scoped fixture leaves the schema at ``_REVISION`` after the up-down-up
    cycle above; these run after and insert against the live table.
    """

    @pytest.fixture(autouse=True)
    def _ensure_upgraded(self, pg: tuple[Engine, str]) -> None:
        _, sync_url = pg
        _run("upgrade", sync_url, _REVISION)

    def _insert(self, engine: Engine, **overrides: str) -> None:
        cols = {
            "cycle_ts_utc": "now()",
            "session_date_et": "CURRENT_DATE",
            "market": "'/MES'",
            "direction": "'long'",
            "trend_flip_state": "'holding'",
            "stop_state": "'holding'",
            "reversal_state": "'holding'",
            "decommission_state": "'holding'",
            "overall_state": "'holding'",
            "closest_exit": "'trend_flip'",
            "gate_status": "'active'",
        }
        cols.update(overrides)
        sql = (
            f"INSERT INTO {_TABLE} ("
            + ", ".join(cols.keys())
            + ") VALUES ("
            + ", ".join(cols.values())
            + ")"
        )
        with engine.begin() as conn:
            conn.execute(text(sql))

    def test_valid_row_inserts(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        self._insert(engine)  # all-default values are valid → no raise
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {_TABLE}"))

    @pytest.mark.parametrize(
        ("column", "bad_value"),
        [
            ("overall_state", "'pass'"),  # entry-side GateState value, not ExitState
            ("stop_state", "'open'"),
            ("closest_exit", "'profit_target'"),  # V1 has no profit target
            ("gate_status", "'ok'"),  # entry-side gate_status value
            ("direction", "'flat'"),  # signals allow 'flat'; positions don't
        ],
    )
    def test_check_rejects_out_of_alphabet_value(
        self, pg: tuple[Engine, str], column: str, bad_value: str
    ) -> None:
        engine, _ = pg
        with pytest.raises(Exception, match=r"(?i)check"):
            self._insert(engine, **{column: bad_value})
