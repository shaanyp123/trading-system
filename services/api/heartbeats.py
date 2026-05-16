"""services/api/heartbeats.py — in-memory service heartbeat registry.

Closes a silent-failure surface: pre-this-module, the LEAN container's
17:30 ET cycle could stop firing (process hang, deadlock in the strategy
loop, network partition that survives docker healthcheck) and the
operator would only notice on the next time they checked /signals. There
was no observable "LEAN hasn't run in N hours" signal anywhere.

This module provides a process-local registry that the LEAN POST path
updates on every ``lean_cycle_heartbeat`` / ``lean_strategy_initialized``
event. ``GET /api/system/heartbeats`` exposes the registry to the
operator (via the System page + Discord bot in follow-up PRs).

**Scope intentionally narrow** — this PR ships the registry + endpoint
only. A follow-up adds:
  * Background coroutine in the api lifespan that ticks every 15 min and
    fires ``dispatch_alert(severity="P2", category="heartbeat_stale")``
    when the LEAN cycle hasn't recorded a heartbeat in >26h.
  * ``HEARTBEAT_STALE_DETECTED`` audit event type so each stale-detection
    leaves a durable chain breadcrumb.
  * Same registry extended for other cron-like services (recon scheduler,
    order-placement worker).

**Why in-memory instead of a new DB table:**
  * Heartbeats fire daily; writing to ``audit_log`` per cycle flouts the
    "no operational liveness pings in the chain" rule (A22 + the explicit
    comment at the top of ``services/api/schemas/lean.py``).
  * The existing ``liveness_probes`` table is semantically a *watchdog →
    operator* ack channel (sent_at_utc + acknowledged_at_utc columns) —
    not a service-heartbeat sink.
  * In-memory is good enough: the api restarts infrequently; when it
    does the registry starts empty + the endpoint returns
    ``freshness='unknown'`` until the next real heartbeat arrives. The
    operator sees "we restarted, freshness unknown" — true and useful,
    not noise.

Concurrency: writes from the LEAN POST handler are serialized by
``asyncio.Lock`` so a burst of cycles cannot interleave the timestamp
update with a concurrent ``snapshot()`` read. Reads are lock-free —
dict copies are atomic in CPython for the small set of services tracked
here (<10 even in the wildest Phase 1+ projections).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

#: Locked enum of services whose cron-like heartbeats this module tracks.
#:
#: ``lean_cycle`` — emitted by the LEAN Local container on every scheduled
#: signal-generation cycle (daily 17:30 ET = 21:30 UTC) AND on algorithm
#: initialize. See ``lean/v1_strategy.py::_post_event``.
#:
#: Follow-up PRs add ``reconciliation_eod`` (recon scheduler, 18:30 ET) +
#: ``order_placement_worker`` (continuous 5s poll). Each addition is a
#: single Literal-union extension + a call to
#: ``HeartbeatRegistry.record(...)`` from the producer side.
HeartbeatService = Literal["lean_cycle"]


@dataclass(frozen=True, slots=True)
class HeartbeatRecord:
    """One service's most-recent heartbeat snapshot.

    ``last_heartbeat_utc`` is the source-clock timestamp the producer
    sent (LEAN's ``self.utc_time`` for cycles, or ``datetime.now(UTC)``
    at server-side receive for fallback). Source clock is preferred
    because cron skew + container clock drift on the producer side is
    visible to the freshness check; rejecting clock-skewed updates
    would mask real producer-side problems.

    ``received_at_utc`` is the server-side wall clock when the api
    accepted the heartbeat — used in the endpoint response so the
    frontend can compute a "transport latency" if the two diverge.

    ``count`` accumulates so the operator can see "LEAN has fired 8
    cycles since api last restarted" — useful for spotting a cron that
    fires twice as often as it should.
    """

    last_heartbeat_utc: datetime
    received_at_utc: datetime
    count: int


@dataclass(slots=True)
class HeartbeatRegistry:
    """Process-local in-memory map of ``HeartbeatService → HeartbeatRecord``.

    Constructed once at module import time + retrieved everywhere via
    :func:`get_heartbeat_registry`. The api's lifespan does not need to
    instantiate this — it is a passive store; the first
    :func:`record` call from the LEAN POST handler initializes its entry.
    """

    _records: dict[HeartbeatService, HeartbeatRecord] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(
        self,
        service: HeartbeatService,
        *,
        source_clock_utc: datetime,
        received_at_utc: datetime | None = None,
    ) -> HeartbeatRecord:
        """Stamp a fresh heartbeat for ``service``.

        Raises :class:`ValueError` if either timestamp is naive (dev-guide
        §3 / A06: all in-process datetimes are tz-aware UTC).
        """
        if source_clock_utc.tzinfo is None:
            raise ValueError(
                "source_clock_utc must be tz-aware (dev-guide §3 / A06); "
                f"got naive {source_clock_utc!r}"
            )
        if received_at_utc is None:
            received_at_utc = datetime.now(tz=UTC)
        elif received_at_utc.tzinfo is None:
            raise ValueError(
                "received_at_utc must be tz-aware (dev-guide §3 / A06); "
                f"got naive {received_at_utc!r}"
            )

        async with self._lock:
            prior = self._records.get(service)
            new_count = (prior.count if prior is not None else 0) + 1
            record = HeartbeatRecord(
                last_heartbeat_utc=source_clock_utc,
                received_at_utc=received_at_utc,
                count=new_count,
            )
            self._records[service] = record
            return record

    def snapshot(self) -> dict[HeartbeatService, HeartbeatRecord]:
        """Return a defensive copy of the registry state.

        Lock-free intentionally: the dict copy is fast + the registry
        is the producer's sole writer (the api worker can call this
        without contending on the LEAN POST path).
        """
        return dict(self._records)

    def reset(self) -> None:
        """Drop all records. Test-only; production has no reset path."""
        self._records.clear()


#: Module-level singleton. The convention mirrors the SSE multiplexer:
#: one process-wide instance, retrieved via :func:`get_heartbeat_registry`.
_REGISTRY = HeartbeatRegistry()


def get_heartbeat_registry() -> HeartbeatRegistry:
    """Return the process-wide singleton."""
    return _REGISTRY


#: Locked freshness thresholds per service. Values are seconds since
#: ``last_heartbeat_utc`` past which the service is considered stale.
#:
#: ``lean_cycle`` = 26h: one daily cycle (24h) + a 2h soft cushion for
#: weekend boundaries / DST flips / a single missed cycle. Above this,
#: the operator should investigate; the follow-up alert PR fires a P2.
HEARTBEAT_STALE_THRESHOLDS_S: dict[HeartbeatService, int] = {
    "lean_cycle": 26 * 3600,
}


HeartbeatFreshness = Literal["fresh", "stale", "unknown"]


def classify_freshness(
    service: HeartbeatService,
    record: HeartbeatRecord | None,
    *,
    now_utc: datetime,
) -> HeartbeatFreshness:
    """Map ``HeartbeatRecord`` + ``now_utc`` to a freshness label.

    ``unknown`` covers two cases: no record yet (api just booted) OR the
    service hasn't been seen since the most recent restart. Both render
    identically in the UI; the registry's emptiness alone is not a
    signal of trouble.
    """
    if record is None:
        return "unknown"
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")
    threshold_s = HEARTBEAT_STALE_THRESHOLDS_S[service]
    age_s = (now_utc - record.last_heartbeat_utc).total_seconds()
    if age_s > float(threshold_s):
        return "stale"
    return "fresh"


__all__ = [
    "HEARTBEAT_STALE_THRESHOLDS_S",
    "HeartbeatFreshness",
    "HeartbeatRecord",
    "HeartbeatRegistry",
    "HeartbeatService",
    "classify_freshness",
    "get_heartbeat_registry",
]
