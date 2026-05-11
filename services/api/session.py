"""services/api/session.py — Phase-0 session-stub middleware.

The real session ceremony (WebAuthn registration → session-cookie issuance →
``last_uv_at`` tracking → re-auth gating) lands Week 6 alongside the frontend
auth flow. Day 15 ships a deliberate stub so the Phase-1 REST endpoints can:

  * accept any (or no) ``__Host-trading_session`` cookie value as a proxy for
    "logged in as the bootstrap operator" — sufficient for ``curl`` smoke
    tests + the operator browser bringup before WebAuthn lands.
  * inject ``request.state.session`` with mock owner-role + ``auth_strength
    = "weak"`` so endpoints that branch on auth_strength have a concrete
    value to read from rather than a None-check.

When real session middleware lands Week 6 the entire body of this file
deletes; only the ``SessionContext`` dataclass survives (downstream
endpoints depend on its shape). The middleware class is replaced by one
that hits the real ``sessions`` table.

The stub is INACTIVE outside the ``API_ENVIRONMENT in {"dev", "paper"}``
horizon. ``live-small`` and ``live-scale`` environments fail closed: the
middleware raises ``AppError("AUTH_REQUIRED")`` if a ``sessions`` table
lookup hasn't been wired and the env says we're in production. This
defends against a forgotten removal of the stub when the WebAuthn ceremony
ships — production SHOULD NEVER accept anonymous requests under the stub
contract, even by accident.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from services.api.config import APISettings
from services.api.errors import ErrorEnvelope

log = structlog.get_logger()


#: Routes that don't need a session — mirror of the CSRF allowlist plus
#: the health probe (public). Keep in sync with
#: ``services/api/middleware._CSRF_EXEMPT_PATHS`` plus the read-only public
#: probes.
#:
#: ``/api/sse/events`` is NOT exempt as of Day 16 — the SSE multiplexer
#: needs ``session.user_id`` to enforce the per-user N=4 tab limit
#: (frontend-spec §4.6). Before Day 16 the path was exempt because the
#: Day-5 heartbeat scaffold had no per-user state; the Day 16 multiplexer
#: introduces it.
_SESSION_EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/health",
        "/api/setup/verify-token",
        "/api/internal/watchdog",
    }
)


@dataclass(frozen=True, slots=True)
class SessionContext:
    """The shape downstream route handlers read from ``request.state.session``.

    Fields mirror the ``AuthMeResponse`` schema (services/api/schemas/auth.py)
    plus a ``is_phase0_stub`` flag so future code paths can branch on whether
    the data is real (Week 6+) or scaffolded (now). When real auth lands the
    flag is removed; downstream code should treat its absence as "real".
    """

    user_id: str
    username: str
    role: Literal["owner", "reader"]
    auth_strength: Literal["weak", "strong"]
    last_uv_at: datetime | None
    session_expires_at: datetime
    webauthn_enrolled: bool
    totp_enrolled: bool
    is_phase0_stub: bool


def _build_phase0_stub_session(idle_seconds: int) -> SessionContext:
    """Compose the canonical Phase-0 stub session.

    ``last_uv_at`` is set to NOW - 1 minute so risk-loosening endpoints
    that gate on the 5-minute re-auth window see a recent UV (the 5-min
    window per dev-guide §1.5 covers requests up to 5 minutes after the
    last WebAuthn assertion). The 1-minute offset is symbolic — Phase 0
    has no real WebAuthn so the value is mock; downstream code should
    NOT depend on the exact timestamp until Week 6 wiring lands.

    ``session_expires_at`` is NOW + idle_seconds (default 30 min from
    APISettings). The stub does NOT track absolute expiry — that lands
    when the real ``sessions`` table is wired.
    """
    now = datetime.now(tz=UTC)
    return SessionContext(
        user_id="phase0-stub-owner",
        username="operator",
        role="owner",
        auth_strength="weak",
        last_uv_at=now - timedelta(minutes=1),
        session_expires_at=now + timedelta(seconds=idle_seconds),
        webauthn_enrolled=False,
        totp_enrolled=False,
        is_phase0_stub=True,
    )


class SessionStubMiddleware(BaseHTTPMiddleware):
    """Inject a stub ``SessionContext`` into ``request.state.session``.

    Production-mode (``live-*``) requests fail closed with HTTP 401 because
    the stub is explicitly Phase-0 only — see module docstring.
    """

    def __init__(self, app: FastAPI, *, settings: APISettings) -> None:
        super().__init__(app)
        self._cookie_name = settings.session_cookie_name
        self._idle_seconds = settings.session_idle_seconds
        self._is_production = settings.environment in ("live-small", "live-scale")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in _SESSION_EXEMPT_PATHS:
            return await call_next(request)

        # Day 23: if BotAuthMiddleware (running OUTER of this middleware)
        # already injected a service-account session, don't overwrite it
        # — the bot's strong-auth context wins over the Phase-0 stub.
        # Without this guard the bot's calls would land in production envs
        # with the stub session, defeating BotAuth's purpose.
        if getattr(request.state, "session", None) is not None:
            return await call_next(request)

        if self._is_production:
            # Fail closed in production. The stub MUST NOT be the auth path
            # in live environments. When the real session middleware ships,
            # this branch becomes the only branch.
            log.error(
                "session_stub_invoked_in_production_environment",
                path=request.url.path,
            )
            return JSONResponse(
                status_code=401,
                content=ErrorEnvelope(
                    error_code="AUTH_REQUIRED",
                    message="Session middleware not yet wired for production.",
                ).model_dump(),
            )

        session = _build_phase0_stub_session(self._idle_seconds)
        request.state.session = session
        log.debug(
            "session_stub_injected",
            user_id=session.user_id,
            auth_strength=session.auth_strength,
            cookie_present=self._cookie_name in request.cookies,
        )
        return await call_next(request)


def get_session_context(request: Request) -> SessionContext:
    """FastAPI dependency: pull the ``SessionContext`` for a route handler.

    The middleware always populates ``request.state.session`` for non-exempt
    paths (or returns 401 first), so this raises in the rare case of a
    misconfigured middleware order — surfaces the bug loudly during route
    development rather than producing a silent ``AttributeError`` at call
    time.
    """
    session = getattr(request.state, "session", None)
    if session is None:
        raise RuntimeError(
            "SessionContext not set on request.state — "
            "SessionStubMiddleware must be registered before this route runs"
        )
    if not isinstance(session, SessionContext):
        raise RuntimeError(
            f"request.state.session is {type(session).__name__}, expected SessionContext"
        )
    return session


__all__ = [
    "SessionContext",
    "SessionStubMiddleware",
    "get_session_context",
]
