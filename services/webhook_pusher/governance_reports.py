"""services/webhook_pusher/governance_reports.py — #reports monthly/quarterly pushes.

Delta spec §3.8 (C2 deliverable): the scheduled leg of the monthly +
quarterly governance reports. Mirrors the ``/cycle`` digest split
exactly — the stdlib-only embed builders live in
:mod:`services.discord_shared.governance_digest`; this module holds the
``EventPushPlan``-typed planners + the fire-window scheduler (it
imports httpx + the sqlalchemy-touching dispatcher, so it cannot ship
in the Discord bot image).

**Cadence.** Fires on the **1st of each month at 00:30 UTC** (after the
00:05 decision / 00:10 digest / 00:15 recon / 00:20 capture / 00:25
dormant-sweep slots), reporting the JUST-ENDED month via
``GET /api/system/governance-report``. On quarter-start months
(Jan/Apr/Jul/Oct) a SECOND push follows: the quarterly report over the
just-ended quarter, with the §4 revalidation reminder + governance
checklist.

**Once-per-period discipline** (cycle-digest pattern, period-sized):
an in-memory guard keyed by period label; a catch-up window of
:data:`REPORT_CATCHUP_WINDOW_S` (48 h) past the fire instant so a
container down at 00:30 on the 1st still reports when it comes back —
a monthly report is worth a late delivery, unlike a stale daily digest.
Past the window the period is missed (logged); the worst restart race
duplicates one informational embed, same accepted residual as the
cycle digest.

**Missing #reports webhook = logged skip, never a crash**: the runner
(`__main__.py`) only starts this scheduler when
``WEBHOOK_PUSHER_REPORTS_WEBHOOK_URL`` is set (secrets
``discord.webhook_urls.reports`` — OPTIONAL in the entrypoint, unlike
the fail-closed signals/fills/daily_brief URLs, because the channel
webhook may not exist yet on the VPS).

A02 N/A — §2.3 hot-fix whitelist. [A06]: every datetime tz-aware UTC.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal

import httpx
import structlog

from services.discord_shared.gates_digest import format_gates_summary_line
from services.discord_shared.governance_digest import (
    build_monthly_report_embed,
    build_quarterly_report_embed,
)
from services.webhook_pusher.dispatcher import dispatch_event_push
from services.webhook_pusher.event_pushes import EventChannel, EventPushPlan

log = structlog.get_logger()

#: Fire on the 1st of the month at 00:30 UTC (a quiet pipeline slot).
REPORT_FIRE_HOUR_UTC: Final[int] = 0
REPORT_FIRE_MINUTE_UTC: Final[int] = 30

#: A monthly/quarterly report is worth a late delivery: fire up to 48 h
#: past the slot before declaring the period missed.
REPORT_CATCHUP_WINDOW_S: Final[int] = 48 * 3600

#: Transient api-fetch retry policy (same shape as the cycle digest).
FETCH_MAX_ATTEMPTS: Final[int] = 3
FETCH_RETRY_SECONDS: Final[float] = 60.0

#: Months whose 1st starts a new quarter (the fire covers the quarter
#: that just ENDED).
QUARTER_START_MONTHS: Final[frozenset[int]] = frozenset({1, 4, 7, 10})

FireOutcome = Literal["pushed", "partial", "already_handled", "fetch_failed", "skipped_no_url"]


# ---------------------------------------------------------------------------
# Pure period math
# ---------------------------------------------------------------------------


def month_period_for_fire(fire_date: date) -> tuple[date, date, str]:
    """(period_start, period_end_exclusive, label) for the month that
    ended at ``fire_date`` (the 1st). E.g. fired 2026-08-01 → July."""
    end = fire_date.replace(day=1)
    prev_last_day = end - timedelta(days=1)
    start = prev_last_day.replace(day=1)
    return start, end, start.strftime("%Y-%m")


def quarter_period_for_fire(fire_date: date) -> tuple[date, date, str]:
    """(period_start, period_end_exclusive, label) for the quarter that
    ended at ``fire_date``. E.g. fired 2026-07-01 → 2026-Q2 (Apr-Jun)."""
    end = fire_date.replace(day=1)
    prev = end - timedelta(days=1)
    quarter = (prev.month - 1) // 3 + 1
    start = date(prev.year, 3 * (quarter - 1) + 1, 1)
    return start, end, f"{prev.year}-Q{quarter}"


def plan_monthly_report_push(
    report: dict[str, Any],
    *,
    now_utc: datetime,
    label: str,
    gates_payload: dict[str, Any] | None = None,
) -> EventPushPlan:
    """Report payload → #reports monthly push plan."""
    gates_summary = format_gates_summary_line(gates_payload) if gates_payload else None
    embed = build_monthly_report_embed(report, now_utc=now_utc, gates_summary=gates_summary)
    return EventPushPlan(
        channel=EventChannel.REPORTS,
        embed=embed,
        dedupe_key=f"governance_monthly:{label}",
    )


