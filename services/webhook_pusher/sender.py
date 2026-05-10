"""Outbound HTTP sender — Discord webhooks + Resend API (backend-spec §1.6 + §3.27).

Pairs with ``services/webhook_pusher/payloads.py`` (pure-policy planner):
the planner returns :class:`OutboundMessage` records; this module POSTs each
one and classifies the response into a :class:`DeliveryResult`.

Why httpx + structlog:

- ``httpx.AsyncClient`` matches dev-guide §3.4 (async-first IO) and §3.3
  (typed signatures) — Resend + Discord are both modest-volume, so no
  connection-pool tuning beyond defaults.
- ``structlog`` (dev-guide §3.5) — every POST emits ``webhook_post_started``
  + ``webhook_post_completed`` (or ``_failed``) with ``channel``, ``url``
  (host only — never the auth-bearing path), ``status``, ``duration_ms``.
  Bearer tokens are NEVER logged; the log filter strips ``auth_header``.

User-Agent gotcha (Day 4 PR #21 lesson, dev-guide §6.8 + decisions-log
2026-05-07):

- Discord's API explicitly rejects requests whose User-Agent starts with
  ``Python-urllib/...`` or ``python-httpx/...`` (the stdlib + httpx
  defaults respectively) as an anti-bot measure — returns 403 with no
  useful body.
- We set an explicit, identifiable User-Agent on every outbound POST.
  Same hygiene applies to Resend (no known UA reject; defense-in-depth).

429 handling:

- Discord: typical 429 carries ``X-RateLimit-Reset-After`` (float seconds
  to wait) AND a JSON body ``{"retry_after": N, "global": false, ...}``.
  We prefer ``retry_after`` from the JSON body (it's been more reliable in
  practice than the header on non-global rate limits); fall back to
  ``Retry-After`` standard header if absent.
- Resend: standard ``Retry-After`` header (seconds, integer).
- Single retry only — caller is responsible for re-dispatching the whole
  alert if the retry also fails. We don't loop here because the caller's
  background-task scheduler is the right unit of retry.

Failure mode classification (consumed by ``alerts.delivery_status`` JSONB):

| HTTP outcome      | DeliveryStatus         | Retry?          |
|-------------------|------------------------|-----------------|
| 2xx               | OK                     | n/a             |
| 401, 403          | FAILED_AUTH            | no (creds bad)  |
| 404               | FAILED_NOT_FOUND       | no (URL wrong)  |
| 429               | RATE_LIMITED           | yes (1 retry)   |
| 4xx (other)       | FAILED_CLIENT_ERROR    | no              |
| 5xx               | FAILED_SERVER_ERROR    | no (caller's)   |
| timeout           | FAILED_TIMEOUT         | no (caller's)   |
| connection error  | FAILED_NETWORK         | no (caller's)   |

A22 reminder: tests must NOT hit live Discord webhooks or live Resend.
The integration suite uses ``respx`` to mock httpx at the transport layer;
unit tests of the planner have zero HTTP at all. See
``tests/unit/test_webhook_pusher.py``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

import httpx
import structlog
from structlog.stdlib import BoundLogger

from services.webhook_pusher.payloads import ChannelName, OutboundMessage

log = structlog.get_logger()

#: Identifiable User-Agent for ALL outbound POSTs. Mirrors the watchdog's
#: pattern (``WATCHDOG_USER_AGENT`` in ``watchdog/watchdog.py``) — Discord
#: 403s the httpx default. See decisions-log 2026-05-07 entry "Discord
#: webhook 403 from stdlib User-Agent" + dev-guide §6.8 strike (4).
WEBHOOK_PUSHER_USER_AGENT: Final[str] = (
    "trading-webhook-pusher/0.1.0 (+https://github.com/shaanyp123/trading-system)"
)

#: Per-request HTTP timeout. Discord webhook P99 is ~500ms in normal
#: conditions; 10s is generous and bounded so a stuck request can't pin
#: the dispatcher's background task forever.
HTTP_TIMEOUT_SECONDS: Final[float] = 10.0

#: Default Retry-After when the platform doesn't tell us. Conservative —
#: prefer waiting too long over hammering a rate-limited endpoint.
DEFAULT_RETRY_AFTER_SECONDS: Final[float] = 1.0

#: Cap on Retry-After we'll honor inline. If the platform asks for a
#: longer wait, we treat the alert as failed (caller schedules retry).
MAX_RETRY_AFTER_SECONDS: Final[float] = 30.0


class DeliveryStatus(StrEnum):
    """Per-channel outcome written to ``alerts.delivery_status`` JSONB.

    String values are the canonical wire form; the dispatcher serializes
    these directly into JSONB without re-mapping. Renaming a value here
    is a breaking change for any consumer of ``delivery_status`` (the
    web UI, audit grep tooling, etc.) — treat as a wire-schema migration.
    """

    OK = "ok"
    FAILED_AUTH = "failed_auth"
    FAILED_NOT_FOUND = "failed_not_found"
    RATE_LIMITED = "rate_limited"
    FAILED_CLIENT_ERROR = "failed_client_error"
    FAILED_SERVER_ERROR = "failed_server_error"
    FAILED_TIMEOUT = "failed_timeout"
    FAILED_NETWORK = "failed_network"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Per-channel POST outcome.

    Returned by :func:`post_outbound_message` (one POST). For multi-channel
    alerts the dispatcher accumulates a list of these and aggregates into
    the JSONB row update.

    ``http_status`` is None for transport failures (timeout, connection
    error). ``error`` is a short human string captured for the structured
    log + the JSONB row's diagnostic context (NOT the canonical status —
    consumers should branch on ``status`` instead).
    """

    channel: ChannelName
    status: DeliveryStatus
    http_status: int | None
    error: str | None
    retried: bool = False


