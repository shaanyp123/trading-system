"""services/api/main.py — FastAPI app entrypoint.

Wired in execution order:

  1. structlog configured for JSON output on production / Console on dev.
  2. Settings loaded from env (sops-decrypted bundle exported by entrypoint).
  3. Lifespan:
       - Init asyncpg pool.
       - First-boot bootstrap: if no unconsumed/unexpired owner setup_token
         exists, create one, print raw to stdout, store hash. Idempotent.
       - Worker-PR-1 follow-up (post-pivot 2026-05-12): start the
         api-resident OrderPlacementWorker if an active accounts row +
         IBKR account number resolve. Best-effort — a fresh deploy
         without an accounts row or ib_gateway connectivity skips
         worker startup with a warning rather than crashing.
  4. Middleware (RequestContext + CSRF + optional CORS).
  5. Error handlers (canonical envelope per dev-guide §3.6).
  6. Routes (health, setup, sse).

Day 5 scope deliberately omits: WebAuthn, TOTP, sessions, audit writes,
risk endpoints, signals. Those land Phase-0 Week-2+ behind the existing
forbidden-paths CI gate.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import FastAPI

from services.api import db as api_db
from services.api import sse as sse_multiplexer
from services.api.config import APISettings, get_settings
from services.api.db import close_pool, init_pool, session_scope
from services.api.errors import register_error_handlers
from services.api.middleware import BotAuthMiddleware, LeanAuthMiddleware, register_middleware
from services.api.repos.phase1 import PostgresPhase1QueryRepo
from services.api.repos.setup_tokens import PostgresSetupTokenRepo
from services.api.routes.alerts import router as alerts_router
from services.api.routes.auth import router as auth_router
from services.api.routes.fills import router as fills_router
from services.api.routes.health import router as health_router
from services.api.routes.internal.lean import router as lean_router
from services.api.routes.orders import router as orders_router
from services.api.routes.positions import router as positions_router
from services.api.routes.setup import router as setup_router
from services.api.routes.signals import router as signals_router
from services.api.routes.sse import router as sse_router
from services.api.routes.system import router as system_router
from services.api.routes.today import router as today_router
from services.api.routes.trades import router as trades_router
from services.api.session import SessionStubMiddleware

log = structlog.get_logger()


def _configure_structlog(settings: APISettings) -> None:
    is_dev = settings.environment == "dev"
    level = getattr(logging, settings.log_level, logging.INFO)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
        structlog.processors.add_log_level,
        structlog.processors.CallsiteParameterAdder(
            [structlog.processors.CallsiteParameter.FUNC_NAME]
        ),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.dev.ConsoleRenderer() if is_dev else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _bootstrap_owner_token() -> None:
    """Emit a first-run owner setup token if none exists.

    Per backend-spec §3.1.1: raw token printed to stdout exactly once at
    boot; only the Argon2id hash is persisted. Idempotent across restarts —
    once a token is consumed (or another is in flight), no new token is
    minted.
    """
    async with session_scope() as session:
        repo = PostgresSetupTokenRepo(session)
        if await repo.has_unconsumed_owner_token():
            log.info("setup_owner_token_already_present")
            return
        raw_token = secrets.token_urlsafe(32)
        token_uuid = await repo.insert_owner_token(raw_token)
        # Bind to root logger so the token is visible regardless of caller
        # context. Single-line emit because operators grep for SETUP_TOKEN.
        log.warning(
            "SETUP_TOKEN_EMITTED",
            token_uuid=str(token_uuid),
            raw_token=raw_token,
            instructions=(
                "Submit this token at POST /api/setup/verify-token within 24h. "
                "It will not be re-emitted; if lost, run "
                "`python -m services.api.bootstrap_owner_token` to mint another."
            ),
        )


def _audit_env_from_settings(settings: APISettings) -> Literal["paper", "live-small", "live-scale"]:
    """Map APISettings.environment → audit_log.env enum value.

    `dev` is the local-only env tag; it has no corresponding `audit_log.env`
    CHECK constraint value so we degrade it to `paper` (the dry-run env tag).
    """
    if settings.environment in ("paper", "live-small", "live-scale"):
        return settings.environment
    return "paper"


async def _start_order_placement_worker(settings: APISettings) -> tuple[object, object] | None:
    """Construct + start the OrderPlacementWorker; return (worker, task) or None.

    Best-effort: any failure to construct the worker (no active account,
    ib_async unavailable, etc.) logs a warning and returns None. The api
    still serves the rest of its surface.

    The lifecycle of the IbkrClient connection is owned by the adapter
    (auto-reconnect on transient disconnects). The connect() call here
    is the initial best-effort handshake; if ib_gateway is down at boot
    the worker.run_once() iterations will keep retrying with the
    adapter's reconnect path.
    """
    if not settings.order_placement_worker_enabled:
        log.warning("order_placement_worker_disabled_via_setting")
        return None

    # Lazy imports — keeps the rest of the api importable even when
    # ib_async isn't installed (e.g., dev hosts running unit tests).
    from services.execution.ibkr_adapter import IbAsyncIbkrClient
    from services.execution.types import IbkrPlacementError
    from services.risk.order_placement_worker import OrderPlacementWorker

    # Resolve active account_id from the accounts table. If no account
    # exists, defer startup — the operator must complete /setup before
    # signals flow.
    async with session_scope() as repo_session:
        repo = PostgresPhase1QueryRepo(repo_session)
        account_id = await repo.fetch_active_account_id()
    if account_id is None:
        log.warning(
            "order_placement_worker_no_active_account",
            note="run /setup before the worker can resolve an account_id",
        )
        return None

    # Construct + best-effort connect. IBKR account number is optional —
    # IbAsyncIbkrClient falls back to the default account on the TWS
    # session when None is passed.
    ibkr_account = settings.ibkr_account
    ibkr_client = IbAsyncIbkrClient(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        account_id=ibkr_account,
        client_id=settings.ibkr_client_id,
    )

    try:
        await ibkr_client.connect()
    except IbkrPlacementError as exc:
        log.warning(
            "order_placement_worker_initial_ibkr_connect_failed",
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            error=str(exc),
            note=(
                "Worker will keep retrying via the adapter's reconnect "
                "path on each run_once() iteration."
            ),
        )

    worker = OrderPlacementWorker(
        session_factory=api_db.get_session_factory(),
        ibkr_client=ibkr_client,
        account_id=account_id,
        env=_audit_env_from_settings(settings),
        poll_interval_seconds=settings.order_placement_poll_interval_seconds,
    )
    task = asyncio.create_task(worker.run_forever(), name="order_placement_worker.run_forever")
    log.info(
        "order_placement_worker_spawned",
        account_id=str(account_id),
        env=_audit_env_from_settings(settings),
        ibkr_host=settings.ibkr_host,
        ibkr_port=settings.ibkr_port,
        ibkr_account=ibkr_account,
        poll_interval=settings.order_placement_poll_interval_seconds,
    )
    return worker, task


async def _start_reconciliation_scheduler(
    settings: APISettings,
) -> tuple[object, object] | None:
    """Construct + start the ReconciliationScheduler; return (sched, task) or None.

    Best-effort: requires both ``flex_query_id`` and ``flex_query_token``
    populated in sops (mapped via ``services/api/entrypoint.py``). When
    either is missing — or the operator has flipped
    ``reconciliation_scheduler_enabled=False`` — the scheduler does not
    start + a structured warning is logged so the operator knows to
    populate sops + restart the api.

    The cycle callback is built by :func:`services.reconciliation.eod_cycle.make_cycle_callback`
    and fires once per America/New_York calendar day at 18:30 ET (per
    backend-spec §2.6). Errors inside the callback are logged + swallowed
    by the scheduler so a transient FlexQuery outage doesn't kill the
    scheduler — tomorrow's cycle still fires.
    """
    if not settings.reconciliation_scheduler_enabled:
        log.warning("reconciliation_scheduler_disabled_via_setting")
        return None

    if settings.flex_query_id is None or settings.flex_query_token is None:
        log.warning(
            "reconciliation_scheduler_flex_credentials_missing",
            note=(
                "Set ibkr.flex_query_id + ibkr.flex_query_token in sops to "
                "enable the EOD reconciliation cycle. See "
                "deploy/reconciliation/README.md."
            ),
        )
        return None

    from services.reconciliation.eod_cycle import EodCycleConfig, make_cycle_callback
    from services.reconciliation.scheduler import ReconciliationScheduler

    async with session_scope() as repo_session:
        repo = PostgresPhase1QueryRepo(repo_session)
        account_id = await repo.fetch_active_account_id()
    if account_id is None:
        log.warning(
            "reconciliation_scheduler_no_active_account",
            note="run /setup before the scheduler can resolve an account_id",
        )
        return None

    config = EodCycleConfig(
        account_id=account_id,
        env=_audit_env_from_settings(settings),
        flex_query_id=settings.flex_query_id,
        flex_query_token=settings.flex_query_token.get_secret_value(),
    )
    callback = make_cycle_callback(
        config=config,
        session_factory=api_db.get_session_factory(),
    )
    scheduler = ReconciliationScheduler(callback=callback)
    task = asyncio.create_task(scheduler.run_forever(), name="reconciliation_scheduler.run_forever")
    log.info(
        "reconciliation_scheduler_spawned",
        account_id=str(account_id),
        env=_audit_env_from_settings(settings),
        flex_query_id=settings.flex_query_id,
    )
    return scheduler, task


async def _stop_reconciliation_scheduler(state: tuple[object, object] | None) -> None:
    """Request stop + await the scheduler task. Best-effort."""
    if state is None:
        return
    scheduler, task = state
    try:
        scheduler.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("reconciliation_scheduler_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("reconciliation_scheduler_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("reconciliation_scheduler_shutdown_cancelled")
        except Exception:
            log.exception("reconciliation_scheduler_shutdown_unclean")
    except Exception:
        log.exception("reconciliation_scheduler_task_join_failed")


async def _stop_order_placement_worker(state: tuple[object, object] | None) -> None:
    """Request stop + await task + disconnect the IBKR client. Best-effort."""
    if state is None:
        return
    worker, task = state
    try:
        worker.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("order_placement_worker_request_stop_failed")
    # Give the worker a few seconds to finish its in-flight run_once.
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("order_placement_worker_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("order_placement_worker_shutdown_cancelled")
        except Exception:
            log.exception("order_placement_worker_shutdown_unclean")
    except Exception:
        log.exception("order_placement_worker_task_join_failed")
    # Disconnect the IBKR client. The worker holds a reference; pull it
    # out via the internal attribute (the worker class predates the
    # need for a public accessor; surfacing one is a future tweak).
    ibkr_client = getattr(worker, "_ibkr_client", None)
    if ibkr_client is not None:
        try:
            await ibkr_client.disconnect()
        except Exception:
            log.exception("order_placement_worker_ibkr_disconnect_failed")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    _configure_structlog(settings)
    log.info(
        "api_starting",
        environment=settings.environment,
        version=settings.version,
    )
    await init_pool(settings)
    sse_multiplexer.reset_state()
    sse_multiplexer.start_heartbeat()
    worker_state: tuple[object, object] | None = None
    recon_state: tuple[object, object] | None = None
    try:
        try:
            await _bootstrap_owner_token()
        except Exception:
            # Bootstrap failure shouldn't crash the api — alembic may not
            # have run yet on this VPS. Log loudly and continue; operator
            # runs the bootstrap CLI after migrations finish.
            log.exception("setup_token_bootstrap_failed")
        try:
            worker_state = await _start_order_placement_worker(settings)
        except Exception:
            # Worker startup is best-effort; failure shouldn't take
            # down the api. The operator can re-enable + restart once
            # the underlying issue (ib_async install, accounts row,
            # network) is resolved.
            log.exception("order_placement_worker_startup_failed")
        try:
            recon_state = await _start_reconciliation_scheduler(settings)
        except Exception:
            # Scheduler startup is best-effort; failure shouldn't take
            # down the api. Most failure modes are config-time (missing
            # sops fields, no active account) which we already log at
            # WARNING from inside _start_reconciliation_scheduler.
            log.exception("reconciliation_scheduler_startup_failed")
        log.info("api_ready")
        yield
    finally:
        log.info("api_stopping")
        await _stop_reconciliation_scheduler(recon_state)
        await _stop_order_placement_worker(worker_state)
        await sse_multiplexer.stop_heartbeat()
        await close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_structlog(settings)
    app = FastAPI(
        title="trading-system api",
        version=settings.version,
        docs_url="/api/docs" if settings.environment == "dev" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.environment == "dev" else None,
        lifespan=_lifespan,
    )
    register_error_handlers(app)
    register_middleware(app, settings)
    # Session stub middleware sits BETWEEN the request-context binding and
    # the CSRF gate so route handlers see ``request.state.session`` after
    # CSRF has cleared. Added after register_middleware so it ends up
    # innermost (FastAPI/Starlette: last-added = innermost in the request
    # path; runs after CSRF + RequestContext).
    app.add_middleware(SessionStubMiddleware, settings=settings)  # type: ignore[arg-type]
    # Day 23: BotAuthMiddleware sits OUTERMOST so the Discord bot's
    # bearer-authenticated requests bypass CSRF (no cookies) and
    # short-circuit SessionStub's fail-close in production envs. The
    # middleware is a noop on requests without an Authorization: Bearer
    # header, so adding it OUTERMOST has zero overhead on the human path.
    # See services/api/middleware.BotAuthMiddleware docstring for the
    # full request-flow diagram.
    app.add_middleware(BotAuthMiddleware, settings=settings)  # type: ignore[arg-type]
    # Pivot-PR-A (post-pivot 2026-05-12): LeanAuthMiddleware sits
    # outermost-of-outermost (added AFTER BotAuthMiddleware so Starlette's
    # last-added-outermost convention puts it first in the request flow).
    # Order: LeanAuth → BotAuth → SessionStub → RequestContext → RateLimit
    # → CSRF → routes. The Lean container POSTs to /api/internal/lean/signals
    # bearing its own token (sourced from sops `lean.api_bearer_token`); if
    # it doesn't match, the request falls through to BotAuth which tries the
    # bot's token, then to SessionStub which fails closed in production envs.
    app.add_middleware(LeanAuthMiddleware, settings=settings)  # type: ignore[arg-type]
    # Day 5 routes
    app.include_router(health_router)
    app.include_router(setup_router)
    app.include_router(sse_router)
    # Day 15 — Phase 1 REST scaffold per backend-spec §4.1 / IG §3 Week 5 Mon
    app.include_router(auth_router)
    app.include_router(signals_router)
    app.include_router(system_router)
    app.include_router(today_router)
    app.include_router(positions_router)
    app.include_router(orders_router)
    app.include_router(fills_router)
    app.include_router(alerts_router)
    # Day 26 — Week 7 Tue Trades page surface (backend-spec §4.1.2).
    app.include_router(trades_router)
    # Pivot-PR-A (post-pivot 2026-05-12): /api/internal/lean/signals — POST
    # endpoint for LEAN Local to push signal_emitted events. Shared-bearer
    # auth via LeanAuthMiddleware (outermost in the middleware stack);
    # writes audit row via services.audit.writer.append_audit_event;
    # INSERTs into signals table.
    app.include_router(lean_router)
    return app


app = create_app()
