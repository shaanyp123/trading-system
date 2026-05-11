"""Integration test for ``services.api.auth.sessions`` against real Postgres.

Exercises the full session-row lifecycle:

  * create_session -> row landed in ``sessions``
  * load_active_session -> returns the row when fresh
  * refresh_activity -> bumps ``last_activity_utc``
  * load_active_session with stale ``last_activity_utc`` -> None + auto-revoke
  * revoke_session -> stamps evicted_at_utc + evicted_reason
  * upgrade_strength_to_strong -> flips ``totp_bootstrap_only`` + stamps UV
  * stamp_uv -> updates ``last_uv_at_utc``

Uses the same testcontainers Postgres pattern as
``tests/integration/test_audit_immutability.py`` and
``tests/integration/test_alembic_auth_tables.py``. Skips on Docker-unavailable.

A22 binds (real Postgres triggers + grants + column types).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer  # type: ignore[attr-defined]


REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_IDLE_SECONDS: Final[int] = 30 * 60
_ABSOLUTE_SECONDS: Final[int] = 24 * 60 * 60


def _require_docker() -> None:
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon unavailable: {exc}")


def _alembic_upgrade(sync_url: str) -> None:
    from alembic.config import Config

    from alembic import command

    prev_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sync_url
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", sync_url)
        command.upgrade(cfg, "head")
    finally:
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    _require_docker()
    with PostgresContainer("postgres:16") as container:
        sync_url = container.get_connection_url()  # postgresql+psycopg2://...
        _alembic_upgrade(sync_url)
        async_url = sync_url.replace("+psycopg2", "+asyncpg")
        # NullPool so each AsyncSession opens + closes a fresh connection.
        # The default QueuePool keeps connections open across the event-loop
        # boundary that pytest-asyncio sets up per test, which trips
        # asyncpg's "another operation is in progress" check when the loop
        # restarts and the prior connection's task is still mid-flight.
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(async_url, echo=False, poolclass=NullPool)
        yield engine
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        yield session
        await session.close()


@pytest_asyncio.fixture
async def seeded_account(db_session: AsyncSession) -> UUID:
    """Insert a fresh accounts row per test; return its UUID.

    Each test gets a unique external_account_id (UUID-suffixed) so the
    UNIQUE constraint on accounts(external_account_id) WHERE active_to IS
    NULL doesn't collide across tests.
    """
    ext_id = f"TEST_ACCT_{uuid.uuid4().hex[:12]}"
    row = (
        await db_session.execute(
            text(
                """
                INSERT INTO accounts
                    (external_account_id, account_type, role, active_from)
                VALUES (:ext, 'individual', 'owner', now())
                RETURNING id
                """
            ),
            {"ext": ext_id},
        )
    ).fetchone()
    await db_session.commit()
    assert row is not None
    return row.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestCreateAndLoad:
    @pytest.mark.asyncio
    async def test_create_session_lands_row(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=False,
            last_uv_at_utc=datetime.now(tz=UTC),
            user_agent="pytest/1.0",
            ip_address="127.0.0.1",
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        assert row.account_id == seeded_account
        assert row.totp_bootstrap_only is False
        assert row.last_uv_at_utc is not None
        # Cross-check raw DB.
        result = (
            await db_session.execute(
                text("SELECT account_id, totp_bootstrap_only FROM sessions WHERE id = :sid"),
                {"sid": row.session_id},
            )
        ).fetchone()
        assert result is not None
        assert result.account_id == seeded_account
        assert result.totp_bootstrap_only is False

    @pytest.mark.asyncio
    async def test_load_active_session_returns_row(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=False,
            last_uv_at_utc=datetime.now(tz=UTC),
            user_agent=None,
            ip_address=None,
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        loaded = await sm.load_active_session(
            db_session, session_id=row.session_id, idle_seconds=_IDLE_SECONDS
        )
        assert loaded is not None
        assert loaded.session_id == row.session_id

    @pytest.mark.asyncio
    async def test_load_active_session_returns_none_for_unknown_id(
        self, db_session: AsyncSession
    ) -> None:
        from services.api.auth import sessions as sm

        unknown = b"\x42" * sm.SESSION_ID_BYTES
        loaded = await sm.load_active_session(
            db_session, session_id=unknown, idle_seconds=_IDLE_SECONDS
        )
        assert loaded is None


class TestRefreshActivity:
    @pytest.mark.asyncio
    async def test_refresh_bumps_last_activity_utc(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=False,
            last_uv_at_utc=None,
            user_agent=None,
            ip_address=None,
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        original_la = row.last_activity_utc
        # Sleep a small interval so the bump shows up at millisecond precision.
        import asyncio

        await asyncio.sleep(0.01)
        await sm.refresh_activity(db_session, session_id=row.session_id)
        reloaded = (
            await db_session.execute(
                text("SELECT last_activity_utc FROM sessions WHERE id = :sid"),
                {"sid": row.session_id},
            )
        ).fetchone()
        assert reloaded is not None
        assert reloaded.last_activity_utc > original_la


class TestRevoke:
    @pytest.mark.asyncio
    async def test_revoke_stamps_evicted_at_and_reason(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=False,
            last_uv_at_utc=None,
            user_agent=None,
            ip_address=None,
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        await sm.revoke_session(db_session, session_id=row.session_id, reason="explicit_logout")
        # Loading the revoked session returns None.
        loaded = await sm.load_active_session(
            db_session, session_id=row.session_id, idle_seconds=_IDLE_SECONDS
        )
        assert loaded is None
        # The row itself records the eviction.
        result = (
            await db_session.execute(
                text("SELECT evicted_at_utc, evicted_reason FROM sessions WHERE id = :sid"),
                {"sid": row.session_id},
            )
        ).fetchone()
        assert result is not None
        assert result.evicted_at_utc is not None
        assert result.evicted_reason == "explicit_logout"

    @pytest.mark.asyncio
    async def test_revoke_is_idempotent(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=False,
            last_uv_at_utc=None,
            user_agent=None,
            ip_address=None,
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        await sm.revoke_session(db_session, session_id=row.session_id, reason="first")
        await sm.revoke_session(db_session, session_id=row.session_id, reason="second")
        # The first reason wins -- second call is a no-op because the WHERE
        # clause guards on evicted_at_utc IS NULL.
        result = (
            await db_session.execute(
                text("SELECT evicted_reason FROM sessions WHERE id = :sid"),
                {"sid": row.session_id},
            )
        ).fetchone()
        assert result is not None
        assert result.evicted_reason == "first"


class TestIdleExpiry:
    @pytest.mark.asyncio
    async def test_idle_expired_session_returns_none_and_auto_revokes(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=False,
            last_uv_at_utc=None,
            user_agent=None,
            ip_address=None,
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        # Backdate last_activity_utc so the idle gate fires.
        await db_session.execute(
            text(
                "UPDATE sessions SET last_activity_utc = now() - interval '1 hour' WHERE id = :sid"
            ),
            {"sid": row.session_id},
        )
        await db_session.commit()

        loaded = await sm.load_active_session(
            db_session, session_id=row.session_id, idle_seconds=_IDLE_SECONDS
        )
        assert loaded is None
        # Auto-revoke stamped with reason "idle_timeout".
        result = (
            await db_session.execute(
                text("SELECT evicted_reason FROM sessions WHERE id = :sid"),
                {"sid": row.session_id},
            )
        ).fetchone()
        assert result is not None
        assert result.evicted_reason == "idle_timeout"


class TestStrengthUpgrade:
    @pytest.mark.asyncio
    async def test_upgrade_flips_totp_bootstrap_only_and_stamps_uv(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        # Start with a weak (TOTP-only) session.
        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=True,
            last_uv_at_utc=None,
            user_agent=None,
            ip_address=None,
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        uv_at = datetime.now(tz=UTC)
        await sm.upgrade_strength_to_strong(
            db_session, session_id=row.session_id, new_uv_at_utc=uv_at
        )
        # Reload and assert both fields updated.
        result = (
            await db_session.execute(
                text("SELECT totp_bootstrap_only, last_uv_at_utc FROM sessions WHERE id = :sid"),
                {"sid": row.session_id},
            )
        ).fetchone()
        assert result is not None
        assert result.totp_bootstrap_only is False
        assert result.last_uv_at_utc is not None


class TestUvStamp:
    @pytest.mark.asyncio
    async def test_stamp_uv_updates_last_uv_at(
        self, db_session: AsyncSession, seeded_account: UUID
    ) -> None:
        from services.api.auth import sessions as sm

        row = await sm.create_session(
            db_session,
            account_id=seeded_account,
            totp_bootstrap_only=False,
            last_uv_at_utc=None,
            user_agent=None,
            ip_address=None,
            idle_seconds=_IDLE_SECONDS,
            absolute_seconds=_ABSOLUTE_SECONDS,
        )
        # Initial state: last_uv_at_utc is NULL.
        new_uv = datetime.now(tz=UTC)
        await sm.stamp_uv(db_session, session_id=row.session_id, uv_at_utc=new_uv)
        result = (
            await db_session.execute(
                text("SELECT last_uv_at_utc FROM sessions WHERE id = :sid"),
                {"sid": row.session_id},
            )
        ).fetchone()
        assert result is not None
        assert result.last_uv_at_utc is not None


# ---------------------------------------------------------------------------
# Cookie encoding helpers
# ---------------------------------------------------------------------------
class TestCookieEncoding:
    def test_round_trip(self) -> None:
        from services.api.auth import sessions as sm

        raw = sm.mint_session_id()
        encoded = sm.encode_cookie_value(raw)
        decoded = sm.decode_cookie_value(encoded)
        assert decoded == raw

    def test_decode_returns_none_on_malformed_input(self) -> None:
        from services.api.auth import sessions as sm

        assert sm.decode_cookie_value("") is None
        assert sm.decode_cookie_value("not-base64!!!") is None
        assert sm.decode_cookie_value("AAAA") is None  # decodes to 3 bytes, not 32

    def test_mint_session_id_is_32_bytes(self) -> None:
        from services.api.auth import sessions as sm

        raw = sm.mint_session_id()
        assert len(raw) == sm.SESSION_ID_BYTES == 32
