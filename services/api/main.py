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
from typing import Literal
from uuid import UUID

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

    worker = OrderPlacementWorker(
        session_factory=api_db.get_session_factory(),
        ibkr_client=ibkr_client,
        account_id=account_id,
        env=_audit_env_from_settings(settings),
        poll_interval_seconds=settings.order_placement_poll_interval_seconds,
        ibkr_call_timeout_seconds=settings.ibkr_call_timeout_seconds,
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


async def _start_async_task_monitor(
    settings: APISettings,
    *,
    order_placement: tuple[object, object] | None,
    reconciliation: tuple[object, object] | None,
    heartbeat_probe: tuple[object, object] | None,
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
    from services.api.async_task_monitor import (
        AsyncTaskMonitor,
        collect_tracked_tasks,
    )

    tracked = collect_tracked_tasks(
        order_placement=order_placement,
        reconciliation=reconciliation,
        heartbeat_probe=heartbeat_probe,
    )
    monitor = AsyncTaskMonitor(
        tracked,
        interval_seconds=settings.async_task_monitor_interval_seconds,
    )
    task = asyncio.create_task(
        monitor.run_forever(),
        name="async_task_monitor.run_forever",
    )
    log.info(
        "async_task_monitor_spawned",
        interval_seconds=settings.async_task_monitor_interval_seconds,
        tracked_count=len(tracked),
    )
    return monitor, task


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
            # The monitor MUST start AFTER the other 3 so it can
            # capture their final `(worker, task)` tuples (or `None`
            # for ones that failed to spawn). The monitor itself is
            # a 4th task and is NOT in its own tracked set.
            async_task_monitor_state = await _start_async_task_monitor(
                settings,
                order_placement=worker_state,
                reconciliation=recon_state,
                heartbeat_probe=heartbeat_probe_state,
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
