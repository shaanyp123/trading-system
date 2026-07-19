"""services/api/main.py — FastAPI app entrypoint.

Wired in execution order:

  1. structlog configured for JSON output on production / Console on dev.
  2. Settings loaded from env (host secrets file exported by entrypoint).
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
from services.api import marks
from services.api import sse as sse_multiplexer
from services.api.config import APISettings, get_settings
from services.api.db import close_pool, init_pool, session_scope
from services.api.errors import register_error_handlers
from services.api.middleware import BotAuthMiddleware, register_middleware
from services.api.repos.phase1 import PostgresPhase1QueryRepo
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
from services.api.session import SessionMiddleware

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
                "It will not be re-emitted for this row. If lost: delete the "
                "unconsumed setup_tokens row (or wait out its 24h expiry), "
                "then restart the api container — the lifespan mints a fresh "
                "owner token whenever no unconsumed unexpired one exists "
                "(grep the api logs for SETUP_TOKEN_EMITTED)."
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


def _build_alert_dispatch_hook(
    settings: APISettings,
) -> object | None:
    """Construct the recon ``alert_dispatch_hook`` closure or return None.

    Closes the seam left by PR #135: the recon planner emits
    :class:`AlertDescriptor` rows, the apply orchestrator fires per-alert
    callbacks, and this is where the api lifespan turns a callback into
    "INSERT alerts row + invoke `dispatch_alert`" — the operator-visible
    Discord push.

    Returns ``None`` (cleanly skipping the hook installation) when the secrets
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
                "discord.webhook_urls.alerts not in the secrets file; reconciliation "
                "cycle will run + alerts will log a 'hook not wired' "
                "warning. Wire the secrets field + restart api to enable."
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


def _build_monitor_alert_dispatch_hook(
    settings: APISettings,
) -> object | None:
    """Construct the AsyncTaskMonitor's alert dispatch hook or return None.

    Drill 5 follow-up #2-FU-1 (PR after #177 + #179). Turns a
    :class:`MonitorAlertDescriptor` into an ``alerts`` row + Discord
    ``#alerts`` push so the operator sees monitor-surfaced events
    in-channel within ~30s of the probe tick that observed them.
    (The IBKR connectivity probe that originally motivated this hook
    was retired with the IBKR execution layer in crypto-pivot C0-B2b;
    the hook remains the generic monitor→Discord seam.)

    Returns ``None`` (cleanly skipping the hook installation) when
    secrets ``discord.webhook_urls.alerts`` isn't populated. The probe's
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
                "discord.webhook_urls.alerts not in the secrets file; the monitor's "
                "IBKR connectivity probe will still emit the structured "
                "WARNING log on 1100/1101/1102 events but no Discord "
                "#alerts push will fire. Wire the secrets field + restart "
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
    task transitioned to ``.done()`` unexpectedly (post-C0-B2b:
    ``coinbase_market_data.run_forever``), the monitor fires this
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


def _build_convalescent_tick_hook(
    settings: APISettings,
    account_id: UUID,
) -> object:
    """Construct the recon cycle's CONVALESCENT clean-day tick closure.

    2026-07-09 CONVALESCENT amendment wiring: the 00:15 UTC recon cycle
    is the production tick source for the 3-clean-UTC-days graduation
    counter. The closure wraps
    :func:`services.risk.dispatch.apply_convalescent_clean_day_tick`
    (which owns the predicate, counter UPDATE, and audit-first
    graduation) + the ``risk_state`` SSE fan-out on graduation — the
    same layering as :func:`_build_state_transition_hook` (risk-layer
    call in the closure; recon module stays free of risk imports).

    Returns ``object`` to dodge the circular-import shape (actual type:
    :class:`services.reconciliation.eod_cycle.ConvalescentTickHook`,
    ``Callable[[], Awaitable[bool]]`` — True = graduated).
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from services.api.db import get_session_factory
    from services.risk.dispatch import apply_convalescent_clean_day_tick

    log.info("convalescent_tick_hook_constructed")

    async def _tick() -> bool:
        applied = await apply_convalescent_clean_day_tick(
            session_factory=get_session_factory(),
            account_id=account_id,
            env=_audit_env_from_settings(settings),
            phase_at_emit=1,
            now_utc=_dt.now(tz=UTC),
        )
        if applied is None:
            return False
        # Graduation: fan out the risk_state SSE envelope (existing event
        # type — no [A03] migration) so /system updates immediately.
        try:
            await sse_multiplexer.emit_sse(
                "risk_state",
                {
                    "state": applied.new_state,
                    "severity": applied.new_severity,
                    "reason": "convalescent_graduated",
                    "audit_event_uuid": str(applied.state_transition_audit_event_uuid),
                    "triggered_by": "convalescent_clean_day_tick",
                    "environment": _audit_env_from_settings(settings),
                },
            )
        except Exception:
            log.exception(
                "convalescent_tick_sse_emit_failed",
                account_id=str(account_id),
            )
            # SSE failure doesn't undo the graduation; consumers catch up.
        return True

    return _tick


