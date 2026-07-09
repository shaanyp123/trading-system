"""services/webhook_pusher/__main__.py — long-lived SSE subscriber runner.

Wire-up follow-up to PR #106 (post-pivot 2026-05-12). PR #106 shipped
the SSESubscriber module + 36 unit tests but left the container's
CMD at the Phase-0 ``sleep infinity`` placeholder. This module is the
runtime entrypoint that:

  1. Reads config from env vars (set by ``entrypoint.sh`` from the
     host secrets file at ``/run/secrets/secrets.yaml``).
  2. Builds a :class:`SSESubscriberConfig` and constructs a
     :class:`SSESubscriber`.
  3. Installs SIGTERM/SIGINT handlers so ``docker compose stop`` /
     ``kill -TERM`` propagates to the loop's ``request_stop()``.
  4. Awaits ``run_forever()``.

Invoke via ``python -m services.webhook_pusher``. The Dockerfile's
CMD wires this.

**Auth model:** the subscriber sends ``Authorization: Bearer <token>``
where the token is the discord-bot bearer (secrets ``discord.api_bearer_token``).
This bearer is already accepted by the api's ``BotAuthMiddleware``
(outermost in the middleware stack), which injects a strong-auth
``SessionContext`` so the SSE multiplexer accepts the long-lived
connection. Phase 2+ may issue webhook_pusher its own bearer for
finer-grained blast-radius — for Phase 1 single-operator, sharing
is acceptable.

**Required env vars** (set by ``entrypoint.sh`` from the secrets file):

  * ``WEBHOOK_PUSHER_API_BEARER_TOKEN`` — secrets ``discord.api_bearer_token``.
    Fail-close exit 2 if missing — without it, every SSE connect
    returns 401 and consumed=0 forever (no observability loop close).
  * ``WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL`` — secrets
    ``discord.webhook_urls.signals``. Fail-close exit 2 if missing.
  * ``WEBHOOK_PUSHER_FILLS_WEBHOOK_URL`` — secrets
    ``discord.webhook_urls.fills``. Fail-close exit 2 if missing.
  * ``WEBHOOK_PUSHER_DAILY_BRIEF_WEBHOOK_URL`` — secrets
    ``discord.webhook_urls.daily_brief``. Fail-close exit 2 if missing.
    Target of the crypto-pivot §3.8 00:10 UTC cycle-digest push
    (``services/webhook_pusher/cycle_digest.py``), which runs as a
    second long-lived loop alongside the SSE subscriber.

**Optional env vars** (defaults sane):

  * ``WEBHOOK_PUSHER_API_BASE_URL`` — defaults to ``http://api:8000``
    (the api container's Docker DNS name on the ``internal`` network).
  * ``WEBHOOK_PUSHER_LOG_LEVEL`` — INFO / DEBUG / WARN / ERROR.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import structlog

from services.webhook_pusher.cycle_digest_scheduler import (
    CycleDigestConfig,
    CycleDigestScheduler,
)
from services.webhook_pusher.sse_subscriber import (
    SSESubscriber,
    SSESubscriberConfig,
)


def _configure_logging() -> None:
    """JSON-on-prod structlog config mirroring services.api.main."""
    level_name = os.environ.get("WEBHOOK_PUSHER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _require(name: str) -> str:
    """Read a required env var; exit 2 with operator-readable error if missing."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(
            f"[webhook_pusher_sse_runner] FATAL: required env var {name} missing or empty",
            file=sys.stderr,
            flush=True,
        )
        print(
            "[webhook_pusher_sse_runner] verify the secrets file + the entrypoint.sh "
            "yaml→env mapping; see deploy/webhook_pusher/README.md.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)
    return value


def _build_config() -> SSESubscriberConfig:
    return SSESubscriberConfig(
        api_base_url=os.environ.get("WEBHOOK_PUSHER_API_BASE_URL", "http://api:8000"),
        api_bearer_token=_require("WEBHOOK_PUSHER_API_BEARER_TOKEN"),
        signals_webhook_url=_require("WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL"),
        fills_webhook_url=_require("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL"),
    )


def _build_cycle_digest_config() -> CycleDigestConfig:
    """Crypto-pivot §3.8 — the 00:10 UTC daily-decision digest scheduler.

    Reuses the same api base + bearer as the SSE subscriber; the target
    webhook is the existing #daily-brief channel (secrets
    ``discord.webhook_urls.daily_brief`` — fail-close exit 2 if missing,
    same policy as the signals/fills URLs).
    """
    return CycleDigestConfig(
        api_base_url=os.environ.get("WEBHOOK_PUSHER_API_BASE_URL", "http://api:8000"),
        api_bearer_token=_require("WEBHOOK_PUSHER_API_BEARER_TOKEN"),
        daily_brief_webhook_url=_require("WEBHOOK_PUSHER_DAILY_BRIEF_WEBHOOK_URL"),
    )


async def _run() -> None:
    log = structlog.get_logger()
    config = _build_config()
    subscriber = SSESubscriber(config)
    digest_scheduler = CycleDigestScheduler(_build_cycle_digest_config())
    log.info(
        "webhook_pusher_sse_runner_starting",
        api_base_url=config.api_base_url,
        reconnect_initial_seconds=config.reconnect_initial_seconds,
        reconnect_max_seconds=config.reconnect_max_seconds,
    )

    # SIGTERM / SIGINT → request_stop() on both loops. Docker sends
    # SIGTERM on `docker compose stop`; we want a graceful exit at the
    # next loop iteration boundary rather than mid-frame.
    def _request_stop() -> None:
        subscriber.request_stop()
        digest_scheduler.request_stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Signal handlers can't be installed on some platforms (e.g.,
            # Windows asyncio default loop). Fall back to no-op; docker
            # stop will SIGKILL after the 10s grace period.
            pass

    try:
        # Two long-lived loops in one container: the SSE subscriber
        # (#signals/#fills event pushes) + the 00:10 UTC cycle-digest
        # scheduler (#daily-brief). Both are stop-responsive and never
        # raise out; gather propagates only genuine bugs.
        await asyncio.gather(
            subscriber.run_forever(),
            digest_scheduler.run_forever(),
        )
    finally:
        log.info("webhook_pusher_sse_runner_stopped")


def main() -> int:
    _configure_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
