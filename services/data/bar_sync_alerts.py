"""services/data/bar_sync_alerts.py — alert descriptors for bar_sync cycles.

Operator-facing visibility for two operational gaps the silent
``bar_sync_cycle_completed`` log can hide:

1. **Partial cycle failure** — one or more markets failed to sync
   (``failed_count > 0``). Today the operator only finds out by manual
   log grep. Common causes: IBKR Error 162/322 transient unavailability,
   reqHistoricalData timeout, contract symbology mismatch, gateway-
   restart cycle landing during the 17:00 ET fire.

2. **OI sentinel substitution** — one or more futures markets had the
   OI sentinel substituted into their universe file because IBKR's
   delayed feed returned ``0`` (entitlement gap). Today's known case is
   /MCL on a paper-tier NYMEX subscription; the substitution unblocks
   LEAN's resolver but is the canonical signal that an IBKR subscription
   upgrade (or universe trim) is overdue.

Both alerts are P2 (→ ``#alerts`` channel only) and fire at
``consecutive_count >= 2`` to avoid flap noise on transient single-cycle
failures. A cycle that recovers (no failures + no sentinels) resets
both counters.

The api lifespan wiring (a separate hot-fix PR; see PR-body note) is
expected to inject a hook that translates :class:`BarSyncAlertDescriptor`
into an ``alerts`` row INSERT + a
:func:`services.webhook_pusher.dispatcher.dispatch_alert` call,
analogous to the recon-side ``_build_alert_dispatch_hook`` in
``services/api/main.py``. Until then the worker logs structured +
drops the descriptor; this module ships the seam.

AlertCategory mapping
---------------------

The 29-value :class:`services.webhook_pusher.payloads.AlertCategory`
enum + alembic 0004's ``alert_category`` Postgres enum are LOCKED;
adding a new value requires an alembic migration (forbidden-whitelist
``alembic/**``). This module reuses two existing canonical values:

* ``data_quality_reject`` — for partial cycle failures (the failed
  markets' data was rejected — not synced; LEAN reads stale on those
  markets the next cycle).
* ``data_quality_quarantine`` — for sentinel substitution (the OI
  value was quarantined behind a sentinel; LEAN sees the sentinel,
  not the real value).

Both categories already route through the existing webhook_pusher
dispatcher + alembic 0004 alerts.category CHECK constraint. No schema
changes required.

A02 / A04 / A06 enforced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import structlog

from services.data.bar_sync import (
    SENTINEL_OI_WHEN_FETCH_FAILED,
    BarSyncCycleResult,
    MarketSyncResult,
)

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Locked constants — alert taxonomy + threshold
# ---------------------------------------------------------------------------


#: Severity literal mirror of
#: ``services.webhook_pusher.payloads.AlertSeverity`` + alembic 0004's
#: ``alerts.severity`` CHECK constraint. Locked at "P2" for both bar_sync
#: alert flavors per the brief's "operational visibility, not P0
#: escalation" framing. P2 routes to ``#alerts`` Discord channel only
#: (no email, no critical channel).
AlertSeverityLiteral = Literal["P0", "P1", "P2"]

#: Locked-taxonomy alert categories reused from the canonical
#: webhook_pusher.payloads.AlertCategory + alembic 0004 alert_category
#: enums. No new values added (would require an alembic/** migration —
#: forbidden whitelist).
AlertCategoryLiteral = Literal["data_quality_reject", "data_quality_quarantine"]

#: Locked severity for both bar_sync alert flavors (P2 → #alerts only).
BAR_SYNC_ALERT_SEVERITY: Final[AlertSeverityLiteral] = "P2"

#: Locked category for partial-cycle-failure alerts.
PARTIAL_CYCLE_ALERT_CATEGORY: Final[AlertCategoryLiteral] = "data_quality_reject"

#: Locked category for OI-sentinel-substitution alerts.
SENTINEL_SUBSTITUTION_ALERT_CATEGORY: Final[AlertCategoryLiteral] = "data_quality_quarantine"

#: Threshold below which the builders return ``None`` (alert suppressed).
#: At ``2`` a single-cycle failure or sentinel substitution is treated as
#: transient (no alert); two consecutive occurrences indicate a
#: persistent issue worth waking the operator.
CONSECUTIVE_ALERT_THRESHOLD: Final[int] = 2


# ---------------------------------------------------------------------------
# Pure-policy descriptor + hook signature
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarSyncAlertDescriptor:
    """One operator-facing alert from a bar_sync cycle.

    Pure-policy: describes what to dispatch. The api lifespan-wired hook
    owns the I/O (alerts table INSERT + webhook_pusher.dispatch_alert).

    Mirror of :class:`services.reconciliation.recon.AlertDescriptor` but
    without the recon-specific ``triggering_break_index`` field — bar_sync
    cycles aren't built around the "break" abstraction; the descriptor is
    self-contained.
    """

    severity: AlertSeverityLiteral
    category: AlertCategoryLiteral
    title: str
    body: str
    payload: dict[str, Any]


BarSyncAlertDispatchHook = Callable[[BarSyncAlertDescriptor], Awaitable[None]]
"""Callback fired once per :class:`BarSyncAlertDescriptor`.

