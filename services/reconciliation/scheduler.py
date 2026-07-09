"""services/reconciliation/scheduler.py — long-lived recon scheduler.

Worker-PR-3b (post-pivot 2026-05-12); re-anchored onto UTC in
crypto-pivot C0 §3.5 (2026-07-09). The reconciliation cadence is
once-per-UTC-calendar-day at the EOD cutover: **00:15 UTC**, ten
minutes after the 00:05 UTC daily strategy decision (delta spec §3.5),
so the recon snapshot sees the decision's fills. Crypto trades 24/7 —
there is no exchange session; the UTC calendar day IS the session day
(the same convention the §3.4 CONVALESCENT clean-day criterion counts
on — 3 days per the 2026-07-09 operator amendment. The tick IS wired:
``eod_cycle.run_eod_cycle`` invokes the api lifespan's
``convalescent_tick`` hook after the apply step, which drives
``services/risk/dispatch.py::apply_convalescent_clean_day_tick``).
The CME-era 18:30-ET / America-New_York anchoring died with the
IBKR/LEAN stack.

This module provides a stdlib-asyncio scheduler that:

  * Wakes every ``tick_interval_seconds`` (default 60s) to check
    whether the configured EOD time has been reached for the current
    UTC calendar day.
  * Tracks the last-fired session date in process memory so the
    scheduler fires exactly once per session day (re-deploys mid-day
    don't refire if the cycle already ran).
  * Invokes a caller-supplied async ``cycle_callback`` when due. The
    callback owns the full recon-cycle work (fetch the Coinbase
    snapshot → build views → call plan_reconciliation_check → apply
    via apply_reconciliation_plan). Decoupling the schedule from the
    cycle work keeps this module pure-policy + testable without
    DB/HTTP wiring.
  * Listens for SIGTERM via :meth:`request_stop` for graceful
    shutdown.

**Why stdlib asyncio over apscheduler:** the cadence is one cron-like
event per day; apscheduler's full cron syntax + persistent job store
+ executor pool is overkill. A stdlib loop gives us wall-clock
matching with zero new deps (and UTC anchoring removes even the DST
concern the ET era had). Phase C2+ can adopt apscheduler if recon
cadence grows to N-per-day or multi-account.

**A02 BINDS** — `services/reconciliation/**` is on the forbidden
whitelist; `risk-review-approved` required.
**A06 enforced** — every datetime tz-aware UTC.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final

import structlog

log = structlog.get_logger()


#: Default EOD reconciliation time in UTC. Delta spec §3.5: 00:15 UTC,
#: after the 00:05 UTC daily decision.
DEFAULT_EOD_RECON_TIME_UTC: Final[time] = time(hour=0, minute=15)

#: Default tick cadence — how often the scheduler checks whether the EOD
#: trigger has fired. 60s is a sane upper bound for "fire within a minute
#: of the scheduled time" without burning CPU.
DEFAULT_TICK_INTERVAL_SECONDS: Final[float] = 60.0


CycleCallback = Callable[[date], Awaitable[None]]
"""Caller-supplied async callback invoked when the scheduler fires.

