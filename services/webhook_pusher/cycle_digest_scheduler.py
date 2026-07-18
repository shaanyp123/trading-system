"""services/webhook_pusher/cycle_digest_scheduler.py — 00:10 UTC digest push.

Crypto-pivot delta spec §3.8: the scheduled leg of the ``/cycle``
daily-decision digest — five minutes after the strategy worker's 00:05
UTC decision, fetch ``GET /api/system/cycle`` (§3.7) with the shared api
bearer, render via the pure builder in
:mod:`services.webhook_pusher.cycle_digest`, and POST to the existing
**#daily-brief** webhook through the existing
:func:`services.webhook_pusher.dispatcher.dispatch_event_push` transport.

Split from ``cycle_digest.py`` deliberately: this module imports httpx +
the dispatcher (whose module graph pulls sqlalchemy) — neither ships in
the Discord bot image, which imports only the pure builder.

Semantics:

* **Fires at most once per UTC day** (in-memory guard; the only restart
  race — a container bounce inside the catch-up window after a
  successful push — duplicates one informational embed at worst).
* **Skips when no decision has ever been recorded**
  (:func:`~services.webhook_pusher.cycle_digest.plan_cycle_digest_push`
  returns None) — logged, day marked handled, no POST.
* **Transient api-fetch failures retry** (:data:`FETCH_MAX_ATTEMPTS` x
  :data:`FETCH_RETRY_SECONDS`) then give up for the day — the digest is
  informational; alerting on a dead api is the watchdog's job.
* **Never raises out of** :meth:`CycleDigestScheduler.run_forever` —
  runs as a sibling task of the SSE subscriber inside the
  webhook_pusher container (``services/webhook_pusher/__main__.py``).

A02 N/A — §2.3 hot-fix whitelist. A27: Discord webhook delivery reuses
the smoke-tested ``dispatcher``/``sender`` transport; the operator smoke
step lives in ``deploy/webhook_pusher/README.md``. [A06]: every datetime
tz-aware UTC.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal

import httpx
import structlog

from services.webhook_pusher.cycle_digest import (
    DIGEST_FIRE_HOUR_UTC,
    DIGEST_FIRE_MINUTE_UTC,
    plan_cycle_digest_push,
)
from services.webhook_pusher.dispatcher import dispatch_event_push

log = structlog.get_logger()


#: On a restart shortly after 00:10 the scheduler still fires for today
#: (covers deploy/crash races around the fire time). Past this window the
#: day is treated as missed rather than re-pushed — a mid-day container
#: restart must not duplicate the morning digest.
DIGEST_CATCHUP_WINDOW_S: Final[int] = 3600

#: Transient api-fetch failure retry policy (api restarting at midnight,
#: brief network blip).
FETCH_MAX_ATTEMPTS: Final[int] = 3
FETCH_RETRY_SECONDS: Final[float] = 60.0

FireOutcome = Literal["pushed", "skipped_no_decision", "already_handled", "fetch_failed"]


@dataclass(frozen=True, slots=True)
class CycleDigestConfig:
    """Runtime configuration for :class:`CycleDigestScheduler`.

    Sourced from the webhook_pusher container's env vars (see
    ``services/webhook_pusher/__main__.py`` + ``entrypoint.sh``).
    """

    api_base_url: str  # e.g. "http://api:8000"
    api_bearer_token: str  # shared discord-bot bearer (BotAuthMiddleware)
    daily_brief_webhook_url: str  # secrets discord.webhook_urls.daily_brief
    request_timeout_seconds: float = 30.0


class CycleDigestScheduler:
    """00:10 UTC daily fire loop for the #daily-brief cycle digest.

    Lifecycle mirrors :class:`~services.webhook_pusher.sse_subscriber.SSESubscriber`:

        scheduler = CycleDigestScheduler(config)
        await scheduler.run_forever()   # blocks until request_stop()
        scheduler.request_stop()        # graceful shutdown signal

    ``clock`` is injectable for tests; production uses
    ``datetime.now(tz=UTC)``.
    """

    def __init__(
        self,
        config: CycleDigestConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._injected_client = http_client
        self._stop_event = asyncio.Event()
        #: Fire-once-per-day guard (see module docstring).
        self._last_handled_date: date | None = None

    # -- lifecycle -------------------------------------------------------

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def last_handled_date(self) -> date | None:
        return self._last_handled_date

    # -- schedule math ---------------------------------------------------

    def next_fire_utc(self, now: datetime) -> datetime:
        """Next instant this scheduler should fire (tz-aware UTC).

        Today's 00:10 while it is still ahead; today's 00:10 treated as
        immediately-due inside the catch-up window when today is
        unhandled; otherwise tomorrow's 00:10.
        """
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware UTC; got naive datetime")
        now = now.astimezone(UTC)
        fire_today = now.replace(
            hour=DIGEST_FIRE_HOUR_UTC,
            minute=DIGEST_FIRE_MINUTE_UTC,
            second=0,
            microsecond=0,
        )
        if now < fire_today:
            return fire_today
        in_catchup = (now - fire_today).total_seconds() < DIGEST_CATCHUP_WINDOW_S
        if in_catchup and self._last_handled_date != now.date():
            return fire_today  # due now — run_forever fires immediately
        return fire_today + timedelta(days=1)

    # -- one firing ------------------------------------------------------

    async def fire_once(
        self,
        *,
        now_utc: datetime | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> FireOutcome:
        """Fetch → plan → dispatch, at most once for the current UTC day.

        Marks the day handled on "pushed" AND "skipped_no_decision";
        "fetch_failed" leaves the day unhandled so :meth:`run_forever`'s
        retry loop can try again.
        """
        now = now_utc if now_utc is not None else self._clock()
        if now.tzinfo is None:
            raise ValueError("now_utc must be tz-aware UTC; got naive datetime")
        fire_date = now.astimezone(UTC).date()
        if self._last_handled_date == fire_date:
            return "already_handled"

        payload = await self._fetch_cycle_payload(http_client)
        if payload is None:
            return "fetch_failed"

        # §10 gates section is best-effort: a gates fetch failure never
        # blocks the nightly digest (the field is simply absent).
        gates_payload = await self._fetch_json("/api/system/gates", http_client)
        plan = plan_cycle_digest_push(payload, now_utc=now, gates_payload=gates_payload)
        if plan is None:
            self._last_handled_date = fire_date
            log.info("cycle_digest_skipped_no_decision", fire_date=fire_date.isoformat())
            return "skipped_no_decision"

        client = http_client or self._injected_client
        if client is not None:
            result = await dispatch_event_push(
                plan,
                http_client=client,
                webhook_url=self._config.daily_brief_webhook_url,
            )
        else:
            async with httpx.AsyncClient() as owned:
                result = await dispatch_event_push(
                    plan,
                    http_client=owned,
                    webhook_url=self._config.daily_brief_webhook_url,
                )
        self._last_handled_date = fire_date
        log.info(
            "cycle_digest_pushed",
            fire_date=fire_date.isoformat(),
            dedupe_key=plan.dedupe_key,
            delivery_status=result.status.value,
            http_status=result.http_status,
        )
        return "pushed"

    async def _fetch_cycle_payload(
        self, http_client: httpx.AsyncClient | None
    ) -> dict[str, Any] | None:
        """GET /api/system/cycle with the shared bearer; None on any failure."""
        return await self._fetch_json("/api/system/cycle", http_client)

    async def _fetch_json(
        self, path: str, http_client: httpx.AsyncClient | None
    ) -> dict[str, Any] | None:
        """GET an api JSON object with the shared bearer; None on any failure."""
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
            log.warning("cycle_digest_fetch_network_error", error=str(exc))
            return None
        if response.status_code != 200:
            log.warning("cycle_digest_fetch_http_error", http_status=response.status_code)
            return None
        try:
            body = response.json()
        except ValueError:
            log.warning("cycle_digest_fetch_non_json")
            return None
        if not isinstance(body, dict):
            log.warning("cycle_digest_fetch_unexpected_shape", body_type=type(body).__name__)
            return None
        return body

    # -- main loop -------------------------------------------------------

    async def run_forever(self) -> None:
        """Sleep-until-00:10 loop; stop-responsive; never raises out."""
        log.info(
            "cycle_digest_scheduler_started",
            fire_time_utc=f"{DIGEST_FIRE_HOUR_UTC:02d}:{DIGEST_FIRE_MINUTE_UTC:02d}",
        )
        while not self._stop_event.is_set():
            now = self._clock()
            target = self.next_fire_utc(now)
            wait_s = (target - now).total_seconds()
            if wait_s > 0:
                if await self._wait_or_stop(wait_s):
                    break
                continue  # recompute — clock may have drifted during sleep
            await self._fire_with_retries()
        log.info("cycle_digest_scheduler_stopped")

    async def _fire_with_retries(self) -> None:
        """One due firing with the transient-failure retry policy."""
        fire_date = self._clock().astimezone(UTC).date()
        outcome: FireOutcome = "fetch_failed"
        for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
            try:
                outcome = await self.fire_once()
            except Exception:
                # Defensive: fire_once already contains the expected failure
                # paths; anything else is a bug that must not kill the loop.
                log.exception("cycle_digest_fire_unhandled", attempt=attempt)
                outcome = "fetch_failed"
            if outcome != "fetch_failed":
                return
            if attempt < FETCH_MAX_ATTEMPTS:
                log.warning(
                    "cycle_digest_fetch_retry_scheduled",
                    attempt=attempt,
                    retry_in_seconds=FETCH_RETRY_SECONDS,
                )
                if await self._wait_or_stop(FETCH_RETRY_SECONDS):
                    return
        self._last_handled_date = fire_date
        log.error(
            "cycle_digest_gave_up_for_day",
            fire_date=fire_date.isoformat(),
            attempts=FETCH_MAX_ATTEMPTS,
        )

    async def _wait_or_stop(self, timeout_s: float) -> bool:
        """Wait on the stop event; True = stop requested."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        return True


__all__ = [
    "DIGEST_CATCHUP_WINDOW_S",
    "FETCH_MAX_ATTEMPTS",
    "FETCH_RETRY_SECONDS",
    "CycleDigestConfig",
    "CycleDigestScheduler",
    "FireOutcome",
]
