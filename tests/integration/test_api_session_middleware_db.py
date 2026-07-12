"""Docker-gated integration tests for the real session-validation loader.

Drives ``SessionMiddleware._load_session_context_from_db`` against a REAL
Postgres (testcontainers + alembic head) — the unit suite fakes this seam
per [A22]. Uses the same fixture pattern as
``tests/integration/test_auth_session_lifecycle.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from services.api.auth.sessions import create_session, mint_session_id, revoke_session
from services.api.config import APISettings
from services.api.session import SessionMiddleware

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
        sync_url = container.get_connection_url()
        _alembic_upgrade(sync_url)
        async_url = sync_url.replace("+psycopg2", "+asyncpg")
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


@pytest.fixture
def middleware(pg_engine: AsyncEngine) -> SessionMiddleware:
    """A SessionMiddleware whose session_scope is bound to the test engine.

    The ASGI app is never invoked — these tests call the DB loader directly.
    """

    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(pg_engine, expire_on_commit=False) as session:
            yield session

    settings = APISettings(
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1:0/unused",
        environment="paper",
    )

    async def _never_app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("ASGI app must not be invoked by these tests")

    return SessionMiddleware(_never_app, settings=settings, session_scope=_scope)  # type: ignore[arg-type]


async def _mint_row(
    db: AsyncSession,
    account_id: UUID,
    *,
    weak: bool = False,
    uv_at: datetime | None = None,
) -> bytes:
    row = await create_session(
        db,
        account_id=account_id,
        totp_bootstrap_only=weak,
        last_uv_at_utc=uv_at,
        user_agent="pytest",
        ip_address="127.0.0.1",
        idle_seconds=_IDLE_SECONDS,
        absolute_seconds=_ABSOLUTE_SECONDS,
    )
    return row.session_id


class TestLoaderHappyPath:
    @pytest.mark.asyncio
    async def test_strong_session_builds_real_context(
        self,
        middleware: SessionMiddleware,
        db_session: AsyncSession,
        seeded_account: UUID,
    ) -> None:
        uv = datetime.now(tz=UTC)
        sid = await _mint_row(db_session, seeded_account, uv_at=uv)

        ctx = await middleware._load_session_context_from_db(sid)

        assert ctx is not None
        assert ctx.user_id == str(seeded_account)
        # username reads the account's own identity (external_account_id —
        # the bootstrap flow writes the username there), never a config value.
        ext_id = (
            await db_session.execute(
                text("SELECT external_account_id FROM accounts WHERE id = :a"),
                {"a": seeded_account},
            )
        ).scalar_one()
        assert ctx.username == ext_id
        assert ctx.role == "owner"
        assert ctx.auth_strength == "strong"
        assert ctx.last_uv_at is not None
        assert ctx.is_phase0_stub is False
        # No credentials enrolled for this fresh account.
        assert ctx.webauthn_enrolled is False
        assert ctx.totp_enrolled is False
        # Effective expiry: idle window ends before the 24 h absolute one.
        assert ctx.session_expires_at <= datetime.now(tz=UTC) + timedelta(
            seconds=_IDLE_SECONDS + 60
        )

    @pytest.mark.asyncio
    async def test_totp_only_session_is_weak(
        self,
        middleware: SessionMiddleware,
        db_session: AsyncSession,
        seeded_account: UUID,
    ) -> None:
        sid = await _mint_row(db_session, seeded_account, weak=True, uv_at=None)

        ctx = await middleware._load_session_context_from_db(sid)

        assert ctx is not None
        assert ctx.auth_strength == "weak"
        assert ctx.last_uv_at is None

    @pytest.mark.asyncio
    async def test_enrollment_flags_reflect_tables(
        self,
        middleware: SessionMiddleware,
        db_session: AsyncSession,
        seeded_account: UUID,
    ) -> None:
        from services.api.repos.auth import PostgresAuthRepo

        repo = PostgresAuthRepo(db_session)
        await repo.insert_webauthn_credential(
            account_id=seeded_account,
            credential_id=uuid.uuid4().bytes,
            public_key=b"test-public-key",
            sign_count=0,
            authenticator_attachment=None,
            nickname="pytest-key",
        )
        await repo.upsert_totp_secret(
            account_id=seeded_account, encrypted_secret=b"test-encrypted-secret"
        )
        await db_session.commit()
        sid = await _mint_row(db_session, seeded_account, uv_at=datetime.now(tz=UTC))

        ctx = await middleware._load_session_context_from_db(sid)

        assert ctx is not None
        assert ctx.webauthn_enrolled is True
        assert ctx.totp_enrolled is True

    @pytest.mark.asyncio
    async def test_successful_load_refreshes_activity(
        self,
        middleware: SessionMiddleware,
        db_session: AsyncSession,
        seeded_account: UUID,
    ) -> None:
        sid = await _mint_row(db_session, seeded_account, uv_at=datetime.now(tz=UTC))
        stale = datetime.now(tz=UTC) - timedelta(minutes=10)
        await db_session.execute(
            text("UPDATE sessions SET last_activity_utc = :t WHERE id = :sid"),
            {"t": stale, "sid": sid},
        )
        await db_session.commit()

        ctx = await middleware._load_session_context_from_db(sid)
        assert ctx is not None

        refreshed = (
            await db_session.execute(
                text("SELECT last_activity_utc FROM sessions WHERE id = :sid"),
                {"sid": sid},
            )
        ).scalar_one()
        assert refreshed > stale + timedelta(minutes=5)


class TestLoaderRejects:
    @pytest.mark.asyncio
    async def test_unknown_session_id_is_none(self, middleware: SessionMiddleware) -> None:
        assert await middleware._load_session_context_from_db(mint_session_id()) is None

    @pytest.mark.asyncio
    async def test_revoked_session_is_none(
        self,
        middleware: SessionMiddleware,
        db_session: AsyncSession,
        seeded_account: UUID,
    ) -> None:
        sid = await _mint_row(db_session, seeded_account, uv_at=datetime.now(tz=UTC))
        await revoke_session(db_session, session_id=sid, reason="explicit_logout")

        assert await middleware._load_session_context_from_db(sid) is None

    @pytest.mark.asyncio
    async def test_idle_expired_session_is_none_and_revoked(
        self,
        middleware: SessionMiddleware,
        db_session: AsyncSession,
        seeded_account: UUID,
    ) -> None:
        sid = await _mint_row(db_session, seeded_account, uv_at=datetime.now(tz=UTC))
        idle_past = datetime.now(tz=UTC) - timedelta(seconds=_IDLE_SECONDS + 60)
        await db_session.execute(
            text("UPDATE sessions SET last_activity_utc = :t WHERE id = :sid"),
            {"t": idle_past, "sid": sid},
        )
        await db_session.commit()

        assert await middleware._load_session_context_from_db(sid) is None

        reason = (
            await db_session.execute(
                text("SELECT evicted_reason FROM sessions WHERE id = :sid"),
                {"sid": sid},
            )
        ).scalar_one()
        assert reason == "idle_timeout"

    @pytest.mark.asyncio
    async def test_absolute_expired_session_is_none(
        self,
        middleware: SessionMiddleware,
        db_session: AsyncSession,
        seeded_account: UUID,
    ) -> None:
        sid = await _mint_row(db_session, seeded_account, uv_at=datetime.now(tz=UTC))
        past = datetime.now(tz=UTC) - timedelta(seconds=60)
        await db_session.execute(
            text("UPDATE sessions SET expires_at_utc = :t WHERE id = :sid"),
            {"t": past, "sid": sid},
        )
        await db_session.commit()

        assert await middleware._load_session_context_from_db(sid) is None
