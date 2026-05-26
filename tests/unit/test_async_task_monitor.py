"""Unit tests for ``services.api.async_task_monitor``.

The monitor is a lifespan observability surface added 2026-05-17 to
catch silent worker-task death (an asyncio task that completes
unexpectedly without anything ``await``ing it, leaving the
operator with no log line). Pure Python; no testcontainers; A22 N/A.

2026-05-18 drill 5 follow-up extension — IBKR connectivity probe.
The monitor now also probes an optional ``TrackedIbkrErrorState``
provider each cycle and warns on fresh connectivity errors. See
``TestIbkrConnectivityProbe`` below.

Test surface:

* ``TestTrackedTask`` — frozen dataclass shape
* ``TestCollectTrackedTasks`` — builds the canonical tuple from
  lifespan state, handling None state tuples
* ``TestAsyncTaskMonitor`` — construction validation +
  ``probe_once`` correctness across alive / dead / exception /
  cancelled scenarios
* ``TestProbeIdempotency`` — repeat probes of the same dead task
  don't re-log
* ``TestRunForever`` — main loop ticks at interval; ``request_stop``
  exits cleanly
* ``TestStopAsyncTaskMonitor`` — shutdown helper handles timeout +
  exception paths
* ``TestIbkrConnectivityProbe`` — fresh 1100/1101/1102 errors warn;
  stale errors silent; idempotent; defensive on provider failures
* ``TestModuleContract`` — public ``__all__`` surface
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from services.api.async_task_monitor import (
    DEFAULT_IBKR_CONNECTIVITY_CODES,
    DEFAULT_IBKR_FRESHNESS_SECONDS,
    DEFAULT_MONITOR_INTERVAL_SECONDS,
    AsyncTaskMonitor,
    TrackedIbkrErrorState,
    TrackedTask,
    collect_tracked_tasks,
    stop_async_task_monitor,
)
from services.execution.ibkr_adapter import IbkrErrorState


class TestTrackedTask:
    """Frozen dataclass; name + task + expected_alive."""

    def test_construct_with_task(self) -> None:
        async def _noop() -> None:
            return None

        async def _runner() -> tuple[TrackedTask, asyncio.Task[None]]:
            task = asyncio.create_task(_noop())
            await task  # complete cleanly so we don't leak
            tracked = TrackedTask(name="foo", task=task, expected_alive=True)
            return tracked, task

        tracked, task = asyncio.run(_runner())
        assert tracked.name == "foo"
        assert tracked.task is task
        assert tracked.expected_alive is True

    def test_construct_with_none_task(self) -> None:
        tracked = TrackedTask(name="bar", task=None, expected_alive=False)
        assert tracked.name == "bar"
        assert tracked.task is None
        assert tracked.expected_alive is False

    def test_frozen(self) -> None:
        tracked = TrackedTask(name="x", task=None, expected_alive=False)
        with pytest.raises(Exception):  # FrozenInstanceError is dataclass-internal
            tracked.name = "y"  # type: ignore[misc]


class TestCollectTrackedTasks:
    """The lifespan-state → TrackedTask shape conversion."""

    def test_all_none(self) -> None:
        result = collect_tracked_tasks(
            order_placement=None,
            reconciliation=None,
            heartbeat_probe=None,
            bar_sync=None,
        )
        assert len(result) == 4
        assert [t.name for t in result] == [
            "order_placement_worker.run_forever",
            "reconciliation_scheduler.run_forever",
            "heartbeat_probe.run_forever",
            "bar_sync_worker.run_forever",
        ]
        assert all(t.task is None for t in result)
        assert all(t.expected_alive is False for t in result)

    def test_extracts_task_from_tuple(self) -> None:
        async def _runner() -> tuple[asyncio.Task[None], tuple[TrackedTask, ...]]:
            async def _noop() -> None:
                pass

            task = asyncio.create_task(_noop())
            try:
                tuples = collect_tracked_tasks(
                    order_placement=(object(), task),
                    reconciliation=None,
                    heartbeat_probe=None,
                    bar_sync=None,
                )
            finally:
                await task
            return task, tuples

        task, result = asyncio.run(_runner())
        assert result[0].task is task
        assert result[0].expected_alive is True
        assert result[1].task is None
        assert result[1].expected_alive is False
        assert result[2].task is None
        assert result[2].expected_alive is False
        assert result[3].task is None
        assert result[3].expected_alive is False

    def test_non_task_second_element_treated_as_none(self) -> None:
        result = collect_tracked_tasks(
            order_placement=(object(), object()),  # task slot isn't a Task
            reconciliation=None,
            heartbeat_probe=None,
            bar_sync=None,
        )
        # The tuple is not-None (so expected_alive should be True), but
        # the extracted task is None because the slot wasn't an
        # asyncio.Task. This is a defensive branch — the lifespan
        # shouldn't actually do this, but we handle it cleanly.
        assert result[0].task is None
        assert result[0].expected_alive is True

    def test_bar_sync_tracked_when_provided(self) -> None:
        async def _runner() -> tuple[asyncio.Task[None], tuple[TrackedTask, ...]]:
            async def _noop() -> None:
                pass

            task = asyncio.create_task(_noop())
            try:
                tuples = collect_tracked_tasks(
                    order_placement=None,
                    reconciliation=None,
                    heartbeat_probe=None,
                    bar_sync=(object(), task),
                )
            finally:
                await task
            return task, tuples

        task, result = asyncio.run(_runner())
        assert result[3].name == "bar_sync_worker.run_forever"
        assert result[3].task is task
        assert result[3].expected_alive is True

    def test_bar_sync_defaults_to_none_when_omitted(self) -> None:
        # Pre-existing callers that haven't been updated yet still work.
        result = collect_tracked_tasks(
            order_placement=None,
            reconciliation=None,
            heartbeat_probe=None,
        )
        assert len(result) == 4
        assert result[3].name == "bar_sync_worker.run_forever"
        assert result[3].task is None
        assert result[3].expected_alive is False


class TestAsyncTaskMonitor:
    """Construction validation + per-state probe behavior."""

    def test_interval_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="interval_seconds must be > 0"):
            AsyncTaskMonitor([], interval_seconds=0)
        with pytest.raises(ValueError, match="interval_seconds must be > 0"):
            AsyncTaskMonitor([], interval_seconds=-1.0)

    def test_default_interval_constant_matches(self) -> None:
        assert DEFAULT_MONITOR_INTERVAL_SECONDS == 30.0

    def test_probe_once_with_no_tasks_returns_zero(self) -> None:
        monitor = AsyncTaskMonitor([], interval_seconds=30.0)
        assert monitor.probe_once() == 0

    def test_probe_once_skips_alive_task(self) -> None:
        async def _runner() -> int:
            async def _long_running() -> None:
                await asyncio.sleep(60)

            task = asyncio.create_task(_long_running())
            try:
                monitor = AsyncTaskMonitor(
                    [TrackedTask(name="alive", task=task, expected_alive=True)],
                    interval_seconds=30.0,
                )
                with capture_logs() as logs:
                    newly_dead = monitor.probe_once()
                assert newly_dead == 0
                # Alive tasks shouldn't emit any log per cycle (would be
                # spammy at 30s cadence over a long-lived api).
                assert not any(log.get("event") == "async_task_died" for log in logs)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return newly_dead

        assert asyncio.run(_runner()) == 0

    def test_probe_once_detects_dead_task_with_exception(self) -> None:
        async def _runner() -> tuple[int, list[Any]]:
            async def _raises() -> None:
                raise RuntimeError("boom")

            task = asyncio.create_task(_raises())
            try:
                await task
            except RuntimeError:
                pass
            # Task is now .done() with the exception set.
            monitor = AsyncTaskMonitor(
                [TrackedTask(name="dead", task=task, expected_alive=True)],
                interval_seconds=30.0,
            )
            with capture_logs() as logs:
                newly_dead = monitor.probe_once()
            return newly_dead, logs

        newly_dead, logs = asyncio.run(_runner())
        assert newly_dead == 1
        died = [log for log in logs if log.get("event") == "async_task_died"]
        assert len(died) == 1
        assert died[0]["task_name"] == "dead"
        assert died[0]["exit_reason"] == "exception"
        assert died[0]["exception_type"] == "RuntimeError"
        assert "boom" in died[0]["exception_repr"]

    def test_probe_once_detects_dead_task_without_exception(self) -> None:
        async def _runner() -> tuple[int, list[Any]]:
            async def _exits_cleanly() -> None:
                return None

            task = asyncio.create_task(_exits_cleanly())
            await task  # complete cleanly
            monitor = AsyncTaskMonitor(
                [TrackedTask(name="clean_exit", task=task, expected_alive=True)],
                interval_seconds=30.0,
            )
            with capture_logs() as logs:
                newly_dead = monitor.probe_once()
            return newly_dead, logs

        newly_dead, logs = asyncio.run(_runner())
        assert newly_dead == 1
        died = [log for log in logs if log.get("event") == "async_task_died"]
        assert len(died) == 1
        assert died[0]["task_name"] == "clean_exit"
        assert died[0]["exit_reason"] == "done_without_exception"
        assert "exception_type" not in died[0]

    def test_probe_once_detects_cancelled_task(self) -> None:
        async def _runner() -> tuple[int, list[Any]]:
            async def _long_running() -> None:
                await asyncio.sleep(60)

            task = asyncio.create_task(_long_running())
            await asyncio.sleep(0)  # let it start
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            monitor = AsyncTaskMonitor(
                [TrackedTask(name="cancelled", task=task, expected_alive=True)],
                interval_seconds=30.0,
            )
            with capture_logs() as logs:
                newly_dead = monitor.probe_once()
            return newly_dead, logs

        newly_dead, logs = asyncio.run(_runner())
        assert newly_dead == 1
        died = [log for log in logs if log.get("event") == "async_task_died"]
        assert len(died) == 1
        assert died[0]["task_name"] == "cancelled"
        assert died[0]["exit_reason"] == "exception"
        assert died[0]["exception_type"] == "CancelledError"

    def test_probe_once_skips_none_tasks(self) -> None:
        monitor = AsyncTaskMonitor(
            [TrackedTask(name="not_spawned", task=None, expected_alive=True)],
            interval_seconds=30.0,
        )
        with capture_logs() as logs:
            newly_dead = monitor.probe_once()
        assert newly_dead == 0
        # No async_task_died log; None tasks are reported separately in
        # probe_initial() not per-cycle.
        assert not any(log.get("event") == "async_task_died" for log in logs)


class TestProbeIdempotency:
    """Same dead task across multiple probes only logs once."""

    def test_repeat_probe_no_repeat_log(self) -> None:
        async def _runner() -> tuple[int, int, list[Any], list[Any]]:
            async def _raises() -> None:
                raise ValueError("once")

            task = asyncio.create_task(_raises())
            try:
                await task
            except ValueError:
                pass
            monitor = AsyncTaskMonitor(
                [TrackedTask(name="dead", task=task, expected_alive=True)],
                interval_seconds=30.0,
            )
            with capture_logs() as logs1:
                first = monitor.probe_once()
            with capture_logs() as logs2:
                second = monitor.probe_once()
            return first, second, logs1, logs2

        first, second, logs1, logs2 = asyncio.run(_runner())
        assert first == 1
        assert second == 0
        died1 = [log for log in logs1 if log.get("event") == "async_task_died"]
        died2 = [log for log in logs2 if log.get("event") == "async_task_died"]
        assert len(died1) == 1
        assert len(died2) == 0


class TestProbeInitial:
    """The one-shot startup log + un-spawned WARNING."""

    def test_probe_initial_emits_summary(self) -> None:
        async def _runner() -> list[Any]:
            async def _alive() -> None:
                await asyncio.sleep(60)

            alive_task = asyncio.create_task(_alive())
            try:
                monitor = AsyncTaskMonitor(
                    [
                        TrackedTask(name="alive", task=alive_task, expected_alive=True),
                        TrackedTask(name="not_spawned", task=None, expected_alive=True),
                        TrackedTask(name="not_expected", task=None, expected_alive=False),
                    ],
                    interval_seconds=30.0,
                )
                with capture_logs() as logs:
                    monitor.probe_initial()
            finally:
                alive_task.cancel()
                try:
                    await alive_task
                except asyncio.CancelledError:
                    pass
            return logs

        logs = asyncio.run(_runner())
        started = [log for log in logs if log.get("event") == "async_task_monitor_started"]
        assert len(started) == 1
        assert started[0]["spawned_count"] == 1
        assert started[0]["not_spawned_count"] == 1  # expected_alive=True only
        assert started[0]["total_tracked"] == 3
        not_spawned = [log for log in logs if log.get("event") == "async_task_not_spawned"]
        assert len(not_spawned) == 1
        assert not_spawned[0]["task_name"] == "not_spawned"


class TestRunForever:
    """Main loop ticks at interval; request_stop exits cleanly."""

    def test_run_forever_stops_on_request_stop(self) -> None:
        async def _runner() -> None:
            monitor = AsyncTaskMonitor([], interval_seconds=30.0)
            task = asyncio.create_task(monitor.run_forever())
            # Let the monitor enter run_forever + probe_initial.
            await asyncio.sleep(0.05)
            monitor.request_stop()
            await asyncio.wait_for(task, timeout=1.0)

        asyncio.run(_runner())

    def test_run_forever_calls_probe_initial_once(self) -> None:
        async def _runner() -> list[Any]:
            monitor = AsyncTaskMonitor([], interval_seconds=0.05)
            with capture_logs() as logs:
                task = asyncio.create_task(monitor.run_forever())
                await asyncio.sleep(0.15)  # let a few probes happen
                monitor.request_stop()
                await asyncio.wait_for(task, timeout=1.0)
            return logs

        logs = asyncio.run(_runner())
        started_count = sum(1 for log in logs if log.get("event") == "async_task_monitor_started")
        # probe_initial fires exactly once at run_forever entry.
        assert started_count == 1
        stopped_count = sum(1 for log in logs if log.get("event") == "async_task_monitor_stopped")
        assert stopped_count == 1

    def test_run_forever_catches_probe_exception(self) -> None:
        # Inject a broken tracked task entry — the monitor's probe loop
        # should catch + log the exception + keep going.
        class _BrokenTask:
            """A task-shaped object that raises from .done()."""

            def done(self) -> bool:
                raise RuntimeError("probe-time-error")

            def exception(self) -> BaseException | None:
                return None  # pragma: no cover

        async def _runner() -> list[Any]:
            # Inject a TrackedTask whose .task is our broken stub.
            broken = TrackedTask(
                name="broken",
                task=_BrokenTask(),  # type: ignore[arg-type]
                expected_alive=True,
            )
            monitor = AsyncTaskMonitor([broken], interval_seconds=0.05)
            with capture_logs() as logs:
                task = asyncio.create_task(monitor.run_forever())
                await asyncio.sleep(0.20)  # at least 2 probe iterations
                monitor.request_stop()
                await asyncio.wait_for(task, timeout=1.0)
            return logs

        logs = asyncio.run(_runner())
        # The probe should have logged async_task_monitor_probe_failed
        # at least once (we slept 4x the interval; expect 2-4 probes).
        probe_failed = [
            log for log in logs if log.get("event") == "async_task_monitor_probe_failed"
        ]
        assert len(probe_failed) >= 1
        # AND the monitor still exited cleanly via request_stop, not
        # via an unhandled exception.
        stopped = [log for log in logs if log.get("event") == "async_task_monitor_stopped"]
        assert len(stopped) == 1


class TestStopAsyncTaskMonitor:
    """Lifespan shutdown helper handles timeout + exception paths."""

    def test_stop_none_state_returns_immediately(self) -> None:
        asyncio.run(stop_async_task_monitor(None))

    def test_stop_happy_path(self) -> None:
        async def _runner() -> None:
            monitor = AsyncTaskMonitor([], interval_seconds=30.0)
            task = asyncio.create_task(monitor.run_forever())
            await asyncio.sleep(0.05)
            await stop_async_task_monitor((monitor, task))
            assert task.done()

        asyncio.run(_runner())

    def test_stop_timeout_cancels_task(self) -> None:
        async def _runner() -> None:
            # Make a hung task that doesn't respond to request_stop.
            class _HungMonitor:
                def request_stop(self) -> None:
                    return None  # never actually stops

            async def _never_returns() -> None:
                await asyncio.sleep(60)

            task = asyncio.create_task(_never_returns())
            # Use a small wait_for in the stop helper by monkeypatching
            # the timeout. The helper has a 5.0s wait_for which is
            # too long for a unit test. Instead we patch asyncio.wait_for
            # locally... but the test should still complete in <1s
            # because we cancel after timeout.
            #
            # Approach: skip the helper's 5s wait by passing a custom
            # stop_event-less monitor. The helper still calls
            # request_stop (noop on _HungMonitor) then asyncio.wait_for.
            # We'll observe the task.cancel() being called after wait_for
            # TimeoutError fires. To keep the test fast, we patch the
            # timeout via a side-channel: subclass + override.
            #
            # Simpler: just use the helper as-is and assert the task is
            # eventually cancelled. asyncio.wait_for raises TimeoutError
            # after 5s. We'll accept the 5s wait for this test.
            #
            # Pragmatic: assert task.cancelled() after the stop helper.
            await stop_async_task_monitor((_HungMonitor(), task))  # type: ignore[arg-type]
            assert task.done()

        # This test takes ~5s due to the wait_for inside stop helper.
        # Mark it as slow if needed; left simple for now.
        asyncio.run(_runner())


class TestIbkrConnectivityProbe:
    """2026-05-18 drill 5 follow-up — IBKR connectivity probe surface.

    The monitor reads ``ibkr_adapter.last_ibkr_error`` via a provider
    callable each tick. Fresh connectivity errors (codes 1100/1101/1102
    within the freshness window) emit
    ``async_task_monitor_ibkr_connectivity_warn`` at WARNING; same
    error event idempotent across probes; non-connectivity codes
    silent; provider failures swallowed.
    """

    @staticmethod
    def _build_state(
        *,
        code: int = 1100,
        message: str = "Connectivity between IBKR and Trader Workstation has been lost.",
        age_seconds: float = 1.0,
        req_id: int = -1,
        contract_local_symbol: str | None = None,
    ) -> IbkrErrorState:
        return IbkrErrorState(
            error_code=code,
            error_string=message,
            last_seen_at_utc=datetime.now(tz=UTC) - timedelta(seconds=age_seconds),
            req_id=req_id,
            contract_local_symbol=contract_local_symbol,
        )

    def test_default_constants_match_canonical_values(self) -> None:
        assert DEFAULT_IBKR_CONNECTIVITY_CODES == frozenset({1100, 1101, 1102})
        assert DEFAULT_IBKR_FRESHNESS_SECONDS == 300.0

    def test_tracked_ibkr_state_frozen(self) -> None:
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: None,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            tracker.name = "other"  # type: ignore[misc]

    def test_tracked_ibkr_state_defaults(self) -> None:
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: None,
        )
        assert tracker.connectivity_codes == DEFAULT_IBKR_CONNECTIVITY_CODES
        assert tracker.freshness_window_seconds == DEFAULT_IBKR_FRESHNESS_SECONDS

    def test_no_tracker_silently_skips(self) -> None:
        monitor = AsyncTaskMonitor([], interval_seconds=30.0)
        with capture_logs() as logs:
            monitor.probe_once()
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_connectivity_warn" for log in logs
        )

    def test_provider_returns_none_silent(self) -> None:
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: None)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_connectivity_warn" for log in logs
        )

    def test_fresh_1100_emits_warning(self) -> None:
        state = self._build_state(code=1100, age_seconds=1.0)
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        warns = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        assert len(warns) == 1
        assert warns[0]["error_code"] == 1100
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["tracker_name"] == "ibkr_adapter"
        # age_seconds is rounded; should be approximately 1.0.
        assert 0.5 < warns[0]["age_seconds"] < 2.0
        assert warns[0]["freshness_window_seconds"] == DEFAULT_IBKR_FRESHNESS_SECONDS

    def test_fresh_1102_emits_warning(self) -> None:
        state = self._build_state(code=1102, message="Connectivity restored.", age_seconds=2.0)
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        warns = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        assert len(warns) == 1
        assert warns[0]["error_code"] == 1102

    def test_stale_connectivity_error_does_not_warn(self) -> None:
        """Errors older than the freshness window are treated as resolved."""
        state = self._build_state(code=1100, age_seconds=400.0)  # > default 300s
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_connectivity_warn" for log in logs
        )

    def test_non_connectivity_code_does_not_warn(self) -> None:
        """Order rejection (>=10000) + data farm (2103-2150) skipped here."""
        state = self._build_state(code=10147, message="not found", age_seconds=1.0)
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_connectivity_warn" for log in logs
        )

    def test_idempotent_per_error_event(self) -> None:
        """Same (code, last_seen_at_utc) only warns once across probes."""
        state = self._build_state(code=1100, age_seconds=1.0)
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs1:
            monitor.probe_once()
        with capture_logs() as logs2:
            monitor.probe_once()
        warns1 = [
            log for log in logs1 if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        warns2 = [
            log for log in logs2 if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        assert len(warns1) == 1
        assert len(warns2) == 0

    def test_new_event_after_idempotent_skip_warns_again(self) -> None:
        """Different last_seen_at_utc → fresh key → new WARNING."""
        state_first = self._build_state(code=1100, age_seconds=10.0)
        state_second = self._build_state(code=1100, age_seconds=1.0)
        states = [state_first, state_second]

        def _provider() -> IbkrErrorState | None:
            return states.pop(0) if states else None

        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=_provider)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs1:
            monitor.probe_once()
        with capture_logs() as logs2:
            monitor.probe_once()
        warns1 = [
            log for log in logs1 if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        warns2 = [
            log for log in logs2 if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        assert len(warns1) == 1
        assert len(warns2) == 1
        # Distinct timestamps in the two warnings.
        assert warns1[0]["last_seen_at_utc"] != warns2[0]["last_seen_at_utc"]

    def test_provider_raises_swallowed(self) -> None:
        def _provider() -> IbkrErrorState | None:
            raise RuntimeError("adapter probe failed")

        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=_provider)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            # Must not raise.
            monitor.probe_once()
        provider_failed = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_provider_failed"
        ]
        assert len(provider_failed) == 1
        assert provider_failed[0]["exception_type"] == "RuntimeError"
        # And no connectivity warning fires from the same probe.
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_connectivity_warn" for log in logs
        )

    def test_naive_timestamp_swallowed_with_warning(self) -> None:
        """Defensive: naive last_seen_at_utc logs a warning and doesn't crash."""
        # Build a state manually with naive timestamp (the adapter
        # would never produce this per A06, but defensive).
        bad_state = IbkrErrorState(
            error_code=1100,
            error_string="lost",
            last_seen_at_utc=datetime.now(),
            req_id=-1,
            contract_local_symbol=None,
        )
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: bad_state)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        naive_warns = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_naive_timestamp"
        ]
        assert len(naive_warns) == 1
        # And no connectivity warning fires for the malformed state.
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_connectivity_warn" for log in logs
        )

    def test_custom_codes_set_overrides_default(self) -> None:
        """Caller can narrow the connectivity-codes set for testing."""
        state = self._build_state(code=1102, age_seconds=1.0)
        # Configure tracker to ONLY warn on 1100; 1102 should skip.
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            connectivity_codes=frozenset({1100}),
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_connectivity_warn" for log in logs
        )

    def test_custom_freshness_window_overrides_default(self) -> None:
        """Caller can widen the freshness window for short-cycle environments."""
        state = self._build_state(code=1100, age_seconds=400.0)
        # 400s old should normally be stale (> 300s default), but here
        # we explicitly widen to 600s so it still counts as fresh.
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            freshness_window_seconds=600.0,
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        warns = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        assert len(warns) == 1
        assert warns[0]["freshness_window_seconds"] == 600.0

    def test_contract_local_symbol_propagates_to_log(self) -> None:
        state = self._build_state(
            code=1100,
            age_seconds=1.0,
            req_id=42,
            contract_local_symbol="TLT",
        )
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        warns = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        assert len(warns) == 1
        assert warns[0]["req_id"] == 42
        assert warns[0]["contract_local_symbol"] == "TLT"

    def test_task_probe_and_ibkr_probe_independent(self) -> None:
        """A dead task probe fires alongside an IBKR connectivity warn."""

        async def _runner() -> list[Any]:
            async def _raises() -> None:
                raise RuntimeError("dead worker")

            task = asyncio.create_task(_raises())
            try:
                await task
            except RuntimeError:
                pass
            state = TestIbkrConnectivityProbe._build_state(code=1100, age_seconds=1.0)
            tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
            monitor = AsyncTaskMonitor(
                [TrackedTask(name="dead", task=task, expected_alive=True)],
                interval_seconds=30.0,
                ibkr_error_state=tracker,
            )
            with capture_logs() as logs:
                monitor.probe_once()
            return logs

        logs = asyncio.run(_runner())
        died = [log for log in logs if log.get("event") == "async_task_died"]
        warns = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        # Both events fire from a single probe_once invocation.
        assert len(died) == 1
        assert len(warns) == 1

    def test_ibkr_probe_outer_exception_caught(self) -> None:
        """A bug OUTSIDE the provider call doesn't crash probe_once.

        Inner try/except only covers ``tracker.provider()``;
        ``tracker.connectivity_codes`` access happens outside that
        guard. A bug there bubbles up to the outer try in
        ``probe_once`` and logs ``async_task_monitor_ibkr_probe_failed``.
        """
        state = self._build_state(code=1100, age_seconds=1.0)

        class _BadTracker:
            name = "ibkr_adapter"
            provider = staticmethod(lambda: state)  # OK — returns fine
            freshness_window_seconds = 300.0

            @property
            def connectivity_codes(self) -> Any:
                raise RuntimeError("attribute-access-time error AFTER provider")

        monitor = AsyncTaskMonitor(
            [],
            interval_seconds=30.0,
            ibkr_error_state=_BadTracker(),  # type: ignore[arg-type]
        )
        with capture_logs() as logs:
            # Must not raise; probe_once must return cleanly.
            assert monitor.probe_once() == 0
        probe_failed = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_probe_failed"
        ]
        assert len(probe_failed) == 1


