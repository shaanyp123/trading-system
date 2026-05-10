"""Alert dispatcher — orchestrator that fans an alerts row out to channels.

Sequence (per alert):

    1. SELECT the alerts row by id.
    2. If ``delivery_status`` is non-NULL → return short-circuit
       (idempotent: caller may invoke twice for the same alert id and
       the second call is a no-op against the channels). The dispatcher
       does NOT clear / re-attempt failed channels — caller's retry path
       owns that decision.
    3. Plan the dispatch via
       :func:`services.webhook_pusher.payloads.plan_alert_dispatch`.
    4. POST each :class:`OutboundMessage` via
       :func:`services.webhook_pusher.sender.post_outbound_message`.
    5. UPDATE ``alerts.delivery_status`` JSONB with the per-channel
       :class:`DeliveryStatus` values (e.g. ``{"discord_alerts": "ok",
       "discord_critical": "ok", "email": "ok"}``).

The dispatcher is the I/O boundary; the planner stays pure. This split
mirrors the ``services/reconciliation/recon.py`` (planner) ↔ Week-4
dispatcher (caller) shape. Same reasons: A22 (tests don't write audit
events / hit live HTTP), and the plan struct IS the spec contract.

A22 reminder: tests must NOT actually emit audit events AND must NOT hit
live Discord webhooks during pytest. Unit tests of the planner have zero
mocking; tests of the sender use ``respx`` to mock httpx; tests of the
dispatcher mock both the DB and the http_client. See
``tests/unit/test_webhook_pusher.py``.

Audit-event emission: NONE from this module. Alerts FIRE in response to
audit events from elsewhere (kill-switch transition, reconciliation
break, audit chain break) — webhook_pusher is the delivery layer, not
an audit producer. If operator wants delivery-failure tracking in
``audit_log`` later, add ``alert_delivery_failed`` to the locked
taxonomy in a forbidden-whitelist PR (services/audit/event_types.py +
backend-spec §3.30). Today the failure is visible via ``delivery_status``
JSONB + the structured log line emitted by the sender.

Caller contract (NOT in this module):

- The Week 4 risk dispatcher writes the alerts row when transitioning
  ``risk_state`` (or when reconciliation reports a break, or when audit
  chain verification fails), then calls
  ``await dispatch_alert(session, alert_id, ...)`` on a background task.
- Background task lifetime: caller's responsibility. Typical pattern is
  ``asyncio.create_task(...)`` after the request handler has returned.
- Retry: caller schedules a fresh ``dispatch_alert`` if the first try
  ended with any non-OK channel; idempotency guarantees the OK channels
  aren't double-POSTed. (TODO: add an explicit ``retry_failed`` parameter
  in a future PR if real ops shows we need to selectively retry just the
  failed channels — the canonical path today is "retry the whole alert.")
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.webhook_pusher.payloads import (
    AlertCategory,
    AlertDispatchPlan,
    AlertRow,
    AlertSeverity,
    ChannelName,
    EmailIdentity,
    WebhookPusherError,
    plan_alert_dispatch,
)
from services.webhook_pusher.sender import (
    DeliveryResult,
    DeliveryStatus,
    post_outbound_message,
)

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """Aggregated result of one ``dispatch_alert`` invocation.

    ``delivery_status`` mirrors the JSONB blob written to the row.
    ``short_circuited`` is True when the alert had a pre-existing
    ``delivery_status`` and we returned without POSTing — the row's
    existing JSONB is returned unchanged.
    ``results`` is the per-channel POST outcome (empty when
    ``short_circuited=True``).
    """

    alert_id: UUID
    short_circuited: bool
    delivery_status: Mapping[str, str]
    results: tuple[DeliveryResult, ...]


class AlertNotFoundError(WebhookPusherError):
    """Raised when ``dispatch_alert`` is called with an unknown alert id.

    Subclass of :class:`WebhookPusherError` so a single FastAPI exception
    handler covers planner + dispatcher errors.
    """


# ---------------------------------------------------------------------------
# Public dispatcher API
# ---------------------------------------------------------------------------


async def dispatch_alert(
    *,
    session: AsyncSession,
    alert_id: UUID,
    http_client: httpx.AsyncClient,
    webhook_urls: Mapping[ChannelName, str],
    email_identity: EmailIdentity | None = None,
) -> DeliveryReport:
    """Read alert row, plan dispatch, fan out, UPDATE delivery_status.

    Idempotent: a second call for the same ``alert_id`` returns
    ``short_circuited=True`` with the existing ``delivery_status``
    unchanged. There is NO partial retry — caller schedules a fresh
    ``dispatch_alert`` with a NEW alert id (or NULLs the row's
    ``delivery_status`` first if it wants to re-attempt; that's a
    deliberate manual escape hatch, not a default path).

    Parameters
    ----------
    session:
        SQLAlchemy async session bound to the ``app_service`` Postgres
        role. The dispatcher reads + writes the ``alerts`` table.
    alert_id:
        UUID of the row in ``alerts``. Caller (the risk dispatcher)
        constructed the row before calling this.
    http_client:
        Caller-managed ``httpx.AsyncClient`` so connections pool across
        the (up to 3) channels of a P0 fan-out. The dispatcher does NOT
        own client lifecycle — caller wraps with ``async with`` per
        request.
    webhook_urls:
        Discord channel webhook URLs (sourced from sops). Must cover
        every channel routed by the alert's severity (see
        ``SEVERITY_TO_CHANNELS``); missing entries raise
        :class:`WebhookPusherError` at plan time.
    email_identity:
        Required for severities that route to email (P0). May be None
        for P1/P2 alerts.

    Raises
    ------
    AlertNotFoundError
        If no row exists in ``alerts`` with the given id.
    WebhookPusherError
        From the planner (missing webhook URL, missing email_identity,
        tz-naive ``fired_at_utc`` on the row).
    """
    bound = log.bind(
        service_name="webhook_pusher",
        alert_id=str(alert_id),
    )

    row = await _select_alert_row(session, alert_id)
    if row is None:
        raise AlertNotFoundError(f"alert id {alert_id} not found")

    if row.existing_delivery_status is not None:
        bound.info(
            "alert_dispatch_short_circuited",
            existing_status=row.existing_delivery_status,
        )
        return DeliveryReport(
            alert_id=alert_id,
            short_circuited=True,
            delivery_status=row.existing_delivery_status,
            results=(),
        )

    plan = plan_alert_dispatch(
        alert=row.alert,
        webhook_urls=webhook_urls,
        email_identity=email_identity,
    )
    bound = bound.bind(
        severity=plan.severity.value,
        channels=[c.value for c in plan.channels],
    )
    bound.info("alert_dispatch_started")

    results = await _fan_out(plan, http_client)
    delivery_status = _aggregate_delivery_status(plan, results)

    await _update_delivery_status(session, alert_id, delivery_status)

    bound.info(
        "alert_dispatch_completed",
        delivery_status=delivery_status,
        any_failed=any(r.status is not DeliveryStatus.OK for r in results),
    )

    return DeliveryReport(
        alert_id=alert_id,
        short_circuited=False,
        delivery_status=delivery_status,
        results=results,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LoadedAlert:
    """Internal container for the SELECT result.

    ``existing_delivery_status`` is None when the JSONB column is NULL
    (the never-dispatched state); a non-empty mapping when a prior
    ``dispatch_alert`` has already populated it (idempotency guard).
    """

    alert: AlertRow
    existing_delivery_status: Mapping[str, str] | None


async def _select_alert_row(
    session: AsyncSession,
    alert_id: UUID,
) -> _LoadedAlert | None:
    """SELECT the alerts row + decode JSONB columns.

    Returns ``None`` when the row doesn't exist; raises
    :class:`WebhookPusherError` if a row's ``severity`` / ``category``
    doesn't parse against the locked enums (defensive — alembic 0004's
    enum + CHECK constraints prevent this in practice, but we surface
    early rather than letting the planner trip on it).
    """
    stmt = text(
        """
        SELECT id, severity, category, message, detail, fired_at_utc, delivery_status
        FROM alerts
        WHERE id = :alert_id
        """
    )
    result = await session.execute(stmt, {"alert_id": alert_id})
    raw = result.mappings().one_or_none()
    if raw is None:
        return None

    severity = _parse_severity(raw["severity"])
    category = _parse_category(raw["category"])
    fired_at = raw["fired_at_utc"]
    if not isinstance(fired_at, datetime):  # pragma: no cover — schema-typed
        raise WebhookPusherError(f"alerts.fired_at_utc is not a datetime: {fired_at!r}")

    detail_raw = raw["detail"]
    if detail_raw is None:
        detail: Mapping[str, Any] | None = None
    elif isinstance(detail_raw, dict):
        detail = detail_raw
    else:  # pragma: no cover — JSONB returns dict or None on asyncpg
        raise WebhookPusherError(f"alerts.detail not a dict: {type(detail_raw).__name__}")

    delivery_raw = raw["delivery_status"]
    existing: Mapping[str, str] | None
    if delivery_raw is None or delivery_raw == {}:
        existing = None
    elif isinstance(delivery_raw, dict):
        existing = {str(k): str(v) for k, v in delivery_raw.items()}
    else:  # pragma: no cover
        raise WebhookPusherError(
            f"alerts.delivery_status not a dict: {type(delivery_raw).__name__}"
        )

    return _LoadedAlert(
        alert=AlertRow(
            id=raw["id"],
            severity=severity,
            category=category,
            message=raw["message"],
            detail=detail,
            fired_at_utc=fired_at,
        ),
        existing_delivery_status=existing,
    )


def _parse_severity(value: str) -> AlertSeverity:
    """Parse a TEXT column to :class:`AlertSeverity` with a clear error."""
    try:
        return AlertSeverity(value)
    except ValueError as exc:
        raise WebhookPusherError(f"alerts.severity {value!r} not in locked enum") from exc


def _parse_category(value: str) -> AlertCategory:
    """Parse a alert_category enum value with a clear error.

    Postgres enum widening (alembic 0004 + spec §3.27) keeps this in
    sync; if the row's category is outside the locked Python enum the
    schema is ahead of the code and webhook_pusher needs an update.
    """
    try:
        return AlertCategory(value)
    except ValueError as exc:
        raise WebhookPusherError(
            f"alerts.category {value!r} not in services.webhook_pusher AlertCategory enum "
            "(schema may be ahead of code; see alembic 0004 + spec §3.27)"
        ) from exc


async def _fan_out(
    plan: AlertDispatchPlan,
    http_client: httpx.AsyncClient,
) -> tuple[DeliveryResult, ...]:
    """POST every outbound message; return per-channel results.

    Sequential, not concurrent — the spec doesn't require parallelism
    (a P0 fan-out is at most 3 calls, ~150ms each = <500ms total) and
    sequencing makes structured logs easier to follow during operator
    triage. We can switch to ``asyncio.gather`` if measured P95 fan-out
    latency exceeds the spec's 1s budget under real load.
    """
    results: list[DeliveryResult] = []
    for msg in plan.outbound_messages:
        result = await post_outbound_message(http_client, msg)
        results.append(result)
    return tuple(results)


def _aggregate_delivery_status(
    plan: AlertDispatchPlan,
    results: tuple[DeliveryResult, ...],
) -> Mapping[str, str]:
    """Build the JSONB blob for ``alerts.delivery_status`` UPDATE.

    Pre-populates every routed channel's slot so the JSONB shape is
    deterministic regardless of fan-out outcomes — a missing key would
    otherwise be ambiguous (was that channel skipped or did the result
    get dropped?).
    """
    by_channel = {r.channel: r for r in results}
    return {channel.value: by_channel[channel].status.value for channel in plan.channels}


async def _update_delivery_status(
    session: AsyncSession,
    alert_id: UUID,
    delivery_status: Mapping[str, str],
) -> None:
    """UPDATE the alerts row with the JSONB delivery_status.

    Issues the UPDATE and commits. If the caller wraps in their own
    transaction, ``session.commit()`` here is a no-op against the
    outer transaction (SQLAlchemy's ``begin_nested``-style commit).
    """
    stmt = text(
        """
        UPDATE alerts
        SET delivery_status = CAST(:status AS JSONB)
        WHERE id = :alert_id
        """
    )
    await session.execute(
        stmt,
        {
            "alert_id": alert_id,
            "status": json.dumps(dict(delivery_status), sort_keys=True),
        },
    )
    await session.commit()


__all__ = [
    "AlertNotFoundError",
    "DeliveryReport",
    "dispatch_alert",
]
