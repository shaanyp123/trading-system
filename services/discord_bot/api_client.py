"""services/discord_bot/api_client.py — typed httpx client for api endpoints.

Phase 0 + post-pivot surface (the four endpoints the bot hits):

  * :meth:`ApiClient.get_health` → ``GET /api/health``
    Returns :class:`HealthResponse`. No auth required (public liveness probe
    per backend-spec §7.3).
  * :meth:`ApiClient.get_positions_current` → ``GET /api/positions/current``
    Returns :class:`PositionsResponse`. Requires bot bearer auth.
  * :meth:`ApiClient.invoke_kill_switch` → ``POST /api/system/kill-switch/invoke``
    Returns :class:`KillSwitchInvokeResponse` on 200 OR raises
    :class:`ApiClientHTTPError` on non-2xx (carrying the canonical
    ``ErrorEnvelope`` payload).
  * :meth:`ApiClient.approve_signal` →
    ``POST /api/signals/{signal_id}/approve``
    Returns :class:`SignalApproveResponse` on 200; raises
    :class:`ApiClientHTTPError` on non-2xx. Used by the ``/approve``
    slash command for operator-from-Discord signal approval.

Auth: bearer token in ``Authorization: Bearer <token>`` header for every
request. The api-side ``BotAuthMiddleware`` validates the token and on
match injects a service-account ``SessionContext``.

Why hand-typed Pydantic models instead of importing from
``services.api.schemas``: the bot is a separate runtime that should NOT
depend on the api's full module surface (avoid pulling in FastAPI +
sqlalchemy + asyncpg into the bot container's import graph). The two-way
contract is enforced at integration test + operator runbook time, not at
import time.

The Phase-0 ``positions=[]`` empty response is the production contract
(positions_current table is empty until Week 4 Wed dispatcher); the bot
renders the empty-state UX via :func:`build_positions_embed`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Literal, Self

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Response models (Phase 0 subset; mirror services/api/schemas/*)
# ---------------------------------------------------------------------------


class HealthCheck(BaseModel):
    """One entry in the api's ``GET /api/health`` ``checks`` array.

    Mirrors the dict shape returned by ``services/api/routes/health.py``
    (the api endpoint returns ``dict[str, Any]`` — not a typed model — so
    we re-type it here for the bot).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    """``GET /api/health`` response shape (per backend-spec §7.3)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    environment: Literal["dev", "paper", "live-small", "live-scale"]
    version: str
    db_connected: bool
    checks: list[HealthCheck]


class PositionRow(BaseModel):
    """Single position row from ``GET /api/positions/current``.

    Mirrors ``services/api/schemas/positions.Position`` (Phase 0 returns
    ``positions=[]`` so this type only exercises in fixture-driven tests).
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    symbol: str
    contract_month: str | None
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_pct_of_nav: Decimal
    cluster: str | None
    managed_by_strategy_version: str


class PositionsResponse(BaseModel):
    """``GET /api/positions/current`` response shape."""

    model_config = ConfigDict(extra="forbid")

    positions: list[PositionRow]
    as_of: datetime


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class KillSwitchInvokePayload(BaseModel):
    """``POST /api/system/kill-switch/invoke`` request body.

    Mirrors ``services/api/schemas/system.KillSwitchInvokeRequest`` —
    ``trigger`` is constrained to the §3.30 enum and ``reason`` is 1-500
    chars. The bot's ``/halt <reason>`` command always sends
    ``trigger="manual_judgment"`` (the operator-initiated trigger value).
    """

    model_config = ConfigDict(extra="forbid")

    trigger: Literal[
        "trailing_dd_breach",
        "daily_loss_breach",
        "signal_storm",
        "recon_mismatch",
        "broker_disconnect_5m",
        "vol_regime_z_gt_2",
        "corr_gt_0_85",
        "unhandled_exception",
        "calendar_unratified",
        "heartbeat_engagement_fail",
        "qc_objstore_stale_10m",
        "watchdog_and_discord_both_fail",
        "audit_write_fail",
        "hash_chain_break",
        "decommission_floor",
        "manual_judgment",
    ]
    reason: str


