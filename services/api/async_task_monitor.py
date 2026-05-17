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

This module is hot-fix scope (services/api/**) per dev-guide §2.3.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

import structlog

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
        self._log = log.bind(component="async_task_monitor")

    def request_stop(self) -> None:
        """Signal the run_forever loop to exit at the next iteration."""
        self._stop_event.set()

    def probe_once(self) -> int:
        """Inspect each tracked task; emit logs; return dead count.

        Synchronous (no awaits). Returns the number of NEWLY-detected
        dead tasks this probe — the per-task report fires only once
        per task lifetime to avoid repeating-error log spam.
        """
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
    "DEFAULT_MONITOR_INTERVAL_SECONDS",
    "AsyncTaskMonitor",
    "TrackedTask",
    "collect_tracked_tasks",
    "stop_async_task_monitor",
)


# Suppress "imported but unused" — Iterable is part of the documented
# public API surface for future extensions (per-task health metadata).
_: Final[Iterable[str]] = __all__
