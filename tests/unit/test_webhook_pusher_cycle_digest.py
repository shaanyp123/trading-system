"""Unit tests for the §3.8 cycle digest — builder, planner, scheduler.

Covers ``services/webhook_pusher/cycle_digest.py`` (pure builder +
planner, shared with the bot's ``/cycle``) and
``services/webhook_pusher/cycle_digest_scheduler.py`` (00:10 UTC push
loop). No live HTTP — the httpx client is a MagicMock with AsyncMock
``get``/``post`` returning real ``httpx.Response`` objects.

Task-locked semantics under test:

  * ANNOUNCE-ONLY — the embed dict never carries ``components``.
  * fire-once-per-day — a second ``fire_once`` on the same UTC day is a
    no-op ("already_handled"; exactly one webhook POST).
  * skip-when-no-decision — ``plan_cycle_digest_push`` returns None and
    the scheduler marks the day handled without POSTing.
  * graceful no-decision / worker-down digests (the embed says so).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.webhook_pusher.cycle_digest import (
    COLOR_DIGEST_FAIL,
    COLOR_DIGEST_OK,
    COLOR_DIGEST_WARN,
    DIGEST_FIRE_HOUR_UTC,
    DIGEST_FIRE_MINUTE_UTC,
    build_cycle_digest_embed,
    plan_cycle_digest_push,
)
from services.webhook_pusher.cycle_digest_scheduler import (
    DIGEST_CATCHUP_WINDOW_S,
    CycleDigestConfig,
    CycleDigestScheduler,
)
from services.webhook_pusher.event_pushes import EventChannel

NOW = datetime(2026, 7, 9, 0, 10, tzinfo=UTC)

WEBHOOK_URL = "https://discord.example.com/api/webhooks/123/abc"


def _payload_completed() -> dict[str, Any]:
    """Mirror of the ``GET /api/system/cycle`` wire shape (§3.7)."""
    return {
        "decision": {
            "decision_date": "2026-07-09",
            "decided_at_utc": "2026-07-09T00:05:12Z",
            "status": "completed",
            "env": "paper",
            "equity_usd": "2000.00",
            "weekly_halved": False,
            "m_combined": "1.0",
            "skip_reason": None,
            "assets": [
                {
                    "asset": "BTC",
                    "trend_score": "0.421337",
                    "prior_contracts": 0,
                    "engine_target": 2,
                    "final_target": 2,
                    "action": "buy",
                    "est_cost_usd": "1.23",
                    "mark": "64000.50",
                    "stop_level": "58100.00",
                },
                {
                    "asset": "ETH",
                    "trend_score": "-0.05",
                    "prior_contracts": 0,
                    "engine_target": 0,
                    "final_target": 0,
                    "action": "hold",
                    "est_cost_usd": None,
                    "mark": "3400.10",
                    "stop_level": None,
                },
            ],
        },
        "worker": {
            "risk_loop_heartbeat_utc": "2026-07-09T00:09:58Z",
            "seconds_since_heartbeat": 2,
            "heartbeat_fresh": True,
            "risk_loop_tick_count": 2880,
            "marks_stale": False,
            "last_decision_date": "2026-07-09",
        },
        "next_friday_close_utc": "2026-07-10T21:00:00Z",
        "server_now": "2026-07-09T00:10:00Z",
    }


def _payload_no_decision() -> dict[str, Any]:
    return {
        "decision": None,
        "worker": None,
        "next_friday_close_utc": "2026-07-10T21:00:00Z",
        "server_now": "2026-07-09T00:10:00Z",
    }


def _field(embed: dict[str, Any], name: str) -> str:
    for f in embed["fields"]:
        if f["name"] == name:
            assert isinstance(f["value"], str)
            return f["value"]
    raise AssertionError(f"field {name!r} not in embed: {[f['name'] for f in embed['fields']]}")


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------


class TestBuildCycleDigestEmbed:
    def test_completed_decision_green_with_asset_lines(self) -> None:
        embed = build_cycle_digest_embed(_payload_completed(), now_utc=NOW)
        assert embed["title"] == "Daily decision — 2026-07-09 · completed"
        assert embed["color"] == COLOR_DIGEST_OK
        btc = _field(embed, "BTC")
        assert "trend 0.421337" in btc
        assert "target 2 (prior 0)" in btc
        assert "BUY" in btc
        assert "est cost $1.23" in btc
        assert "mark $64000.50" in btc
        assert "stop $58100.00" in btc
        eth = _field(embed, "ETH")
        assert "HOLD" in eth
        assert "est cost n/a" in eth

    def test_account_and_risk_loop_and_close_fields(self) -> None:
        embed = build_cycle_digest_embed(_payload_completed(), now_utc=NOW)
        account = _field(embed, "Account")
        assert "equity $2000.00" in account
        assert "m_combined 1.0" in account
        assert "weekly-halved no" in account
        assert "paper" in account
        risk = _field(embed, "Risk loop")
        assert "heartbeat 2 s ago (fresh)" in risk
        assert "marks OK" in risk
        # Friday 2026-07-10 21:00Z = 17:00 EDT
        assert _field(embed, "Next CDE Friday close") == "2026-07-10 17:00 ET"

    def test_announce_only_no_components(self) -> None:
        """Dev-guide §1.5 locked: no buttons / approval affordances."""
        embed = build_cycle_digest_embed(_payload_completed(), now_utc=NOW)
        assert "components" not in embed
        plan = plan_cycle_digest_push(_payload_completed(), now_utc=NOW)
        assert plan is not None
        assert "components" not in plan.embed

    def test_no_decision_says_so_amber(self) -> None:
        embed = build_cycle_digest_embed(_payload_no_decision(), now_utc=NOW)
        assert embed["title"] == "Daily decision — none recorded yet"
        assert embed["color"] == COLOR_DIGEST_WARN
        assert "No strategy decision has been recorded" in embed["description"]
        assert "strategy worker has not run" in _field(embed, "Risk loop")

    def test_stale_decision_flags_missing_today(self) -> None:
        """Worker-down case: last decision is yesterday's → amber MISSING."""
        payload = _payload_completed()
        payload["decision"]["decision_date"] = "2026-07-08"
        payload["worker"]["heartbeat_fresh"] = False
        payload["worker"]["seconds_since_heartbeat"] = 86000
        embed = build_cycle_digest_embed(payload, now_utc=NOW)
        assert embed["title"] == "Daily decision — MISSING for 2026-07-09"
        assert embed["color"] == COLOR_DIGEST_WARN
        assert "Last decision: 2026-07-08 (completed)" in embed["description"]
        assert "STALE — worker may be down" in _field(embed, "Risk loop")

    def test_skipped_decision_carries_skip_reason(self) -> None:
        payload = _payload_completed()
        payload["decision"]["status"] = "skipped_risk_state"
        payload["decision"]["skip_reason"] = "risk_state=HALT_NEW does not permit dispatch"
        payload["decision"]["assets"] = []
        embed = build_cycle_digest_embed(payload, now_utc=NOW)
        assert embed["color"] == COLOR_DIGEST_WARN
        assert "HALT_NEW" in _field(embed, "Skip reason")

    def test_failed_decision_red(self) -> None:
        payload = _payload_completed()
        payload["decision"]["status"] = "failed"
        embed = build_cycle_digest_embed(payload, now_utc=NOW)
        assert embed["color"] == COLOR_DIGEST_FAIL

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            build_cycle_digest_embed(_payload_completed(), now_utc=datetime(2026, 7, 9))

    def test_garbage_payload_never_raises(self) -> None:
        embed = build_cycle_digest_embed({}, now_utc=NOW)
        assert embed["color"] == COLOR_DIGEST_WARN
        embed = build_cycle_digest_embed(
            {"decision": {"assets": [None, 42], "weekly_halved": None}, "worker": "junk"},
            now_utc=NOW,
        )
        assert isinstance(embed["fields"], list)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class TestPlanCycleDigestPush:
    def test_completed_decision_targets_daily_brief(self) -> None:
        plan = plan_cycle_digest_push(_payload_completed(), now_utc=NOW)
        assert plan is not None
        assert plan.channel is EventChannel.DAILY_BRIEF
        assert plan.dedupe_key == "cycle_digest:2026-07-09:2026-07-09"
        assert plan.embed["title"].startswith("Daily decision — 2026-07-09")

    def test_no_decision_skips(self) -> None:
        assert plan_cycle_digest_push(_payload_no_decision(), now_utc=NOW) is None

    def test_stale_decision_still_pushes(self) -> None:
        payload = _payload_completed()
        payload["decision"]["decision_date"] = "2026-07-08"
        plan = plan_cycle_digest_push(payload, now_utc=NOW)
        assert plan is not None
        assert plan.dedupe_key == "cycle_digest:2026-07-09:2026-07-08"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _config() -> CycleDigestConfig:
    return CycleDigestConfig(
        api_base_url="http://api:8000",
        api_bearer_token="test-bearer",
        daily_brief_webhook_url=WEBHOOK_URL,
    )