class CapitalEventInvokePayload(BaseModel):
    """``POST /api/system/capital-event`` request body.

    Mirrors :class:`services.api.schemas.capital_events.CapitalEventInvokeRequest`.
    ``amount_usd`` is a string on the wire (A05 — Decimal-as-string).
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["deposit", "withdrawal"]
    amount_usd: str
    reason: str
    current_session_no: int = 0


class CapitalEventInvokeResponse(BaseModel):
    """``POST /api/system/capital-event`` 200 response."""

    model_config = ConfigDict(extra="forbid")

    capital_event_id: str
    event_type: Literal["deposit", "withdrawal"]
    amount_usd: str
    threshold_met: bool
    post_event_equity: str
    capital_event_audit_event_uuid: str
    mode_started_audit_event_uuid: str | None = None


class KillSwitchInvokeResponse(BaseModel):
    """``POST /api/system/kill-switch/invoke`` 200 response (post-Week-4-Wed).

    Until the dispatcher lands, the api returns 501 with
    ``KILL_SWITCH_HANDLER_NOT_WIRED``; the bot translates that into a P2
    error embed via :func:`build_halt_invoke_error_embed`. This shape is
    the post-dispatcher contract (audit_event_uuid + new_state).
    """

    model_config = ConfigDict(extra="forbid")

    audit_event_uuid: str
    new_state: Literal["NORMAL", "HALT_NEW", "CONVALESCENT"]
    severity: Literal["routine", "defensive_envelope", "incident_review"] | None = None


class SignalApprovePayload(BaseModel):
    """``POST /api/signals/{signal_id}/approve`` request body.

    Mirrors ``services/api/schemas/signals.SignalApproveRequest`` —
    ``override_size`` is optional; None means "use the planner-emitted
    target_contracts". The Discord-side ``/approve`` command does NOT
    surface override_size today (Phase 1+ enhancement) — the bot always
    sends None and trusts the planner's target.
    """

    model_config = ConfigDict(extra="forbid")

    override_size: int | None = None


class SignalApproveResponse(BaseModel):
    """``POST /api/signals/{signal_id}/approve`` 200 response.

    Mirrors the dict shape returned by
    ``services/api/routes/signals.approve_signal``. The OrderPlacementWorker
    picks up the approved signal within ``API_ORDER_PLACEMENT_POLL_INTERVAL``
    seconds (5s default) and forwards to IBKR.
    """

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    new_status: str
    audit_event_uuid: str
    audit_sequence_no: int
    intent_to_place_order: bool


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApiClientError(Exception):
    """Base class for api-client errors."""


class ApiClientHTTPError(ApiClientError):
    """Non-2xx HTTP response from the api.

    Carries the parsed ``ErrorEnvelope`` payload when the response body
    matched the canonical ``{error_code, message, details}`` shape, or the
    raw text otherwise. Status code is always populated.
    """

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
        raw_body: str | None = None,
    ) -> None:
        super().__init__(f"api {status_code} {error_code}: {message}")
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details
        self.raw_body = raw_body


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_USER_AGENT = "trading-discord-bot/0.1 (+services.discord_bot)"


class ApiClient:
    """Async httpx client for the Phase-0 api endpoints.

    Use as an async context manager so the underlying httpx client is
    closed deterministically on shutdown (matches discord.py's lifespan
    via ``cog_unload``):

        async with ApiClient(base_url=..., bearer_token=...) as client:
            health = await client.get_health()

    The bearer token is sent on EVERY request (including ``/api/health``
    which doesn't require auth — sending it harmlessly on the public
    endpoint is simpler than per-route header logic).
    """

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer = bearer_token
        self._timeout = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={
                "User-Agent": _USER_AGENT,
                "Authorization": f"Bearer {self._bearer}",
            },
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- public methods -----------------------------------------------------

    async def get_health(self) -> HealthResponse:
        """``GET /api/health`` — public liveness probe.

        Returns the parsed body even on 503 (degraded) responses; only raises
        if the request itself fails (network error, malformed JSON, schema
        mismatch). The bot surfaces the degraded status to the operator via
        the embed color.
        """
        response = await self._client.get("/api/health")
        # 503 is a valid degraded response per backend-spec §7.3 — not raise-worthy
        if response.status_code not in (200, 503):
            self._raise_for_response(response)
        return HealthResponse.model_validate(response.json())

    async def get_positions_current(self) -> PositionsResponse:
        """``GET /api/positions/current`` — open positions snapshot."""
        response = await self._client.get("/api/positions/current")
        if response.status_code != 200:
            self._raise_for_response(response)
        return PositionsResponse.model_validate(response.json())

    async def approve_signal(
        self,
        signal_id: str,
        *,
        override_size: int | None = None,
    ) -> SignalApproveResponse:
        """``POST /api/signals/{signal_id}/approve``.

        Raises :class:`ApiClientHTTPError` on non-2xx — typically
        ``SIGNAL_NOT_FOUND`` (404) when the signal_id is bogus or
        ``SIGNAL_NOT_PENDING`` (409) when the operator approves a signal
        that's already been actioned (a fast-double-click race).

        ``signal_id`` is a string here rather than UUID so the caller can
        pass operator-typed values straight through; UUID validation
        happens at the api boundary via FastAPI's path-param parser
        (returns 422 on malformed). The Discord command pre-validates
        client-side to give a friendlier error before the round-trip.
        """
        payload = SignalApprovePayload(override_size=override_size)
        response = await self._client.post(
            f"/api/signals/{signal_id}/approve",
            json=payload.model_dump(),
        )
        if response.status_code != 200:
            self._raise_for_response(response)
        return SignalApproveResponse.model_validate(response.json())

    async def invoke_kill_switch(
        self,
        *,
        trigger: str,
        reason: str,
    ) -> KillSwitchInvokeResponse:
        """``POST /api/system/kill-switch/invoke``.

        Raises :class:`ApiClientHTTPError` on non-2xx. The Day-15 501
        ``KILL_SWITCH_HANDLER_NOT_WIRED`` stub surfaces here until the
        Week 4 Wed dispatcher lands; the ``/halt`` command catches it and
        renders a P2 error embed pointing at the dispatcher PR.
        """
        payload = KillSwitchInvokePayload(trigger=trigger, reason=reason)  # type: ignore[arg-type]
        response = await self._client.post(
            "/api/system/kill-switch/invoke",
            json=payload.model_dump(),
        )
        if response.status_code != 200:
            self._raise_for_response(response)
        return KillSwitchInvokeResponse.model_validate(response.json())

    async def invoke_capital_event(
        self,
        *,
        event_type: Literal["deposit", "withdrawal"],
        amount_usd: str,
        reason: str,
        current_session_no: int = 0,
    ) -> CapitalEventInvokeResponse:
        """``POST /api/system/capital-event``.

        Records a deposit or withdrawal event per cutover plan §7 +
        backend-spec §3.20. Threshold-met events (>= 5% of pre-event
        equity, or any first-deposit-from-zero) activate the 30-session
        capital-event mode. Raises :class:`ApiClientHTTPError` on non-2xx.
        """
        payload = CapitalEventInvokePayload(
            event_type=event_type,
            amount_usd=amount_usd,
            reason=reason,
            current_session_no=current_session_no,
        )
        response = await self._client.post(
            "/api/system/capital-event",
            json=payload.model_dump(),
        )
        if response.status_code != 200:
            self._raise_for_response(response)
        return CapitalEventInvokeResponse.model_validate(response.json())

    # ---- internals ----------------------------------------------------------

    def _raise_for_response(self, response: httpx.Response) -> None:
        """Translate a non-2xx response into :class:`ApiClientHTTPError`.

        Best-effort canonical-envelope parse: if the body matches the
        ``{error_code, message, details}`` shape per dev-guide §3.6, surface
        each field; otherwise wrap the raw text. Always raise — never
        return.
        """
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            raise ApiClientHTTPError(
                status_code=status,
                error_code=f"HTTP_{status}",
                message=f"Non-JSON response from api ({status}).",
                raw_body=response.text[:4096],
            ) from None
        if isinstance(body, dict) and "error_code" in body and "message" in body:
            raise ApiClientHTTPError(
                status_code=status,
                error_code=str(body["error_code"]),
                message=str(body["message"]),
                details=body.get("details") if isinstance(body.get("details"), dict) else None,
                raw_body=None,
            )
        raise ApiClientHTTPError(
            status_code=status,
            error_code=f"HTTP_{status}",
            message=f"Unexpected response shape from api ({status}).",
            raw_body=str(body)[:4096],
        )


__all__ = [
    "ApiClient",
    "ApiClientError",
    "ApiClientHTTPError",
    "CapitalEventInvokePayload",
    "CapitalEventInvokeResponse",
    "HealthCheck",
    "HealthResponse",
    "KillSwitchInvokePayload",
    "KillSwitchInvokeResponse",
    "PositionRow",
    "PositionsResponse",
    "SignalApprovePayload",
    "SignalApproveResponse",
]
