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
import json
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from services.api.async_task_monitor import TrackedIbkrErrorState

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy import text

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

    # Exit-pipeline PR-C (2026-05-27): build the POSITION_UNPROTECTED
    # P0 alert dispatch hook from sops Discord URLs + Resend identity.
    # When sops isn't populated the hook stays None — the worker still
    # writes the audit row + structured WARNING in logs; only the
    # Discord/email push is skipped (operator should monitor the
    # /audit page until the hook is wired).
    #
    # ``_build_position_unprotected_alert_hook`` returns ``object | None``
    # for the same circular-import dodge as the recon + monitor hook
    # builders (see their docstrings). The runtime type IS
    # ``PositionUnprotectedAlertHook | None``; cast for mypy.
    from typing import cast as _cast

    from services.risk.order_placement_worker import (
        OrderPlacementFailureAlertHook,
        PositionUnprotectedAlertHook,
    )

    position_unprotected_hook = _cast(
        "PositionUnprotectedAlertHook | None",
        _build_position_unprotected_alert_hook(settings),
    )
    order_placement_failure_hook = _cast(
        "OrderPlacementFailureAlertHook | None",
        _build_order_placement_failure_alert_hook(settings),
    )

    worker = OrderPlacementWorker(
        session_factory=api_db.get_session_factory(),
        ibkr_client=ibkr_client,
        account_id=account_id,
        env=_audit_env_from_settings(settings),
        poll_interval_seconds=settings.order_placement_poll_interval_seconds,
        ibkr_call_timeout_seconds=settings.ibkr_call_timeout_seconds,
        position_unprotected_alert_hook=position_unprotected_hook,
        order_placement_failure_alert_hook=order_placement_failure_hook,
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
        ibkr_call_timeout_seconds=settings.ibkr_call_timeout_seconds,
    )
    return worker, task


def _build_alert_dispatch_hook(
    settings: APISettings,
) -> object | None:
    """Construct the recon ``alert_dispatch_hook`` closure or return None.

    Closes the seam left by PR #135: the recon planner emits
    :class:`AlertDescriptor` rows, the apply orchestrator fires per-alert
    callbacks, and this is where the api lifespan turns a callback into
    "INSERT alerts row + invoke `dispatch_alert`" — the operator-visible
    Discord push.

    Returns ``None`` (cleanly skipping the hook installation) when sops
    Discord URLs aren't populated. The reconciliation cycle still runs
    end-to-end + alerts log a WARNING from
    :func:`services.reconciliation.apply._dispatch_alerts` ("hook not
    wired") so the operator knows what to populate.

    P0 escalation (Resend email) requires ALL of api_key + from_address +
    to_address. Missing any → ``email_identity`` stays ``None`` + the
    dispatcher's planner raises on a P0 break. P2-only environments
    (Phase 1 day-1) don't need the email_identity since SEVERITY_TO_CHANNELS
    routes P2 to ``#alerts`` only.

    Returns ``object | None`` to dodge a circular import — the actual
    hook signature is ``services.reconciliation.apply.AlertDispatchHook``
    and the value is exactly that, but stating it here would force an
    import-at-module-load of the recon module (which loads the apply
    module which loads the audit writer ...) at module top.
    """
    # Lazy imports keep module-load fast + avoid the circular-import surface.
    from services.reconciliation.apply import AlertDispatchContext
    from services.webhook_pusher.dispatcher import dispatch_alert
    from services.webhook_pusher.payloads import (
        AlertCategory,
        AlertSeverity,
        ChannelName,
        EmailIdentity,
    )

    if settings.discord_webhook_url_alerts is None:
        log.warning(
            "alert_dispatch_hook_skipped_no_webhook_url",
            note=(
                "discord.webhook_urls.alerts not in sops; reconciliation "
                "cycle will run + alerts will log a 'hook not wired' "
                "warning. Wire the sops field + restart api to enable."
            ),
        )
        return None

    webhook_urls: dict[ChannelName, str] = {
        ChannelName.DISCORD_ALERTS: settings.discord_webhook_url_alerts.get_secret_value(),
    }
    if settings.discord_webhook_url_critical is not None:
        webhook_urls[ChannelName.DISCORD_CRITICAL] = (
            settings.discord_webhook_url_critical.get_secret_value()
        )

    email_identity: EmailIdentity | None = None
    if (
        settings.resend_api_key is not None
        and settings.resend_from_address is not None
        and settings.resend_to_address is not None
    ):
        email_identity = EmailIdentity(
            from_address=settings.resend_from_address,
            to_address=settings.resend_to_address,
            resend_api_key=settings.resend_api_key.get_secret_value(),
        )

    log.info(
        "alert_dispatch_hook_constructed",
        channels=[c.value for c in webhook_urls],
        email_wired=email_identity is not None,
    )

    async def _hook(ctx: AlertDispatchContext) -> None:
        # Resolve session_factory inside the closure so a test can build
        # the hook without init_pool() running (the closure itself only
        # fires at scheduler-fire time, by which point the pool is up).
        session_factory = api_db.get_session_factory()
        """Per-alert: INSERT alerts row + dispatch via webhook_pusher.

        Two separate sessions per call:

        1. INSERT into alerts using a fresh session_factory()-opened
           session so we don't share state with the recon apply
           orchestrator (which has its own session-per-event lifecycle).
        2. Open a fresh httpx.AsyncClient + session for the dispatch.
           Daily cadence makes per-call client OK; pooling is unnecessary
           and the lifetime simplification is worth the per-call setup.

        Errors propagate per the apply contract — the api-side scheduler
        catches + logs at ``_start_reconciliation_scheduler``'s outer
        try/except so a single Discord 5xx doesn't kill the loop.
        """
        # Combine title + body into the alerts.message column per the
        # recommendation in services/reconciliation/apply.AlertDispatchContext
        # docstring. The recon planner's title is short; body has the
        # quantitative split. message is what shows up in the operator's
        # Discord embed body.
        message_text = f"{ctx.descriptor.title}\n\n{ctx.descriptor.body}"
        async with session_factory() as ins_session:
            row = (
                await ins_session.execute(
                    text(
                        "INSERT INTO alerts ("
                        "    account_id, severity, category, message, detail, "
                        "    triggering_audit_event_uuid"
                        ") VALUES ("
                        "    :acct, :sev, :cat, :msg, CAST(:detail AS JSONB), :tau"
                        ") RETURNING id"
                    ),
                    {
                        "acct": ctx.account_id,
                        "sev": ctx.descriptor.severity,
                        "cat": ctx.descriptor.category,
                        "msg": message_text,
                        # Cast Python dict → JSON via SA's JSONB; serialize
                        # explicitly so Decimal-as-str + None survive.
                        "detail": json.dumps(ctx.descriptor.payload),
                        "tau": ctx.triggering_audit_event_uuid,
                    },
                )
            ).fetchone()
            assert row is not None
            alert_id = UUID(str(row.id))
            await ins_session.commit()

        log.info(
            "reconciliation_alert_inserted",
            alert_id=str(alert_id),
            severity=ctx.descriptor.severity,
            category=ctx.descriptor.category,
            account_id=str(ctx.account_id),
            env=ctx.env,
        )

        # Side note on enum cross-check: the dispatcher's planner reads
        # AlertSeverity + AlertCategory enums from the DB row, not from
        # the descriptor. Validating here is defense-in-depth — if a
        # future planner change leaks a non-canonical value past
        # Decimal-as-str checks, this raises before the http fan-out
        # even attempts a malformed Discord webhook POST.
        AlertSeverity(ctx.descriptor.severity)
        AlertCategory(ctx.descriptor.category)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
            async with session_factory() as disp_session:
                report = await dispatch_alert(
                    session=disp_session,
                    alert_id=alert_id,
                    http_client=http_client,
                    webhook_urls=webhook_urls,
                    email_identity=email_identity,
                )
        log.info(
            "reconciliation_alert_dispatched",
            alert_id=str(alert_id),
            short_circuited=report.short_circuited,
            delivery_status=dict(report.delivery_status),
        )

    return _hook