def _mock_http(
    *,
    cycle_payload: dict[str, Any] | None,
    get_error: Exception | None = None,
    get_status: int = 200,
) -> MagicMock:
    """httpx.AsyncClient stand-in: GET /api/system/cycle + webhook POST."""
    client = MagicMock(spec=httpx.AsyncClient)
    if get_error is not None:
        client.get = AsyncMock(side_effect=get_error)
    else:
        client.get = AsyncMock(return_value=httpx.Response(get_status, json=cycle_payload or {}))
    client.post = AsyncMock(return_value=httpx.Response(204))
    return client


class TestNextFireUtc:
    def _scheduler(self) -> CycleDigestScheduler:
        return CycleDigestScheduler(_config())

    def test_before_fire_time_returns_today(self) -> None:
        now = datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
        expected = datetime(2026, 7, 9, DIGEST_FIRE_HOUR_UTC, DIGEST_FIRE_MINUTE_UTC, tzinfo=UTC)
        assert self._scheduler().next_fire_utc(now) == expected

    def test_within_catchup_window_unhandled_is_due_now(self) -> None:
        now = datetime(2026, 7, 9, 0, 30, tzinfo=UTC)
        fire_today = datetime(2026, 7, 9, 0, 10, tzinfo=UTC)
        assert self._scheduler().next_fire_utc(now) == fire_today

    def test_past_catchup_window_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 7, 9, 0, 10, tzinfo=UTC) + timedelta(seconds=DIGEST_CATCHUP_WINDOW_S)
        assert self._scheduler().next_fire_utc(now) == datetime(2026, 7, 10, 0, 10, tzinfo=UTC)

    def test_handled_today_rolls_to_tomorrow(self) -> None:
        scheduler = self._scheduler()
        scheduler._last_handled_date = datetime(2026, 7, 9, tzinfo=UTC).date()
        now = datetime(2026, 7, 9, 0, 30, tzinfo=UTC)
        assert scheduler.next_fire_utc(now) == datetime(2026, 7, 10, 0, 10, tzinfo=UTC)

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            self._scheduler().next_fire_utc(datetime(2026, 7, 9))


