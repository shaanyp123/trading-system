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

Crypto-pivot C0-B2b note: the 2026-05-18 drill-5 IBKR connectivity
probe (``TrackedIbkrErrorState`` reading the ibkr_adapter's
1100/1101/1102 error snapshots) was retired with the IBKR execution
layer. Broker-side connectivity observability is now the Coinbase
market-data staleness watchdog (delta spec §3.2). The generic
monitor→Discord alert seam (``MonitorAlertHook``) remains for the
task-death path and future probes.

This module is hot-fix scope (services/api/**) per dev-guide §2.3.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrackedTask:
    """An asyncio task plus its human-readable name for log attribution.

    The ``expected_alive`` flag distinguishes "we intentionally never
    spawned this task" (e.g., scheduler not configured due to missing
    secrets fields) from "we spawned this task and it died." Only the
    latter gets the ``async_task_died`` ERROR log; the former gets a
    one-shot ``async_task_not_spawned`` INFO at monitor startup.
    """

    name: str
    task: asyncio.Task[object] | None
    expected_alive: bool


DEFAULT_MONITOR_INTERVAL_SECONDS: Final[float] = 30.0


#: Allow-list of tracked-task names whose death triggers the recovery-agent
#: hook. Other tracked tasks still log ``async_task_died`` at ERROR but don't
#: INSERT an ``alerts`` row (category='worker_failure') + fire Discord
#: ``#critical`` + ``#alerts`` + email. Per drill 5 (2026-05-18) +
#: drill 7 (2026-05-18) retrospectives, only the order placement worker's
#: death produces the backend-blind-fills failure mode that
#: ``scripts/operator_tools/recovery_agent.py`` is built to resolve (via
#: subprocess ``replay_executions.py``). Other tracked tasks (recon
#: scheduler, heartbeat probe) fail in ways that don't leave
#: the audit chain inconsistent with IBKR state — their death is still
#: log-visible (and operator-actionable via /verify-chain + Discord
#: ``#alerts`` from the existing partial-cycle alert hooks).
#:
#: Extending this set is an operator-reversible decision — pass a custom
#: ``task_death_allow_list`` to ``AsyncTaskMonitor`` (see
#: ``services/api/main.py::_start_async_task_monitor``). The default is
#: conservative (one task) to limit the recovery agent's blast radius
#: while v1 builds operational confidence.
DEFAULT_TASK_DEATH_ALLOW_LIST: Final[frozenset[str]] = frozenset(
    {
        "order_placement_worker.run_forever",
        # Crypto-pivot C0-B2a: the market-data worker feeds the 30s risk
        # loop's marks (delta spec §3.2) — silent death means the risk
        # loop goes blind, so its death rates the alerts-row + Discord
        # escalation, not just the ERROR log.
        "coinbase_market_data.run_forever",
    }
)


@dataclass(frozen=True, slots=True)
class MonitorAlertDescriptor:
    """Pure-policy alert descriptor for monitor-emitted alerts.

    Mirrors the shape of ``services.reconciliation.recon.AlertDescriptor``
    minus the recon-specific ``triggering_break_index`` (monitor alerts
    have no triggering audit event — they're pure observability). The
    fields land in the ``alerts`` table per backend-spec §3.27:

    - ``severity`` → ``alerts.severity`` ('P0' | 'P1' | 'P2')
    - ``category`` → ``alerts.category`` (must match the
      ``alert_category`` Postgres enum from alembic 0004)
    - ``title`` + ``body`` → composed into ``alerts.message``
      (recon convention is ``f"{title}\\n\\n{body}"``)
    - ``payload`` → ``alerts.detail`` (JSONB)

    Used by the task-death path today; future monitor probes can emit
    the same shape.
    """

    severity: str
    category: str
    title: str
    body: str
    payload: dict[str, Any]


#: Hook signature for monitor-emitted alert dispatch. The hook closure
#: (built in ``services.api.main._build_monitor_alert_dispatch_hook``)
#: INSERTs an ``alerts`` row + calls
#: ``services.webhook_pusher.dispatcher.dispatch_alert``. Returns None
#: on success; raises on infrastructure failure (caught + logged by
#: the monitor — the hook's failure mode is "Discord didn't fire," not
#: "monitor crashed").
MonitorAlertHook = Callable[[MonitorAlertDescriptor], Awaitable[None]]


#: Hook signature for monitor-emitted task-death dispatch. Same shape as
#: :data:`MonitorAlertHook` — the closure (built in
#: ``services.api.main._build_task_death_alert_hook``) (a) emits an
#: ``ASYNC_TASK_DIED`` audit event (audit-first per backend-spec
#: §2.10.1), (b) INSERTs an ``alerts`` row with
#: ``category='worker_failure'`` and ``triggering_audit_event_uuid``
#: pointed at the audit row, (c) calls
#: ``services.webhook_pusher.dispatcher.dispatch_alert`` which fans the
#: P0 alert to ``#alerts`` + ``#critical`` + email. The recovery agent
#: at ``scripts/operator_tools/recovery_agent.py`` polls the alerts row
#: for unhandled ``worker_failure`` events on a 60s systemd timer.
#:
#: Aliased to :data:`MonitorAlertHook` (same signature) to dodge type
#: duplication. The semantic split lives on the
#: :class:`AsyncTaskMonitor` constructor — two separate parameters so
#: lifespan callers can wire each independently.
TaskDeathAlertHook = MonitorAlertHook


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
        task_death_alert_hook: TaskDeathAlertHook | None = None,
        task_death_allow_list: frozenset[str] = DEFAULT_TASK_DEATH_ALLOW_LIST,
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
        # Recovery-agent task-death hook (drill 5/6 follow-up, 2026-05-26).
        # Optional; when wired, the monitor fires the hook on death of a
        # task in ``task_death_allow_list``. The hook closure (built in
        # ``services.api.main._build_task_death_alert_hook``) emits the
        # ``ASYNC_TASK_DIED`` audit event + INSERTs an alerts row +
        # dispatches via webhook_pusher. When None, only the existing
        # structured ``async_task_died`` ERROR log fires.
        self._task_death_alert_hook: TaskDeathAlertHook | None = task_death_alert_hook
        self._task_death_allow_list: frozenset[str] = task_death_allow_list
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
        return self._probe_tracked_tasks()

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
            # Recovery-agent task-death hook (drill 5/6 follow-up).
            # Fires only for tasks in the allow-list (default:
            # ``order_placement_worker.run_forever`` only). The hook
            # closure handles ``ASYNC_TASK_DIED`` audit emit +
            # ``alerts`` INSERT + dispatch_alert; failure is swallowed
            # by ``_schedule_alert_dispatch`` so a Discord 5xx doesn't
            # crash the probe loop. The structured ERROR log above is
            # the load-bearing observability; the hook is the
            # recovery-agent inbox.
            if (
                self._task_death_alert_hook is not None
                and tracked.name in self._task_death_allow_list
            ):
                descriptor = self._build_task_death_descriptor(
                    task_name=tracked.name,
                    exit_reason="done_without_exception" if exc is None else "exception",
                    exception=exc,
                )
                self._schedule_alert_dispatch(self._task_death_alert_hook, descriptor)
        return newly_dead

    @staticmethod
    def _build_task_death_descriptor(
        *,
        task_name: str,
        exit_reason: str,
        exception: BaseException | None,
    ) -> MonitorAlertDescriptor:
        """Translate a dead-task observation into a MonitorAlertDescriptor.

        Severity locked P0 — operator-actionable AND the recovery agent
        needs to act within ~60s (the systemd-timer cadence). P0 routes
        to ``#alerts`` + ``#critical`` + email per
        ``services/webhook_pusher/payloads.py::SEVERITY_TO_CHANNELS``.

        Category locked ``worker_failure`` per the alembic
        ``2026-05-26_worker_failure_alert_category`` migration + spec
        §3.27.

        Title is a short operator-grep handle; body carries the human-
        readable explanation. Payload is structured for the recovery
        agent's classification logic: ``exception_type`` +
        ``exception_repr`` drive the transient-vs-hard-crash heuristic
        (see ``scripts.operator_tools.recovery_agent.classify_failure``).
        """
        title = f"async task died: {task_name}"
        if exception is None:
            body = (
                f"Tracked lifespan task `{task_name}` transitioned to "
                "`.done()` with exit_reason=`done_without_exception` — "
                "the run_forever loop exited cleanly without a stop signal. "
                "Likely cause: BaseException (SystemExit/KeyboardInterrupt) "
                "inside the loop, OR the inner while loop's condition "
                "flipped without an explicit request_stop(). Recovery "
                "agent will classify as alert_only (operator-gated "
                "investigation)."
            )
            exception_type: str | None = None
            exception_repr: str | None = None
        else:
            exception_type = type(exception).__name__
            exception_repr = repr(exception)
            body = (
                f"Tracked lifespan task `{task_name}` raised "
                f"`{exception_type}`: {exception_repr}\n\n"
                "Recovery agent will classify (transient vs hard crash) "
                "and invoke replay_executions.py if there are orphan "
                "fills to recover from this dead window. Audit + alert "
                "details in the audit chain + alerts table."
            )
        observed_at_utc = datetime.now(tz=UTC).isoformat()
        payload: dict[str, Any] = {
            "task_name": task_name,
            "exit_reason": exit_reason,
            "exception_type": exception_type,
            "exception_repr": exception_repr,
            "observed_at_utc": observed_at_utc,
        }
        return MonitorAlertDescriptor(
            severity="P0",
            category="worker_failure",
            title=title,
            body=body,
            payload=payload,
        )

    def _schedule_alert_dispatch(
        self,
        hook: MonitorAlertHook,
        descriptor: MonitorAlertDescriptor,
    ) -> None:
        """Fire-and-forget dispatch of the hook on the running loop.

        Wraps the hook in an exception-swallowing coroutine so a Discord
        5xx or alerts-INSERT failure doesn't crash the monitor's
        run_forever loop. Hook failures emit
        ``async_task_monitor_alert_dispatch_failed`` at WARNING.

        The probe runs inside the monitor's ``run_forever`` task (an
        asyncio task on the lifespan event loop), so
        ``asyncio.create_task`` always has a running loop. Defensive
        fallback for tests that probe outside a loop: log + skip.
        """

        async def _invoke() -> None:
            try:
                await hook(descriptor)
            except Exception as exc:
                self._log.warning(
                    "async_task_monitor_alert_dispatch_failed",
                    error=str(exc),
                    exception_type=type(exc).__name__,
                    error_code=descriptor.payload.get("error_code"),
                )

        coro = _invoke()
        try:
            asyncio.create_task(coro)  # noqa: RUF006 — fire-and-forget by design
        except RuntimeError as exc:
            # No running loop (test path). Close the un-awaited coro
            # to suppress the RuntimeWarning + log so the operator
            # knows the dispatch was skipped. The WARNING log already
            # fired above; the Discord push is the only thing that's
            # gated by the missing loop.
            coro.close()
            self._log.warning(
                "async_task_monitor_alert_dispatch_no_loop",
                error=str(exc),
            )

    def probe_initial(self) -> None:
        """Log a one-shot startup summary; mark un-spawned tasks reported.

        Called once by ``run_forever`` before entering the loop. Emits
        a single ``async_task_monitor_started`` INFO with the count of
        spawned vs un-spawned tasks, plus a per-un-spawned ``async_
        task_not_spawned`` WARNING so the operator sees them explicitly
        in the log stream (some "not spawned" reasons — missing secrets
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
                        "returned None (typically: missing secrets fields, "
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
    reconciliation: tuple[object, object] | None,
    heartbeat_probe: tuple[object, object] | None,
    coinbase_market_data: tuple[object, object] | None = None,
    usdc_rewards_capture: tuple[object, object] | None = None,
    binance_funding_proxy: tuple[object, object] | None = None,
) -> tuple[TrackedTask, ...]:
    """Build the canonical TrackedTask tuple from lifespan state.

    Pure-policy: takes the ``(worker, task)`` tuples the lifespan
    constructs and returns the typed ``TrackedTask`` sequence. Tasks
    whose state-tuple is None get ``task=None`` (the monitor reports
    them as not-spawned at startup but doesn't probe them per-cycle).

    The argument order matches the lifespan's ordering for log
    consistency: reconciliation → heartbeat_probe →
    coinbase_market_data → usdc_rewards_capture. (The IBKR
    order_placement slot was retired with the IBKR execution layer,
    crypto-pivot C0-B2b; the §3.3 strategy worker claims a slot here
    in C0-B3. The usdc_rewards_capture slot is telemetry-only — its
    death is log-visible but deliberately NOT on the task-death
    allow-list: no risk surface depends on it.)
    """
    return (
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
        TrackedTask(
            name="coinbase_market_data.run_forever",
            task=_extract_task(coinbase_market_data),
            expected_alive=coinbase_market_data is not None,
        ),
        TrackedTask(
            name="usdc_rewards_capture.run_forever",
            task=_extract_task(usdc_rewards_capture),
            expected_alive=usdc_rewards_capture is not None,
        ),
        TrackedTask(
            name="binance_funding_proxy.run_forever",
            task=_extract_task(binance_funding_proxy),
            expected_alive=binance_funding_proxy is not None,
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
    "DEFAULT_TASK_DEATH_ALLOW_LIST",
    "AsyncTaskMonitor",
    "MonitorAlertDescriptor",
    "MonitorAlertHook",
    "TaskDeathAlertHook",
    "TrackedTask",
    "collect_tracked_tasks",
    "stop_async_task_monitor",
)


# Suppress "imported but unused" — Iterable is part of the documented
# public API surface for future extensions (per-task health metadata).
_: Final[Iterable[str]] = __all__
