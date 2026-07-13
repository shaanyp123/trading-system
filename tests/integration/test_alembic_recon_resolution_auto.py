"""Alembic up -> down -> up smoke test for the ``auto_rereconciled`` CHECK.

C1 recon-hardening (2026-07-13, the #375 "manual" cosmetic). Spins up
postgres:16 via testcontainers, runs ``alembic upgrade
20260713_recon_resolution_auto``, asserts a ``reconciliation_breaks``
row with ``resolution_path='auto_rereconciled'`` INSERTs cleanly (and a
garbage path still violates the CHECK), runs ``alembic downgrade -1``
(back to ``20260710_usdc_rewards_capture``) and asserts
``auto_rereconciled`` is rejected again — AND that a pre-downgrade
``auto_rereconciled`` row was re-stamped ``manual`` (the downgrade's
evidence-preserving contract: UPDATE, never DELETE) — then upgrades and
re-asserts. Canonical smoke contract per
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
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer  # type: ignore[attr-defined]


REPO_ROOT = Path(__file__).resolve().parents[2]
_PRIOR_REVISION = "20260710_usdc_rewards_capture"
_RESOLUTION_AUTO_REVISION = "20260713_recon_resolution_auto"


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


def _insert_break_with_resolution_path(
    engine: Engine, resolution_path: str, *, commit: bool = False
) -> None:
    """INSERT (and by default roll back) a break row with ``resolution_path``.

    Raises on CHECK violation — exactly what the tests assert on.
    ``commit=True`` persists the row (used to pin the downgrade's
    re-stamp UPDATE against a real surviving row).
    """
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            account_id = conn.execute(
                text(
                    "INSERT INTO accounts (external_account_id, account_type, active_from) "
                    "VALUES (:ext, 'individual', now()) "
                    "RETURNING id"
                ),
                {"ext": f"itest-resolution-{uuid4().hex[:12]}"},
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO reconciliation_breaks ("
                    "    account_id, detected_at_utc, metric, market,"
                    "    expected, actual, delta, tolerance, source,"
                    "    audit_event_uuid, resolved_at_utc, resolution_path"
                    ") VALUES ("
                    "    :acct, now(), 'position_qty', 'BTC',"
                    "    2, 1, 1, 0, 'coinbase_eod',"
                    "    :audit_uuid, now(), :path"
                    ")"
                ),
                {"acct": account_id, "audit_uuid": uuid4(), "path": resolution_path},
            )
        finally:
            if commit:
                trans.commit()
            else:
                trans.rollback()


class TestReconResolutionAutoMigration:
    def test_upgrade_allows_auto_rereconciled(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _RESOLUTION_AUTO_REVISION)
        assert _current_revision(engine) == _RESOLUTION_AUTO_REVISION
        # auto_rereconciled accepted...
        _insert_break_with_resolution_path(engine, "auto_rereconciled")
        # ...legacy paths still accepted...
        _insert_break_with_resolution_path(engine, "manual")
        _insert_break_with_resolution_path(engine, "grace_period")
        # ...garbage still rejected.
        with pytest.raises(Exception, match="reconciliation_breaks_resolution_path_check"):
            _insert_break_with_resolution_path(engine, "not_a_path")

    def test_downgrade_restamps_and_restores_prior_check(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _RESOLUTION_AUTO_REVISION)
        # Persist a real auto_rereconciled row so the downgrade's
        # re-stamp UPDATE has something to act on.
        _insert_break_with_resolution_path(engine, "auto_rereconciled", commit=True)
        _run("downgrade", sync_url, "-1")
        assert _current_revision(engine) == _PRIOR_REVISION
        # The surviving row was re-stamped 'manual' (never deleted).
        with engine.connect() as conn:
            paths = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT resolution_path FROM reconciliation_breaks "
                        "WHERE resolved_at_utc IS NOT NULL"
                    )
                ).fetchall()
            ]
        assert "auto_rereconciled" not in paths
        assert "manual" in paths
        # The narrower CHECK rejects the new value again; legacy fine.
        with pytest.raises(Exception, match="reconciliation_breaks_resolution_path_check"):
            _insert_break_with_resolution_path(engine, "auto_rereconciled")
        _insert_break_with_resolution_path(engine, "manual")

    def test_second_upgrade_reallows_auto_rereconciled(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _RESOLUTION_AUTO_REVISION)
        assert _current_revision(engine) == _RESOLUTION_AUTO_REVISION
        _insert_break_with_resolution_path(engine, "auto_rereconciled")


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