async def _start_reconciliation_scheduler(
    settings: APISettings,
) -> tuple[object, object] | None:
    """Construct + start the ReconciliationScheduler; return (sched, task) or None.

    Best-effort: requires both ``coinbase_api_key_name`` and
    ``coinbase_api_private_key`` populated in the secrets file (mapped
    via ``services/api/entrypoint.py`` — the same CDP key pair the
    execution layer uses). When either is missing — or the operator has
    flipped ``reconciliation_scheduler_enabled=False`` — the scheduler
    does not start + a structured warning is logged so the operator
    knows to populate the secrets file + restart the api.

    The cycle callback is built by :func:`services.reconciliation.eod_cycle.make_cycle_callback`
    over a :class:`services.reconciliation.coinbase_fetcher.CoinbaseEodFetcher`
    and fires once per UTC calendar day at 00:15 UTC (delta spec §3.5 —
    after the 00:05 UTC daily decision). Errors inside the callback are
    logged + swallowed by the scheduler so a transient venue outage
    doesn't kill the scheduler — tomorrow's cycle still fires.
    """
    if not settings.reconciliation_scheduler_enabled:
        log.warning("reconciliation_scheduler_disabled_via_setting")
        return None

    if settings.coinbase_api_key_name is None or settings.coinbase_api_private_key is None:
        log.warning(
            "reconciliation_scheduler_coinbase_credentials_missing",
            note=(
                "Set coinbase.api_key_name + coinbase.api_private_key in the "
                "secrets file to enable the EOD reconciliation cycle. See "
                "deploy/reconciliation/README.md."
            ),
        )
        return None

    from services.execution.coinbase_client import SdkCoinbaseBrokerClient
    from services.reconciliation.coinbase_fetcher import CoinbaseEodFetcher
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

    broker_client = SdkCoinbaseBrokerClient(
        api_key_name=settings.coinbase_api_key_name.get_secret_value(),
        api_private_key=settings.coinbase_api_private_key.get_secret_value(),
    )
    fetcher = CoinbaseEodFetcher(client=broker_client)
    config = EodCycleConfig(
        account_id=account_id,
        env=_audit_env_from_settings(settings),
    )
    alert_dispatch_hook = _build_alert_dispatch_hook(settings)
    state_transition_hook = _build_state_transition_hook(settings)
    convalescent_tick = _build_convalescent_tick_hook(settings, account_id)
    callback = make_cycle_callback(
        config=config,
        session_factory=api_db.get_session_factory(),
        fetcher=fetcher,
        alert_dispatch_hook=alert_dispatch_hook,  # type: ignore[arg-type]
        state_transition_hook=state_transition_hook,  # type: ignore[arg-type]
        convalescent_tick=convalescent_tick,  # type: ignore[arg-type]
    )
    scheduler = ReconciliationScheduler(callback=callback)
    task = asyncio.create_task(scheduler.run_forever(), name="reconciliation_scheduler.run_forever")
    log.info(
        "reconciliation_scheduler_spawned",
        account_id=str(account_id),
        env=_audit_env_from_settings(settings),
        alert_dispatch_hook_wired=alert_dispatch_hook is not None,
        state_transition_hook_wired=state_transition_hook is not None,
        convalescent_tick_wired=True,
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


async def _start_usdc_rewards_capture(
    settings: APISettings,
) -> tuple[object, object] | None:
    """Construct + start the daily USDC rewards capture; (sched, task) or None.

    Operator feature request (decisions-log 2026-07-10, measure-don't-
    estimate): a daily 00:20 UTC job — after the 00:15 recon — that
    polls the Coinbase v2 USDC ledger for candidate reward transactions
    and snapshots the CBI spot USD/USDC balances
    (``services/data/usdc_rewards.py``). Same fail-closed credential
    contract as the recon scheduler (same CDP key pair); unlike recon it
    needs NO active accounts row — its tables are venue-scoped (no
    account FK), matching the funding_rates precedent.

    Reuses :class:`services.reconciliation.scheduler.ReconciliationScheduler`
    as the generic fire-once-per-UTC-day primitive (its
    ``eod_recon_time_utc`` knob is just "the daily fire time"); errors
    inside the callback are logged + swallowed so a venue outage never
    kills the scheduler — and every insert is idempotent, so the
    restart-refire semantics are safe.
    """
    if not settings.usdc_rewards_capture_enabled:
        log.warning("usdc_rewards_capture_disabled_via_setting")
        return None

    if settings.coinbase_api_key_name is None or settings.coinbase_api_private_key is None:
        log.warning(
            "usdc_rewards_capture_coinbase_credentials_missing",
            note=(
                "Set coinbase.api_key_name + coinbase.api_private_key in the "
                "secrets file to enable the daily USDC rewards capture."
            ),
        )
        return None

    from services.data.usdc_rewards import (
        DEFAULT_CASH_CAPTURE_TIME_UTC,
        SdkCoinbaseCashClient,
        UsdcRewardsCaptureJob,
        make_capture_callback,
    )
    from services.reconciliation.scheduler import ReconciliationScheduler

    client = SdkCoinbaseCashClient(
        api_key_name=settings.coinbase_api_key_name.get_secret_value(),
        api_private_key=settings.coinbase_api_private_key.get_secret_value(),
    )
    job = UsdcRewardsCaptureJob(
        client=client,
        session_factory=api_db.get_session_factory(),
    )
    scheduler = ReconciliationScheduler(
        callback=make_capture_callback(job),
        eod_recon_time_utc=DEFAULT_CASH_CAPTURE_TIME_UTC,
    )
    task = asyncio.create_task(scheduler.run_forever(), name="usdc_rewards_capture.run_forever")
    log.info(
        "usdc_rewards_capture_spawned",
        fire_time_utc=DEFAULT_CASH_CAPTURE_TIME_UTC.isoformat(),
    )
    return scheduler, task


async def _start_binance_funding_proxy(
    settings: APISettings,
) -> tuple[object, object] | None:
    """Construct + start the daily Binance funding-proxy logger; or None.

    Gate B3 comparison series (C1→C2 build): settled Binance USDT-perp
    funding rates persisted into ``funding_rates`` with
    ``source='binance_proxy'`` (``services/data/binance_funding_proxy.py``).
    Public endpoint — no credential gate; the only switch is
    ``API_BINANCE_FUNDING_PROXY_ENABLED`` (default on — read-only
    telemetry). Reuses the generic once-per-UTC-day scheduler primitive,
    same shape as the USDC rewards capture.
    """
    if not settings.binance_funding_proxy_enabled:
        log.warning("binance_funding_proxy_disabled_via_setting")
        return None

    from services.data.binance_funding_proxy import (
        DEFAULT_FUNDING_PROXY_POLL_TIME_UTC,
        BinanceFundingProxyJob,
        HttpxBinanceFundingClient,
        make_funding_proxy_callback,
        proxy_symbols_for_spot_products,
    )
    from services.data.coinbase_market_data import SPOT_SIGNAL_PRODUCT_IDS
    from services.reconciliation.scheduler import ReconciliationScheduler

    symbols = proxy_symbols_for_spot_products(SPOT_SIGNAL_PRODUCT_IDS)
    job = BinanceFundingProxyJob(
        client=HttpxBinanceFundingClient(base_url=settings.binance_fapi_base_url),
        session_factory=api_db.get_session_factory(),
        symbols=symbols,
    )
    scheduler = ReconciliationScheduler(
        callback=make_funding_proxy_callback(job),
        eod_recon_time_utc=DEFAULT_FUNDING_PROXY_POLL_TIME_UTC,
    )
    task = asyncio.create_task(scheduler.run_forever(), name="binance_funding_proxy.run_forever")
    log.info(
        "binance_funding_proxy_spawned",
        fire_time_utc=DEFAULT_FUNDING_PROXY_POLL_TIME_UTC.isoformat(),
        symbols=list(symbols),
    )
    return scheduler, task


async def _stop_binance_funding_proxy(state: tuple[object, object] | None) -> None:
    """Request stop + await the proxy scheduler task. Best-effort."""
    if state is None:
        return
    scheduler, task = state
    try:
        scheduler.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("binance_funding_proxy_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("binance_funding_proxy_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("binance_funding_proxy_shutdown_cancelled")
        except Exception:
            log.exception("binance_funding_proxy_shutdown_unclean")
    except Exception:
        log.exception("binance_funding_proxy_task_join_failed")


async def _start_cash_manager(settings: APISettings) -> tuple[object, object] | None:
    """Construct + start the daily cash-yield sweep worker; DORMANT default.

    Delta spec §3.6, shipped behind ``cash_manager_enabled = False``: the
    first branch below early-returns, so in the default configuration NO
    scheduler task, NO venue client, and NO sweep path is ever
    constructed. Activation (C2 decision, delta-spec open question #1 —
    same-day reclaim verified live) = flipping API_CASH_MANAGER_ENABLED
    per the operator runbook ``deploy/cash_manager/README.md``.
    """
    if not settings.cash_manager_enabled:
        log.info("cash_manager_dormant_via_setting")
        return None

    if settings.coinbase_api_key_name is None or settings.coinbase_api_private_key is None:
        log.warning(
            "cash_manager_coinbase_credentials_missing",
            note=(
                "Set coinbase.api_key_name + coinbase.api_private_key in the "
                "secrets file before enabling the cash manager."
            ),
        )
        return None

    from services.execution.coinbase_client import SdkCoinbaseBrokerClient
    from services.reconciliation.scheduler import ReconciliationScheduler
    from services.risk.cash_manager import (
        DEFAULT_SWEEP_TIME_UTC,
        CashManagerJob,
        SdkCashSweepVenueClient,
        make_sweep_callback,
    )

    key_name = settings.coinbase_api_key_name.get_secret_value()
    private_key = settings.coinbase_api_private_key.get_secret_value()
    job = CashManagerJob(
        broker=SdkCoinbaseBrokerClient(
            api_key_name=key_name,
            api_private_key=private_key,
        ),
        sweep_client=SdkCashSweepVenueClient(
            api_key_name=key_name,
            api_private_key=private_key,
        ),
        session_factory=api_db.get_session_factory(),
        env=_audit_env_from_settings(settings),
        phase_at_emit=1,
    )
    scheduler = ReconciliationScheduler(
        callback=make_sweep_callback(job),
        eod_recon_time_utc=DEFAULT_SWEEP_TIME_UTC,
    )
    task = asyncio.create_task(scheduler.run_forever(), name="cash_manager.run_forever")
    log.warning(
        "cash_manager_ENABLED_spawned",
        fire_time_utc=DEFAULT_SWEEP_TIME_UTC.isoformat(),
        note="C2 activation path — verify deploy/cash_manager/README.md checklist",
    )
    return scheduler, task


async def _stop_usdc_rewards_capture(state: tuple[object, object] | None) -> None:
    """Request stop + await the capture scheduler task. Best-effort."""
    if state is None:
        return
    scheduler, task = state
    try:
        scheduler.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("usdc_rewards_capture_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("usdc_rewards_capture_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("usdc_rewards_capture_shutdown_cancelled")
        except Exception:
            log.exception("usdc_rewards_capture_shutdown_unclean")
    except Exception:
        log.exception("usdc_rewards_capture_task_join_failed")


async def _stop_cash_manager(state: tuple[object, object] | None) -> None:
    """Request stop + await the sweep scheduler task. Best-effort; a
    dormant worker (state None — the default) is a no-op."""
    if state is None:
        return
    scheduler, task = state
    try:
        scheduler.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("cash_manager_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("cash_manager_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("cash_manager_shutdown_cancelled")
        except Exception:
            log.exception("cash_manager_shutdown_unclean")
    except Exception:
        log.exception("cash_manager_task_join_failed")


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
    scheduler uses — when secrets-file Discord URLs aren't populated, the hook
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


def _build_coinbase_market_data_alert_dispatch_hook(
    settings: APISettings,
) -> object | None:
    """Construct the market-data worker's ``alert_hook`` closure or return None.

    Crypto-pivot C0-B2a (delta spec §3.2). The worker emits
    :class:`CoinbaseMarketDataAlertDescriptor` from two seams: the
    3-minute staleness watchdog (P2 ``broker_disconnect`` — remapped to
    "Coinbase WS/REST outage" per §3.8) and the hourly funding logger's
    consecutive-miss policy (P2 ``data_quality_reject``). With the hook
    wired, each descriptor becomes an ``alerts`` row INSERT + a
    ``dispatch_alert`` Discord #alerts push. Without it (secrets-file
    ``discord.webhook_urls.alerts`` unpopulated), the worker logs
    ``coinbase_market_data_alert_dropped_no_hook`` and drops — same
    skip semantics as the recon/monitor hooks (and the retired bar_sync
    hook this builder is a pattern-mirror of).

    Returns ``object | None`` to dodge circular imports; the concrete
    signature is
    :class:`services.data.coinbase_market_data.MarketDataAlertDispatchHook`.
    """
    from services.data.coinbase_market_data_alerts import CoinbaseMarketDataAlertDescriptor
    from services.webhook_pusher.dispatcher import dispatch_alert
    from services.webhook_pusher.payloads import (
        AlertCategory,
        AlertSeverity,
        ChannelName,
        EmailIdentity,
    )

    if settings.discord_webhook_url_alerts is None:
        log.warning(
            "coinbase_market_data_alert_dispatch_hook_skipped_no_webhook_url",
            note=(
                "discord.webhook_urls.alerts not in the secrets file; the market-data "
                "worker will still emit structured "
                "`coinbase_market_data_alert_dropped_no_hook` WARNING logs "
                "for staleness + funding-miss alerts, but no Discord "
                "#alerts push will fire. Wire the secrets field + restart api."
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

    # Market-data alerts are P2-only by spec (MARKET_DATA_ALERT_SEVERITY);
    # P2 routes to #alerts only. Email is wired if the resend fields are
    # populated so a future severity escalation is already plumbed.
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
        "coinbase_market_data_alert_dispatch_hook_constructed",
        channels=[c.value for c in webhook_urls],
        email_wired=email_identity is not None,
    )

    audit_env = _audit_env_from_settings(settings)

    async def _hook(descriptor: CoinbaseMarketDataAlertDescriptor) -> None:
        """Per-alert: INSERT alerts row + dispatch via webhook_pusher.

        Resolves account_id at fire time (missing-account boot state
        doesn't break hook construction). Market-data alerts have no
        audit-event upstream — ``triggering_audit_event_uuid`` stays
        NULL, matching the monitor + retired-bar_sync surfaces. Hook
        failures propagate to the worker's ``_dispatch_alert``
        catch-and-log so a Discord 5xx never wedges a tick.
        """
        session_factory = api_db.get_session_factory()
        async with session_factory() as repo_session:
            repo = PostgresPhase1QueryRepo(repo_session)
            account_id = await repo.fetch_active_account_id()
        if account_id is None:
            log.warning(
                "coinbase_market_data_alert_dispatch_skipped_no_account",
                note=(
                    "market-data worker fired a P2 alert descriptor but no "
                    "active account is provisioned; alerts row INSERT would "
                    "violate the NOT NULL FK to accounts. Run /setup + "
                    "restart api."
                ),
                severity=descriptor.severity,
                category=descriptor.category,
            )
            return

        # Defense-in-depth: validate against the locked enums at the wire
        # boundary (Literal types constrain the descriptor upstream).
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
            "coinbase_market_data_alert_inserted",
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
            "coinbase_market_data_alert_dispatched",
            alert_id=str(alert_id),
            short_circuited=report.short_circuited,
            delivery_status=dict(report.delivery_status),
        )

    return _hook


async def _start_coinbase_market_data_worker(
    settings: APISettings,
) -> tuple[object, object] | None:
    """Spawn the Coinbase market-data worker; return (worker, task) or None.

    Crypto-pivot C0-B2a (delta spec §3.2). Unlike the retired bar_sync
    worker this needs NO broker credentials and NO active accounts row
    to start — the WS ticker + funding/metadata capture are public-
    endpoint reads and the DB tables it writes have no account FK. The
    alert hook resolves account_id at fire time.
    """
    if not settings.coinbase_market_data_enabled:
        log.warning("coinbase_market_data_worker_disabled_via_setting")
        return None
    from services.data.coinbase_market_data import (
        CoinbaseMarketDataConfig,
        CoinbaseMarketDataWorker,
    )

    config = CoinbaseMarketDataConfig(
        rest_base_url=settings.coinbase_rest_base_url,
        ws_url=settings.coinbase_ws_url,
        tick_interval_s=settings.coinbase_market_data_tick_interval_seconds,
        stale_threshold_s=settings.coinbase_market_data_stale_threshold_seconds,
        stale_realert_cooldown_s=(settings.coinbase_market_data_stale_realert_cooldown_seconds),
        startup_grace_s=settings.coinbase_market_data_startup_grace_seconds,
        http_timeout_s=settings.coinbase_market_data_http_timeout_seconds,
    )
    alert_hook = _build_coinbase_market_data_alert_dispatch_hook(settings)
    worker = CoinbaseMarketDataWorker(
        config=config,
        session_factory=api_db.get_session_factory(),
        alert_hook=alert_hook,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(worker.run_forever(), name="coinbase_market_data.run_forever")
    # Crypto-pivot §3.9: expose the worker's in-memory MarkStore to the
    # route layer (positions mark ladder rung 1) via the module-level
    # holder — see services/api/marks.py for the staleness contract.
    marks.set_mark_store(worker.mark_store)
    log.info(
        "coinbase_market_data_worker_spawned",
        ws_url=settings.coinbase_ws_url,
        rest_base_url=settings.coinbase_rest_base_url,
        alert_dispatch_hook_wired=alert_hook is not None,
    )
    return worker, task


async def _stop_coinbase_market_data_worker(state: tuple[object, object] | None) -> None:
    """Request stop + await the market-data worker task. Best-effort."""
    if state is None:
        return
    # Drop the route-layer mark seam FIRST so no request reads a store
    # whose feeding WS loop is winding down (absent > stale, per marks.py).
    marks.clear_mark_store()
    worker, task = state
    try:
        worker.request_stop()  # type: ignore[attr-defined]
    except Exception:
        log.exception("coinbase_market_data_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=15.0)  # type: ignore[arg-type]
    except TimeoutError:
        log.warning("coinbase_market_data_shutdown_timeout")
        task.cancel()  # type: ignore[attr-defined]
        try:
            await task  # type: ignore[misc]
        except asyncio.CancelledError:
            log.info("coinbase_market_data_shutdown_cancelled")
        except Exception:
            log.exception("coinbase_market_data_shutdown_unclean")
    except Exception:
        log.exception("coinbase_market_data_task_join_failed")


async def _start_async_task_monitor(
    settings: APISettings,
    *,
    reconciliation: tuple[object, object] | None,
    heartbeat_probe: tuple[object, object] | None,
    coinbase_market_data: tuple[object, object] | None = None,
    usdc_rewards_capture: tuple[object, object] | None = None,
    binance_funding_proxy: tuple[object, object] | None = None,
    monitor_alert_hook: object | None = None,
    task_death_alert_hook: object | None = None,
) -> tuple[object, object] | None:
    """Construct + start the AsyncTaskMonitor; return (monitor, task) or None.

    2026-05-17 follow-up to the silent-worker-death pattern: lifespan
    background tasks silently die when they hit an uncaught
    BaseException. Without an observer, dead tasks are invisible. The
    monitor ticks every ``async_task_monitor_interval_seconds`` and
    logs ``async_task_died`` events when a tracked task transitions to
    ``.done()`` unexpectedly.

    Crypto-pivot C0-B2b: the IBKR connectivity probe (1100/1101/1102
    error tracker on the order-placement worker's client) was retired
    with the IBKR execution layer; broker connectivity observability is
    now the market-data staleness watchdog (§3.2) + the future strategy
    worker's own error paths.

    Best-effort: the monitor is a debugging aid, not load-bearing. If
    construction fails the api still serves traffic.
    """
    if not settings.async_task_monitor_enabled:
        log.info("async_task_monitor_disabled_via_setting")
        return None
    from services.api.async_task_monitor import AsyncTaskMonitor, collect_tracked_tasks

    tracked = collect_tracked_tasks(
        reconciliation=reconciliation,
        heartbeat_probe=heartbeat_probe,
        coinbase_market_data=coinbase_market_data,
        usdc_rewards_capture=usdc_rewards_capture,
        binance_funding_proxy=binance_funding_proxy,
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
        task_death_hook_wired=typed_task_death_hook is not None,
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
    recon_state: tuple[object, object] | None = None
    heartbeat_probe_state: tuple[object, object] | None = None
    coinbase_market_data_state: tuple[object, object] | None = None
    usdc_rewards_capture_state: tuple[object, object] | None = None
    binance_funding_proxy_state: tuple[object, object] | None = None
    cash_manager_state: tuple[object, object] | None = None
    async_task_monitor_state: tuple[object, object] | None = None
    try:
        try:
            await _bootstrap_owner_token()
        except Exception:
            # Bootstrap failure shouldn't crash the api — alembic may not
            # have run yet on this VPS. Log loudly and continue; operator
            # runs the bootstrap CLI after migrations finish.
            log.exception("setup_token_bootstrap_failed")
        # Crypto-pivot C0-B2b: the IBKR OrderPlacementWorker was retired
        # with the IBKR execution layer. Order placement moves in-process
        # to the §3.3 strategy worker (C0-B3), which drives the Coinbase
        # execution adapter directly — no approval queue (announce-only).
        try:
            recon_state = await _start_reconciliation_scheduler(settings)
        except Exception:
            # Scheduler startup is best-effort; failure shouldn't take
            # down the api. Most failure modes are config-time (missing
            # secrets fields, no active account) which we already log at
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
            coinbase_market_data_state = await _start_coinbase_market_data_worker(settings)
        except Exception:
            # Market-data worker startup is best-effort; the api serves
            # requests without it (positions mark at avg_cost fallback and
            # funding/metadata capture pauses until the next restart).
            log.exception("coinbase_market_data_worker_startup_failed")
        try:
            usdc_rewards_capture_state = await _start_usdc_rewards_capture(settings)
        except Exception:
            # Capture startup is best-effort; the api serves requests
            # without it (the funding strip's cash fields go stale and
            # rewards capture pauses until the next restart).
            log.exception("usdc_rewards_capture_startup_failed")
        try:
            binance_funding_proxy_state = await _start_binance_funding_proxy(settings)
        except Exception:
            # Proxy-logger startup is best-effort; gate B3 simply keeps
            # reporting insufficient_data until the next restart.
            log.exception("binance_funding_proxy_startup_failed")
        try:
            cash_manager_state = await _start_cash_manager(settings)
        except Exception:
            # Own handler (risk-review finding 6): a capture-startup
            # failure must not suppress (or misattribute) the cash
            # manager's startup, and vice versa. Best-effort either way.
            log.exception("cash_manager_startup_failed")
        try:
            # The monitor MUST start AFTER the other workers so it can
            # capture their final `(worker, task)` tuples (or `None`
            # for ones that failed to spawn). The monitor itself is
            # NOT in its own tracked set.
            #
            # Drill 5 follow-up #2-FU-1: build the monitor's alert
            # dispatch hook BEFORE starting the monitor. Hook returns
            # None if secrets `discord.webhook_urls.alerts` is
            # unpopulated; monitor degrades gracefully (WARNING log
            # fires, no Discord push).
            monitor_alert_hook = _build_monitor_alert_dispatch_hook(settings)
            # Task-death hook (drill 5/6 follow-up landed 2026-05-26).
            # Audit-first emit of ASYNC_TASK_DIED + INSERT of an alerts
            # row (category='worker_failure', severity='P0') when an
            # allow-listed lifespan task transitions to .done(). The
            # IBKR-era recovery agent that consumed these alerts was
            # retired in the crypto-pivot C0 decommission; the alert +
            # Discord push remain the operator signal. Hook returns None only when
            # explicitly disabled via setting; default is always-on.
            task_death_alert_hook = _build_task_death_alert_hook(settings)
            async_task_monitor_state = await _start_async_task_monitor(
                settings,
                reconciliation=recon_state,
                heartbeat_probe=heartbeat_probe_state,
                coinbase_market_data=coinbase_market_data_state,
                usdc_rewards_capture=usdc_rewards_capture_state,
                binance_funding_proxy=binance_funding_proxy_state,
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
        await _stop_usdc_rewards_capture(usdc_rewards_capture_state)
        await _stop_binance_funding_proxy(binance_funding_proxy_state)
        await _stop_cash_manager(cash_manager_state)
        await _stop_coinbase_market_data_worker(coinbase_market_data_state)
        await _stop_heartbeat_probe(heartbeat_probe_state)
        await _stop_reconciliation_scheduler(recon_state)
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
    # Session middleware (real cookie validation outside dev; Phase-0 stub
    # in dev). Added BEFORE register_middleware so it ends up INNERMOST
    # (Starlette: last-added = outermost, empirically verified Day 17 +
    # Day 23) — request flow:
    #
    #     BotAuth → RequestContext → RateLimit → CSRF → Session → routes
    #
    # The 2026-07-12 real-validation swap moved it here from the old
    # outermost-of-four stub position: unlike the stub, this middleware
    # SHORT-CIRCUITS (401/503) and hits the database, so it must run after
    # RequestContext (rejections carry trace_id), after RateLimit (an
    # anonymous cookie-probe flood consumes its per-IP budget instead of
    # free DB lookups), and after CSRF (a CSRF-failing POST is rejected
    # before it can spend a session lookup).
    app.add_middleware(SessionMiddleware, settings=settings)  # type: ignore[arg-type]
    register_middleware(app, settings)
    # Day 23: BotAuthMiddleware sits OUTERMOST so the Discord bot's
    # bearer-authenticated requests bypass CSRF (no cookies) and
    # short-circuit SessionStub's fail-close in production envs. The
    # middleware is a noop on requests without an Authorization: Bearer
    # header, so adding it OUTERMOST has zero overhead on the human path.
    # See services/api/middleware.BotAuthMiddleware docstring for the
    # full request-flow diagram.
    app.add_middleware(BotAuthMiddleware, settings=settings)  # type: ignore[arg-type]
    # Crypto-pivot C0 (2026-07-08): LeanAuthMiddleware + the
    # /api/internal/lean/* ingress are RETIRED — signals are generated
    # in-process by the crypto strategy worker (delta spec §3.3), not
    # POSTed by an external LEAN container. Middleware order is now:
    # BotAuth → SessionStub → RequestContext → RateLimit → CSRF → routes.
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
