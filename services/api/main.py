"""services/api/main.py — FastAPI app entrypoint.

Wired in execution order:

  1. structlog configured for JSON output on production / Console on dev.
  2. Settings loaded from env (sops-decrypted bundle exported by entrypoint).
  3. Lifespan:
       - Init asyncpg pool.
       - First-boot bootstrap: if no unconsumed/unexpired owner setup_token
         exists, create one, print raw to stdout, store hash. Idempotent.
  4. Middleware (RequestContext + CSRF + optional CORS).
  5. Error handlers (canonical envelope per dev-guide §3.6).
  6. Routes (health, setup, sse).

Day 5 scope deliberately omits: WebAuthn, TOTP, sessions, audit writes,
risk endpoints, signals. Those land Phase-0 Week-2+ behind the existing
forbidden-paths CI gate.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from services.api import sse as sse_multiplexer
from services.api.config import APISettings, get_settings
from services.api.db import close_pool, init_pool, session_scope
from services.api.errors import register_error_handlers
from services.api.middleware import BotAuthMiddleware, register_middleware
from services.api.repos.setup_tokens import PostgresSetupTokenRepo
from services.api.routes.alerts import router as alerts_router
from services.api.routes.auth import router as auth_router
from services.api.routes.fills import router as fills_router
from services.api.routes.health import router as health_router
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
    try:
        try:
            await _bootstrap_owner_token()
        except Exception:
            # Bootstrap failure shouldn't crash the api — alembic may not
            # have run yet on this VPS. Log loudly and continue; operator
            # runs the bootstrap CLI after migrations finish.
            log.exception("setup_token_bootstrap_failed")
        log.info("api_ready")
        yield
    finally:
        log.info("api_stopping")
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
    return app


app = create_app()
