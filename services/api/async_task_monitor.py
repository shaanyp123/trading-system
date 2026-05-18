"""Async-task liveness monitor for api lifespan background tasks.

Context
-------

The api lifespan (``services.api.main._lifespan``) spawns 3+ long-lived
asyncio tasks: ``OrderPlacementWorker.run_forever``, the EOD
``ReconciliationScheduler.run_forever``, and the
``HeartbeatProbe.run_forever``. Each task wraps a ``while
not self._stop_event.is_set(): ...`` loop with an inner ``try/except
Exception`` so transient errors don't kill the loop.

But if a task encounters a ``BaseException`` (asyncio.CancelledError,
KeyboardInterrupt, SystemExit) OR if its ``await`` blocks indefinitely
on an unresponsive external service (IBKR TWS API, Postgres) with no
timeout, the task either silently exits or hangs forever. In both cases
asyncio's standard pattern requires SOMEONE to ``await`` the task or
call ``task.exception()`` to surface the failure — without that, the
operator sees no log line and the api appears healthy.

The monitor is the observer. It ticks every
``async_task_monitor_interval_seconds``, inspects each tracked task's
``.done()`` flag, and logs a structured ``async_task_died`` event with
the task name + exception repr (or "done without exception" if the
task exited cleanly). The monitor does NOT attempt to restart dead
tasks — restart policy is a per-task decision, deferred to Phase 1+.

The monitor itself runs as another asyncio task spawned at api boot
and cancelled at api shutdown. It tolerates its own failures: any
exception inside the probe loop is caught + logged at ERROR; the
monitor keeps running.

2026-05-18 drill 5 extension — IBKR connectivity probe
------------------------------------------------------

The monitor now also probes an optional ``TrackedIbkrErrorState``
provider callable each cycle. When IBKR fires Error 1100/1101/1102
("Connectivity between IBKR and Trader Workstation has been lost") the
``ibkr_adapter`` captures the event via its ``errorEvent`` subscription
and surfaces it via its ``last_ibkr_error`` property. The monitor reads
the snapshot each tick — when an error is in the connectivity-codes set
AND the event is fresher than the freshness window, the monitor emits a
``async_task_monitor_ibkr_connectivity_warn`` WARNING (once per distinct
``(error_code, last_seen_at_utc)`` pair, so the log doesn't flood while
the connection stays sick).

This catches the silent-absence pattern from the 2026-05-18 drill 5
incident: ib_gateway↔IBKR upstream broke at the 23:59 ET overnight
maintenance restart; the api's clientId=N socket to the local gateway
sidecar stayed alive (``_ib.isConnected()`` returned True), but inbound
orderStatus events for fills stopped propagating. Without this probe,
the operator only notices when checking on drill state mid-day. The
WARNING log + (in a follow-up PR) a Discord ``#alerts`` P1 dispatch
closes the gap.

This module is hot-fix scope (services/api/**) per dev-guide §2.3.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from services.execution.ibkr_adapter import IbkrErrorStateProvider

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrackedTask:
    """An asyncio task plus its human-readable name for log attribution.

    The ``expected_alive`` flag distinguishes "we intentionally never
    spawned this task" (e.g., scheduler not configured due to missing
    sops fields) from "we spawned this task and it died." Only the
    latter gets the ``async_task_died`` ERROR log; the former gets a
    one-shot ``async_task_not_spawned`` INFO at monitor startup.
    """

    name: str
    task: asyncio.Task[object] | None
    expected_alive: bool


DEFAULT_MONITOR_INTERVAL_SECONDS: Final[float] = 30.0


#: Connectivity-error codes that trigger an
#: ``async_task_monitor_ibkr_connectivity_warn`` WARNING. Default set
#: lifted from ``services.execution.ibkr_adapter.IBKR_CONNECTIVITY_ERROR_CODES``
#: but kept here as a module-level frozenset so the monitor doesn't have
#: to import the adapter (single-headed dependency direction).
DEFAULT_IBKR_CONNECTIVITY_CODES: Final[frozenset[int]] = frozenset({1100, 1101, 1102})

#: Default freshness window for IBKR error probes. Errors older than
#: this window are treated as stale and won't re-trigger a WARNING
#: (the original event already fired its WARNING when it was fresh;
#: re-warning every probe cycle would be noisy). 5 min matches the
#: typical IBKR overnight-maintenance window so a single connectivity
#: blip produces 1-2 WARNING lines, not 10+.
DEFAULT_IBKR_FRESHNESS_SECONDS: Final[float] = 300.0


@dataclass(frozen=True, slots=True)
class TrackedIbkrErrorState:
    """Probe descriptor for the ibkr_adapter's most-recent error state.

    Wraps the ``last_ibkr_error`` provider callable so the monitor
    can read the adapter's latest error each tick without importing
    the adapter class itself. ``provider`` returns ``None`` when no
    error has fired since adapter boot OR raises if the adapter is
    in an inconsistent state — the monitor handles both cleanly.

    ``connectivity_codes`` is the set of IBKR error codes that
    trigger a WARNING. Defaults to the canonical {1100, 1101, 1102}
    upstream-connectivity set; the caller can pass a wider/narrower
    set for testing or for environments where the desired alert
    threshold differs.

    ``freshness_window_seconds`` caps how old an error event can be
    before the monitor treats it as resolved (no log). Defaults to
    300s (5 min); errors older than this don't re-warn even if
    they're still the latest stored state.
    """

    name: str
    provider: IbkrErrorStateProvider
    connectivity_codes: frozenset[int] = field(
        default_factory=lambda: DEFAULT_IBKR_CONNECTIVITY_CODES,
    )
    freshness_window_seconds: float = DEFAULT_IBKR_FRESHNESS_SECONDS


class AsyncTaskMonitor:
    """Periodic liveness probe over a fixed set of lifespan asyncio tasks.

    Spawned + cancelled by the api lifespan; not intended for use
    outside ``services.api.main``. Constructed with the full tracked-
    task list at api boot (some tasks may be ``None`` if the corresponding
    subsystem failed to start; those are reported once as "not spawned"
    and excluded from subsequent probes).
    """

    def __init__(
        self,
        tracked: Sequence[TrackedTask],
        *,
        interval_seconds: float = DEFAULT_MONITOR_INTERVAL_SECONDS,
        ibkr_error_state: TrackedIbkrErrorState | None = None,
    ) -> None:
        self._tracked: tuple[TrackedTask, ...] = tuple(tracked)
        self._interval: float = float(interval_seconds)
        if self._interval <= 0:
            raise ValueError(
                f"AsyncTaskMonitor interval_seconds must be > 0; got {self._interval!r}"
            )
        self._stop_event: asyncio.Event = asyncio.Event()
        # Set of task names we've already reported as dead. We only log
        # the death once; subsequent probes of the same dead task are
        # silent (the dead task remains dead and we don't want a noisy
        # repeating ERROR log).
        self._reported_dead: set[str] = set()
        # Optional IBKR error-state probe (2026-05-18 drill 5 follow-up).
        # None when the adapter isn't wired into the lifespan yet (Phase
        # 0 stub envs / Phase 1 dev with no broker connection); the
        # probe simply skips.
        self._ibkr_error_state: TrackedIbkrErrorState | None = ibkr_error_state
        # Idempotency guard for IBKR connectivity warnings: keyed on
        # ``(error_code, last_seen_at_utc)`` so a single error event
        # only generates one WARNING even though the probe runs every
        # 30s. A new error event (different timestamp OR different code)
        # produces a fresh WARNING.
        self._reported_ibkr_errors: set[tuple[int, datetime]] = set()
        self._log = log.bind(component="async_task_monitor")

    def request_stop(self) -> None:
        """Signal the run_forever loop to exit at the next iteration."""
        self._stop_event.set()

    def probe_once(self) -> int:
        """Inspect each tracked task; emit logs; return dead count.

        Synchronous (no awaits). Returns the number of NEWLY-detected
        dead tasks this probe — the per-task report fires only once
        per task lifetime to avoid repeating-error log spam.

        Also probes the optional ``TrackedIbkrErrorState`` for IBKR
        connectivity errors (2026-05-18 drill 5 follow-up). The IBKR
        probe runs independently of task-death detection — both fire
        each cycle if conditions are met.
        """
        newly_dead = self._probe_tracked_tasks()
        # IBKR probe runs after the task probe; failures are logged
        # but don't inhibit the next probe cycle.
        try:
            self._probe_ibkr_error_state()
        except Exception:
            # Defensive: a bug in the probe MUST NOT kill the monitor
            # loop. ``run_forever`` already wraps probe_once in a
            # broader try/except, but we wrap here too so the IBKR
            # probe's failure mode is structurally distinct from the
            # task probe's.
            self._log.exception("async_task_monitor_ibkr_probe_failed")
        return newly_dead

    def _probe_tracked_tasks(self) -> int:
        """Pre-2026-05-18 ``probe_once`` body — task liveness only."""
        newly_dead = 0
        for tracked in self._tracked:
            if tracked.task is None:
                # Reported once at startup via probe_initial(); subsequent
                # probes silently skip.
                continue
            if tracked.name in self._reported_dead:
                continue
            if not tracked.task.done():
                # Healthy: task still running its event loop. No log
                # (would be too noisy at 30s cadence).
                continue
            # Task is .done() — get the exception (if any) and report.
            exc: BaseException | None
            try:
                exc = tracked.task.exception()
            except asyncio.CancelledError:
                # Task was cancelled; exception() raises CancelledError
                # rather than returning it.
                exc = asyncio.CancelledError("task was cancelled")
            except asyncio.InvalidStateError:
                # Shouldn't happen — we checked .done() above. Be safe.
                continue
            if exc is None:
                # Task exited cleanly — unusual for a run_forever loop
                # but possible if a shutdown signal raced the monitor.
                self._log.error(
                    "async_task_died",
                    task_name=tracked.name,
                    exit_reason="done_without_exception",
                    note=(
                        "run_forever loop exited cleanly without "
                        "a stop signal. Likely cause: BaseException "
                        "(SystemExit/KeyboardInterrupt) inside the "
                        "loop, OR the inner while loop's condition "
                        "flipped without an explicit request_stop()."
                    ),
                )
            else:
                self._log.error(
                    "async_task_died",
                    task_name=tracked.name,
                    exit_reason="exception",
                    exception_type=type(exc).__name__,
                    exception_repr=repr(exc),
                )
            self._reported_dead.add(tracked.name)
            newly_dead += 1
        return newly_dead

    def _probe_ibkr_error_state(self) -> None:
        """Read the IBKR adapter's last-error snapshot; warn if connectivity loss is fresh.

        Three skip paths:

        * No ``TrackedIbkrErrorState`` configured (Phase 0 / dev envs
          without a broker connection) — silent no-op.
        * Provider raises — log at WARNING (don't crash the monitor)
          and skip; the next probe will retry.
        * Provider returns ``None`` (no error event has fired since
          adapter boot) — silent no-op.

        When the provider returns a fresh connectivity-code error
        that we haven't already reported, emit
        ``async_task_monitor_ibkr_connectivity_warn`` at WARNING and
        add the ``(code, last_seen_at_utc)`` key to the idempotency
        set so subsequent probes of the same event are silent.

        "Fresh" means ``last_seen_at_utc`` is within
        ``freshness_window_seconds`` of ``datetime.now(tz=UTC)`` —
        older errors are treated as already-resolved.
        """
        if self._ibkr_error_state is None:
            return
        tracker = self._ibkr_error_state
        try:
            state = tracker.provider()
        except Exception as exc:
            self._log.warning(
                "async_task_monitor_ibkr_provider_failed",
                error=str(exc),
                exception_type=type(exc).__name__,
            )
            return
        if state is None:
            return
        if state.error_code not in tracker.connectivity_codes:
            # Non-connectivity error (e.g., order rejection 10147, data
            # farm 2103). The adapter already structured-logged it at
            # the appropriate level; the monitor doesn't need to add
            # another log line.
            return
        # Freshness check — stale errors don't re-warn.
        now = datetime.now(tz=UTC)
        # Guard against naive timestamps (would raise TypeError on
        # subtract). The adapter writes tz-aware UTC per [A06]; this
        # is defensive against a future regression.
        if state.last_seen_at_utc.tzinfo is None:
            self._log.warning(
                "async_task_monitor_ibkr_naive_timestamp",
                error_code=state.error_code,
                note="IbkrErrorState.last_seen_at_utc was tz-naive; expected tz-aware UTC ([A06])",
            )
            return
        age_seconds = (now - state.last_seen_at_utc).total_seconds()
        if age_seconds > tracker.freshness_window_seconds:
            return
        # Idempotency — same (code, timestamp) only warns once.
        key = (state.error_code, state.last_seen_at_utc)
        if key in self._reported_ibkr_errors:
            return
        self._reported_ibkr_errors.add(key)
        self._log.warning(
            "async_task_monitor_ibkr_connectivity_warn",
            error_code=state.error_code,
            error_string=state.error_string,
            req_id=state.req_id,
            contract_local_symbol=state.contract_local_symbol,
            last_seen_at_utc=state.last_seen_at_utc.isoformat(),
            age_seconds=round(age_seconds, 2),
            freshness_window_seconds=tracker.freshness_window_seconds,
            tracker_name=tracker.name,
            note=(
                "IBKR fired a connectivity-loss/restoration error "
                "(see https://interactivebrokers.github.io/tws-api/"
                "message_codes.html for the canonical list). The api's "
                "TWS API socket to the local ib_gateway sidecar may "
                "still report connected even while inbound orderStatus "
                "events have stopped propagating. Verify by checking "
                "the ibkr_adapter logs around `last_seen_at_utc` and "
                "the gateway↔IBKR upstream state via `docker compose "
                "logs ib_gateway`."
            ),
        )

    def probe_initial(self) -> None:
        """Log a one-shot startup summary; mark un-spawned tasks reported.

        Called once by ``run_forever`` before entering the loop. Emits
        a single ``async_task_monitor_started`` INFO with the count of
        spawned vs un-spawned tasks, plus a per-un-spawned ``async_
        task_not_spawned`` WARNING so the operator sees them explicitly
        in the log stream (some "not spawned" reasons — missing sops
        fields, no active account row — are recoverable; the
        WARNING is the operator's flag to revisit).
        """
        spawned_count = sum(1 for t in self._tracked if t.task is not None)
        not_spawned_count = sum(1 for t in self._tracked if t.task is None and t.expected_alive)
        self._log.info(
            "async_task_monitor_started",
            interval_seconds=self._interval,
            spawned_count=spawned_count,
            not_spawned_count=not_spawned_count,
            total_tracked=len(self._tracked),
        )
        for tracked in self._tracked:
            if tracked.task is None and tracked.expected_alive:
                self._log.warning(
                    "async_task_not_spawned",
                    task_name=tracked.name,
                    note=(
                        "Lifespan attempted to spawn this task but it "
                        "returned None (typically: missing sops fields, "
                        "no active account row, or a startup exception "
                        "logged separately). The monitor will not probe "
                        "this slot."
                    ),
                )
                # Don't add to _reported_dead — the WARNING already
                # surfaced it. The per-probe loop already skips None tasks.

    async def run_forever(self) -> None:
        """Main loop: probe every ``interval_seconds`` until ``request_stop``."""
        self._log.info("async_task_monitor_run_forever_entered")
        self.probe_initial()
        try:
            while not self._stop_event.is_set():
                try:
                    self.probe_once()
                except Exception:
                    # The monitor MUST NOT crash on its own bugs — log
                    # at EXCEPTION (includes traceback) and continue.
                    self._log.exception("async_task_monitor_probe_failed")
                # Sleep until interval elapses OR stop_event fires,
                # whichever comes first. asyncio.wait_for raises
                # TimeoutError on the timer; we suppress that and loop.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._interval,
                    )
                except TimeoutError:
                    pass
        finally:
            self._log.info("async_task_monitor_stopped")


async def stop_async_task_monitor(
    monitor_task_tuple: tuple[AsyncTaskMonitor, asyncio.Task[None]] | None,
) -> None:
    """Lifespan helper — request stop + await the monitor task; best-effort.

    Mirrors the shape of ``_stop_order_placement_worker`` etc. so the
    lifespan ``finally`` block has a consistent shutdown surface.
    """
    if monitor_task_tuple is None:
        return
    monitor, task = monitor_task_tuple
    try:
        monitor.request_stop()
    except Exception:
        log.exception("async_task_monitor_request_stop_failed")
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except TimeoutError:
        log.warning("async_task_monitor_shutdown_timeout")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    except Exception:
        log.exception("async_task_monitor_task_join_failed")


def collect_tracked_tasks(
    *,
    order_placement: tuple[object, object] | None,
    reconciliation: tuple[object, object] | None,
    heartbeat_probe: tuple[object, object] | None,
) -> tuple[TrackedTask, ...]:
    """Build the canonical TrackedTask tuple from lifespan state.

    Pure-policy: takes the 3 ``(worker, task)`` tuples the lifespan
    constructs and returns the typed ``TrackedTask`` sequence. Tasks
    whose state-tuple is None get ``task=None`` (the monitor reports
    them as not-spawned at startup but doesn't probe them per-cycle).

    The argument order matches the lifespan's ordering for log
    consistency: order_placement → reconciliation → heartbeat_probe.
    """
    return (
        TrackedTask(
            name="order_placement_worker.run_forever",
            task=_extract_task(order_placement),
            expected_alive=order_placement is not None,
        ),
        TrackedTask(
            name="reconciliation_scheduler.run_forever",
            task=_extract_task(reconciliation),
            expected_alive=reconciliation is not None,
        ),
        TrackedTask(
            name="heartbeat_probe.run_forever",
            task=_extract_task(heartbeat_probe),
            expected_alive=heartbeat_probe is not None,
        ),
    )


def _extract_task(
    state_tuple: tuple[object, object] | None,
) -> asyncio.Task[object] | None:
    """Pull the asyncio.Task out of a ``(worker, task)`` tuple if present.

    The lifespan stores tasks as ``tuple[object, object]`` because the
    worker classes don't share a common base. Here we type-narrow + None-
    handle in one place.
    """
    if state_tuple is None:
        return None
    _, task = state_tuple
    if isinstance(task, asyncio.Task):
        return task
    return None


__all__: Final = (
    "DEFAULT_IBKR_CONNECTIVITY_CODES",
    "DEFAULT_IBKR_FRESHNESS_SECONDS",
    "DEFAULT_MONITOR_INTERVAL_SECONDS",
    "AsyncTaskMonitor",
    "TrackedIbkrErrorState",
    "TrackedTask",
    "collect_tracked_tasks",
    "stop_async_task_monitor",
)


# Suppress "imported but unused" — Iterable is part of the documented
# public API surface for future extensions (per-task health metadata).
_: Final[Iterable[str]] = __all__