The hook receives the descriptor + is responsible for:

  1. INSERT one row into the ``alerts`` table with severity / category /
     message / detail stamped from descriptor fields.
  2. Invoke :func:`services.webhook_pusher.dispatcher.dispatch_alert` with
     the newly minted alert_id.

The hook is invoked at the END of a bar_sync cycle (after on-disk writes
land). Errors raised inside the hook propagate — the worker is expected
to catch + log so a Discord outage doesn't kill the cycle.

When the worker is constructed without a hook (Phase 1 day-1 default;
hook injection happens in a separate api-lifespan PR), the descriptor is
logged via :data:`log` and dropped.
"""


# ---------------------------------------------------------------------------
# Builders — pure-policy: BarSyncCycleResult → AlertDescriptor | None
# ---------------------------------------------------------------------------


def _failed_markets(cycle_result: BarSyncCycleResult) -> tuple[MarketSyncResult, ...]:
    return cycle_result.failed_markets


def _sentinel_substituted_markets(
    cycle_result: BarSyncCycleResult,
) -> tuple[MarketSyncResult, ...]:
    return tuple(r for r in cycle_result.successful_markets if r.open_interest_was_sentinel)


def build_partial_cycle_alert(
    cycle_result: BarSyncCycleResult,
    *,
    consecutive_failure_count: int,
) -> BarSyncAlertDescriptor | None:
    """Return a P2 alert descriptor if the cycle is partial AND threshold met.

    Returns ``None`` when:
      * ``failed_count == 0`` (all markets synced cleanly)
      * ``consecutive_failure_count < CONSECUTIVE_ALERT_THRESHOLD`` (single-
        cycle failure treated as transient)

    Returns a populated :class:`BarSyncAlertDescriptor` when at least one
    market failed AND ``consecutive_failure_count >= 2``. The descriptor
    enumerates the failed markets + their error strings for operator
    diagnostic context.

    NOTE: a cycle where all markets succeeded but some had OI sentinel-
    substituted (e.g., /MCL post-Task-2) does NOT trip this alert —
    the sentinel path returns ``success=True``. Use
    :func:`build_sentinel_substitution_alert` to detect that pattern.
    """
    failed = _failed_markets(cycle_result)
    if not failed:
        return None
    if consecutive_failure_count < CONSECUTIVE_ALERT_THRESHOLD:
        return None
    failed_markets = [r.market for r in failed]
    title = (
        f"bar_sync partial cycle failure: "
        f"{len(failed)}/{cycle_result.total_markets} markets failed "
        f"(consecutive cycles: {consecutive_failure_count})"
    )
    body_lines = [
        "The api's bar_sync worker has failed to fetch bars for at least one",
        "market across consecutive cycles. LEAN reads stale data on the failed",
        "markets the next signal cycle. Common causes: IBKR Error 162/322,",
        "reqHistoricalData timeout, contract symbology mismatch, or a gateway",
        "restart landing during the 17:00 ET fire.",
        "",
        "Failed markets:",
    ]
    for r in failed:
        body_lines.append(f"  - {r.market}: {r.error}")
    body = "\n".join(body_lines)
    payload: dict[str, Any] = {
        "alert_kind": "partial_cycle_failure",
        "consecutive_failure_count": consecutive_failure_count,
        "total_markets": cycle_result.total_markets,
        "successful_count": len(cycle_result.successful_markets),
        "failed_count": len(failed),
        "failed_markets": failed_markets,
        "failed_market_errors": {r.market: (r.error or "") for r in failed},
        "cycle_started_at_utc": _format_utc(cycle_result.cycle_started_at_utc),
        "cycle_completed_at_utc": _format_utc(cycle_result.cycle_completed_at_utc),
    }
    return BarSyncAlertDescriptor(
        severity=BAR_SYNC_ALERT_SEVERITY,
        category=PARTIAL_CYCLE_ALERT_CATEGORY,
        title=title,
        body=body,
        payload=payload,
    )


def build_sentinel_substitution_alert(
    cycle_result: BarSyncCycleResult,
    *,
    consecutive_sentinel_count: int,
) -> BarSyncAlertDescriptor | None:
    """Return a P2 alert descriptor if OI was sentinel-substituted AND threshold met.

    Surfaces the persistent data-entitlement gap pattern (/MCL on a
    paper-tier NYMEX subscription, as of 2026-05-21): the OI fetch
    returns ``0`` not because the cycle failed, but because IBKR's
    delayed feed publishes ``futuresOpenInterest=0.0`` as the
    "not entitled" sentinel. Bar_sync's Task-2 substitution path unblocks
    LEAN; this alert tells the operator the substitution is happening so
    the IBKR subscription upgrade decision stays visible.

    Returns ``None`` when:
      * No market had ``open_interest_was_sentinel=True`` this cycle
      * ``consecutive_sentinel_count < CONSECUTIVE_ALERT_THRESHOLD``
        (single-cycle substitution treated as transient)

    Returns a populated descriptor otherwise. Distinct from
    :func:`build_partial_cycle_alert`: a clean cycle with /MCL's OI
    sentinel-substituted is partial-failure-clean (success=True for all
    markets) but sentinel-alert-eligible.
    """
    substituted = _sentinel_substituted_markets(cycle_result)
    if not substituted:
        return None
    if consecutive_sentinel_count < CONSECUTIVE_ALERT_THRESHOLD:
        return None
    sentinel_markets = [r.market for r in substituted]
    title = (
        f"bar_sync OI sentinel substituted: "
        f"{len(substituted)}/{cycle_result.total_markets} markets had OI "
        f"replaced with sentinel ({SENTINEL_OI_WHEN_FETCH_FAILED}) "
        f"(consecutive cycles: {consecutive_sentinel_count})"
    )
    body_lines = [
        "The api's bar_sync worker substituted a positive sentinel for the OI",
        "value of at least one futures market across consecutive cycles. This",
        "indicates IBKR's delayed feed published `futuresOpenInterest=0.0` —",
        "typically a data-entitlement gap (e.g., paper-tier NYMEX subscription",
        "for /MCL). LEAN's DataMappingMode.OPEN_INTEREST resolver still picks",
        "the contract correctly because the sentinel is positive, but the OI",
        "value on disk is synthetic.",
        "",
        "Operator decision space:",
        "  (a) Upgrade IBKR subscription to a tier with real-time OI for the",
        "      affected exchange.",
        "  (b) Drop the affected market(s) from the Phase 1 universe.",
        "  (c) Continue with the sentinel — strategy logic is unaffected.",
        "",
        "Markets with sentinel-substituted OI:",
    ]
    for r in substituted:
        front_month = r.front_month_expiry or "?"
        body_lines.append(f"  - {r.market} (front-month {front_month})")
    body = "\n".join(body_lines)
    payload: dict[str, Any] = {
        "alert_kind": "sentinel_substitution",
        "consecutive_sentinel_count": consecutive_sentinel_count,
        "sentinel_value": SENTINEL_OI_WHEN_FETCH_FAILED,
        "total_markets": cycle_result.total_markets,
        "sentinel_market_count": len(substituted),
        "sentinel_markets": sentinel_markets,
        "sentinel_market_expiries": {r.market: (r.front_month_expiry or "") for r in substituted},
        "cycle_started_at_utc": _format_utc(cycle_result.cycle_started_at_utc),
        "cycle_completed_at_utc": _format_utc(cycle_result.cycle_completed_at_utc),
    }
    return BarSyncAlertDescriptor(
        severity=BAR_SYNC_ALERT_SEVERITY,
        category=SENTINEL_SUBSTITUTION_ALERT_CATEGORY,
        title=title,
        body=body,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Cycle classification + counter helpers
# ---------------------------------------------------------------------------


def cycle_had_failures(cycle_result: BarSyncCycleResult) -> bool:
    """True if any market failed to sync this cycle."""
    return len(cycle_result.failed_markets) > 0


def cycle_had_sentinel_substitution(cycle_result: BarSyncCycleResult) -> bool:
    """True if any successful market had OI sentinel-substituted this cycle."""
    return any(r.open_interest_was_sentinel for r in cycle_result.successful_markets)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _format_utc(ts: datetime) -> str:
    """RFC 3339 UTC string with `Z` suffix for JSONB payloads (A06)."""
    if ts.tzinfo is None:
        raise ValueError("_format_utc: ts must be tz-aware (A06)")
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "BAR_SYNC_ALERT_SEVERITY",
    "CONSECUTIVE_ALERT_THRESHOLD",
    "PARTIAL_CYCLE_ALERT_CATEGORY",
    "SENTINEL_SUBSTITUTION_ALERT_CATEGORY",
    "AlertCategoryLiteral",
    "AlertSeverityLiteral",
    "BarSyncAlertDescriptor",
    "BarSyncAlertDispatchHook",
    "build_partial_cycle_alert",
    "build_sentinel_substitution_alert",
    "cycle_had_failures",
    "cycle_had_sentinel_substitution",
]
