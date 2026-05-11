"""Alembic up -> down -> up smoke test for the Day 21 auth tables migration.

Spins up postgres:16 via testcontainers, runs ``alembic upgrade head`` (which
applies every migration through ``20260513_auth_tables``), asserts the four
new auth tables exist with the column shapes mandated by backend-spec §8.5,
runs ``alembic downgrade -1`` (back to ``20260509_qc_adapter_cursor_seed``),
asserts the four tables are gone, then ``alembic upgrade head`` again and
re-asserts. This is the canonical smoke contract for the new migration --
proves both ``upgrade()`` and ``downgrade()`` are idempotent and reversible.

Skips cleanly when Docker is unreachable -- the same fail-soft pattern used
by ``test_audit_immutability.py``. Pinned to ``postgres:16`` to match the
production image.

A22 enforced: real Postgres triggers via testcontainers, not mocks.
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
_AUTH_TABLES: tuple[str, ...] = (
    "webauthn_credentials",
    "totp_secrets",
    "backup_codes",
    "sessions",
)
_PRIOR_REVISION = "20260509_qc_adapter_cursor_seed"
_AUTH_REVISION = "20260513_auth_tables"


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
        sync_url = container.get_connection_url()  # postgresql+psycopg2://...
        engine = create_engine(sync_url)
        yield engine, sync_url
        engine.dispose()


class TestUpDownUp:
    """alembic upgrade head -> downgrade -1 -> upgrade head."""

    def test_initial_upgrade_creates_all_four_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, "head")
        assert _current_revision(engine) == _AUTH_REVISION
        for tname in _AUTH_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after upgrade head"

    def test_downgrade_one_step_drops_all_four_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("downgrade", sync_url, "-1")
        assert _current_revision(engine) == _PRIOR_REVISION
        for tname in _AUTH_TABLES:
            assert not _table_exists(engine, tname), f"{tname} still present after downgrade -1"

    def test_second_upgrade_recreates_all_four_tables(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        _run("upgrade", sync_url, "head")
        assert _current_revision(engine) == _AUTH_REVISION
        for tname in _AUTH_TABLES:
            assert _table_exists(engine, tname), f"{tname} missing after second upgrade head"


class TestSchemaShape:
    """Column-level assertions against backend-spec §8.5.1/§8.5.2/§8.5.3.

    Module-scoped fixture leaves the schema at ``head`` after the up-down-up
    cycle above; these tests run after and can read the live schema.
    """

    @pytest.fixture(autouse=True)
    def _ensure_head(self, pg: tuple[Engine, str]) -> None:
        engine, sync_url = pg
        # Defensive: re-upgrade in case tests are re-ordered.
        if _current_revision(engine) != _AUTH_REVISION:
            _run("upgrade", sync_url, "head")

    @staticmethod
    def _columns(engine: Engine, table: str) -> dict[str, dict[str, object]]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT column_name, data_type, udt_name, is_nullable,
                           column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :t
                    ORDER BY ordinal_position
                    """
                ),
                {"t": table},
            ).all()
        return {
            r[0]: {
                "data_type": r[1],
                "udt_name": r[2],
                "nullable": r[3] == "YES",
                "default": r[4],
            }
            for r in rows
        }

    def test_webauthn_credentials_columns(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        cols = self._columns(engine, "webauthn_credentials")
        assert set(cols) == {
            "id",
            "account_id",
            "credential_id",
            "public_key",
            "sign_count",
            "authenticator_attachment",
            "registered_at_utc",
            "last_used_at_utc",
            "nickname",
            "active",
        }
        assert cols["id"]["udt_name"] == "uuid"
        assert cols["credential_id"]["udt_name"] == "bytea"
        assert cols["public_key"]["udt_name"] == "bytea"
        assert cols["sign_count"]["udt_name"] == "int4"
        assert cols["registered_at_utc"]["udt_name"] == "timestamptz"
        assert cols["last_used_at_utc"]["udt_name"] == "timestamptz"
        assert cols["active"]["udt_name"] == "bool"
        assert not cols["id"]["nullable"]
        assert not cols["account_id"]["nullable"]
        assert cols["last_used_at_utc"]["nullable"]
        assert cols["nickname"]["nullable"]

    def test_totp_secrets_columns(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        cols = self._columns(engine, "totp_secrets")
        assert set(cols) == {
            "account_id",
            "encrypted_secret",
            "enrolled_at_utc",
            "last_used_at_utc",
        }
        assert cols["account_id"]["udt_name"] == "uuid"
        assert cols["encrypted_secret"]["udt_name"] == "bytea"
        assert cols["enrolled_at_utc"]["udt_name"] == "timestamptz"
        assert not cols["account_id"]["nullable"]
        assert not cols["enrolled_at_utc"]["nullable"]

    def test_backup_codes_columns(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        cols = self._columns(engine, "backup_codes")
        assert set(cols) == {
            "id",
            "account_id",
            "code_hash",
            "used_at_utc",
            "generation_id",
        }
        assert cols["id"]["udt_name"] == "uuid"
        assert cols["account_id"]["udt_name"] == "uuid"
        assert cols["code_hash"]["udt_name"] == "text"
        assert cols["used_at_utc"]["udt_name"] == "timestamptz"
        assert cols["generation_id"]["udt_name"] == "uuid"
        assert not cols["code_hash"]["nullable"]
        assert cols["used_at_utc"]["nullable"]

    def test_sessions_columns(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        cols = self._columns(engine, "sessions")
        assert set(cols) == {
            "id",
            "account_id",
            "created_at_utc",
            "last_activity_utc",
            "last_uv_at_utc",
            "user_agent",
            "ip_address",
            "totp_bootstrap_only",
            "evicted_at_utc",
            "evicted_reason",
            "expires_at_utc",
        }
        assert cols["id"]["udt_name"] == "bytea"
        assert cols["account_id"]["udt_name"] == "uuid"
        assert cols["created_at_utc"]["udt_name"] == "timestamptz"
        assert cols["last_activity_utc"]["udt_name"] == "timestamptz"
        assert cols["last_uv_at_utc"]["udt_name"] == "timestamptz"
        assert cols["ip_address"]["udt_name"] == "inet"
        assert cols["totp_bootstrap_only"]["udt_name"] == "bool"
        assert cols["expires_at_utc"]["udt_name"] == "timestamptz"
        assert not cols["expires_at_utc"]["nullable"]
        assert cols["last_uv_at_utc"]["nullable"]

    def test_sessions_indexes_exist(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        with engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'sessions'")
                ).all()
            }
        assert "sessions_account" in names
        assert "sessions_expiry" in names

    def test_webauthn_credential_id_unique_constraint(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = 'webauthn_credentials'
                      AND c.contype = 'u'
                      AND EXISTS (
                          SELECT 1 FROM pg_attribute a
                          WHERE a.attrelid = t.oid
                            AND a.attname = 'credential_id'
                            AND a.attnum = ANY(c.conkey)
                      )
                    """
                )
            ).first()
        assert row is not None, "UNIQUE constraint on credential_id missing"

    def test_account_id_foreign_keys_present(self, pg: tuple[Engine, str]) -> None:
        engine, _ = pg
        with engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT t.relname
                        FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        JOIN pg_class r ON r.oid = c.confrelid
                        WHERE c.contype = 'f' AND r.relname = 'accounts'
                          AND t.relname IN (
                              'webauthn_credentials', 'totp_secrets',
                              'backup_codes', 'sessions'
                          )
                        """
                    )
                ).all()
            }
        assert names == {
            "webauthn_credentials",
            "totp_secrets",
            "backup_codes",
            "sessions",
        }, f"Missing FK -> accounts on: {set(_AUTH_TABLES) - names}"