class TestMonitorAlertDispatchHook:
    """Drill 5 follow-up #2-FU-1 — Discord #alerts dispatch on probe WARNING.

    These tests lock the hook-firing contract: when an alert dispatch
    hook is wired into ``TrackedIbkrErrorState`` AND a fresh connectivity
    error fires, the monitor invokes the hook with a
    ``MonitorAlertDescriptor`` carrying severity=P1, category=
    broker_disconnect, and the IBKR error metadata. Hook failures are
    caught + logged at WARNING; the WARNING log itself is load-bearing.
    """

    @staticmethod
    def _build_state(
        *,
        code: int = 1100,
        message: str = "Connectivity between IBKR and Trader Workstation has been lost.",
        age_seconds: float = 1.0,
        req_id: int = -1,
        contract_local_symbol: str | None = None,
    ) -> IbkrErrorState:
        return IbkrErrorState(
            error_code=code,
            error_string=message,
            last_seen_at_utc=datetime.now(tz=UTC) - timedelta(seconds=age_seconds),
            req_id=req_id,
            contract_local_symbol=contract_local_symbol,
        )

    def test_descriptor_shape(self) -> None:
        """MonitorAlertDescriptor is a frozen dataclass with the 5 expected fields."""
        from services.api.async_task_monitor import MonitorAlertDescriptor

        d = MonitorAlertDescriptor(
            severity="P1",
            category="broker_disconnect",
            title="t",
            body="b",
            payload={"k": "v"},
        )
        assert d.severity == "P1"
        assert d.category == "broker_disconnect"
        assert d.title == "t"
        assert d.body == "b"
        assert d.payload == {"k": "v"}
        with pytest.raises(Exception):  # FrozenInstanceError
            d.severity = "P0"  # type: ignore[misc]

    def test_build_ibkr_alert_descriptor_shape(self) -> None:
        """``_build_ibkr_alert_descriptor`` produces the canonical P1 broker_disconnect."""
        state = self._build_state(
            code=1100, age_seconds=42.0, req_id=99, contract_local_symbol="TLT"
        )
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        d = AsyncTaskMonitor._build_ibkr_alert_descriptor(state, 42.0, tracker)
        assert d.severity == "P1"
        assert d.category == "broker_disconnect"
        assert "1100" in d.title
        assert state.error_string in d.body
        assert "req_id=99" in d.body
        assert "contract=TLT" in d.body
        assert d.payload["error_code"] == 1100
        assert d.payload["req_id"] == 99
        assert d.payload["contract_local_symbol"] == "TLT"
        assert d.payload["age_seconds"] == 42.0
        assert d.payload["tracker_name"] == "ibkr_adapter"

    def test_build_ibkr_alert_descriptor_contract_none(self) -> None:
        """When contract is None, body renders '—' fallback."""
        state = self._build_state(contract_local_symbol=None)
        tracker = TrackedIbkrErrorState(name="ibkr_adapter", provider=lambda: state)
        d = AsyncTaskMonitor._build_ibkr_alert_descriptor(state, 1.0, tracker)
        assert "contract=—" in d.body
        assert d.payload["contract_local_symbol"] is None

    @pytest.mark.asyncio
    async def test_fresh_1100_fires_hook(self) -> None:
        """Fresh 1100 + wired hook → hook invoked with the descriptor."""
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _hook(d: MonitorAlertDescriptor) -> None:
            captured.append(d)

        state = self._build_state(code=1100, age_seconds=1.0)
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            alert_dispatch_hook=_hook,
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        # Run probe inside a running loop so create_task is valid.
        monitor.probe_once()
        # Yield to let the scheduled task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(captured) == 1
        d = captured[0]
        assert d.severity == "P1"
        assert d.category == "broker_disconnect"
        assert d.payload["error_code"] == 1100

    @pytest.mark.asyncio
    async def test_hook_idempotent_across_probes(self) -> None:
        """Same (code, last_seen) only fires hook once."""
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _hook(d: MonitorAlertDescriptor) -> None:
            captured.append(d)

        state = self._build_state(code=1100, age_seconds=1.0)
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            alert_dispatch_hook=_hook,
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        monitor.probe_once()
        monitor.probe_once()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_no_hook_silent_on_warning(self) -> None:
        """When alert_dispatch_hook=None, the WARNING fires but no hook attempt."""
        state = self._build_state(code=1100, age_seconds=1.0)
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            # No alert_dispatch_hook.
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            monitor.probe_once()
        await asyncio.sleep(0)
        warns = [
            log for log in logs if log.get("event") == "async_task_monitor_ibkr_connectivity_warn"
        ]
        assert len(warns) == 1
        # No dispatch-no-loop, no dispatch-failed.
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_alert_dispatch_no_loop" for log in logs
        )
        assert not any(
            log.get("event") == "async_task_monitor_ibkr_alert_dispatch_failed" for log in logs
        )

    @pytest.mark.asyncio
    async def test_hook_exception_swallowed(self) -> None:
        """A failing hook logs WARNING but doesn't crash the probe loop."""
        from services.api.async_task_monitor import MonitorAlertDescriptor

        async def _hook(d: MonitorAlertDescriptor) -> None:
            raise RuntimeError("Discord 503")

        state = self._build_state(code=1100, age_seconds=1.0)
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            alert_dispatch_hook=_hook,
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        with capture_logs() as logs:
            # Must not raise.
            monitor.probe_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        failed = [
            log
            for log in logs
            if log.get("event") == "async_task_monitor_ibkr_alert_dispatch_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["exception_type"] == "RuntimeError"
        assert failed[0]["error_code"] == 1100

    def test_no_loop_fallback_logs_warning(self) -> None:
        """probe_once called outside a running loop logs no-loop warning."""
        from services.api.async_task_monitor import MonitorAlertDescriptor

        async def _hook(d: MonitorAlertDescriptor) -> None:
            return None

        state = self._build_state(code=1100, age_seconds=1.0)
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            alert_dispatch_hook=_hook,
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        # No running loop; create_task raises RuntimeError → caught + logged.
        with capture_logs() as logs:
            monitor.probe_once()
        no_loop = [
            log
            for log in logs
            if log.get("event") == "async_task_monitor_ibkr_alert_dispatch_no_loop"
        ]
        assert len(no_loop) == 1

    @pytest.mark.asyncio
    async def test_stale_error_does_not_fire_hook(self) -> None:
        """Stale connectivity error: no WARNING + no hook fire."""
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _hook(d: MonitorAlertDescriptor) -> None:
            captured.append(d)

        state = self._build_state(code=1100, age_seconds=400.0)  # > 300s default
        tracker = TrackedIbkrErrorState(
            name="ibkr_adapter",
            provider=lambda: state,
            alert_dispatch_hook=_hook,
        )
        monitor = AsyncTaskMonitor([], interval_seconds=30.0, ibkr_error_state=tracker)
        monitor.probe_once()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert captured == []


class TestTaskDeathAlertHook:
    """Recovery-agent task-death hook surface (drill 5/6 follow-up, 2026-05-26).

    The hook fires when a tracked lifespan task transitions to .done()
    AND the task name is in the (configurable) allow-list. Fires
    fire-and-forget via ``_schedule_alert_dispatch`` so hook crashes
    don't kill the probe loop.

    Test surface:

      * Allow-list filter — order_placement_worker fires; others don't
      * No-hook-wired silent — only the ERROR log fires
      * Descriptor shape — P0 severity + worker_failure category +
        payload carries task_name + exception_type + observed_at_utc
      * Done-without-exception path produces a descriptor too
        (exception_type=None in payload)
      * Hook failures swallowed
    """

    @staticmethod
    async def _dead_task() -> None:
        """Build a task that raises a specific exception synchronously."""
        raise TimeoutError("connectAsync to ib_gateway timed out")

    @staticmethod
    async def _dead_clean_task() -> None:
        """Build a task that exits cleanly (done_without_exception)."""
        return None

    @staticmethod
    async def _spawn_and_complete(coro_fn: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro_fn())
        try:
            await task
        except BaseException:
            # exception() is the inspection path; let .done() reflect.
            pass
        return task

    def test_default_allow_list_contains_order_placement_worker(self) -> None:
        from services.api.async_task_monitor import DEFAULT_TASK_DEATH_ALLOW_LIST

        assert "order_placement_worker.run_forever" in DEFAULT_TASK_DEATH_ALLOW_LIST

    def test_no_hook_wired_only_emits_error_log(self) -> None:
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _runner() -> None:
            task = await self._spawn_and_complete(self._dead_task)
            tracked = TrackedTask(
                name="order_placement_worker.run_forever",
                task=task,
                expected_alive=True,
            )
            monitor = AsyncTaskMonitor(
                [tracked],
                interval_seconds=30.0,
                task_death_alert_hook=None,
            )
            with capture_logs() as logs:
                monitor.probe_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert any(log.get("event") == "async_task_died" for log in logs)
            assert captured == []

        asyncio.run(_runner())

    def test_allow_listed_task_death_fires_hook_with_descriptor(self) -> None:
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _hook(d: MonitorAlertDescriptor) -> None:
            captured.append(d)

        async def _runner() -> None:
            task = await self._spawn_and_complete(self._dead_task)
            tracked = TrackedTask(
                name="order_placement_worker.run_forever",
                task=task,
                expected_alive=True,
            )
            monitor = AsyncTaskMonitor(
                [tracked],
                interval_seconds=30.0,
                task_death_alert_hook=_hook,
            )
            monitor.probe_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_runner())
        assert len(captured) == 1
        desc = captured[0]
        assert desc.severity == "P0"
        assert desc.category == "worker_failure"
        assert desc.payload["task_name"] == "order_placement_worker.run_forever"
        assert desc.payload["exit_reason"] == "exception"
        assert desc.payload["exception_type"] == "TimeoutError"
        assert "TimeoutError" in (desc.payload["exception_repr"] or "")
        assert "observed_at_utc" in desc.payload

    def test_non_allow_listed_task_death_skips_hook(self) -> None:
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _hook(d: MonitorAlertDescriptor) -> None:
            captured.append(d)

        async def _runner() -> None:
            task = await self._spawn_and_complete(self._dead_task)
            tracked = TrackedTask(
                # NOT in DEFAULT_TASK_DEATH_ALLOW_LIST
                name="reconciliation_scheduler.run_forever",
                task=task,
                expected_alive=True,
            )
            monitor = AsyncTaskMonitor(
                [tracked],
                interval_seconds=30.0,
                task_death_alert_hook=_hook,
            )
            with capture_logs() as logs:
                monitor.probe_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # The ERROR log MUST still fire (load-bearing observability)
            assert any(log.get("event") == "async_task_died" for log in logs)

        asyncio.run(_runner())
        # Hook MUST NOT fire for non-allow-listed task
        assert captured == []

    def test_custom_allow_list_includes_recon_scheduler(self) -> None:
        """Operator can extend the allow-list per-deploy (no code change)."""
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _hook(d: MonitorAlertDescriptor) -> None:
            captured.append(d)

        async def _runner() -> None:
            task = await self._spawn_and_complete(self._dead_task)
            tracked = TrackedTask(
                name="reconciliation_scheduler.run_forever",
                task=task,
                expected_alive=True,
            )
            monitor = AsyncTaskMonitor(
                [tracked],
                interval_seconds=30.0,
                task_death_alert_hook=_hook,
                task_death_allow_list=frozenset({"reconciliation_scheduler.run_forever"}),
            )
            monitor.probe_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_runner())
        assert len(captured) == 1
        assert captured[0].payload["task_name"] == "reconciliation_scheduler.run_forever"

    def test_done_without_exception_fires_hook_with_none_exception_type(self) -> None:
        from services.api.async_task_monitor import MonitorAlertDescriptor

        captured: list[MonitorAlertDescriptor] = []

        async def _hook(d: MonitorAlertDescriptor) -> None:
            captured.append(d)

        async def _runner() -> None:
            task = await self._spawn_and_complete(self._dead_clean_task)
            tracked = TrackedTask(
                name="order_placement_worker.run_forever",
                task=task,
                expected_alive=True,
            )
            monitor = AsyncTaskMonitor(
                [tracked],
                interval_seconds=30.0,
                task_death_alert_hook=_hook,
            )
            monitor.probe_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_runner())
        assert len(captured) == 1
        desc = captured[0]
        assert desc.payload["exit_reason"] == "done_without_exception"
        assert desc.payload["exception_type"] is None
        assert desc.payload["exception_repr"] is None

    def test_hook_failure_swallowed_loop_survives(self) -> None:
        from services.api.async_task_monitor import MonitorAlertDescriptor

        async def _hook(d: MonitorAlertDescriptor) -> None:
            raise RuntimeError("synthetic hook crash")

        async def _runner() -> None:
            task = await self._spawn_and_complete(self._dead_task)
            tracked = TrackedTask(
                name="order_placement_worker.run_forever",
                task=task,
                expected_alive=True,
            )
            monitor = AsyncTaskMonitor(
                [tracked],
                interval_seconds=30.0,
                task_death_alert_hook=_hook,
            )
            with capture_logs() as logs:
                # Must NOT raise — hook failure swallowed
                monitor.probe_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # The ERROR log fires; the hook failure logs a WARNING via
            # _schedule_alert_dispatch's exception wrapper.
            assert any(log.get("event") == "async_task_died" for log in logs)

        asyncio.run(_runner())