class TestFireOnce:
    async def test_pushes_digest_to_daily_brief_webhook(self) -> None:
        client = _mock_http(cycle_payload=_payload_completed())
        scheduler = CycleDigestScheduler(_config())
        outcome = await scheduler.fire_once(now_utc=NOW, http_client=client)
        assert outcome == "pushed"
        client.get.assert_awaited_once()
        get_args = client.get.await_args
        assert get_args.args[0] == "http://api:8000/api/system/cycle"
        assert get_args.kwargs["headers"]["Authorization"] == "Bearer test-bearer"
        client.post.assert_awaited_once()
        post_args = client.post.await_args
        assert post_args.args[0] == WEBHOOK_URL
        embeds = post_args.kwargs["json"]["embeds"]
        assert len(embeds) == 1
        assert embeds[0]["title"].startswith("Daily decision — 2026-07-09")
        assert scheduler.last_handled_date == NOW.date()

    async def test_fires_once_per_day(self) -> None:
        client = _mock_http(cycle_payload=_payload_completed())
        scheduler = CycleDigestScheduler(_config())
        first = await scheduler.fire_once(now_utc=NOW, http_client=client)
        second = await scheduler.fire_once(now_utc=NOW + timedelta(minutes=5), http_client=client)
        assert first == "pushed"
        assert second == "already_handled"
        client.post.assert_awaited_once()  # exactly one webhook POST

    async def test_next_day_fires_again(self) -> None:
        client = _mock_http(cycle_payload=_payload_completed())
        scheduler = CycleDigestScheduler(_config())
        assert await scheduler.fire_once(now_utc=NOW, http_client=client) == "pushed"
        tomorrow = NOW + timedelta(days=1)
        assert await scheduler.fire_once(now_utc=tomorrow, http_client=client) == "pushed"
        assert client.post.await_count == 2

    async def test_skips_when_no_decision(self) -> None:
        client = _mock_http(cycle_payload=_payload_no_decision())
        scheduler = CycleDigestScheduler(_config())
        outcome = await scheduler.fire_once(now_utc=NOW, http_client=client)
        assert outcome == "skipped_no_decision"
        client.post.assert_not_awaited()
        # skip still consumes the day — no retry-POST loop at 00:10:30
        assert scheduler.last_handled_date == NOW.date()
        assert (
            await scheduler.fire_once(now_utc=NOW + timedelta(minutes=1), http_client=client)
            == "already_handled"
        )

    async def test_fetch_network_error_leaves_day_unhandled(self) -> None:
        client = _mock_http(cycle_payload=None, get_error=httpx.ConnectError("boom"))
        scheduler = CycleDigestScheduler(_config())
        outcome = await scheduler.fire_once(now_utc=NOW, http_client=client)
        assert outcome == "fetch_failed"
        client.post.assert_not_awaited()
        assert scheduler.last_handled_date is None  # retryable

    async def test_fetch_http_error_leaves_day_unhandled(self) -> None:
        client = _mock_http(cycle_payload={}, get_status=503)
        scheduler = CycleDigestScheduler(_config())
        assert await scheduler.fire_once(now_utc=NOW, http_client=client) == "fetch_failed"
        assert scheduler.last_handled_date is None

    async def test_naive_now_rejected(self) -> None:
        scheduler = CycleDigestScheduler(_config())
        with pytest.raises(ValueError, match="tz-aware"):
            await scheduler.fire_once(now_utc=datetime(2026, 7, 9))


class TestRunForeverStops:
    async def test_request_stop_exits_promptly_while_waiting(self) -> None:
        import asyncio

        # Clock pinned well before the fire time → run_forever parks on the
        # stop event; request_stop must unblock it.
        scheduler = CycleDigestScheduler(
            _config(), clock=lambda: datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
        )
        task = asyncio.create_task(scheduler.run_forever())
        await asyncio.sleep(0.05)
        assert not task.done()
        scheduler.request_stop()
        await asyncio.wait_for(task, timeout=1.0)
