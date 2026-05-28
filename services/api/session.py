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

import secrets
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

    ``auth_strength`` is ``"strong"``: the stub explicitly models a
    "WebAuthn-just-verified" session so risk-loosening endpoints
    (``POST /api/system/kill-switch/resume``,
    ``POST /api/auth/backup-codes/regenerate``) are reachable in
    Phase-0 paper/dev environments. The Day-15 author left this field as
    ``"weak"`` while writing ``last_uv_at`` to a recent timestamp; Day 25
    realigned the field to match the docstring intent. The semantic of
    the change: the stub IS the operator in paper/dev — the operator's
    real WebAuthn session has been verified, just by a different
    middleware that hasn't landed yet (Week 6+). The fail-closed
    behavior in ``live-small``/``live-scale`` means production never
    sees this strong-stub session, so this carries no security weight
    in those envs.

    ``session_expires_at`` is NOW + idle_seconds (default 30 min from
    APISettings). The stub does NOT track absolute expiry — that lands
    when the real ``sessions`` table is wired.
    """
    now = datetime.now(tz=UTC)
    return SessionContext(
        user_id="phase0-stub-owner",
        username="operator",
        role="owner",
        auth_strength="strong",
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

    CSRF cookie bootstrap (2026-05-28 fix):
      Real auth flows (``services/api/routes/auth.py::_set_session_cookies``)
      mint the ``__Host-csrf_token`` cookie alongside the session cookie on
      every WebAuthn/TOTP login. The Phase-0 stub has no equivalent ceremony
      — pre-fix, an operator who visited a non-auth page (e.g. ``/system``)
      in a fresh browser session never received a CSRF cookie. The first
      state-changing POST (Resume kill switch, regenerate backup codes,
      etc.) was then rejected by :class:`CSRFMiddleware` with
      ``CSRF_REJECTED``. This middleware now piggybacks a server-minted
      CSRF token onto the response of every stub-injected request that
      arrived without one. The cookie attributes mirror
      ``_set_session_cookies`` exactly (Secure outside dev, SameSite=Strict,
      Path=/, Max-Age=session_absolute_seconds, no Domain) so the
      ``__Host-`` prefix browser invariants hold. See
      ``Docs/decisions-log.md`` 2026-05-28 entry for the late-evening
      manual-override incident that surfaced this gap.
    """

    def __init__(self, app: FastAPI, *, settings: APISettings) -> None:
        super().__init__(app)
        self._cookie_name = settings.session_cookie_name
        self._csrf_cookie_name = settings.csrf_cookie_name
        self._idle_seconds = settings.session_idle_seconds
        self._csrf_max_age = settings.session_absolute_seconds
        self._is_production = settings.environment in ("live-small", "live-scale")
        # Match ``_set_session_cookies`` semantics: Secure flag everywhere
        # except ``dev`` env. Required for the ``__Host-`` cookie-name prefix
        # to survive browser-side validation on https origins.
        self._cookie_secure = settings.environment != "dev"

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
        csrf_cookie_present = self._csrf_cookie_name in request.cookies
        log.debug(
            "session_stub_injected",
            user_id=session.user_id,
            auth_strength=session.auth_strength,
            cookie_present=self._cookie_name in request.cookies,
            csrf_cookie_present=csrf_cookie_present,
        )
        response = await call_next(request)
        if not csrf_cookie_present:
            _mint_csrf_cookie_on_response(
                response,
                csrf_cookie_name=self._csrf_cookie_name,
                max_age_seconds=self._csrf_max_age,
                secure=self._cookie_secure,
            )
            log.debug(
                "session_stub_csrf_cookie_bootstrapped",
                path=request.url.path,
            )
        return response


def _mint_csrf_cookie_on_response(
    response: Response,
    *,
    csrf_cookie_name: str,
    max_age_seconds: int,
    secure: bool,
) -> None:
    """Attach a fresh ``__Host-csrf_token`` cookie to the response.

    Helper extracted from :class:`SessionStubMiddleware` so the same
    cookie-attribute set lives in one place (this module) rather than
    being duplicated between session.py and routes/auth.py. The real
    auth flow's ``_set_session_cookies`` in routes/auth.py mints the
    same cookie shape — when real session middleware lands Week 6+,
    that callsite can move to this helper too.

    The value is 256 bits of cryptographic randomness via
    :func:`secrets.token_urlsafe` — matches the format used by
    ``_set_session_cookies``. The CSRF middleware only does a
    constant-time compare against the ``X-CSRF-Token`` header so the
    value is opaque to the server, but consistency keeps audit-log
    payloads uniform.

    Attribute rationale (must keep these aligned with the ``__Host-``
    cookie-name prefix browser invariants):
      * ``secure=True`` outside dev — required for ``__Host-`` prefix
      * ``httponly=False`` — frontend JS reads it via ``document.cookie``
        to populate the ``X-CSRF-Token`` request header (double-submit
        pattern per backend-spec §8.5.4)
      * ``samesite="strict"`` — CSRF defence at the cookie layer
      * ``path="/"`` — required for ``__Host-`` prefix
      * no Domain attribute — required for ``__Host-`` prefix
    """
    response.set_cookie(
        key=csrf_cookie_name,
        value=secrets.token_urlsafe(32),
        max_age=max_age_seconds,
        secure=secure,
        httponly=False,
        samesite="strict",
        path="/",
    )


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
