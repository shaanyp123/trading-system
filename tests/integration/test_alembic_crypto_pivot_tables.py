"""Alembic up -> down -> up smoke test for the crypto-pivot C0-B1 tables.

Spins up postgres:16 via testcontainers, runs ``alembic upgrade
20260708_crypto_pivot_tables``, asserts the three new tables
(``funding_rates`` / ``cash_sweeps`` / ``product_metadata``) exist with
their idempotency constraints, runs ``alembic downgrade -1`` (back to
``20260608_order_placement_failed``), asserts they are gone, then
upgrades again and re-asserts. Canonical smoke contract per
``test_alembic_auth_tables.py`` (DP-022 form: targets the explicit
revision, not ``head``, so future migrations on top don't break it).

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
_CRYPTO_TABLES: tuple[str, ...] = (
    "funding_rates",
    "cash_sweeps",
    "product_metadata",
)
_PRIOR_REVISION = "20260608_order_placement_failed"
_CRYPTO_REVISION = "20260708_crypto_pivot_tables"


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
    def test_initial_upgrade_creates_all_three_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _CRYPTO_REVISION)
        assert _current_revision(engine) == _CRYPTO_REVISION
        for tname in _CRYPTO_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after upgrade"

    def test_downgrade_one_step_drops_all_three_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("downgrade", sync_url, "-1")
        assert _current_revision(engine) == _PRIOR_REVISION
        for tname in _CRYPTO_TABLES:
            assert not _table_exists(engine, tname), f"{tname} still present after downgrade -1"

    def test_second_upgrade_recreates_all_three_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, _CRYPTO_REVISION)
        assert _current_revision(engine) == _CRYPTO_REVISION
        for tname in _CRYPTO_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after second upgrade"


class TestSchemaContracts:
    """Constraint-level assertions the C0 writers depend on."""

    @pytest.fixture(autouse=True)
    def _ensure_at_revision(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        if _current_revision(engine) != _CRYPTO_REVISION:
            _run("upgrade", sync_url, _CRYPTO_REVISION)

    def test_funding_rates_hourly_ingest_is_idempotent(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        insert = text(
            "INSERT INTO funding_rates "
            "(product_id, observed_at_utc, rate_per_interval, interval_hours) "
            "VALUES ('BTC-PERP-CDE', '2026-07-08T00:00:00Z', '0.0000125', '1') "
            "ON CONFLICT (product_id, observed_at_utc) DO NOTHING"
        )
        with engine.begin() as conn:
            conn.execute(insert)
            conn.execute(insert)  # same hour re-observed: no dup, no error
            count = conn.execute(
                text("SELECT count(*) FROM funding_rates WHERE product_id = 'BTC-PERP-CDE'")
            ).scalar_one()
        assert count == 1

    def test_cash_sweeps_rejects_bad_direction_and_nonpositive_amount(
        self, pg: tuple[Engine, str]
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        engine, _ = pg
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO cash_sweeps "
                        "(requested_at_utc, direction, amount_usd, reason) "
                        "VALUES (now(), 'sideways', '100', 'x')"
                    )
                )
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO cash_sweeps "
                        "(requested_at_utc, direction, amount_usd, reason) "
                        "VALUES (now(), 'to_yield', '0', 'x')"
                    )
                )

    def test_product_metadata_requires_raw_snapshot(self, pg: tuple[Engine, str]) -> None:
        from sqlalchemy.exc import IntegrityError

        engine, _ = pg
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO product_metadata (product_id, captured_at_utc) "
                        "VALUES ('ETH-PERP-CDE', now())"
                    )
                )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO product_metadata (product_id, captured_at_utc, raw) "
                    "VALUES ('ETH-PERP-CDE', now(), '{}'::jsonb)"
                )
            )
            count = conn.execute(
                text("SELECT count(*) FROM product_metadata WHERE product_id = 'ETH-PERP-CDE'")
            ).scalar_one()
        assert count == 1