def plan_quarterly_report_push(
    report: dict[str, Any],
    *,
    now_utc: datetime,
    label: str,
    gates_payload: dict[str, Any] | None = None,
) -> EventPushPlan:
    """Report payload → #reports quarterly push plan."""
    gates_summary = format_gates_summary_line(gates_payload) if gates_payload else None
    embed = build_quarterly_report_embed(
        report, now_utc=now_utc, gates_summary=gates_summary, quarter_label=label
    )
    return EventPushPlan(
        channel=EventChannel.REPORTS,
        embed=embed,
        dedupe_key=f"governance_quarterly:{label}",
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GovernanceReportConfig:
    """Runtime configuration (env-sourced by ``__main__.py``)."""

    api_base_url: str
    api_bearer_token: str
    reports_webhook_url: str
    request_timeout_seconds: float = 30.0


class GovernanceReportScheduler:
    """1st-of-month 00:30 UTC fire loop for the #reports governance pushes.

    Lifecycle mirrors :class:`~services.webhook_pusher.cycle_digest_scheduler.CycleDigestScheduler`.
    """

    def __init__(
        self,
        config: GovernanceReportConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._injected_client = http_client
        self._stop_event = asyncio.Event()
        #: Once-per-period guards ("2026-07" / "2026-Q2").
        self._handled_monthly: set[str] = set()
        self._handled_quarterly: set[str] = set()

    # -- lifecycle -------------------------------------------------------

    def request_stop(self) -> None:
        self._stop_event.set()

    # -- schedule math ---------------------------------------------------

    def next_fire_utc(self, now: datetime) -> datetime:
        """Next due fire instant: this month's 1st 00:30 while inside its
        catch-up window and unhandled; else the next month's 1st 00:30."""
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware UTC; got naive datetime")
        now = now.astimezone(UTC)
        this_slot = now.replace(
            day=1,
            hour=REPORT_FIRE_HOUR_UTC,
            minute=REPORT_FIRE_MINUTE_UTC,
            second=0,
            microsecond=0,
        )
        if now < this_slot:
            return this_slot
        in_catchup = (now - this_slot).total_seconds() < REPORT_CATCHUP_WINDOW_S
        _, _, month_label = month_period_for_fire(this_slot.date())
        if in_catchup and month_label not in self._handled_monthly:
            return this_slot  # due now
        next_month = (this_slot + timedelta(days=32)).replace(
            day=1,
            hour=REPORT_FIRE_HOUR_UTC,
            minute=REPORT_FIRE_MINUTE_UTC,
            second=0,
            microsecond=0,
        )
        return next_month

    # -- one firing ------------------------------------------------------

    async def fire_once(
        self,
        *,
        now_utc: datetime | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> FireOutcome:
        """Fetch → plan → dispatch the due monthly (and quarterly) report."""
        now = now_utc if now_utc is not None else self._clock()
        if now.tzinfo is None:
            raise ValueError("now_utc must be tz-aware UTC; got naive datetime")
        fire_date = now.astimezone(UTC).date().replace(day=1)
        m_start, m_end, m_label = month_period_for_fire(fire_date)
        quarterly_due = fire_date.month in QUARTER_START_MONTHS
        q_label: str | None = None
        if quarterly_due:
            _, _, q_label = quarter_period_for_fire(fire_date)
            if q_label in self._handled_quarterly:
                quarterly_due = False
        if m_label in self._handled_monthly and not quarterly_due:
            return "already_handled"

        client = http_client or self._injected_client
        # Gates payload is best-effort on both report shapes.
        gates_payload = await self._fetch_json("/api/system/gates", client)

        pushed_any = False
        failed_any = False

        if m_label not in self._handled_monthly:
            report = await self._fetch_report(m_start, m_end, client)
            if report is None:
                failed_any = True
            else:
                await self._dispatch(
                    plan_monthly_report_push(
                        report, now_utc=now, label=m_label, gates_payload=gates_payload
                    ),
                    client,
                )
                self._handled_monthly.add(m_label)
                pushed_any = True
                log.info("governance_monthly_report_pushed", period=m_label)

        if quarterly_due and q_label is not None:
            q_start, q_end, _ = quarter_period_for_fire(fire_date)
            report = await self._fetch_report(q_start, q_end, client)
            if report is None:
                failed_any = True
            else:
                await self._dispatch(
                    plan_quarterly_report_push(
                        report, now_utc=now, label=q_label, gates_payload=gates_payload
                    ),
                    client,
                )
                self._handled_quarterly.add(q_label)
                pushed_any = True
                log.info("governance_quarterly_report_pushed", period=q_label)

        if failed_any and not pushed_any:
            return "fetch_failed"
        if failed_any:
            return "partial"
        return "pushed"

    async def _dispatch(self, plan: EventPushPlan, client: httpx.AsyncClient | None) -> None:
        if client is not None:
            result = await dispatch_event_push(
                plan, http_client=client, webhook_url=self._config.reports_webhook_url
            )
        else:
            async with httpx.AsyncClient() as owned:
                result = await dispatch_event_push(
                    plan, http_client=owned, webhook_url=self._config.reports_webhook_url
                )
        log.info(
            "governance_report_dispatched",
            dedupe_key=plan.dedupe_key,
            delivery_status=result.status.value,
            http_status=result.http_status,
        )

    async def _fetch_report(
        self, start: date, end: date, client: httpx.AsyncClient | None
    ) -> dict[str, Any] | None:
        return await self._fetch_json(
            f"/api/system/governance-report?period_start={start.isoformat()}"
            f"&period_end={end.isoformat()}",
            client,
        )

    async def _fetch_json(
        self, path: str, http_client: httpx.AsyncClient | None
    ) -> dict[str, Any] | None:
        url = f"{self._config.api_base_url.rstrip('/')}{path}"
        headers = {
            "Authorization": f"Bearer {self._config.api_bearer_token}",
            "User-Agent": "trading-webhook-pusher/0.1",
        }
        client = http_client or self._injected_client
        try:
            if client is not None:
                response = await client.get(
                    url, headers=headers, timeout=self._config.request_timeout_seconds
                )
            else:
                async with httpx.AsyncClient() as owned:
                    response = await owned.get(
                        url, headers=headers, timeout=self._config.request_timeout_seconds
                    )
        except httpx.HTTPError as exc:
            log.warning("governance_report_fetch_network_error", path=path, error=str(exc))
            return None
        if response.status_code != 200:
            log.warning(
                "governance_report_fetch_http_error",
                path=path,
                http_status=response.status_code,
            )
            return None
        try:
            body = response.json()
        except ValueError:
            log.warning("governance_report_fetch_non_json", path=path)
            return None
        if not isinstance(body, dict):
            log.warning(
                "governance_report_fetch_unexpected_shape",
                path=path,
                body_type=type(body).__name__,
            )
            return None
        return body

    # -- main loop -------------------------------------------------------

    async def run_forever(self) -> None:
        """Sleep-until-1st-00:30 loop; stop-responsive; never raises out."""
        log.info(
            "governance_report_scheduler_started",
            fire_slot=f"1st of month {REPORT_FIRE_HOUR_UTC:02d}:{REPORT_FIRE_MINUTE_UTC:02d} UTC",
        )
        while not self._stop_event.is_set():
            now = self._clock()
            target = self.next_fire_utc(now)
            wait_s = (target - now).total_seconds()
            if wait_s > 0:
                if await self._wait_or_stop(min(wait_s, 6 * 3600)):
                    break
                continue  # recompute after (partial) sleep
            await self._fire_with_retries()
        log.info("governance_report_scheduler_stopped")

    async def _fire_with_retries(self) -> None:
        fire_date = self._clock().astimezone(UTC).date().replace(day=1)
        _, _, m_label = month_period_for_fire(fire_date)
        outcome: FireOutcome = "fetch_failed"
        for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
            try:
                outcome = await self.fire_once()
            except Exception:
                log.exception("governance_report_fire_unhandled", attempt=attempt)
                outcome = "fetch_failed"
            if outcome not in ("fetch_failed", "partial"):
                return
            if attempt < FETCH_MAX_ATTEMPTS:
                log.warning(
                    "governance_report_fetch_retry_scheduled",
                    attempt=attempt,
                    retry_in_seconds=FETCH_RETRY_SECONDS,
                )
                if await self._wait_or_stop(FETCH_RETRY_SECONDS):
                    return
        # Give up for this period: mark handled so the loop sleeps to the
        # next month instead of hammering (the report is informational;
        # a dead api pages via the watchdog, not via this loop).
        _, _, q_label = quarter_period_for_fire(fire_date)
        self._handled_monthly.add(m_label)
        if fire_date.month in QUARTER_START_MONTHS:
            self._handled_quarterly.add(q_label)
        log.error(
            "governance_report_gave_up_for_period",
            period=m_label,
            attempts=FETCH_MAX_ATTEMPTS,
        )

    async def _wait_or_stop(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        return True


__all__ = [
    "FETCH_MAX_ATTEMPTS",
    "FETCH_RETRY_SECONDS",
    "QUARTER_START_MONTHS",
    "REPORT_CATCHUP_WINDOW_S",
    "REPORT_FIRE_HOUR_UTC",
    "REPORT_FIRE_MINUTE_UTC",
    "FireOutcome",
    "GovernanceReportConfig",
    "GovernanceReportScheduler",
    "month_period_for_fire",
    "plan_monthly_report_push",
    "plan_quarterly_report_push",
    "quarter_period_for_fire",
]