def _build_position_unprotected_alert_hook(
    settings: APISettings,
) -> object | None:
    """Construct the order_placement_worker's POSITION_UNPROTECTED P0
    alert dispatch hook or return None.

    Exit-pipeline PR-C (2026-05-27). The worker's exit branch invokes
    this hook from the cancel-success+place-fail failure path; the hook
    INSERTs an ``alerts`` row of category='position_unprotected' +
    severity='P0' (linked via ``triggering_audit_event_uuid`` to the
    POSITION_UNPROTECTED audit row that JUST landed), then invokes
    :func:`services.webhook_pusher.dispatcher.dispatch_alert` for the
    Discord #alerts + #critical + Resend email fan-out per
    SEVERITY_TO_CHANNELS[P0].

    Returns ``None`` when sops Discord URLs aren't populated (same
    degradation contract as the recon + monitor hooks). The worker
    still writes the audit row + a structured WARNING in logs so the
    operator can monitor /audit until the hook is wired.

    Pattern mirror of ``_build_alert_dispatch_hook`` but the hook
    signature is ``PositionUnprotectedAlertHook`` taking a
    :class:`PositionUnprotectedAlertDescriptor`. P0 routing requires
    all of ``discord.webhook_urls.alerts``, ``discord.webhook_urls.critical``,
    and the Resend identity fields; missing any → the
    :func:`dispatch_alert` planner raises at fan-out time, the
    worker's exception handler swallows + logs (failure-of-failure
    handler tolerated since the audit row already records the durable
    state). Recommend wiring ALL three before live cutover.

    Returns ``object | None`` to dodge a circular import on the worker
    side (same crutch as the other two hooks).
    """
    from services.risk.order_placement_worker import (
        PositionUnprotectedAlertDescriptor,
    )
    from services.webhook_pusher.dispatcher import dispatch_alert
    from services.webhook_pusher.payloads import (
        AlertCategory,
        AlertSeverity,
        ChannelName,
        EmailIdentity,
    )

    if settings.discord_webhook_url_alerts is None:
        log.warning(
            "position_unprotected_alert_hook_skipped_no_webhook_url",
            note=(
                "discord.webhook_urls.alerts not in sops; the exit-pipeline "
                "POSITION_UNPROTECTED audit row will still land, but no "
                "P0 Discord push will fire. Wire the sops field + restart "
                "api to enable. THIS IS A LIVE-CUTOVER BLOCKER per "
                "Docs/exit-pipeline-design.md §11 R3."
            ),
        )
        return None

    webhook_urls: dict[ChannelName, str] = {
        ChannelName.DISCORD_ALERTS: settings.discord_webhook_url_alerts.get_secret_value(),
    }
    if settings.discord_webhook_url_critical is not None:
        webhook_urls[ChannelName.DISCORD_CRITICAL] = (
            settings.discord_webhook_url_critical.get_secret_value()
        )
    else:
        log.warning(
            "position_unprotected_alert_hook_missing_critical_channel",
            note=(
                "discord.webhook_urls.critical not in sops; P0 alerts will "
                "fall back to #alerts only. The dispatcher's planner WILL "
                "raise at fan-out time when SEVERITY_TO_CHANNELS[P0] "
                "expects #critical. Wire before live cutover."
            ),
        )

    email_identity: EmailIdentity | None = None
    if (
        settings.resend_api_key is not None
        and settings.resend_from_address is not None
        and settings.resend_to_address is not None
    ):
        email_identity = EmailIdentity(
            from_address=settings.resend_from_address,
            to_address=settings.resend_to_address,
            resend_api_key=settings.resend_api_key.get_secret_value(),
        )

    log.info(
        "position_unprotected_alert_hook_constructed",
        channels=[c.value for c in webhook_urls],
        email_wired=email_identity is not None,
    )

    async def _hook(descriptor: PositionUnprotectedAlertDescriptor) -> None:
        """Per-failure: INSERT alerts row + dispatch P0 fan-out."""
        session_factory = api_db.get_session_factory()
        message_text = (
            f"POSITION_UNPROTECTED · {descriptor.market} "
            f"({descriptor.prior_position_direction} "
            f"qty={abs(descriptor.prior_position_quantity)}) — bracket-stop "
            f"cancel succeeded, close placement FAILED. "
            f"close_failure_reason={descriptor.close_failure_reason}. "
            f"last_known_stop_price={descriptor.last_known_stop_price}."
        )
        detail_payload: dict[str, Any] = {
            "signal_id": str(descriptor.signal_id),
            "market": descriptor.market,
            "prior_position_direction": descriptor.prior_position_direction,
            "prior_position_quantity": descriptor.prior_position_quantity,
            "last_known_stop_price": str(descriptor.last_known_stop_price),
            "close_client_order_id": descriptor.close_client_order_id,
            "close_failure_reason": descriptor.close_failure_reason,
        }
        async with session_factory() as ins_session:
            row = (
                await ins_session.execute(
                    text(
                        "INSERT INTO alerts ("
                        "    account_id, severity, category, message, detail, "
                        "    triggering_audit_event_uuid"
                        ") VALUES ("
                        "    :acct, :sev, :cat, :msg, CAST(:detail AS JSONB), :tau"
                        ") RETURNING id"
                    ),
                    {
                        "acct": descriptor.account_id,
                        "sev": AlertSeverity.P0.value,
                        "cat": AlertCategory.POSITION_UNPROTECTED.value,
                        "msg": message_text,
                        "detail": json.dumps(detail_payload),
                        "tau": descriptor.triggering_audit_event_uuid,
                    },
                )
            ).fetchone()
            assert row is not None
            alert_id = UUID(str(row.id))
            await ins_session.commit()

        log.error(
            "position_unprotected_alert_inserted",
            alert_id=str(alert_id),
            account_id=str(descriptor.account_id),
            signal_id=str(descriptor.signal_id),
            market=descriptor.market,
            env=descriptor.env,
            close_failure_reason=descriptor.close_failure_reason,
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
            async with session_factory() as disp_session:
                report = await dispatch_alert(
                    session=disp_session,
                    alert_id=alert_id,
                    http_client=http_client,
                    webhook_urls=webhook_urls,
                    email_identity=email_identity,
                )
        log.error(
            "position_unprotected_alert_dispatched",
            alert_id=str(alert_id),
            short_circuited=report.short_circuited,
            delivery_status=dict(report.delivery_status),
        )

    return _hook


def _build_order_placement_failure_alert_hook(
    settings: APISettings,
) -> object | None:
    """Construct the order_placement_worker's ORDER_PLACEMENT_FAILED P1
    alert hook, or return None.

    Silent-failure follow-up (2026-06-08). The worker invokes this from the
    dispatch loop's ``IbkrPlacementError`` catch — broker unavailable OR a
    contract rejection (e.g. the 2026-06-04 /MYM Error-200 wrong-exchange
    case fixed in #327). The hook INSERTs an ``alerts`` row
    category='order_placement_failed' + severity='P1', then invokes
    :func:`services.webhook_pusher.dispatcher.dispatch_alert` for the
    Discord ``#alerts`` push (``SEVERITY_TO_CHANNELS[P1]`` = #alerts only;
    no #critical, no email — no money is at risk, the order simply didn't
    place and the signal stays ``approved`` for the next poll).

    Returns ``None`` when ``discord.webhook_urls.alerts`` isn't in sops —
    the worker then logs-only (same degradation contract as the other
    hooks; the failure is still in the structlog ``order_placement_broker_
    unavailable`` line). Returns ``object | None`` to dodge the worker-side
    circular import (same crutch as the position-unprotected hook).
    """
    from services.risk.order_placement_worker import (
        OrderPlacementFailureAlertDescriptor,
    )
    from services.webhook_pusher.dispatcher import dispatch_alert
    from services.webhook_pusher.payloads import (
        AlertCategory,
        AlertSeverity,
        ChannelName,
    )

    if settings.discord_webhook_url_alerts is None:
        log.warning(
            "order_placement_failure_alert_hook_skipped_no_webhook_url",
            note=(
                "discord.webhook_urls.alerts not in sops; order-placement "
                "failures stay log-only (no #alerts push). Wire the sops "
                "field + restart api to enable the P1 alert."
            ),
        )
        return None

    webhook_urls: dict[ChannelName, str] = {
        ChannelName.DISCORD_ALERTS: settings.discord_webhook_url_alerts.get_secret_value(),
    }

    log.info("order_placement_failure_alert_hook_constructed")

    async def _hook(descriptor: OrderPlacementFailureAlertDescriptor) -> None:
        """Per-failure: INSERT alerts row + dispatch the P1 #alerts push."""
        session_factory = api_db.get_session_factory()
        message_text = (
            f"ORDER_PLACEMENT_FAILED · {descriptor.market} "
            f"({descriptor.signal_type}) — IBKR order placement failed; the "
            f"signal remains approved for the next poll cycle. "
            f"reason={descriptor.failure_reason}"
        )
        detail_payload: dict[str, Any] = {
            "signal_id": str(descriptor.signal_id),
            "market": descriptor.market,
            "signal_type": descriptor.signal_type,
            "failure_reason": descriptor.failure_reason,
        }
        async with session_factory() as ins_session:
            row = (
                await ins_session.execute(
                    text(
                        "INSERT INTO alerts ("
                        "    account_id, severity, category, message, detail"
                        ") VALUES ("
                        "    :acct, :sev, :cat, :msg, CAST(:detail AS JSONB)"
                        ") RETURNING id"
                    ),
                    {
                        "acct": descriptor.account_id,
                        "sev": AlertSeverity.P1.value,
                        "cat": AlertCategory.ORDER_PLACEMENT_FAILED.value,
                        "msg": message_text,
                        "detail": json.dumps(detail_payload),
                    },
                )
            ).fetchone()
            assert row is not None
            alert_id = UUID(str(row.id))
            await ins_session.commit()

        log.error(
            "order_placement_failure_alert_inserted",
            alert_id=str(alert_id),
            account_id=str(descriptor.account_id),
            signal_id=str(descriptor.signal_id),
            market=descriptor.market,
            signal_type=descriptor.signal_type,
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
            async with session_factory() as disp_session:
                report = await dispatch_alert(
                    session=disp_session,
                    alert_id=alert_id,
                    http_client=http_client,
                    webhook_urls=webhook_urls,
                    email_identity=None,
                )
        log.error(
            "order_placement_failure_alert_dispatched",
            alert_id=str(alert_id),
            short_circuited=report.short_circuited,
            delivery_status=dict(report.delivery_status),
        )

    return _hook


def _build_monitor_alert_dispatch_hook(
    settings: APISettings,
) -> object | None:
    """Construct the AsyncTaskMonitor's alert dispatch hook or return None.

    Drill 5 follow-up #2-FU-1 (PR after #177 + #179). The
    AsyncTaskMonitor's IBKR connectivity probe emits an
    ``async_task_monitor_ibkr_connectivity_warn`` WARNING on fresh
    1100/1101/1102 errors; this hook adds the Discord ``#alerts``
    P1 push so the operator sees the event in-channel within 30s of
    its first probe-tick observation.

    Returns ``None`` (cleanly skipping the hook installation) when
    sops ``discord.webhook_urls.alerts`` isn't populated. The probe's
    WARNING log still fires unconditionally; only the Discord side
    degrades.

    Pattern mirror of ``_build_alert_dispatch_hook`` for the recon
    surface, but the hook signature is the monitor's
    ``MonitorAlertHook`` taking a ``MonitorAlertDescriptor`` (no
    triggering_audit_event_uuid — monitor alerts have no audit row
    upstream; ``alerts.triggering_audit_event_uuid`` is nullable per
    alembic 0004 schema).

    Resolves account_id at hook-FIRE time (not construction time) so
    the closure is cheap to build at lifespan startup; the
    fetch-at-fire pattern means the hook still works after operator
    runs /setup post-boot.

    Returns ``object | None`` to dodge a circular import — the actual
    return type is ``services.api.async_task_monitor.MonitorAlertHook``
    but importing that at module-load forces a transitive load of the
    monitor module which itself imports api_db. Lazy-import below.
    """
    # Lazy imports keep module-load fast + avoid the circular-import surface.
    from services.api.async_task_monitor import MonitorAlertDescriptor
    from services.webhook_pusher.dispatcher import dispatch_alert
    from services.webhook_pusher.payloads import (
        AlertCategory,
        AlertSeverity,
        ChannelName,
        EmailIdentity,
    )

    if settings.discord_webhook_url_alerts is None:
        log.warning(
            "monitor_alert_dispatch_hook_skipped_no_webhook_url",
            note=(
                "discord.webhook_urls.alerts not in sops; the monitor's "
                "IBKR connectivity probe will still emit the structured "
                "WARNING log on 1100/1101/1102 events but no Discord "
                "#alerts push will fire. Wire the sops field + restart "
                "api to enable."
            ),
        )
        return None

    webhook_urls: dict[ChannelName, str] = {
        ChannelName.DISCORD_ALERTS: settings.discord_webhook_url_alerts.get_secret_value(),
    }
    if settings.discord_webhook_url_critical is not None:
        webhook_urls[ChannelName.DISCORD_CRITICAL] = (
            settings.discord_webhook_url_critical.get_secret_value()
        )

    email_identity: EmailIdentity | None = None
    if (
        settings.resend_api_key is not None
        and settings.resend_from_address is not None
        and settings.resend_to_address is not None
    ):
        email_identity = EmailIdentity(
            from_address=settings.resend_from_address,
            to_address=settings.resend_to_address,
            resend_api_key=settings.resend_api_key.get_secret_value(),
        )

    log.info(
        "monitor_alert_dispatch_hook_constructed",
        channels=[c.value for c in webhook_urls],
        email_wired=email_identity is not None,
    )

    audit_env = _audit_env_from_settings(settings)

    async def _hook(descriptor: MonitorAlertDescriptor) -> None:
        """Per-alert: INSERT alerts row + dispatch via webhook_pusher.

        Resolves account_id at fire time so a missing-account-at-boot
        path (operator hasn't run /setup yet) doesn't break hook
        construction. If account_id is still unresolved at fire time,
        log + skip the dispatch — the monitor WARNING already fired.

        Two separate sessions per call mirror the recon hook pattern:

        1. INSERT into alerts using a fresh session_factory()-opened
           session.
        2. Open a fresh httpx.AsyncClient + session for the dispatch.

        Hook failures propagate; the monitor's ``_schedule_alert_dispatch``
        catches them + logs at WARNING so a Discord 5xx doesn't crash
        the run_forever loop.
        """
        session_factory = api_db.get_session_factory()
        async with session_factory() as repo_session:
            repo = PostgresPhase1QueryRepo(repo_session)
            account_id = await repo.fetch_active_account_id()
        if account_id is None:
            log.warning(
                "monitor_alert_dispatch_skipped_no_account",
                note=(
                    "Monitor fired the IBKR connectivity WARNING but "
                    "no active account is provisioned; alerts row INSERT "
                    "would violate the NOT NULL FK to accounts. Run "
                    "/setup to create the account row + restart api."
                ),
                severity=descriptor.severity,
                category=descriptor.category,
            )
            return

        # Defense-in-depth: validate severity + category map to the
        # locked enums before attempting the INSERT (matches the
        # recon hook's enum cross-check at line 361-362).
        AlertSeverity(descriptor.severity)
        AlertCategory(descriptor.category)

        message_text = f"{descriptor.title}\n\n{descriptor.body}"
        async with session_factory() as ins_session:
            row = (
                await ins_session.execute(
                    text(
                        "INSERT INTO alerts ("
                        "    account_id, severity, category, message, detail"
                        ") VALUES ("
                        "    :acct, :sev, :cat, :msg, CAST(:detail AS JSONB)"
                        ") RETURNING id"
                    ),
                    {
                        "acct": account_id,
                        "sev": descriptor.severity,
                        "cat": descriptor.category,
                        "msg": message_text,
                        "detail": json.dumps(descriptor.payload),
                    },
                )
            ).fetchone()
            assert row is not None
            alert_id = UUID(str(row.id))
            await ins_session.commit()

        log.info(
            "monitor_alert_inserted",
            alert_id=str(alert_id),
            severity=descriptor.severity,
            category=descriptor.category,
            account_id=str(account_id),
            env=audit_env,
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
            async with session_factory() as disp_session:
                report = await dispatch_alert(
                    session=disp_session,
                    alert_id=alert_id,
                    http_client=http_client,
                    webhook_urls=webhook_urls,
                    email_identity=email_identity,
                )
        log.info(
            "monitor_alert_dispatched",
            alert_id=str(alert_id),
            short_circuited=report.short_circuited,
            delivery_status=dict(report.delivery_status),
        )

    return _hook


def _build_task_death_alert_hook(
    settings: APISettings,
) -> object | None:
    """Construct the AsyncTaskMonitor's task-death hook or return None.

    Recovery-agent follow-up to drill 5 (2026-05-18) + drill 7 (2026-05-18),
    landed 2026-05-26. When the AsyncTaskMonitor's
    ``_probe_tracked_tasks`` observes that an allow-listed lifespan
    task transitioned to ``.done()`` unexpectedly (today: only
    ``order_placement_worker.run_forever``), the monitor fires this
    hook with a ``MonitorAlertDescriptor``. The hook closure performs
    two state mutations in audit-first order (backend-spec §2.10.1):

      1. ``append_audit_event(ASYNC_TASK_DIED)`` — durable audit row
         capturing the task name, exit reason, exception type/repr,
         monitor's observed_at_utc.
      2. ``INSERT INTO alerts (category='worker_failure', severity='P0',
         triggering_audit_event_uuid=<the audit row's UUID>, ...)`` —
         the recovery-agent's inbox. The recovery agent at
         ``scripts/operator_tools/recovery_agent.py`` polls this table
         every 60s via systemd timer.

    Notably, this hook does NOT call ``dispatch_alert``. The recovery
    agent fires its own Discord ``#critical`` post via a dedicated
    webhook URL (``/etc/trading/critical-webhook.url``, mirroring the
    verify-chain pattern) — independent of api uptime. If the api is
    down at the time of detection, the alerts row + audit row land via
    the in-process hook; the Discord push then becomes the recovery
    agent's responsibility on its next 60s tick. This decouples
    notification from queueing and matches the operator's locked
    preference for an independent Discord webhook (per the operator
    brief).

    Returns ``None`` only on test paths that explicitly disable the
    hook via ``settings.task_death_alert_hook_enabled = False``. The
    default is always-on — the audit + alerts INSERT are best-effort
    safety-net writes that the recovery agent depends on.

    The account_id is resolved at hook fire time (not construction
    time) so a missing-account-at-boot path (operator hasn't run
    /setup yet) doesn't break hook construction. If account_id is
    still unresolved at fire time, log + skip both writes — the
    monitor ERROR log already fired the load-bearing observability.

    Pattern mirror of ``_build_monitor_alert_dispatch_hook`` (the IBKR
    connectivity hook) minus the dispatch_alert call + plus the
    leading append_audit_event. Returns ``object | None`` to dodge the
    same circular-import surface.
    """
    # Lazy imports keep module-load fast + avoid the circular-import surface.
    from services.api.async_task_monitor import MonitorAlertDescriptor
    from services.audit.event_types import AuditEventType
    from services.audit.writer import append_audit_event
    from services.webhook_pusher.payloads import AlertCategory, AlertSeverity

    if not getattr(settings, "task_death_alert_hook_enabled", True):
        log.info(
            "task_death_alert_hook_disabled_via_setting",
            note=(
                "settings.task_death_alert_hook_enabled is False; the "
                "AsyncTaskMonitor's structured async_task_died ERROR log "
                "will still fire on death, but no audit row or alerts "
                "row will be written. Disable only for test envs."
            ),
        )
        return None

    log.info("task_death_alert_hook_constructed")
    audit_env = _audit_env_from_settings(settings)

    async def _hook(descriptor: MonitorAlertDescriptor) -> None:
        """Per-task-death: audit-first emit + alerts INSERT.

        Resolves account_id at fire time so a missing-account-at-boot
        path doesn't break hook construction. If account_id is still
        unresolved at fire time, log + skip — the monitor ERROR log
        already fired the load-bearing observability.

        Three separate sessions:

          1. Account resolution session (read-only).
          2. Audit emit session (committed by ``append_audit_event``'s
             internal SERIALIZABLE + advisory-lock transaction).
          3. Alerts INSERT session (committed inline).

        Order: account → audit → alerts. The audit row's UUID is
        threaded into the alerts row's ``triggering_audit_event_uuid``
        FK so the recovery agent can join back to it without parsing
        the descriptor's payload.

        Hook failures propagate; the monitor's
        ``_schedule_alert_dispatch`` catches them + logs at WARNING so
        a Postgres serialization-retry-exhausted or unexpected error
        doesn't crash the run_forever loop.
        """
        # Defense-in-depth: validate severity + category map to the
        # locked enums before any DB I/O (matches the recon + IBKR
        # hooks' cross-checks).
        AlertSeverity(descriptor.severity)
        AlertCategory(descriptor.category)

        session_factory = api_db.get_session_factory()
        async with session_factory() as repo_session:
            repo = PostgresPhase1QueryRepo(repo_session)
            account_id = await repo.fetch_active_account_id()
        if account_id is None:
            log.warning(
                "task_death_alert_skipped_no_account",
                note=(
                    "Monitor fired task-death hook but no active account "
                    "is provisioned; the audit emit's FK to accounts AND "
                    "the alerts INSERT's FK both would fail. Run /setup "
                    "to create the account row + restart api."
                ),
                severity=descriptor.severity,
                category=descriptor.category,
                task_name=descriptor.payload.get("task_name"),
            )
            return

        # ---- 1. Audit-first: emit ASYNC_TASK_DIED ------------------
        # Per backend-spec §2.10.1: the audit row commits BEFORE the
        # alerts row (which is the dependent state mutation). The
        # append_audit_event helper handles SERIALIZABLE + advisory
        # lock + retry internally; we get back the inserted row's
        # event_uuid for FK chaining into the alerts INSERT.
        async with session_factory() as audit_session:
            audit_payload: dict[str, object] = {
                "task_name": descriptor.payload.get("task_name"),
                "exit_reason": descriptor.payload.get("exit_reason"),
                "exception_type": descriptor.payload.get("exception_type"),
                "exception_repr": descriptor.payload.get("exception_repr"),
                "monitor_observed_at_utc": descriptor.payload.get("observed_at_utc"),
            }
            audit_record = await append_audit_event(
                audit_session,
                AuditEventType.ASYNC_TASK_DIED,
                audit_payload,
                account_id=account_id,
                env=audit_env,
                phase_at_emit=1,
            )

        log.info(
            "task_death_audit_emitted",
            audit_event_uuid=str(audit_record.event_uuid),
            sequence_no=audit_record.sequence_no,
            task_name=descriptor.payload.get("task_name"),
            env=audit_env,
        )

        # ---- 2. INSERT alerts row with FK to the audit row ---------
        message_text = f"{descriptor.title}\n\n{descriptor.body}"
        async with session_factory() as ins_session:
            row = (
                await ins_session.execute(
                    text(
                        "INSERT INTO alerts ("
                        "    account_id, severity, category, message, detail, "
                        "    triggering_audit_event_uuid"
                        ") VALUES ("
                        "    :acct, :sev, :cat, :msg, CAST(:detail AS JSONB), :audit_uuid"
                        ") RETURNING id"
                    ),
                    {
                        "acct": account_id,
                        "sev": descriptor.severity,
                        "cat": descriptor.category,
                        "msg": message_text,
                        "detail": json.dumps(descriptor.payload),
                        "audit_uuid": audit_record.event_uuid,
                    },
                )
            ).fetchone()
            assert row is not None
            alert_id = UUID(str(row.id))
            await ins_session.commit()

        log.info(
            "task_death_alert_inserted",
            alert_id=str(alert_id),
            audit_event_uuid=str(audit_record.event_uuid),
            severity=descriptor.severity,
            category=descriptor.category,
            account_id=str(account_id),
            task_name=descriptor.payload.get("task_name"),
            env=audit_env,
        )

    return _hook


def _build_bar_sync_alert_dispatch_hook(
    settings: APISettings,
) -> object | None:
    """Construct the bar_sync ``partial_cycle_alert_hook`` closure or return None.

    2026-05-21 OI saga follow-up batch (PR #211 deferred). The bar_sync
    worker emits :class:`BarSyncAlertDescriptor` rows from
    :func:`services.data.bar_sync.BarSyncWorker._evaluate_alerts` when
    either (a) a partial cycle failure crosses
    ``CONSECUTIVE_ALERT_THRESHOLD`` (= 2 consecutive cycles with at
    least one failed market) or (b) the OI sentinel substitution
    crosses the same threshold (LEAN's resolver picks the contract
    because the sentinel is positive, but the value on disk is
    synthetic — typically /MCL's paper-tier NYMEX entitlement gap).

    Before this hook lands, the worker's ``_dispatch_alert`` path logs
    ``bar_sync_alert_dropped_no_hook`` + drops the descriptor; with the
    hook wired, each descriptor becomes:

      1. INSERT one row into ``alerts`` (severity / category / message /
         detail). ``triggering_audit_event_uuid`` stays NULL — bar_sync
         alerts have no audit-event upstream (alembic 0004 schema
         allows NULL on this column).
      2. Invoke :func:`services.webhook_pusher.dispatcher.dispatch_alert`
         with the newly minted alert_id so the operator-visible Discord
         ``#alerts`` push fires.

    Returns ``None`` (cleanly skipping the hook installation) when sops
    ``discord.webhook_urls.alerts`` isn't populated. The worker still
    fires alerts logging-only via ``bar_sync_alert_dropped_no_hook`` so
    the operator can grep manually until the sops field lands. This
    matches the recon + monitor hook-skip semantics.

    Pattern mirror of ``_build_monitor_alert_dispatch_hook`` (not the
    recon variant) because bar_sync alerts share the no-audit-upstream
    + fire-time account_id resolution shape of the monitor surface.

    Returns ``object | None`` to dodge a circular import — the actual
    hook signature is :class:`services.data.bar_sync_alerts.BarSyncAlertDispatchHook`
    and importing that at module-load would force a transitive load of
    the bar_sync module (which loads ib_async lazily at first-cycle, so
    the load itself is cheap, but the indirection keeps the module-top
    consistent with the other hook builders).
    """
    # Lazy imports keep module-load fast + avoid the circular-import surface.
    from services.data.bar_sync_alerts import BarSyncAlertDescriptor
    from services.webhook_pusher.dispatcher import dispatch_alert
    from services.webhook_pusher.payloads import (
        AlertCategory,
        AlertSeverity,
        ChannelName,
        EmailIdentity,
    )

    if settings.discord_webhook_url_alerts is None:
        log.warning(
            "bar_sync_alert_dispatch_hook_skipped_no_webhook_url",
            note=(
                "discord.webhook_urls.alerts not in sops; bar_sync cycles "
                "will still emit structured `bar_sync_alert_dropped_no_hook` "
                "WARNING logs on partial-cycle-failure + OI-sentinel "
                "substitution at the consecutive_count >= 2 threshold, but "
                "no Discord #alerts push will fire. Wire the sops field + "
                "restart api to enable."
            ),
        )
        return None

    webhook_urls: dict[ChannelName, str] = {
        ChannelName.DISCORD_ALERTS: settings.discord_webhook_url_alerts.get_secret_value(),
    }
    if settings.discord_webhook_url_critical is not None:
        webhook_urls[ChannelName.DISCORD_CRITICAL] = (
            settings.discord_webhook_url_critical.get_secret_value()
        )

    # bar_sync alerts are P2-only by spec (services.data.bar_sync_alerts
    # BAR_SYNC_ALERT_SEVERITY = "P2"); P2 routes to #alerts only per
    # webhook_pusher.payloads.SEVERITY_TO_CHANNELS. Email_identity is
    # only required for P0 routing, so the construction here matches the
    # recon hook's wire-it-if-the-fields-are-populated semantics rather
    # than the P0-blocking "all-or-none" pattern. If a future bar_sync
    # severity is added, the email path is already plumbed.
    email_identity: EmailIdentity | None = None
    if (
        settings.resend_api_key is not None
        and settings.resend_from_address is not None
        and settings.resend_to_address is not None
    ):
        email_identity = EmailIdentity(
            from_address=settings.resend_from_address,
            to_address=settings.resend_to_address,
            resend_api_key=settings.resend_api_key.get_secret_value(),
        )

    log.info(
        "bar_sync_alert_dispatch_hook_constructed",
        channels=[c.value for c in webhook_urls],
        email_wired=email_identity is not None,
    )

    audit_env = _audit_env_from_settings(settings)

    async def _hook(descriptor: BarSyncAlertDescriptor) -> None:
        """Per-alert: INSERT alerts row + dispatch via webhook_pusher.

        Resolves account_id at fire time so a missing-account-at-boot
        path (operator hasn't run /setup yet) doesn't break hook
        construction. If account_id is still unresolved at fire time,
        log + skip the dispatch — the worker's structured
        ``bar_sync_alert_dropped_no_hook`` path won't fire here because
        the hook IS wired; instead we surface
        ``bar_sync_alert_dispatch_skipped_no_account`` so the operator
        can correlate against the worker's per-cycle log.

        Two separate sessions per call mirror the recon + monitor hook
        pattern:

          1. INSERT into alerts using a fresh session_factory()-opened
             session.
          2. Open a fresh httpx.AsyncClient + session for the dispatch.

        Hook failures propagate up to the worker's ``_dispatch_alert``
        catch-and-log so a Discord 5xx doesn't wedge the cycle.
        """
        session_factory = api_db.get_session_factory()
        async with session_factory() as repo_session:
            repo = PostgresPhase1QueryRepo(repo_session)
            account_id = await repo.fetch_active_account_id()
        if account_id is None:
            log.warning(
                "bar_sync_alert_dispatch_skipped_no_account",
                note=(
                    "bar_sync fired a P2 alert descriptor but no active "
                    "account is provisioned; alerts row INSERT would "
                    "violate the NOT NULL FK to accounts. Run /setup to "
                    "create the account row + restart api."
                ),
                severity=descriptor.severity,
                category=descriptor.category,
            )
            return

        # Defense-in-depth: validate severity + category map to the
        # locked enums before attempting the INSERT (matches the
        # recon + monitor hooks' enum cross-check). bar_sync_alerts
        # already constrains via Literal types but enum validation
        # raises early on the wire boundary if a future planner change
        # leaks a non-canonical value.
        AlertSeverity(descriptor.severity)
        AlertCategory(descriptor.category)

        message_text = f"{descriptor.title}\n\n{descriptor.body}"
        async with session_factory() as ins_session:
            row = (
                await ins_session.execute(
                    text(
                        "INSERT INTO alerts ("
                        "    account_id, severity, category, message, detail"
                        ") VALUES ("
                        "    :acct, :sev, :cat, :msg, CAST(:detail AS JSONB)"
                        ") RETURNING id"
                    ),
                    {
                        "acct": account_id,
                        "sev": descriptor.severity,
                        "cat": descriptor.category,
                        "msg": message_text,
                        "detail": json.dumps(descriptor.payload),
                    },
                )
            ).fetchone()
            assert row is not None
            alert_id = UUID(str(row.id))
            await ins_session.commit()

        log.info(
            "bar_sync_alert_inserted",
            alert_id=str(alert_id),
            severity=descriptor.severity,
            category=descriptor.category,
            account_id=str(account_id),
            env=audit_env,
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
            async with session_factory() as disp_session:
                report = await dispatch_alert(
                    session=disp_session,
                    alert_id=alert_id,
                    http_client=http_client,
                    webhook_urls=webhook_urls,
                    email_identity=email_identity,
                )
        log.info(
            "bar_sync_alert_dispatched",
            alert_id=str(alert_id),
            short_circuited=report.short_circuited,
            delivery_status=dict(report.delivery_status),
        )

    return _hook


def _build_state_transition_hook(
    settings: APISettings,
) -> object | None:
    """Construct the recon ``state_transition_hook`` closure or return None.

    PR-J: closes the auto-halt seam left by PR #135. The recon planner
    flags actionable breaks (``plan.should_invoke_kill_switch=True``);
    the apply orchestrator fires this hook AFTER audit + breaks +
    alerts have landed. The hook is responsible for:

      1. Reading the current ``risk_state`` for the account
      2. Planning the transition via
         ``services.risk.state_machine.plan_invoke_kill_switch(
             trigger=TransitionTrigger.RECON_MISMATCH, ...)``
      3. Applying via ``services.risk.dispatch.apply_state_transition``
      4. Emitting the ``risk_state`` SSE envelope so the web /system
         page updates in real-time

    Returns ``object | None`` to dodge a circular import (the actual
    hook signature is :class:`services.reconciliation.apply.StateTransitionHook`).

    Returns ``None`` only if explicitly disabled via a future settings
    knob; today the hook always installs.

    The closure deliberately catches ``Exception`` around the
    state-transition pipeline. The audit chain + alerts have already
    fired by the time the hook runs; a transient state-machine failure
    (DB unreachable, etc.) shouldn't kill the scheduler. The operator
    sees the error in logs + the un-halted state via /system, can
    invoke kill-switch manually.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from services.api.db import get_session_factory
    from services.reconciliation.apply import ReconciliationKillSwitchContext
    from services.risk.dispatch import apply_state_transition
    from services.risk.state_machine import (
        HaltSeverity,
        RiskState,
        TransitionTrigger,
        plan_invoke_kill_switch,
    )

    log.info("state_transition_hook_constructed")

    async def _hook(ctx: ReconciliationKillSwitchContext) -> None:
        """Auto-halt the system on actionable recon break.

        Fetches current ``risk_state`` → plans NORMAL/CONVALESCENT →
        HALT_NEW via ``plan_invoke_kill_switch(trigger=RECON_MISMATCH)``
        → applies via ``apply_state_transition`` → emits SSE.

        If the system is ALREADY HALT_NEW (e.g., a prior cycle's break
        triggered the halt + recovery hasn't happened), the policy
        layer's IllegalTransitionError fires; we log + swallow because
        the desired state is already reached.
        """
        session_factory = get_session_factory()

        # Step 1: read current state.
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state, severity, convalescent_session_count "
                        "FROM risk_state WHERE account_id = :acct AND is_current = TRUE"
                    ),
                    {"acct": ctx.account_id},
                )
            ).fetchone()
        if row is None:
            log.warning(
                "state_transition_hook_no_current_risk_state",
                account_id=str(ctx.account_id),
                env=ctx.env,
                note=(
                    "No is_current=TRUE risk_state row; the auto-halt "
                    "cannot proceed. Operator should bootstrap the row "
                    "via the System page or psql."
                ),
            )
            return

        current_state = RiskState(row.state)
        current_severity = HaltSeverity(row.severity) if row.severity else None
        current_counter = row.convalescent_session_count

        if current_state == RiskState.HALT_NEW:
            # Already halted — desired terminal state. No-op + log.
            log.info(
                "state_transition_hook_already_halted",
                account_id=str(ctx.account_id),
                env=ctx.env,
                current_severity=current_severity.value if current_severity else None,
                actionable_break_count=ctx.actionable_break_count,
            )
            return

        # Step 2: plan the transition.
        try:
            plan = plan_invoke_kill_switch(
                current_state=current_state,
                current_severity=current_severity,
                convalescent_counter=current_counter or 0,
                trigger=TransitionTrigger.RECON_MISMATCH,
                triggered_by="risk_engine",
                timestamp_utc=_dt.now(tz=UTC).isoformat(),
            )
        except Exception:
            log.exception(
                "state_transition_hook_plan_failed",
                account_id=str(ctx.account_id),
                env=ctx.env,
            )
            return

        # Step 3: apply. apply_state_transition takes an AsyncSession,
        # not a session_factory, so open one for the apply.
        try:
            async with session_factory() as apply_session:
                applied = await apply_state_transition(
                    plan=plan,
                    db=apply_session,
                    account_id=ctx.account_id,
                    env=ctx.env,
                    phase_at_emit=1,
                )
        except Exception:
            log.exception(
                "state_transition_hook_apply_failed",
                account_id=str(ctx.account_id),
                env=ctx.env,
            )
            return

        log.warning(
            "state_transition_hook_halt_invoked",
            account_id=str(ctx.account_id),
            env=ctx.env,
            new_state=applied.new_state,
            new_severity=applied.new_severity,
            actionable_break_count=ctx.actionable_break_count,
            primary_audit_event_uuid=str(ctx.primary_audit_event_uuid),
            state_transition_audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        )

        # Step 4: SSE emit so the web /system page updates immediately.
        try:
            await sse_multiplexer.emit_sse(
                "risk_state",
                {
                    "state": applied.new_state,
                    "severity": applied.new_severity,
                    "reason": plan.reason,
                    "audit_event_uuid": str(applied.state_transition_audit_event_uuid),
                    "triggered_by": "auto_halt_recon_mismatch",
                    "triggering_audit_event_uuid": str(ctx.primary_audit_event_uuid),
                    "environment": ctx.env,
                },
            )
        except Exception:
            log.exception(
                "state_transition_hook_sse_emit_failed",
                account_id=str(ctx.account_id),
                env=ctx.env,
            )
            # SSE failure doesn't undo the state transition; consumers
            # will reconnect with Last-Event-ID + catch up.
            return

    return _hook


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
        # Option C (2026-05-28): EOD position source + the ib_gateway
        # connection params the reqPositions path needs. Recon reuses the
        # worker's host/port/account (same gateway + account) but on its
        # own clientId=4 (the ibkr_intraday default), so it stays isolated
        # from the order worker (clientId=1) and bar_sync (clientId=3).
        position_source=settings.eod_recon_position_source,
        ibkr_host=settings.ibkr_host,
        ibkr_port=settings.ibkr_port,
        ibkr_account_id=settings.ibkr_account,
    )
    alert_dispatch_hook = _build_alert_dispatch_hook(settings)
    state_transition_hook = _build_state_transition_hook(settings)
    callback = make_cycle_callback(
        config=config,
        session_factory=api_db.get_session_factory(),
        alert_dispatch_hook=alert_dispatch_hook,  # type: ignore[arg-type]
        state_transition_hook=state_transition_hook,  # type: ignore[arg-type]
    )
    scheduler = ReconciliationScheduler(callback=callback)
    task = asyncio.create_task(scheduler.run_forever(), name="reconciliation_scheduler.run_forever")
    log.info(
        "reconciliation_scheduler_spawned",
        account_id=str(account_id),
        env=_audit_env_from_settings(settings),
        flex_query_id=settings.flex_query_id,
        position_source=settings.eod_recon_position_source,
        alert_dispatch_hook_wired=alert_dispatch_hook is not None,
        state_transition_hook_wired=state_transition_hook is not None,
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


async def _start_heartbeat_probe(
    settings: APISettings,
) -> tuple[object, object] | None:
    """Spawn the heartbeat staleness probe; return (probe, task) or None.

    PR #154 follow-up (2026-05-16). Ticks every 15 min, snapshots the
    in-memory ``HeartbeatRegistry``, and emits
    ``HEARTBEAT_STALE_DETECTED`` audit rows + P2 Discord alerts for each
    cron-like service past its locked threshold.

    Best-effort: requires an active accounts row (so the audit row has a
    valid account_id FK). When the row is missing — typical Phase 0
    pre-setup state — the probe doesn't start + a structured warning is
    logged. The api continues without it.

    Shares the same ``alert_dispatch_hook`` closure the reconciliation
    scheduler uses — when sops Discord URLs aren't populated, the hook
    is None and the probe still writes audit rows but skips the alert
    dispatch (the audit chain is the durable breadcrumb).
    """
    from services.api.heartbeat_probe import HeartbeatProbe

    async with session_scope() as repo_session:
        repo = PostgresPhase1QueryRepo(repo_session)
        account_id = await repo.fetch_active_account_id()
    if account_id is None:
        log.warning(
            "heartbeat_probe_no_active_account",
            note="run /setup before the staleness probe can resolve an account_id",
        )
        return None

    alert_dispatch_hook = _build_alert_dispatch_hook(settings)
    probe = HeartbeatProbe(
        session_factory=api_db.get_session_factory(),
        account_id=account_id,
        env=_audit_env_from_settings(settings),
        alert_dispatch_hook=alert_dispatch_hook,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(probe.run_forever(), name="heartbeat_probe.run_forever")
    log.info(
        "heartbeat_probe_spawned",
        account_id=str(account_id),
        env=_audit_env_from_settings(settings),
        alert_dispatch_hook_wired=alert_dispatch_hook is not None,
    )
    return probe, task


async def _stop_heartbeat_probe(state: tuple[object, object] | None) -> None:
    """Request stop + await the probe task. Best-effort."""
    if state is None:
        return
    probe, task = state
    try:
        probe.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("heartbeat_probe_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("heartbeat_probe_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("heartbeat_probe_shutdown_cancelled")
        except Exception:
            log.exception("heartbeat_probe_shutdown_unclean")
    except Exception:
        log.exception("heartbeat_probe_task_join_failed")


async def _start_bar_sync_worker(
    settings: APISettings,
) -> tuple[object, object] | None:
    """Construct + start the BarSyncWorker; return (worker, task) or None.

    Option C of the 2026-05-20 data-layer pivot v2 (see
    ``Docs/decisions-log.md`` 2026-05-20 evening entry). Fetches daily
    OHLCV bars for the Phase 1 universe from IBKR via a dedicated
    ib-async connection on ``clientId=3`` (locked code default since
    PR #210; deploy reality; distinct from the OrderPlacementWorker's
    ``clientId=1`` code default + the ``clientId=2`` deploy override
    the order-worker currently runs with) and writes them to the
    shared ``lean_data`` Docker volume in LEAN's expected on-disk
    format. LEAN reads via FakeDataQueue +
    SubscriptionDataReaderHistoryProvider on its 17:30 ET signal cycle.

    Best-effort: when ``bar_sync_enabled=False`` returns None + logs a
    structured warning. Other failure modes (ib-async dep missing, etc.)
    log + return None without crashing the api boot.

    Connection lifecycle is per-cycle (connect → fetch → disconnect)
    inside ``BarSyncWorker.run_cycle``, not held across ticks. The
    short-lived socket is intentional defense-in-depth: a bug or hang
    in the read-only historical path cannot backpressure the long-lived
    order socket on ``clientId=1`` (or the ``clientId=2`` deploy-
    override variant).

    PR #211 follow-up (this PR): the worker is now constructed with a
    ``partial_cycle_alert_hook`` built via
    :func:`_build_bar_sync_alert_dispatch_hook`. When sops
    ``discord.webhook_urls.alerts`` is populated the hook fires on the
    two P2 alert flavors (partial-cycle failure + OI-sentinel
    substitution) at ``consecutive_count >= 2``. When the URL isn't
    populated the hook is None and the worker logs
    ``bar_sync_alert_dropped_no_hook`` instead — same operator-visible
    descriptor fields, manual-grep workflow.
    """
    if not settings.bar_sync_enabled:
        log.warning("bar_sync_worker_disabled_via_setting")
        return None

    # Lazy imports — keeps the rest of the api importable even if the
    # services.data subpackage churns + avoids loading ib-async at
    # module-load (the worker only needs it at first-cycle).
    from datetime import time as _time
    from pathlib import Path as _Path

    from services.data.bar_sync import BarSyncConfig, BarSyncWorker

    # Parse the HH:MM schedule string. The Pydantic field's regex
    # constraint guarantees the shape; the split is safe.
    hh_str, mm_str = settings.bar_sync_schedule_et.split(":", 1)
    sync_time = _time(hour=int(hh_str), minute=int(mm_str))

    config = BarSyncConfig(
        data_root=_Path(settings.bar_sync_data_root),
        bars_per_fetch=settings.bar_sync_bars_per_fetch,
        sync_time_et=sync_time,
        ibkr_host=settings.ibkr_host,
        ibkr_port=settings.ibkr_port,
        ibkr_client_id=settings.bar_sync_client_id,
        ibkr_account=settings.ibkr_account,
        ibkr_call_timeout_seconds=settings.bar_sync_ibkr_call_timeout_seconds,
    )
    alert_dispatch_hook = _build_bar_sync_alert_dispatch_hook(settings)
    worker = BarSyncWorker(config=config, partial_cycle_alert_hook=alert_dispatch_hook)
    # Register the worker with the read-only status provider so
    # GET /api/system/bar-sync (+ the Discord /barsync command) can read
    # the last cycle outcome without SSHing to grep journald. In-memory
    # only; mirrors the heartbeat registry. See services/api/bar_sync_status.py.
    from services.api.bar_sync_status import get_bar_sync_status_provider

    get_bar_sync_status_provider().set_source(worker)
    task = asyncio.create_task(worker.run_forever(), name="bar_sync_worker.run_forever")
    log.info(
        "bar_sync_worker_spawned",
        env=_audit_env_from_settings(settings),
        ibkr_host=settings.ibkr_host,
        ibkr_port=settings.ibkr_port,
        ibkr_client_id=settings.bar_sync_client_id,
        bars_per_fetch=settings.bar_sync_bars_per_fetch,
        sync_time_et=settings.bar_sync_schedule_et,
        data_root=settings.bar_sync_data_root,
        ibkr_call_timeout_seconds=settings.bar_sync_ibkr_call_timeout_seconds,
        universe_size=len(config.markets),
        alert_dispatch_hook_wired=alert_dispatch_hook is not None,
    )
    return worker, task


async def _stop_bar_sync_worker(state: tuple[object, object] | None) -> None:
    """Request stop + await the worker task. Best-effort."""
    # Clear the status provider unconditionally so a stale worker handle
    # can't outlive the lifespan (the next start re-registers a fresh one).
    from services.api.bar_sync_status import get_bar_sync_status_provider

    get_bar_sync_status_provider().clear()
    if state is None:
        return
    worker, task = state
    try:
        worker.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("bar_sync_worker_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("bar_sync_worker_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("bar_sync_worker_shutdown_cancelled")
        except Exception:
            log.exception("bar_sync_worker_shutdown_unclean")
    except Exception:
        log.exception("bar_sync_worker_task_join_failed")


async def _start_async_task_monitor(
    settings: APISettings,
    *,
    order_placement: tuple[object, object] | None,
    reconciliation: tuple[object, object] | None,
    heartbeat_probe: tuple[object, object] | None,
    bar_sync: tuple[object, object] | None = None,
    monitor_alert_hook: object | None = None,
    task_death_alert_hook: object | None = None,
) -> tuple[object, object] | None:
    """Construct + start the AsyncTaskMonitor; return (monitor, task) or None.

    2026-05-17 follow-up to the silent-worker-death pattern. The 3
    lifespan background tasks (worker, scheduler, probe) silently die
    when they hit an uncaught BaseException OR hang indefinitely on an
    unresponsive IBKR await. Without an observer, dead tasks are
    invisible. The monitor ticks every
    ``async_task_monitor_interval_seconds`` and logs
    ``async_task_died`` events when a tracked task transitions to
    ``.done()`` unexpectedly.

    Best-effort: the monitor is a debugging aid, not load-bearing. If
    construction fails the api still serves traffic.
    """
    if not settings.async_task_monitor_enabled:
        log.info("async_task_monitor_disabled_via_setting")
        return None
    from services.api.async_task_monitor import AsyncTaskMonitor, collect_tracked_tasks

    tracked = collect_tracked_tasks(
        order_placement=order_placement,
        reconciliation=reconciliation,
        heartbeat_probe=heartbeat_probe,
        bar_sync=bar_sync,
    )

    ibkr_error_tracker = _build_ibkr_error_tracker(order_placement, monitor_alert_hook)
    if ibkr_error_tracker is not None:
        log.info(
            "async_task_monitor_ibkr_probe_wired",
            tracker_name=ibkr_error_tracker.name,
            note=(
                "IBKR connectivity probe is live. Fresh "
                "1100/1101/1102 errors will emit "
                "async_task_monitor_ibkr_connectivity_warn WARNING."
            ),
        )
    elif order_placement is None:
        log.info(
            "async_task_monitor_ibkr_probe_not_wired",
            reason="order_placement_worker_not_spawned",
            note=(
                "OrderPlacementWorker is None (likely no active account "
                "or worker disabled via setting). IBKR connectivity "
                "probe will not run; structured-log path via adapter "
                "is unaffected."
            ),
        )
    else:
        log.warning(
            "async_task_monitor_ibkr_probe_not_wired",
            reason="worker_has_no_ibkr_client_attr",
            note=(
                "OrderPlacementWorker exists but exposes no "
                "_ibkr_client. IBKR connectivity probe will not run."
            ),
        )

    # Recovery-agent task-death hook (drill 5/6 follow-up, 2026-05-26).
    # Typed as the concrete TaskDeathAlertHook for the monitor; the
    # builder returns object | None to dodge import-time circularity.
    from services.api.async_task_monitor import TaskDeathAlertHook

    typed_task_death_hook: TaskDeathAlertHook | None = (
        None if task_death_alert_hook is None else task_death_alert_hook  # type: ignore[assignment]
    )
    monitor = AsyncTaskMonitor(
        tracked,
        interval_seconds=settings.async_task_monitor_interval_seconds,
        ibkr_error_state=ibkr_error_tracker,
        task_death_alert_hook=typed_task_death_hook,
    )
    task = asyncio.create_task(
        monitor.run_forever(),
        name="async_task_monitor.run_forever",
    )
    log.info(
        "async_task_monitor_spawned",
        interval_seconds=settings.async_task_monitor_interval_seconds,
        tracked_count=len(tracked),
        ibkr_probe_wired=ibkr_error_tracker is not None,
        task_death_hook_wired=typed_task_death_hook is not None,
    )
    return monitor, task


def _build_ibkr_error_tracker(
    order_placement: tuple[object, object] | None,
    monitor_alert_hook: object | None = None,
) -> TrackedIbkrErrorState | None:
    """Pure-policy: build a ``TrackedIbkrErrorState`` from lifespan state.

    2026-05-18 drill 5 follow-up #2 — extracted from
    ``_start_async_task_monitor`` so it can be unit-tested without
    spawning a real ``AsyncTaskMonitor`` (whose ``run_forever`` task
    would emit structlog log lines through the
    ``services.api.async_task_monitor`` module logger, caching it
    under ``cache_logger_on_first_use=True`` set by
    ``_configure_structlog``, which then prevents downstream
    ``test_async_task_monitor.py`` tests from intercepting via
    ``capture_logs()``).

    Resolves the IBKR adapter from the worker via
    ``getattr(worker, "_ibkr_client", None)`` (same pattern as the
    disconnect path in ``_stop_order_placement_worker``) and builds
    a provider callable closing over the adapter instance. The
    provider reads the adapter's ``last_ibkr_error`` property each
    probe; the property returns ``None`` until the first errorEvent
    fires.

    Drill 5 follow-up #2-FU-1: optional ``monitor_alert_hook`` is the
    Discord ``#alerts`` dispatch closure built by
    ``_build_monitor_alert_dispatch_hook``. When wired, the monitor
    fires the hook with a ``MonitorAlertDescriptor`` immediately
    after emitting ``async_task_monitor_ibkr_connectivity_warn``. When
    None, the WARNING fires but no Discord push is attempted.

    Returns ``None`` when:
    * ``order_placement`` is None (worker disabled / no active account).
    * The worker exposes no ``_ibkr_client`` attribute (unexpected;
      worker class shape changed).

    Both branches leave the adapter's own ``ibkr_error_received``
    structured-log path unaffected — only the monitor's per-cycle
    WARNING surface is gated.
    """
    if order_placement is None:
        return None
    worker, _task = order_placement
    ibkr_client = getattr(worker, "_ibkr_client", None)
    if ibkr_client is None:
        return None

    # Lazy imports at function call time (mirror the pattern in
    # _start_async_task_monitor). The TYPE_CHECKING-only quoted return
    # type above keeps mypy happy without importing at module level.
    from services.api.async_task_monitor import MonitorAlertHook, TrackedIbkrErrorState
    from services.execution.ibkr_adapter import IbkrErrorState

    captured_client = ibkr_client

    def _ibkr_error_provider() -> IbkrErrorState | None:
        return captured_client.last_ibkr_error  # type: ignore[no-any-return]

    # The hook arg is `object | None` at this function's API boundary
    # (dodges import-time circularity at module-load); narrow to the
    # concrete callable type here for the dataclass field.
    typed_hook: MonitorAlertHook | None = (
        None if monitor_alert_hook is None else monitor_alert_hook  # type: ignore[assignment]
    )

    return TrackedIbkrErrorState(
        name="ibkr_adapter",
        provider=_ibkr_error_provider,
        alert_dispatch_hook=typed_hook,
    )


async def _stop_async_task_monitor(state: tuple[object, object] | None) -> None:
    """Request stop + await the monitor task; best-effort."""
    if state is None:
        return
    from services.api.async_task_monitor import stop_async_task_monitor

    # The state tuple is (monitor, task); pass through via the typed
    # helper which handles cancel/timeout/exception swallow.
    await stop_async_task_monitor(state)  # type: ignore[arg-type]


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
    heartbeat_probe_state: tuple[object, object] | None = None
    bar_sync_state: tuple[object, object] | None = None
    async_task_monitor_state: tuple[object, object] | None = None
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
        try:
            heartbeat_probe_state = await _start_heartbeat_probe(settings)
        except Exception:
            # Probe startup is best-effort; same rationale as the recon
            # scheduler. The /api/system/heartbeats endpoint still works
            # without the probe running.
            log.exception("heartbeat_probe_startup_failed")
        try:
            # Bar-sync worker (Option C of the 2026-05-20 data-layer
            # pivot v2). Reads from IBKR on clientId=2, writes to the
            # lean_data Docker volume that lean_local mounts. Cycle
            # fires at 17:00 ET daily.
            bar_sync_state = await _start_bar_sync_worker(settings)
        except Exception:
            # Bar-sync startup is best-effort; without it LEAN reads stale
            # bars on the next cycle. The api still serves the rest of
            # its surface and the operator can restart to recover.
            log.exception("bar_sync_worker_startup_failed")
        try:
            # The monitor MUST start AFTER the other 4 so it can
            # capture their final `(worker, task)` tuples (or `None`
            # for ones that failed to spawn). The monitor itself is
            # a 5th task and is NOT in its own tracked set.
            #
            # Drill 5 follow-up #2-FU-1: build the monitor's alert
            # dispatch hook BEFORE starting the monitor so a fresh
            # 1100 surfaces in Discord #alerts. Hook returns None if
            # sops `discord.webhook_urls.alerts` is unpopulated;
            # monitor degrades gracefully (WARNING log fires, no
            # Discord push).
            monitor_alert_hook = _build_monitor_alert_dispatch_hook(settings)
            # Recovery-agent task-death hook (drill 5/6 follow-up landed
            # 2026-05-26). Audit-first emit of ASYNC_TASK_DIED +
            # INSERT of an alerts row (category='worker_failure',
            # severity='P0') when an allow-listed lifespan task
            # transitions to .done(). The recovery agent at
            # scripts/operator_tools/recovery_agent.py polls the
            # alerts table every 60s and invokes replay_executions.py
            # for transient failures. Hook returns None only when
            # explicitly disabled via setting; default is always-on.
            task_death_alert_hook = _build_task_death_alert_hook(settings)
            async_task_monitor_state = await _start_async_task_monitor(
                settings,
                order_placement=worker_state,
                reconciliation=recon_state,
                heartbeat_probe=heartbeat_probe_state,
                bar_sync=bar_sync_state,
                monitor_alert_hook=monitor_alert_hook,
                task_death_alert_hook=task_death_alert_hook,
            )
        except Exception:
            # Monitor startup is best-effort; the api functions without
            # it (just with less observability into silent task death).
            log.exception("async_task_monitor_startup_failed")
        log.info("api_ready")
        yield
    finally:
        log.info("api_stopping")
        await _stop_async_task_monitor(async_task_monitor_state)
        await _stop_bar_sync_worker(bar_sync_state)
        await _stop_heartbeat_probe(heartbeat_probe_state)
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
