"""services/api/heartbeat_probe.py — periodic staleness check for cron-like services.

PR #154 follow-up (2026-05-16). Builds on the in-memory
:class:`services.api.heartbeats.HeartbeatRegistry` shipped in PR #154.
Pre-this-module, the registry tracked heartbeats + exposed them on
``GET /api/system/heartbeats``, but nothing actively raised an alarm
when a service went stale. The operator would have to poll the
endpoint manually.

This module adds the background probe that:

  1. Ticks every :data:`HEARTBEAT_PROBE_INTERVAL_S` (15 min).
  2. Snapshots the registry; classifies each tracked service via
     :func:`services.api.heartbeats.classify_freshness`.
  3. For each ``stale`` service AND the cooldown window has passed,
     appends a ``HEARTBEAT_STALE_DETECTED`` audit row + dispatches
     a P2 alert through the same hook the reconciliation scheduler
     uses (``services.reconciliation.apply.AlertDispatchContext``).
  4. Stamps :meth:`HeartbeatRegistry.mark_alerted` so subsequent
     ticks within the cooldown window suppress further re-alerts
     for the same service.

A fresh heartbeat (``HeartbeatRegistry.record``) clears the alert
cooldown immediately, so a service recovery → re-failure pattern
alerts on the second failure right away.

**A02 BINDS** — file is on ``services/api/**`` hot-fix whitelist for
the probe coroutine, but it appends to the audit chain via
:func:`services.audit.writer.append_audit_event` + uses the new
``HEARTBEAT_STALE_DETECTED`` enum value (locked taxonomy add). The
enum extension carries the `risk-review-approved` label.

**A05 enforced** — no money values in the audit payload (just service
name + thresholds).
**A06 enforced** — every datetime tz-aware UTC.
**A22 enforced by design** — one audit row per stale-detection cycle,
NOT per probe tick. Cooldown defaults to 24h; the operator gets one
alert per stuck-service per day, not one per 15 min.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.api.heartbeats import (
    HEARTBEAT_PROBE_INTERVAL_S,
    HEARTBEAT_STALE_THRESHOLDS_S,
    HeartbeatRegistry,
    HeartbeatService,
    classify_freshness,
    get_heartbeat_registry,
)
from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

if TYPE_CHECKING:
    from services.reconciliation.apply import AlertDispatchContext

log = structlog.get_logger()


#: Webhook category for a staleness alert. Mirrors the locked
#: AlertCategory enum in :mod:`services.webhook_pusher.payloads`.
HEARTBEAT_ALERT_CATEGORY: str = "heartbeat_stale"


#: Locked severity for staleness alerts. P2 → ``#alerts`` channel only.
#: A LEAN cycle that stops firing is a serious operational issue (no new
#: signals can be generated), but not a P0 emergency requiring an
#: incident-review halt. Operator should investigate within hours.
HEARTBEAT_ALERT_SEVERITY: str = "P2"


@dataclass(frozen=True, slots=True)
class HeartbeatAlertDescriptor:
    """One staleness-alert descriptor; shape-compatible with the recon
    planner's ``AlertDescriptor`` so the same dispatch hook can fire it.

    The ``payload`` is JCS-canonicalized into the audit row's payload
    + the alerts row's detail JSONB.
    """

    title: str
    body: str
    severity: str
    category: str
    payload: dict[str, Any]


# AlertDispatchHook is duck-typed across recon + heartbeat probe; we
# accept any callable matching the recon shape so the api lifespan can
# wire the same closure into both surfaces.
HeartbeatAlertDispatchHook = Callable[["AlertDispatchContext"], Awaitable[None]]


def _build_descriptor(
    service: HeartbeatService,
    *,
    last_heartbeat_utc: datetime,
    seconds_since: int,
    threshold_s: int,
) -> HeartbeatAlertDescriptor:
    """Compose the operator-readable alert descriptor for one stale service."""
    hours_since = round(seconds_since / 3600, 1)
    threshold_h = threshold_s // 3600
    title = f"Heartbeat stale: {service}"
    body = (
        f"Service {service!r} hasn't recorded a heartbeat in "
        f"{hours_since}h (threshold: {threshold_h}h).\n"
        f"Last heartbeat: {last_heartbeat_utc.isoformat()}.\n"
        "Investigate the producer (LEAN container, recon scheduler, etc.) — "
        "no new signals / cycles will fire until the heartbeat resumes."
    )
    payload: dict[str, Any] = {
        "service": service,
        "last_heartbeat_utc": last_heartbeat_utc.isoformat(),
        "seconds_since_last_heartbeat": seconds_since,
        "stale_threshold_s": threshold_s,
        "alert_category": HEARTBEAT_ALERT_CATEGORY,
        "alert_severity": HEARTBEAT_ALERT_SEVERITY,
    }
    return HeartbeatAlertDescriptor(
        title=title,
        body=body,
        severity=HEARTBEAT_ALERT_SEVERITY,
        category=HEARTBEAT_ALERT_CATEGORY,
        payload=payload,
    )


async def _emit_stale_event(
    service: HeartbeatService,
    *,
    session_factory: async_sessionmaker[Any],
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
    now_utc: datetime,
    last_heartbeat_utc: datetime,
    seconds_since: int,
    threshold_s: int,
    alert_dispatch_hook: HeartbeatAlertDispatchHook | None,
) -> UUID:
    """Append the audit row + dispatch the alert for one stale service.

    Returns the audit ``event_uuid`` so the caller can log it. The
    alert dispatch is best-effort: a hook failure logs at WARNING but
    does NOT roll back the audit row (the audit is the durable
    breadcrumb; the Discord push is the notification).

    Audit-first per backend-spec §2.10.1.
    """
    descriptor = _build_descriptor(
        service,
        last_heartbeat_utc=last_heartbeat_utc,
        seconds_since=seconds_since,
        threshold_s=threshold_s,
    )

    async with session_factory() as audit_session:
        record = await append_audit_event(
            audit_session,
            AuditEventType.HEARTBEAT_STALE_DETECTED,
            descriptor.payload,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=now_utc,
        )

    audit_uuid = record.event_uuid

    if alert_dispatch_hook is not None:
        try:
            # Lazy import to avoid the recon → audit → models import cycle
            # at module load.
            from services.reconciliation.apply import AlertDispatchContext
            from services.reconciliation.recon import AlertDescriptor

            # Build the recon-typed AlertDescriptor directly: the api
            # lifespan hook reads .title / .body / .severity / .category /
            # .payload — the same surface our HeartbeatAlertDescriptor
            # exposes. ``triggering_break_index`` is meta-data only the
            # recon planner uses internally; set to ``-1`` as a sentinel
            # marking "not a reconciliation break — heartbeat probe".
            recon_descriptor = AlertDescriptor(
                triggering_break_index=-1,
                severity=descriptor.severity,  # type: ignore[arg-type]
                category=descriptor.category,  # type: ignore[arg-type]
                title=descriptor.title,
                body=descriptor.body,
                payload=descriptor.payload,
            )
            ctx = AlertDispatchContext(
                descriptor=recon_descriptor,
                triggering_audit_event_uuid=audit_uuid,
                account_id=account_id,
                env=env,
            )
            await alert_dispatch_hook(ctx)
        except Exception:
            log.warning(
                "heartbeat_stale_alert_dispatch_failed",
                service=service,
                audit_event_uuid=str(audit_uuid),
                exc_info=True,
            )

    log.info(
        "heartbeat_stale_event_emitted",
        service=service,
        audit_event_uuid=str(audit_uuid),
        last_heartbeat_utc=last_heartbeat_utc.isoformat(),
        seconds_since=seconds_since,
    )

    return audit_uuid


class HeartbeatProbe:
    """Long-lived async coroutine that ticks every ``interval_s`` and emits
    stale-detected events for each cron-like service past its threshold.

    Designed to be spawned once at api lifespan startup + torn down at
    shutdown via :meth:`request_stop`. The :meth:`run_once` method is
    exposed for tests + an operator's manual on-demand check.

    The probe does NOT poll the producer side directly (no SSH out to
    the LEAN container, no HTTP healthcheck). It only reads the
    in-memory registry — the producer's heartbeat POSTs update the
    registry, the probe reads it. Decoupled by design: a network
    partition between this probe and the producer is invisible (would
    show as registry not updating, which is the same signal as "producer
    is hung"); the probe still fires the alert correctly.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        account_id: UUID,
        env: Environment,
        registry: HeartbeatRegistry | None = None,
        alert_dispatch_hook: HeartbeatAlertDispatchHook | None = None,
        interval_s: int = HEARTBEAT_PROBE_INTERVAL_S,
        phase_at_emit: PhaseAtEmit = 1,
        log_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._account_id = account_id
        self._env = env
        self._registry = registry if registry is not None else get_heartbeat_registry()
        self._alert_dispatch_hook = alert_dispatch_hook
        self._interval_s = interval_s
        self._phase_at_emit = phase_at_emit
        self._log_id = log_id or f"heartbeat_probe_{uuid4().hex[:8]}"
        self._stop_event = asyncio.Event()
        self._log = log.bind(probe_id=self._log_id, env=env)

    def request_stop(self) -> None:
        """Set the stop event; the run loop exits at its next sleep boundary."""
        self._stop_event.set()

    async def run_once(self, *, now_utc: datetime | None = None) -> int:
        """One probe pass: snapshot + classify + emit stale events.

        Returns the count of audit rows written this pass (zero if no
        stale services or all stale services in cooldown).
        """
        if now_utc is None:
            now_utc = datetime.now(tz=UTC)
        if now_utc.tzinfo is None:
            raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")

        emitted = 0
        snapshot = self._registry.snapshot()
        for service in HEARTBEAT_STALE_THRESHOLDS_S.keys():
            record = snapshot.get(service)
            freshness = classify_freshness(service, record, now_utc=now_utc)
            if freshness != "stale":
                continue
            assert record is not None  # narrow: stale implies record exists
            if not self._registry.should_alert(service, now_utc=now_utc):
                last_alerted = self._registry.last_alerted_at(service)
                self._log.info(
                    "heartbeat_stale_alert_in_cooldown",
                    service=service,
                    last_alerted_at=(
                        last_alerted.isoformat() if last_alerted is not None else None
                    ),
                )
                continue

            seconds_since = max(int((now_utc - record.last_heartbeat_utc).total_seconds()), 0)
            threshold_s = HEARTBEAT_STALE_THRESHOLDS_S[service]
            try:
                await _emit_stale_event(
                    service,
                    session_factory=self._session_factory,
                    account_id=self._account_id,
                    env=self._env,
                    phase_at_emit=self._phase_at_emit,
                    now_utc=now_utc,
                    last_heartbeat_utc=record.last_heartbeat_utc,
                    seconds_since=seconds_since,
                    threshold_s=threshold_s,
                    alert_dispatch_hook=self._alert_dispatch_hook,
                )
                self._registry.mark_alerted(service, now_utc=now_utc)
                emitted += 1
            except Exception:
                # Audit failure: caller's outer loop catches + logs; we
                # do NOT mark_alerted so the next tick retries. The
                # operator's audit chain is the source of truth — a
                # repeated audit-append failure means something else is
                # very wrong + the operator will see it from other paths.
                self._log.exception(
                    "heartbeat_stale_emit_failed",
                    service=service,
                )

        return emitted

    async def run_forever(self) -> None:
        """Tick every ``interval_s`` until ``request_stop()`` fires.

        Catches all exceptions in the per-tick body so a transient DB
        failure doesn't kill the probe. The catch is per-tick — the
        next tick retries fresh.
        """
        self._log.info("heartbeat_probe_started", interval_s=self._interval_s)
        try:
            while not self._stop_event.is_set():
                try:
                    emitted = await self.run_once()
                    if emitted > 0:
                        self._log.info(
                            "heartbeat_probe_tick_completed",
                            stale_emitted=emitted,
                        )
                except Exception:
                    self._log.exception("heartbeat_probe_tick_failed")
                # Wait for stop_event OR the interval, whichever fires first.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_s)
                except TimeoutError:
                    pass  # normal cadence
        finally:
            self._log.info("heartbeat_probe_stopped")


__all__ = [
    "HEARTBEAT_ALERT_CATEGORY",
    "HEARTBEAT_ALERT_SEVERITY",
    "HeartbeatAlertDescriptor",
    "HeartbeatAlertDispatchHook",
    "HeartbeatProbe",
]
