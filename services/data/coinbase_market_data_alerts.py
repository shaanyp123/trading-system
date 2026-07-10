"""services/data/coinbase_market_data_alerts.py — pure alert policy for the market-data worker.

Crypto-pivot C0-B2a (delta spec §3.2). Sibling of
:mod:`services.data.coinbase_market_data` following the (retired)
``bar_sync_alerts`` precedent: this module holds ONLY pure descriptor
types + builder functions — no I/O, no imports from the webhook pusher
— so the alert policy is unit-testable without an event loop and the
worker module never imports dispatcher machinery.

Two alert flavors:

1. **Marks stale** (``build_marks_stale_alert``) — the 3-minute
   staleness watchdog (strategy §7 data-outage row) found at least one
   CRITICAL-tier product (spot signal pairs + traded-asset perps; see
   the worker's ``run_staleness_check_once``) whose last WebSocket
   tick is older than the threshold. Telemetry-tier alt perps never
   feed this builder — they are log-only (decisions-log 2026-07-09,
   "C1 night one" alert-fatigue fix); the descriptor shape/category is
   unchanged, only the product set feeding it narrowed. Severity P2,
   category ``broker_disconnect`` — the delta spec §3.8 remaps that
   existing category to "Coinbase WS/REST outage" (free-text payload;
   no enum migration). The *policy response* (protected-hold vs
   flatten) is the risk loop's job (§3.3); this alert is the
   operator-visible detection signal.

2. **Funding snapshot miss** (``build_funding_snapshot_miss_alert``) —
   the hourly funding logger (§3.2 "funding logger from day one",
   REPORT F-3 rider) completed a pass without persisting a single
   funding row for ``consecutive_misses`` consecutive hours. Severity
   P2, category ``data_quality_reject`` (§3.8: funding telemetry
   alerts ride the existing ``data_quality_*`` categories). Fires at
   ``CONSECUTIVE_ALERT_THRESHOLD`` consecutive misses (2, matching the
   bar_sync precedent) — a single missed hour is retried silently;
   two in a row means gate B3's input is degrading and the operator
   should look. Whether CDE funding is programmatically retrievable at
   all is strategy §11 open question 4 — if it turns out NOT to be,
   this alert is the mechanism that surfaces it on day one of C1.

Alert-on-transition + cooldown discipline lives in the worker (it owns
the state); these builders are stateless.

[A05] — payloads carry Decimals as strings. [A06] — tz-aware UTC
timestamps only, serialized RFC-3339.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

#: Literal mirrors of the locked webhook_pusher enums (same pattern as
#: the retired bar_sync_alerts): keeps this module import-free of
#: services.webhook_pusher while the api-lifespan hook validates
#: against the real enums at the wire boundary.
AlertSeverityLiteral = Literal["P0", "P1", "P2"]
AlertCategoryLiteral = Literal["broker_disconnect", "data_quality_reject"]

#: Locked severity for both market-data alert flavors. P2 → #alerts
#: channel only. Rationale: the worker's alerts are *detection*
#: signals; the load-bearing protective response to stale data
#: (protected-hold / flatten per strategy §7) is enforced by the risk
#: loop, which has its own escalation path.
MARKET_DATA_ALERT_SEVERITY: Final[AlertSeverityLiteral] = "P2"

#: Consecutive-failure threshold for the funding-miss alert. Matches
#: the bar_sync precedent (alert at >= 2 consecutive dirty cycles).
CONSECUTIVE_ALERT_THRESHOLD: Final[int] = 2


@dataclass(frozen=True, slots=True)
class CoinbaseMarketDataAlertDescriptor:
    """One market-data alert; shape-compatible with the api lifespan's
    INSERT-alerts-row + ``dispatch_alert`` hook closure (same surface
    as the retired ``BarSyncAlertDescriptor``).
    """

    severity: AlertSeverityLiteral
    category: AlertCategoryLiteral
    title: str
    body: str
    payload: dict[str, Any]


def _format_utc(ts: datetime) -> str:
    """RFC-3339 ``Z`` string for JSONB payloads; rejects naive datetimes (A06)."""
    if ts.tzinfo is None:
        raise ValueError("timestamp must be tz-aware (dev-guide §3 / A06); got naive")
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_marks_stale_alert(
    *,
    stale_products: dict[str, float],
    stale_threshold_s: float,
    now_utc: datetime,
) -> CoinbaseMarketDataAlertDescriptor:
    """Compose the staleness alert for the products past the threshold.

    ``stale_products`` maps product_id → seconds since its last tick
    (already filtered to >= threshold by the caller). Caller guarantees
    non-empty; raises ``ValueError`` otherwise so a policy bug is loud.
    """
    if not stale_products:
        raise ValueError("build_marks_stale_alert called with no stale products")
    worst_product, worst_age_s = max(stale_products.items(), key=lambda kv: kv[1])
    listing = ", ".join(
        f"{pid} ({int(age)}s)" for pid, age in sorted(stale_products.items(), key=lambda kv: -kv[1])
    )
    title = f"Coinbase market data stale: {len(stale_products)} product(s)"
    body = (
        f"WebSocket marks are stale for {len(stale_products)} subscribed "
        f"product(s): {listing}. Threshold: {int(stale_threshold_s)}s; worst: "
        f"{worst_product} at {int(worst_age_s)}s.\n"
        "Strategy §7 outage policy applies (risk loop enforces "
        "protected-hold if native stops are confirmed resting, else "
        "flatten). Investigate the Coinbase WS connection — the worker "
        "auto-reconnects with backoff; persistent staleness means the "
        "venue feed or network path is down."
    )
    payload: dict[str, Any] = {
        "stale_products": {pid: int(age) for pid, age in sorted(stale_products.items())},
        "stale_threshold_s": int(stale_threshold_s),
        "worst_product_id": worst_product,
        "worst_age_s": int(worst_age_s),
        "observed_at_utc": _format_utc(now_utc),
        "alert_severity": MARKET_DATA_ALERT_SEVERITY,
        "alert_category": "broker_disconnect",
    }
    return CoinbaseMarketDataAlertDescriptor(
        severity=MARKET_DATA_ALERT_SEVERITY,
        category="broker_disconnect",
        title=title,
        body=body,
        payload=payload,
    )


def build_funding_snapshot_miss_alert(
    *,
    consecutive_misses: int,
    perp_products_seen: int,
    last_success_utc: datetime | None,
    now_utc: datetime,
) -> CoinbaseMarketDataAlertDescriptor | None:
    """Compose the funding-miss alert, or ``None`` below the threshold.

    A "miss" is an hourly funding pass that persisted zero rows —
    either the products fetch failed, no perp products were
    discovered, or none of the discovered products carried a parseable
    funding rate (strategy §11 open question 4).
    """
    if consecutive_misses < CONSECUTIVE_ALERT_THRESHOLD:
        return None
    title = f"CDE funding capture failing: {consecutive_misses} consecutive hourly misses"
    body = (
        f"The hourly funding logger has persisted zero funding_rates rows "
        f"for {consecutive_misses} consecutive passes (perp products "
        f"discovered on last pass: {perp_products_seen}).\n"
        f"Last successful funding snapshot: "
        f"{_format_utc(last_success_utc) if last_success_utc is not None else 'never'}.\n"
        "Live gate B3 (realized vs parametric funding) depends on this "
        "capture. If the products endpoint does not expose CDE funding "
        "programmatically (strategy §11 open question 4), escalate to "
        "the operator — that needs a data-source decision, not a retry."
    )
    payload: dict[str, Any] = {
        "consecutive_misses": consecutive_misses,
        "perp_products_seen": perp_products_seen,
        "last_success_utc": (
            _format_utc(last_success_utc) if last_success_utc is not None else None
        ),
        "observed_at_utc": _format_utc(now_utc),
        "alert_severity": MARKET_DATA_ALERT_SEVERITY,
        "alert_category": "data_quality_reject",
    }
    return CoinbaseMarketDataAlertDescriptor(
        severity=MARKET_DATA_ALERT_SEVERITY,
        category="data_quality_reject",
        title=title,
        body=body,
        payload=payload,
    )


__all__ = [
    "CONSECUTIVE_ALERT_THRESHOLD",
    "MARKET_DATA_ALERT_SEVERITY",
    "AlertCategoryLiteral",
    "AlertSeverityLiteral",
    "CoinbaseMarketDataAlertDescriptor",
    "build_funding_snapshot_miss_alert",
    "build_marks_stale_alert",
]