# ---------------------------------------------------------------------------
# Public sender API
# ---------------------------------------------------------------------------


async def post_outbound_message(
    client: httpx.AsyncClient,
    msg: OutboundMessage,
    *,
    retry_on_rate_limit: bool = True,
) -> DeliveryResult:
    """POST one :class:`OutboundMessage` and classify the response.

    Single-attempt by default; on a 429 with a tractable Retry-After we
    sleep + retry exactly once, mark the result ``retried=True``, and
    return whichever outcome the second attempt yielded.

    Other failure classes (timeout, 401, 5xx, ...) return immediately —
    the caller (``dispatcher.dispatch_alert``) is responsible for
    full-alert retry via its background scheduler.

    Parameters
    ----------
    client:
        Caller-managed httpx client. The dispatcher creates one per
        ``dispatch_alert`` invocation so HTTP sockets are pooled across
        the (up to 3) channels of a single P0 fan-out.
    msg:
        The outbound message produced by
        :func:`services.webhook_pusher.payloads.plan_alert_dispatch`.
    retry_on_rate_limit:
        When False, a 429 returns immediately as ``RATE_LIMITED`` without
        sleeping. Used by tests to assert the retry decision separately
        from the wait behavior.

    Returns
    -------
    DeliveryResult
        Channel-tagged outcome the dispatcher writes to ``delivery_status``.
    """
    bound = log.bind(
        service_name="webhook_pusher",
        channel=msg.channel.value,
        url_host=urlsplit(msg.url).netloc,
    )

    first = await _post_once(client, msg, bound)

    if first.status is not DeliveryStatus.RATE_LIMITED or not retry_on_rate_limit:
        return first

    sleep_s = _sleep_seconds_from_retry_after(
        retry_after=first.error,
        default=DEFAULT_RETRY_AFTER_SECONDS,
        cap=MAX_RETRY_AFTER_SECONDS,
    )
    if sleep_s is None:
        # Retry-After exceeded our cap; caller's scheduler is the right
        # retry path. Return the original 429 result without retrying.
        return first

    bound.info("webhook_post_rate_limited", retry_after_s=sleep_s)
    await asyncio.sleep(sleep_s)
    second = await _post_once(client, msg, bound.bind(attempt="retry"))
    return DeliveryResult(
        channel=second.channel,
        status=second.status,
        http_status=second.http_status,
        error=second.error,
        retried=True,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _post_once(
    client: httpx.AsyncClient,
    msg: OutboundMessage,
    bound: BoundLogger,
) -> DeliveryResult:
    """One POST attempt; classify response or transport failure."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": WEBHOOK_PUSHER_USER_AGENT,
    }
    if msg.auth_header is not None:
        headers["Authorization"] = msg.auth_header

    bound.info("webhook_post_started")
    try:
        response = await client.post(
            msg.url,
            json=dict(msg.payload),
            headers=headers,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        bound.warning("webhook_post_timeout", error=str(exc))
        return DeliveryResult(
            channel=msg.channel,
            status=DeliveryStatus.FAILED_TIMEOUT,
            http_status=None,
            error=f"timeout: {exc}",
        )
    except httpx.RequestError as exc:
        bound.warning("webhook_post_network_error", error=str(exc))
        return DeliveryResult(
            channel=msg.channel,
            status=DeliveryStatus.FAILED_NETWORK,
            http_status=None,
            error=f"network: {exc}",
        )

    return _classify_response(msg.channel, response, bound)


def _classify_response(
    channel: ChannelName,
    response: httpx.Response,
    bound: BoundLogger,
) -> DeliveryResult:
    """Map HTTP status to a :class:`DeliveryStatus` + log the outcome.

    Discord returns 204 (No Content) on success; Resend returns 200 with
    a JSON body. Both are 2xx — we treat the whole 2xx range as ``OK``.
    """
    status = response.status_code

    if 200 <= status < 300:
        bound.info("webhook_post_completed", status=status)
        return DeliveryResult(
            channel=channel,
            status=DeliveryStatus.OK,
            http_status=status,
            error=None,
        )

    body_excerpt = _safe_body_excerpt(response)
    bound = bound.bind(status=status, body_excerpt=body_excerpt)

    if status in (401, 403):
        bound.warning("webhook_post_failed_auth")
        return DeliveryResult(
            channel=channel,
            status=DeliveryStatus.FAILED_AUTH,
            http_status=status,
            error=f"auth: {body_excerpt}",
        )
    if status == 404:
        bound.warning("webhook_post_failed_not_found")
        return DeliveryResult(
            channel=channel,
            status=DeliveryStatus.FAILED_NOT_FOUND,
            http_status=status,
            error=f"not_found: {body_excerpt}",
        )
    if status == 429:
        retry_after = _read_retry_after(response)
        bound.warning("webhook_post_rate_limited_response", retry_after=retry_after)
        # Stash Retry-After in the error field for the retry path to read
        # without parsing the response twice. Public consumers should
        # branch on ``.status`` not ``.error``.
        return DeliveryResult(
            channel=channel,
            status=DeliveryStatus.RATE_LIMITED,
            http_status=status,
            error=retry_after,
        )
    if 400 <= status < 500:
        bound.warning("webhook_post_failed_client_error")
        return DeliveryResult(
            channel=channel,
            status=DeliveryStatus.FAILED_CLIENT_ERROR,
            http_status=status,
            error=f"client_error: {body_excerpt}",
        )

    bound.warning("webhook_post_failed_server_error")
    return DeliveryResult(
        channel=channel,
        status=DeliveryStatus.FAILED_SERVER_ERROR,
        http_status=status,
        error=f"server_error: {body_excerpt}",
    )


def _read_retry_after(response: httpx.Response) -> str | None:
    """Read Retry-After preference: Discord JSON ``retry_after`` first, then header.

    Returns the value as a string for storage in :attr:`DeliveryResult.error`;
    the retry path parses it back to float. Returns ``None`` when no
    indication is present (caller falls back to default).
    """
    # Discord prefers a JSON body retry_after on per-route limits. Resend
    # uses the standard header. Try the body first since it's more
    # specific when present; the header is a safe fallback.
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        retry_after = body.get("retry_after")
        if isinstance(retry_after, int | float):
            return f"{float(retry_after):.3f}"

    header = response.headers.get("Retry-After")
    if header:
        return str(header)

    return None


def _sleep_seconds_from_retry_after(
    *,
    retry_after: str | None,
    default: float,
    cap: float,
) -> float | None:
    """Parse ``Retry-After`` (string seconds) into a sleep duration.

    Returns ``None`` if the platform asks for a wait longer than ``cap``
    (caller's scheduler is the right retry path). Returns ``default`` when
    the value can't be parsed (defensive — accept the retry decision but
    don't trust the payload).
    """
    if retry_after is None:
        return default
    try:
        seconds = float(retry_after)
    except ValueError:
        # HTTP Retry-After also allows an HTTP-date format. We don't
        # support it here — Discord/Resend always return seconds. Fall
        # back to default; if the format actually IS an HTTP-date in
        # the future, the operator will see logs + can extend this
        # parser.
        return default

    if seconds < 0:
        return default
    if seconds > cap:
        return None
    return seconds


def _safe_body_excerpt(response: httpx.Response) -> str:
    """Return up to ``200`` chars of the response body for log + JSONB.

    Body content can be large (Resend returns full-account JSON on auth
    failures) and may contain leakable identifiers — we cap at 200 chars
    so neither the log nor the JSONB row balloons.
    """
    try:
        text = response.text
    except UnicodeDecodeError:
        return "<non-text body>"
    return text[:200] if text else ""


__all__ = [
    "DEFAULT_RETRY_AFTER_SECONDS",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "WEBHOOK_PUSHER_USER_AGENT",
    "DeliveryResult",
    "DeliveryStatus",
    "post_outbound_message",
]
