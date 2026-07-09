"""Unit tests for ``services.reconciliation.scheduler``.

Worker-PR-3b (post-pivot 2026-05-12); re-anchored onto UTC at 00:15 in
crypto-pivot C0 §3.5. Tests the pure-policy decision helper
(``should_fire_now``) + the long-lived scheduler with an injected
clock so tests don't wait wall-clock time.

A06 enforced — every datetime tz-aware UTC; naive datetimes raise.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from services.reconciliation.scheduler import (
    DEFAULT_EOD_RECON_TIME_UTC,
    DEFAULT_TICK_INTERVAL_SECONDS,
    ReconciliationScheduler,
    current_session_date_utc,
    should_fire_now,
)

# ---------------------------------------------------------------------------
# should_fire_now
# ---------------------------------------------------------------------------


class TestShouldFireNow:
    def test_before_eod_time_returns_false(self) -> None:
        now_utc = datetime(2026, 7, 10, 0, 10, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=None,
            )
            is False
        )

    def test_at_eod_time_returns_true(self) -> None:
        now_utc = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=None,
            )
            is True
        )

    def test_after_eod_time_returns_true(self) -> None:
        now_utc = datetime(2026, 7, 10, 13, 0, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=None,
            )
            is True
        )

    def test_already_fired_today_returns_false(self) -> None:
        """Re-checking after the cycle fired for today's date is a no-op."""
        now_utc = datetime(2026, 7, 10, 23, 0, tzinfo=UTC)
        today_utc = date(2026, 7, 10)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=today_utc,
            )
            is False
        )

    def test_fires_again_next_day(self) -> None:
        """After midnight UTC, a new session date — fires again."""
        now_utc = datetime(2026, 7, 11, 0, 15, tzinfo=UTC)
        yesterday_utc = date(2026, 7, 10)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=yesterday_utc,
            )
            is True
        )

    def test_non_utc_tz_input_normalized_to_utc(self) -> None:
        """Any tz-aware input works; the comparison is UTC wall-clock.

        20:15 ET on 2026-07-09 == 00:15 UTC on 2026-07-10 (EDT) — fires,
        and the session date is the UTC day (the 10th), not the ET day.
        """
        et = ZoneInfo("America/New_York")
        now_et = datetime(2026, 7, 9, 20, 15, tzinfo=et)
        assert (
            should_fire_now(
                now_utc=now_et,
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=None,
            )
            is True
        )
        assert current_session_date_utc(now_et) == date(2026, 7, 10)

    def test_no_dst_sensitivity(self) -> None:
        """UTC anchoring: the US DST cutover does not move the fire time."""
        # 2026-03-08 is the US spring-forward date; 00:15 UTC still fires.
        now_utc = datetime(2026, 3, 8, 0, 15, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=None,
            )
            is True
        )

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            should_fire_now(
                now_utc=datetime(2026, 7, 10, 0, 20),  # naive
                eod_recon_time_utc=time(0, 15),
                last_fired_session_date_utc=None,
            )


class TestCurrentSessionDateUtc:
    def test_returns_utc_calendar_date(self) -> None:
        now_utc = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)
        assert current_session_date_utc(now_utc) == date(2026, 7, 10)

    def test_non_utc_input_converted(self) -> None:
        # 22:00 EDT on 2026-07-09 = 02:00 UTC on 2026-07-10.
        et = ZoneInfo("America/New_York")
        now_et = datetime(2026, 7, 9, 22, 0, tzinfo=et)
        assert current_session_date_utc(now_et) == date(2026, 7, 10)

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError):
            current_session_date_utc(datetime(2026, 7, 10, 12, 0))


# ---------------------------------------------------------------------------
# ReconciliationScheduler
# ---------------------------------------------------------------------------


