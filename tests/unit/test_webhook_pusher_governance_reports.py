"""Unit tests for ``services/webhook_pusher/governance_reports.py``.

Period math (month/quarter boundaries incl. year rollover), fire-window
scheduling (before slot / catch-up / handled / past window), fire_once
behavior (monthly push; quarter-start months push BOTH reports; dedupe
keys; once-per-period; fetch-failure leaves the period retryable; gates
payload best-effort), and the stop contract. No live HTTP — the httpx
client is a MagicMock whose GET dispatches on URL and whose POST
returns 204 (the webhook leg), mirroring the cycle-digest test harness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.webhook_pusher.event_pushes import EventChannel
from services.webhook_pusher.governance_reports import (
    GovernanceReportConfig,
    GovernanceReportScheduler,
    month_period_for_fire,
    plan_monthly_report_push,
    plan_quarterly_report_push,
    quarter_period_for_fire,
)

_CONFIG = GovernanceReportConfig(
    api_base_url="http://api:8000",
    api_bearer_token="bearer-token",
    reports_webhook_url="https://discord.invalid/webhook/reports",
)


def _report_payload(start: str, end: str) -> dict[str, Any]:
    return {
        "period_start_utc": start,
        "period_end_utc": end,
        "trades": {"closed_count": 0, "winners": 0, "losers": 0},
        "fees": {"fill_count": 0},
        "funding_sources": [],
        "funding_note": "telemetry",
        "sweeps": {"completed_count": 0, "failed_count": 0},
        "capital_events": [],
    }


def _mock_http(*, report_status: int = 200, gates_status: int = 200) -> MagicMock:
    """GET dispatches on URL; POST (webhook) returns 204."""
    client = MagicMock(spec=httpx.AsyncClient)

    async def _get(url: str, **kwargs: Any) -> httpx.Response:
        if "/api/system/gates" in url:
            return httpx.Response(
                gates_status,
                json={"gates": [], "days_live": 9, "phase_a_min_days": 45},
            )
        if "/api/system/governance-report" in url:
            if report_status != 200:
                return httpx.Response(report_status, json={"error_code": "X", "message": "boom"})
            # Echo the requested period back so dedupe/label assertions
            # can see which window was fetched.
            start = url.split("period_start=")[1].split("&")[0]
            end = url.split("period_end=")[1]
            return httpx.Response(200, json=_report_payload(start, end))
        raise AssertionError(f"unexpected GET {url}")

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(return_value=httpx.Response(204))
    return client


class TestPeriodMath:
    def test_month_period_mid_year(self) -> None:
        assert month_period_for_fire(date(2026, 8, 1)) == (
            date(2026, 7, 1),
            date(2026, 8, 1),
            "2026-07",
        )

    def test_month_period_year_rollover(self) -> None:
        assert month_period_for_fire(date(2027, 1, 1)) == (
            date(2026, 12, 1),
            date(2027, 1, 1),
            "2026-12",
        )

    def test_quarter_period_q2(self) -> None:
        assert quarter_period_for_fire(date(2026, 7, 1)) == (
            date(2026, 4, 1),
            date(2026, 7, 1),
            "2026-Q2",
        )

    def test_quarter_period_year_rollover(self) -> None:
        assert quarter_period_for_fire(date(2027, 1, 1)) == (
            date(2026, 10, 1),
            date(2027, 1, 1),
            "2026-Q4",
        )


class TestPlanners:
    def test_monthly_plan_targets_reports_channel(self) -> None:
        plan = plan_monthly_report_push(
            _report_payload("2026-07-01", "2026-08-01"),
            now_utc=datetime(2026, 8, 1, 0, 30, tzinfo=UTC),
            label="2026-07",
        )
        assert plan.channel is EventChannel.REPORTS
        assert plan.dedupe_key == "governance_monthly:2026-07"
        assert "Monthly governance report" in plan.embed["title"]

    def test_quarterly_plan_targets_reports_channel(self) -> None:
        plan = plan_quarterly_report_push(
            _report_payload("2026-04-01", "2026-07-01"),
            now_utc=datetime(2026, 7, 1, 0, 30, tzinfo=UTC),
            label="2026-Q2",
        )
        assert plan.channel is EventChannel.REPORTS
        assert plan.dedupe_key == "governance_quarterly:2026-Q2"
        assert "Quarterly governance report" in plan.embed["title"]


class TestNextFireUtc:
    def _scheduler(self) -> GovernanceReportScheduler:
        return GovernanceReportScheduler(_CONFIG)

    def test_before_slot_returns_this_months_first(self) -> None:
        s = self._scheduler()
        now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        # Mid-month past the window: next fire is September 1st.
        assert s.next_fire_utc(now) == datetime(2026, 9, 1, 0, 30, tzinfo=UTC)

    def test_before_slot_on_the_first(self) -> None:
        s = self._scheduler()
        now = datetime(2026, 8, 1, 0, 5, tzinfo=UTC)
        assert s.next_fire_utc(now) == datetime(2026, 8, 1, 0, 30, tzinfo=UTC)

    def test_within_catchup_window_unhandled_is_due_now(self) -> None:
        s = self._scheduler()
        now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)  # ~36h past the slot
        assert s.next_fire_utc(now) == datetime(2026, 8, 1, 0, 30, tzinfo=UTC)

    def test_handled_rolls_to_next_month(self) -> None:
        s = self._scheduler()
        s._handled_monthly.add("2026-07")
        now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
        assert s.next_fire_utc(now) == datetime(2026, 9, 1, 0, 30, tzinfo=UTC)

    def test_past_catchup_window_rolls_to_next_month(self) -> None:
        s = self._scheduler()
        now = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)  # 72h past the slot
        assert s.next_fire_utc(now) == datetime(2026, 9, 1, 0, 30, tzinfo=UTC)

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            self._scheduler().next_fire_utc(datetime(2026, 8, 1))


class TestFireOnce:
    async def test_non_quarter_month_pushes_monthly_only(self) -> None:
        client = _mock_http()
        s = GovernanceReportScheduler(
            _CONFIG, clock=lambda: datetime(2026, 8, 1, 0, 30, tzinfo=UTC)
        )
        outcome = await s.fire_once(http_client=client)
        assert outcome == "pushed"
        # One webhook POST (monthly only — August 1st is not a quarter start).
        client.post.assert_awaited_once()
        post_args = client.post.await_args
        assert post_args.args[0] == _CONFIG.reports_webhook_url
        embeds = post_args.kwargs["json"]["embeds"]
        assert len(embeds) == 1
        assert embeds[0]["title"] == "Monthly governance report — 2026-07"
        # Report fetched for the July window.
        report_urls = [
            c.args[0] for c in client.get.await_args_list if "governance-report" in c.args[0]
        ]
        assert report_urls == [
            "http://api:8000/api/system/governance-report"
            "?period_start=2026-07-01&period_end=2026-08-01"
        ]

    async def test_quarter_start_month_pushes_both(self) -> None:
        client = _mock_http()
        s = GovernanceReportScheduler(
            _CONFIG, clock=lambda: datetime(2026, 7, 1, 0, 30, tzinfo=UTC)
        )
        outcome = await s.fire_once(http_client=client)
        assert outcome == "pushed"
        assert client.post.await_count == 2  # monthly (June) + quarterly (Q2)
        report_urls = [
            c.args[0] for c in client.get.await_args_list if "governance-report" in c.args[0]
        ]
        assert (
            "http://api:8000/api/system/governance-report"
            "?period_start=2026-06-01&period_end=2026-07-01" in report_urls
        )
        assert (
            "http://api:8000/api/system/governance-report"
            "?period_start=2026-04-01&period_end=2026-07-01" in report_urls
        )

    async def test_once_per_period(self) -> None:
        client = _mock_http()
        s = GovernanceReportScheduler(
            _CONFIG, clock=lambda: datetime(2026, 8, 1, 0, 30, tzinfo=UTC)
        )
        assert await s.fire_once(http_client=client) == "pushed"
        assert await s.fire_once(http_client=client) == "already_handled"
        assert client.post.await_count == 1

    async def test_fetch_failure_leaves_period_retryable(self) -> None:
        client = _mock_http(report_status=503)
        s = GovernanceReportScheduler(
            _CONFIG, clock=lambda: datetime(2026, 8, 1, 0, 30, tzinfo=UTC)
        )
        assert await s.fire_once(http_client=client) == "fetch_failed"
        assert client.post.await_count == 0
        # Retry with a healthy api succeeds — the period was NOT marked.
        healthy = _mock_http()
        assert await s.fire_once(http_client=healthy) == "pushed"

    async def test_gates_failure_is_best_effort(self) -> None:
        client = _mock_http(gates_status=500)
        s = GovernanceReportScheduler(
            _CONFIG, clock=lambda: datetime(2026, 8, 1, 0, 30, tzinfo=UTC)
        )
        assert await s.fire_once(http_client=client) == "pushed"
        assert client.post.await_count == 1

    async def test_naive_now_rejected(self) -> None:
        s = GovernanceReportScheduler(_CONFIG)
        with pytest.raises(ValueError, match="tz-aware"):
            await s.fire_once(now_utc=datetime(2026, 8, 1), http_client=_mock_http())


class TestRunForeverStops:
    async def test_request_stop_exits_promptly_while_waiting(self) -> None:
        s = GovernanceReportScheduler(
            _CONFIG, clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        )
        task = asyncio.create_task(s.run_forever())
        await asyncio.sleep(0.05)
        s.request_stop()
        await asyncio.wait_for(task, timeout=2.0)
