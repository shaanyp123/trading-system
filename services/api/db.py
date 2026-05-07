"""services/api/db.py — asyncpg connection pool wrapper.

Per dev-guide §7.2 (Async-First). The api container uses the `app_service`
Postgres role for all reads + non-audit writes (per backend-spec §8.2);
audit writes flow through services/audit which holds its own SERIALIZABLE-
isolated path (§5.1) and is not exercised in Day 5 scope.

Settings come from `services/api/config.APISettings.database_url`. The
SQLAlchemy URL must use `postgresql+asyncpg://` — `pool_pre_ping` survives
a transient Postgres restart without needing a service bounce.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from services.api.config import APISettings

log = structlog.get_logger()


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: APISettings) -> AsyncEngine:
    """Construct the asyncpg engine. Called once per process at startup."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=5,
        max_overflow=15,
        pool_timeout=30,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": "api_service",
                "statement_timeout": "30000",
            },
        },
        echo=False,
    )


async def init_pool(settings: APISettings) -> None:
    """Create the engine + sessionmaker. Idempotent across reloads."""
    global _engine, _session_factory
    if _engine is not None:
        return
    _engine = _build_engine(settings)
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    log.info("db_pool_initialized", pool_size=5, max_overflow=15)


async def close_pool() -> None:
    """Drain the pool on shutdown."""
    global _engine, _session_factory
    if _engine is None:
        return
    await _engine.dispose()
    _engine = None
    _session_factory = None
    log.info("db_pool_closed")


def get_engine() -> AsyncEngine:
    """Return the engine; raises if init_pool hasn't been called."""
    if _engine is None:
        raise RuntimeError("DB pool not initialized; call init_pool() first")
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager session for ad-hoc queries (e.g. health probe)."""
    if _session_factory is None:
        raise RuntimeError("DB pool not initialized; call init_pool() first")
    async with _session_factory() as session:
        yield session


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: per-request session that auto-rolls-back on raise."""
    if _session_factory is None:
        raise RuntimeError("DB pool not initialized; call init_pool() first")
    async with _session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def ping(timeout_seconds: float = 0.2) -> tuple[bool, str | None]:
    """Probe Postgres for the health endpoint.

    Returns (ok, error_message). Per backend-spec §7.3 the criterion is
    "Postgres reachable AND `SELECT 1` returns < 200ms".
    """
    if _engine is None:
        return False, "pool_not_initialized"
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, _short_error(exc)


def _short_error(exc: BaseException) -> str:
    """Truncate exception messages for inclusion in /api/health responses.

    Avoid leaking connection strings, query bodies, or stack frames into the
    public health endpoint payload.
    """
    msg = type(exc).__name__
    detail = str(exc).splitlines()[0] if str(exc) else ""
    if detail:
        msg = f"{msg}: {detail[:80]}"
    return msg


__all__ = [
    "close_pool",
    "get_engine",
    "get_session",
    "init_pool",
    "ping",
    "session_scope",
]


# Re-export for type hints in callers.
SessionFactory = async_sessionmaker[AsyncSession]


def session_factory_for_tests() -> Any:
    """Test hook: returns the live sessionmaker (or None if pool not init)."""
    return _session_factory
