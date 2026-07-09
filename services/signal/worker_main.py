"""services/signal/worker_main.py — strategy-worker container runner.

Runtime entrypoint for the ``strategy_worker`` compose service (delta
spec §3.3). Invoked as ``python -m services.signal.worker_main`` by
:mod:`services.signal.worker_entrypoint` (which maps the host secrets
file onto the ``API_*`` env vars first — the worker reuses the api
image + entrypoint mapping, so DB URL and Coinbase CDP credentials
arrive exactly the way the api receives them).

Responsibilities:

1. Configure structlog (JSON, UTC timestamps — mirrors
   ``services.webhook_pusher.__main__``; [A25]).
2. Build the async engine/session factory from ``API_DATABASE_URL``.
3. Build the authenticated broker transport
   (:class:`~services.execution.coinbase_client.SdkCoinbaseBrokerClient`)
   — **fail closed** when the CDP credentials are missing: a strategy
   worker that cannot trade must not start half-alive.
4. Construct :class:`~services.signal.strategy_worker.StrategyWorker`
   and run it; SIGTERM/SIGINT request a graceful stop (in-flight
   execution legs are cancelled; deterministic client_order_ids + the
   adapter's startup reconcile make the next boot idempotent — gate A3).

A02 BINDS — ``services/signal/**`` forbidden path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.audit.writer import Environment
from services.execution.coinbase_adapter import CoinbaseExecutionAdapter
from services.execution.coinbase_client import SdkCoinbaseBrokerClient
from services.signal.strategy_worker import (
    StrategyWorker,
    StrategyWorkerConfig,
    StrategyWorkerStore,
)

log = structlog.get_logger()


def _configure_logging() -> None:
    """JSON structlog config mirroring services.webhook_pusher.__main__."""
    level_name = os.environ.get("STRATEGY_WORKER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _env_from_environ() -> Environment:
    raw = os.environ.get("API_ENVIRONMENT", "paper")
    if raw in ("paper", "live-small", "live-scale"):
        return raw  # type: ignore[return-value]  # narrowed by the membership check
    log.warning("strategy_worker_env_fallback_paper", requested=raw)
    return "paper"


def _decimal_env(name: str, default: Decimal | None) -> Decimal | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if raw.strip().lower() in ("none", "off", "disabled"):
        return None
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        log.warning("strategy_worker_bad_decimal_env", name=name, value=raw)
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def build_config() -> StrategyWorkerConfig:
    """StrategyWorkerConfig from env (defaults = Phase-A small-live)."""
    return StrategyWorkerConfig(
        env=_env_from_environ(),
        risk_tick_interval_s=float(os.environ.get("STRATEGY_WORKER_RISK_TICK_SECONDS", "30")),
        e_effective_cap_usd=_decimal_env("STRATEGY_WORKER_E_EFFECTIVE_CAP_USD", Decimal("1500")),
        max_contracts={
            "BTC": _int_env("STRATEGY_WORKER_MAX_BTC_CONTRACTS", 2),
            "ETH": _int_env("STRATEGY_WORKER_MAX_ETH_CONTRACTS", 4),
        },
        heartbeat_file=os.environ.get(
            "STRATEGY_WORKER_HEARTBEAT_FILE",
            "/tmp/strategy-worker.heartbeat",  # noqa: S108
        ),
        ws_url=os.environ.get("STRATEGY_WORKER_WS_URL", "wss://advanced-trade-ws.coinbase.com"),
    )


async def _amain() -> int:
    database_url = os.environ.get("API_DATABASE_URL")
    if not database_url:
        log.error("strategy_worker_missing_database_url")
        return 2
    key_name = os.environ.get("API_COINBASE_API_KEY_NAME")
    private_key = os.environ.get("API_COINBASE_API_PRIVATE_KEY")
    if not key_name or not private_key:
        # Fail closed: nothing that trades starts without credentials
        # (services/api/entrypoint.py maps secrets coinbase.* → these).
        log.error(
            "strategy_worker_missing_coinbase_credentials_fail_closed",
            note="populate coinbase.api_key_name/api_private_key in the host secrets file",
        )
        return 2

    engine = create_async_engine(database_url, pool_size=5, max_overflow=5)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    config = build_config()
    broker = SdkCoinbaseBrokerClient(api_key_name=key_name, api_private_key=private_key)
    adapter = CoinbaseExecutionAdapter(client=broker)
    store = StrategyWorkerStore(
        session_factory=session_factory,
        env=config.env,
        phase_at_emit=config.phase_at_emit,
    )
    worker = StrategyWorker(
        config=config,
        store=store,
        broker=broker,
        adapter=adapter,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)

    log.info("strategy_worker_main_starting", env=config.env)
    try:
        await worker.run_forever()
    finally:
        await engine.dispose()
    log.info("strategy_worker_main_exited")
    return 0


def main() -> int:
    _configure_logging()
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