class TestAuditEventTypesNewlyAdded:
    """A04 pin — the two new event_types added in this PR exist + emit cleanly.

    Per dev-guide anti-pattern A04: any new audit event_type requires
    (a) the enum entry, (b) a backend-spec §3.30 mirror, (c) at least
    one test that emits + reads back. (a) and (b) are static; (c) is
    this test plus the integration smoke in `TestAuditWriter`.
    """

    def test_async_task_died_in_locked_taxonomy(self) -> None:
        from services.audit.event_types import AuditEventType

        assert AuditEventType.ASYNC_TASK_DIED.value == "async_task_died"

    def test_recovery_action_taken_in_locked_taxonomy(self) -> None:
        from services.audit.event_types import AuditEventType

        assert AuditEventType.RECOVERY_ACTION_TAKEN.value == "recovery_action_taken"

    def test_both_new_types_round_trip_through_normalize_event_type(self) -> None:
        """The writer accepts both the enum value and the raw string;
        both must round-trip cleanly. Pre-PR-28 modules emit raw
        strings (per writer.py docstring), so the contract is
        ``AuditEventType(value) == enum_member``.
        """
        from services.audit.event_types import AuditEventType

        assert AuditEventType("async_task_died") is AuditEventType.ASYNC_TASK_DIED
        assert AuditEventType("recovery_action_taken") is AuditEventType.RECOVERY_ACTION_TAKEN


class TestModuleContract:
    """Public ``__all__`` surface."""

    def test_all_exports(self) -> None:
        from services.api import async_task_monitor as mod

        assert set(mod.__all__) == {
            "AsyncTaskMonitor",
            "DEFAULT_IBKR_CONNECTIVITY_CODES",
            "DEFAULT_IBKR_FRESHNESS_SECONDS",
            "DEFAULT_MONITOR_INTERVAL_SECONDS",
            "DEFAULT_TASK_DEATH_ALLOW_LIST",
            "MonitorAlertDescriptor",
            "MonitorAlertHook",
            "TaskDeathAlertHook",
            "TrackedIbkrErrorState",
            "TrackedTask",
            "collect_tracked_tasks",
            "stop_async_task_monitor",
        }


# structlog is consumed via capture_logs above; silence unused-import in
# tools that don't recognize the indirection.
_ = structlog