class TestReconciliationSchedulerMaybeFire:
    @pytest.mark.asyncio
    async def test_does_not_fire_before_eod(self) -> None:
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 7, 10, 0, 10, tzinfo=UTC)  # pre-00:15 UTC
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        fired = await sched.maybe_fire()
        assert fired is False
        assert called == []

    @pytest.mark.asyncio
    async def test_fires_at_eod(self) -> None:
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        fired = await sched.maybe_fire()
        assert fired is True
        assert called == [date(2026, 7, 10)]

    @pytest.mark.asyncio
    async def test_does_not_refire_same_day(self) -> None:
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        await sched.maybe_fire()
        await sched.maybe_fire()  # second tick same day
        assert called == [date(2026, 7, 10)]

    @pytest.mark.asyncio
    async def test_callback_exception_is_swallowed(self) -> None:
        async def cb(session_date: date) -> None:
            raise RuntimeError("venue down")

        now_utc = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        fired = await sched.maybe_fire()
        assert fired is True
        # Last-fired marker still bumps so we don't retry-storm on next tick.
        assert sched.snapshot().last_fired_session_date_utc == date(2026, 7, 10)

    @pytest.mark.asyncio
    async def test_initial_fired_date_skips_first_day(self) -> None:
        """Operator can pre-seed initial_fired_date to skip refire after restart."""
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)
        sched = ReconciliationScheduler(
            callback=cb,
            clock=lambda: now_utc,
            initial_fired_date=date(2026, 7, 10),
        )
        await sched.maybe_fire()
        assert called == []

    @pytest.mark.asyncio
    async def test_next_day_fires_again(self) -> None:
        called: list[date] = []
        clock_value = [datetime(2026, 7, 10, 0, 15, tzinfo=UTC)]

        async def cb(session_date: date) -> None:
            called.append(session_date)

        sched = ReconciliationScheduler(callback=cb, clock=lambda: clock_value[0])
        await sched.maybe_fire()  # day 1
        # advance to next day after EOD
        clock_value[0] = clock_value[0] + timedelta(days=1)
        await sched.maybe_fire()  # day 2
        assert called == [date(2026, 7, 10), date(2026, 7, 11)]


class TestReconciliationSchedulerRunForever:
    @pytest.mark.asyncio
    async def test_run_forever_exits_on_stop(self) -> None:
        """request_stop() makes run_forever return within tick_interval."""

        async def cb(session_date: date) -> None:
            pass

        sched = ReconciliationScheduler(
            callback=cb,
            tick_interval_seconds=0.05,
        )
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, sched.request_stop)
        await asyncio.wait_for(sched.run_forever(), timeout=2.0)
        assert sched.snapshot().is_running is False

    @pytest.mark.asyncio
    async def test_run_forever_fires_callback_when_clock_passes_eod(self) -> None:
        """Driven by an advancing fake clock, run_forever fires on schedule."""
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        clock_value = [datetime(2026, 7, 10, 0, 10, tzinfo=UTC)]  # pre-EOD

        def fake_clock() -> datetime:
            return clock_value[0]

        sched = ReconciliationScheduler(
            callback=cb,
            tick_interval_seconds=0.02,
            clock=fake_clock,
        )

        async def advance_then_stop() -> None:
            # Let the scheduler tick a couple of times before EOD
            await asyncio.sleep(0.05)
            # Now jump the clock past EOD
            clock_value[0] = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)
            # Give it a tick to fire
            await asyncio.sleep(0.1)
            sched.request_stop()

        await asyncio.gather(
            sched.run_forever(),
            advance_then_stop(),
        )
        assert called == [date(2026, 7, 10)]


class TestDefaults:
    def test_default_eod_is_00_15_utc(self) -> None:
        # Delta spec §3.5: 00:15 UTC, after the 00:05 UTC daily decision.
        assert DEFAULT_EOD_RECON_TIME_UTC == time(0, 15)

    def test_default_tick_interval_is_60s(self) -> None:
        assert DEFAULT_TICK_INTERVAL_SECONDS == 60.0
