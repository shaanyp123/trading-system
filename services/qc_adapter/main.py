"""services/qc_adapter/main.py — process entrypoint module.

Run-time wiring:

  1. Configure structlog (JSON renderer in production; console in dev).
  2. Build the Postgres engine + async_sessionmaker (``app_service`` role).
  3. Resolve the active account_id via ``db.fetch_active_account_id``.
  4. Open a long-lived :class:`QCObjectStoreClient`.
  5. Instantiate :class:`Orchestrator` + ``await orch.run_forever()``.
  6. Trap SIGTERM/SIGINT, request_stop, and shut down cleanly.

Invoked by ``services/qc_adapter/entrypoint.py`` (which decrypts sops
secrets → env vars first). Direct invocation is supported for local
development when env vars are set externally.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.audit.writer import Environment

from .config import QCAdapterSettings, get_settings
from .db import fetch_active_account_id
from .orchestrator import Orchestrator
from .qc_objectstore_client import QCObjectStoreClient


def _narrow_environment(env: str) -> Environment:
    """Narrow ``settings.environment`` ('dev'/'paper'/'live-small'/'live-scale')
    to the 3-value audit Environment Literal.

    Maps the dev escape hatch to 'paper' for the audit env tag (dev runs
    in operator workstations + would never write audit rows in
    production); rejects unknown values loudly.
    """
    if env in ("paper", "live-small", "live-scale"):
        return env  # type: ignore[return-value]
    if env == "dev":
        return "paper"
    raise ValueError(f"unrecognized environment: {env!r}")


def _configure_logging(settings: QCAdapterSettings) -> None:
    """Configure structlog per dev-guide §3.5."""
    level_name = settings.log_level
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    log_level = level_map.get(level_name, logging.INFO)
    is_dev = settings.environment == "dev"

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.dev.ConsoleRenderer() if is_dev else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _run(settings: QCAdapterSettings) -> int:
    log = structlog.get_logger().bind(service_name="qc_adapter")
    log.info(
        "qc_adapter_booting",
        environment=settings.environment,
        version=settings.version,
        phase_at_emit=settings.phase_at_emit,
    )

    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=4,
        max_overflow=4,
        pool_pre_ping=True,
        future=True,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    account_id = settings.account_id
    if account_id is None:
        async with session_factory() as session:
            account_id = await fetch_active_account_id(session)
        if account_id is None:
            log.error(
                "qc_adapter_no_active_account",
                detail=(
                    "accounts table has no row with active_to IS NULL — "
                    "operator must bootstrap via the api setup wizard "
                    "before qc_adapter can attribute signals"
                ),
            )
            await engine.dispose()
            return 3
    log.info("qc_adapter_account_resolved", account_id=str(account_id))

    client = QCObjectStoreClient(
        user_id=settings.qc_user_id,
        api_token=settings.qc_api_token.get_secret_value(),
        base_url=settings.qc_objectstore_base_url,
        timeout_seconds=settings.qc_http_timeout_seconds,
    )

    # ``settings.environment`` includes ``"dev"`` (used for local
    # development); the Orchestrator's ``Environment`` type narrows to
    # the 3 production envs that ``audit_log.env`` CHECK accepts. The
    # cast is safe because qc_adapter is never run with env="dev" in
    # production (the entrypoint fails closed without the sops bundle).
    env_literal = _narrow_environment(settings.environment)
    orchestrator = Orchestrator(
        client=client,
        session_factory=session_factory,
        account_id=account_id,
        env=env_literal,
        phase_at_emit=settings.phase_at_emit,
    )

    loop = asyncio.get_running_loop()
    stop_signals = (signal.SIGTERM, signal.SIGINT)
    for sig in stop_signals:
        try:
            loop.add_signal_handler(sig, orchestrator.request_stop)
        except NotImplementedError:
            # Windows tests / non-asyncio-friendly platforms.
            pass

    exit_code = 0
    try:
        async with orchestrator:
            await orchestrator.run_forever()
    except Exception:
        log.exception("qc_adapter_uncaught_in_run_forever")
        exit_code = 4
    finally:
        await client.aclose()
        await engine.dispose()
        log.info("qc_adapter_stopped")
    return exit_code


def main() -> int:
    settings = get_settings()
    _configure_logging(settings)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    sys.exit(main())
