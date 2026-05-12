"""Unit tests for ``services.reconciliation.scheduler``.

Worker-PR-3b (post-pivot 2026-05-12). Tests the pure-policy decision
helper (``should_fire_now``) + the long-lived scheduler with an
injected clock so tests don't wait wall-clock time.

A06 enforced — every datetime tz-aware UTC; naive datetimes raise.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from services.reconciliation.scheduler import (
    DEFAULT_EOD_RECON_TIME_ET,
    DEFAULT_TICK_INTERVAL_SECONDS,
    ReconciliationScheduler,
    current_session_date_et,
    should_fire_now,
)

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# should_fire_now
# ---------------------------------------------------------------------------


class TestShouldFireNow:
    def test_before_eod_time_returns_false(self) -> None:
        # 18:00 ET on 2026-05-12 = 22:00 UTC (EDT offset -4)
        now_utc = datetime(2026, 5, 12, 22, 0, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_et=time(18, 30),
                last_fired_session_date_et=None,
            )
            is False
        )

    def test_at_eod_time_returns_true(self) -> None:
        # 18:30 ET = 22:30 UTC (EDT)
        now_utc = datetime(2026, 5, 12, 22, 30, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_et=time(18, 30),
                last_fired_session_date_et=None,
            )
            is True
        )

    def test_after_eod_time_returns_true(self) -> None:
        now_utc = datetime(2026, 5, 12, 23, 0, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_et=time(18, 30),
                last_fired_session_date_et=None,
            )
            is True
        )

    def test_already_fired_today_returns_false(self) -> None:
        """Re-checking after the cycle fired for today's date is a no-op."""
        now_utc = datetime(2026, 5, 12, 23, 0, tzinfo=UTC)
        today_et = date(2026, 5, 12)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_et=time(18, 30),
                last_fired_session_date_et=today_et,
            )
            is False
        )

    def test_fires_again_next_day(self) -> None:
        """After midnight ET, a new session date — fires again."""
        # 19:00 ET on 2026-05-13 = 23:00 UTC (EDT)
        now_utc = datetime(2026, 5, 13, 23, 0, tzinfo=UTC)
        yesterday_et = date(2026, 5, 12)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_et=time(18, 30),
                last_fired_session_date_et=yesterday_et,
            )
            is True
        )

    def test_dst_transition_respected(self) -> None:
        """Around DST cutover, the ET wall-clock controls — not UTC."""
        # 2025-03-09 02:00 local is DST start. 18:30 ET on 2025-03-09:
        # before the transition we're at EST (UTC-5); after we're EDT (UTC-4).
        # 2025-03-09 18:30 EDT = 2025-03-09 22:30 UTC.
        now_utc = datetime(2025, 3, 9, 22, 30, tzinfo=UTC)
        assert (
            should_fire_now(
                now_utc=now_utc,
                eod_recon_time_et=time(18, 30),
                last_fired_session_date_et=None,
            )
            is True
        )

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            should_fire_now(
                now_utc=datetime(2026, 5, 12, 22, 0),  # naive
                eod_recon_time_et=time(18, 30),
                last_fired_session_date_et=None,
            )


class TestCurrentSessionDateET:
    def test_pre_midnight_utc_returns_same_day_in_et(self) -> None:
        # 23:00 UTC = 19:00 EDT on 2026-05-12
        now_utc = datetime(2026, 5, 12, 23, 0, tzinfo=UTC)
        assert current_session_date_et(now_utc) == date(2026, 5, 12)

    def test_post_midnight_utc_returns_prior_day_in_et(self) -> None:
        # 02:00 UTC on 2026-05-13 = 22:00 EDT on 2026-05-12
        now_utc = datetime(2026, 5, 13, 2, 0, tzinfo=UTC)
        assert current_session_date_et(now_utc) == date(2026, 5, 12)

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError):
            current_session_date_et(datetime(2026, 5, 12, 12, 0))


# ---------------------------------------------------------------------------
# ReconciliationScheduler
# ---------------------------------------------------------------------------


class TestReconciliationSchedulerMaybeFire:
    @pytest.mark.asyncio
    async def test_does_not_fire_before_eod(self) -> None:
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 5, 12, 21, 0, tzinfo=UTC)  # 17:00 ET
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        fired = await sched.maybe_fire()
        assert fired is False
        assert called == []

    @pytest.mark.asyncio
    async def test_fires_at_eod(self) -> None:
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 5, 12, 22, 30, tzinfo=UTC)  # 18:30 ET
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        fired = await sched.maybe_fire()
        assert fired is True
        assert called == [date(2026, 5, 12)]

    @pytest.mark.asyncio
    async def test_does_not_refire_same_day(self) -> None:
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 5, 12, 22, 30, tzinfo=UTC)
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        await sched.maybe_fire()
        await sched.maybe_fire()  # second tick same day
        assert called == [date(2026, 5, 12)]

    @pytest.mark.asyncio
    async def test_callback_exception_is_swallowed(self) -> None:
        async def cb(session_date: date) -> None:
            raise RuntimeError("FlexQuery down")

        now_utc = datetime(2026, 5, 12, 22, 30, tzinfo=UTC)
        sched = ReconciliationScheduler(callback=cb, clock=lambda: now_utc)
        fired = await sched.maybe_fire()
        assert fired is True
        # Last-fired marker still bumps so we don't retry-storm on next tick.
        assert sched.snapshot().last_fired_session_date_et == date(2026, 5, 12)

    @pytest.mark.asyncio
    async def test_initial_fired_date_skips_first_day(self) -> None:
        """Operator can pre-seed initial_fired_date to skip refire after restart."""
        called: list[date] = []

        async def cb(session_date: date) -> None:
            called.append(session_date)

        now_utc = datetime(2026, 5, 12, 22, 30, tzinfo=UTC)
        sched = ReconciliationScheduler(
            callback=cb,
            clock=lambda: now_utc,
            initial_fired_date=date(2026, 5, 12),
        )
        await sched.maybe_fire()
        assert called == []

    @pytest.mark.asyncio
    async def test_next_day_fires_again(self) -> None:
        called: list[date] = []
        clock_value = [datetime(2026, 5, 12, 22, 30, tzinfo=UTC)]

        async def cb(session_date: date) -> None:
            called.append(session_date)

        sched = ReconciliationScheduler(callback=cb, clock=lambda: clock_value[0])
        await sched.maybe_fire()  # day 1
        # advance to next day after EOD
        clock_value[0] = clock_value[0] + timedelta(days=1)
        await sched.maybe_fire()  # day 2
        assert called == [date(2026, 5, 12), date(2026, 5, 13)]


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

        clock_value = [datetime(2026, 5, 12, 21, 0, tzinfo=UTC)]  # pre-EOD

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
            clock_value[0] = datetime(2026, 5, 12, 22, 30, tzinfo=UTC)
            # Give it a tick to fire
            await asyncio.sleep(0.1)
            sched.request_stop()

        await asyncio.gather(
            sched.run_forever(),
            advance_then_stop(),
        )
        assert called == [date(2026, 5, 12)]


class TestDefaults:
    def test_default_eod_is_18_30_et(self) -> None:
        assert DEFAULT_EOD_RECON_TIME_ET == time(18, 30)

    def test_default_tick_interval_is_60s(self) -> None:
        assert DEFAULT_TICK_INTERVAL_SECONDS == 60.0