The callback receives the UTC session date (the UTC calendar day) at
which the cycle is firing. The callback owns the full recon-cycle work
(fetch the venue snapshot, build BackendView + BrokerView, call
plan_reconciliation_check, apply via apply_reconciliation_plan, handle
errors). Exceptions raised inside the callback are logged and
swallowed — they DO NOT stop the scheduler. A failed cycle waits for
the next session day's tick.
"""


@dataclass(frozen=True, slots=True)
class SchedulerStateSnapshot:
    """Observable snapshot of scheduler state — for tests + telemetry.

    Returned by :meth:`ReconciliationScheduler.snapshot` so callers can
    assert on cycle-fire bookkeeping without poking private fields.
    """

    last_fired_session_date_utc: date | None
    next_check_in_seconds: float
    is_running: bool


# ---------------------------------------------------------------------------
# Pure-policy decision helpers (testable without an event loop)
# ---------------------------------------------------------------------------


def should_fire_now(
    *,
    now_utc: datetime,
    eod_recon_time_utc: time,
    last_fired_session_date_utc: date | None,
) -> bool:
    """Pure-policy: should the scheduler fire at this wall-clock moment?

    Fires when:
      1. ``now_utc`` is at or past ``eod_recon_time_utc`` for the
         current UTC calendar date, AND
      2. ``last_fired_session_date_utc`` is not the current UTC calendar
         date (i.e., we haven't already fired today).

    Crypto trades 24/7 — every UTC calendar day is a session day, so
    there is no weekend/holiday consideration.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (A06)")
    utc_now = now_utc.astimezone(UTC)
    if utc_now.time() < eod_recon_time_utc:
        return False
    today_utc = utc_now.date()
    if last_fired_session_date_utc == today_utc:
        return False
    return True


def current_session_date_utc(now_utc: datetime) -> date:
    """Return the UTC calendar date for a tz-aware datetime."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (A06)")
    return now_utc.astimezone(UTC).date()


# ---------------------------------------------------------------------------
# Long-lived scheduler
# ---------------------------------------------------------------------------


class ReconciliationScheduler:
    """Long-lived task that fires ``cycle_callback`` at the EOD recon time.

    Lifecycle:
        sched = ReconciliationScheduler(callback=cycle, ...)
        await sched.run_forever()  # blocks until request_stop()
        sched.request_stop()        # graceful shutdown signal

    The callback is invoked exactly once per UTC calendar day at or
    after :attr:`eod_recon_time_utc`. Re-deploys mid-day after the
    cycle has fired re-initialize the scheduler with a None last-fired
    marker, so the cycle CAN re-fire on the same day if the process
    restarted (intentional — a failed pre-restart cycle should retry
    post-restart). Operator can pre-load ``initial_fired_date`` if a
    restart-without-refire is desired.
    """

    def __init__(
        self,
        *,
        callback: CycleCallback,
        eod_recon_time_utc: time = DEFAULT_EOD_RECON_TIME_UTC,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
        initial_fired_date: date | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._callback = callback
        self._eod_recon_time_utc = eod_recon_time_utc
        self._tick_interval_seconds = tick_interval_seconds
        self._last_fired_session_date_utc: date | None = initial_fired_date
        self._stop_event = asyncio.Event()
        self._is_running = False
        # Injectable clock so tests can drive the scheduler without
        # waiting wall-clock time. Production passes the default.
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._log = log.bind(
            scheduler="reconciliation",
            eod_recon_time_utc=eod_recon_time_utc.isoformat(),
        )

    def request_stop(self) -> None:
        """Signal :meth:`run_forever` to exit at the next iteration boundary."""
        self._stop_event.set()

    def snapshot(self) -> SchedulerStateSnapshot:
        """Return current observable state — for tests + telemetry."""
        return SchedulerStateSnapshot(
            last_fired_session_date_utc=self._last_fired_session_date_utc,
            next_check_in_seconds=self._tick_interval_seconds,
            is_running=self._is_running,
        )

    async def maybe_fire(self) -> bool:
        """Check the schedule + invoke the callback if due.

        Returns True iff the callback was invoked this call. Exceptions
        raised inside the callback are logged + swallowed; the
        scheduler keeps running.

        Useful as a one-shot tick for tests + manual invocation.
        """
        now_utc = self._clock()
        if not should_fire_now(
            now_utc=now_utc,
            eod_recon_time_utc=self._eod_recon_time_utc,
            last_fired_session_date_utc=self._last_fired_session_date_utc,
        ):
            return False
        session_date = current_session_date_utc(now_utc)
        self._log.info(
            "reconciliation_scheduler_firing",
            session_date_utc=session_date.isoformat(),
        )
        try:
            await self._callback(session_date)
        except Exception as exc:
            self._log.exception(
                "reconciliation_scheduler_callback_error",
                session_date_utc=session_date.isoformat(),
                error=str(exc),
            )
        # Mark as fired AFTER the callback runs (regardless of exception)
        # so a transient failure doesn't trigger a retry storm on the
        # next tick. Caller can rerun by clearing _last_fired_session_date_utc
        # or restarting the scheduler.
        self._last_fired_session_date_utc = session_date
        return True

    async def run_forever(self) -> None:
        """Supervisor: tick + maybe_fire loop until request_stop."""
        self._is_running = True
        self._log.info("reconciliation_scheduler_started")
        try:
            while not self._stop_event.is_set():
                try:
                    await self.maybe_fire()
                except Exception as exc:
                    self._log.exception(
                        "reconciliation_scheduler_tick_error",
                        error=str(exc),
                    )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._tick_interval_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            self._is_running = False
            self._log.info("reconciliation_scheduler_stopped")


__all__ = [
    "DEFAULT_EOD_RECON_TIME_UTC",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "CycleCallback",
    "ReconciliationScheduler",
    "SchedulerStateSnapshot",
    "current_session_date_utc",
    "should_fire_now",
]
