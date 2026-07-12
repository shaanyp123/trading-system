"""services/api/session.py — session middleware (real validation + dev stub).

**Real session validation (2026-07-12, security priority from the 2026-07-09
"C1 night one, later" decisions-log entry):** in every environment except
``dev``, requests to non-exempt paths must present a valid
``__Host-trading_session`` cookie. The middleware decodes the cookie,
loads the row via ``services.api.auth.sessions.load_active_session``
(which enforces eviction + absolute-expiry + idle-timeout per backend-spec
§8.5.3), refreshes ``last_activity_utc``, and builds the real
:class:`SessionContext` (auth_strength from ``totp_bootstrap_only``,
``last_uv_at`` from the row — the 5-minute re-auth window now gates on the
REAL WebAuthn UV timestamp). Anything else — missing cookie, malformed
cookie, unknown/evicted/expired session — is refused with 401
``AUTH_REQUIRED``. A database failure during validation refuses with 503
``AUTH_UNAVAILABLE`` (fail closed: an unverifiable session is not a
session).

**Phase-0 stub (dev only):** the Day-15 stub survives exclusively under
``API_ENVIRONMENT=dev`` for local development + the unit-test suite: any
request gets a strong-auth operator session injected. The stub previously
also covered ``paper`` — that was the C1 exposure of 2026-07-09 (the VPS
runs ``paper``, so the entire API on the public apex was stub-authenticated
behind only the Caddy basic-auth edge gate). ``paper`` now takes the real
path.

**Auth ceremony paths are session-exempt** (you cannot hold a session
before logging in): the WebAuthn/TOTP login + recover endpoints, and the
setup-wizard endpoints, which carry their own gate (a ``ceremony_session_id``
minted only by the setup-token flow — see ``routes/setup.py``). CSRF
double-submit still applies to all of them (separate middleware).

**CSRF cookie bootstrap:** any response to a request that arrived without a
``__Host-csrf_token`` cookie gets one minted (bot-authenticated requests
excluded). In real mode this is what lets a fresh anonymous browser POST to
the login endpoints at all — the double-submit token proves same-origin,
not identity, so handing it to anonymous visitors is sound. Idempotent: a
request that already carries the cookie never gets it churned (2026-05-28
regression class).
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast

import structlog
from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from services.api.config import APISettings
from services.api.db import session_scope as _default_session_scope
from services.api.errors import ErrorEnvelope

log = structlog.get_logger()


#: Routes that don't need a session. Three groups:
#:   * the public health probe;
#:   * the setup-token gate itself (mirror of the CSRF allowlist in
#:     ``services/api/middleware._CSRF_EXEMPT_PATHS``);
#:   * the auth ceremony: login endpoints (no session exists yet by
#:     definition), the setup-wizard endpoints (gated by the
#:     ``ceremony_session_id`` that only ``POST /api/setup/verify-token``
#:     mints), and logout (reads + revokes the presented cookie itself; must
#:     work for an already-expired session so the browser can always clear
#:     state).
#:
#: ``/api/sse/events`` is NOT exempt — the SSE multiplexer needs
#: ``session.user_id`` to enforce the per-user N=4 tab limit
#: (frontend-spec §4.6).
_SESSION_EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/health",
        "/api/setup/verify-token",
        "/api/auth/webauthn/register/challenge",
        "/api/auth/webauthn/register/verify",
        "/api/auth/webauthn/challenge",
        "/api/auth/webauthn/verify",
        "/api/auth/totp/enroll-challenge",
        "/api/auth/totp/setup-verify",
        "/api/auth/totp/verify",
        "/api/auth/backup-codes/generate",
        "/api/auth/recover",
        "/api/auth/logout",
    }
)


@dataclass(frozen=True, slots=True)
class SessionContext:
    """The shape downstream route handlers read from ``request.state.session``.

    Fields mirror the ``AuthMeResponse`` schema (services/api/schemas/auth.py)
    plus a ``is_phase0_stub`` flag so code paths can branch on whether the
    data is real (cookie-validated against ``sessions``) or scaffolded
    (dev-only stub).
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
    """Compose the canonical Phase-0 stub session (dev environments only).

    ``last_uv_at`` is set to NOW - 1 minute so risk-loosening endpoints
    that gate on the 5-minute re-auth window see a recent UV.
    ``auth_strength`` is ``"strong"``: the stub explicitly models a
    "WebAuthn-just-verified" session so risk-loosening endpoints are
    reachable in local development. Since 2026-07-12 the stub is
    dev-exclusive — ``paper`` and ``live-*`` validate real session cookies —
    so this synthetic strength never authenticates anything on a deployed
    host.
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


class SessionMiddleware(BaseHTTPMiddleware):
    """Populate ``request.state.session`` or refuse the request.

    Runs INNERMOST of the middleware stack (outer → inner: BotAuth →
    RequestContext → RateLimit → CSRF → **this** → routes; see
    ``services/api/middleware.register_middleware`` for the ordering
    rationale). Dispatch:

      1. Exempt paths pass through untouched (plus CSRF bootstrap).
      2. A session already injected by ``BotAuthMiddleware`` (service
         account, bearer-authenticated) wins — pass through.
      3. ``dev`` env: inject the Phase-0 stub session (local development +
         unit tests).
      4. Everything else (``paper``/``live-small``/``live-scale``): validate
         the session cookie against the ``sessions`` table — 401 on any
         miss, 503 if the database can't answer (fail closed).

    The DB read lives in :meth:`_load_session_context_from_db`; unit tests
    monkeypatch that seam ([A22] — no SQL in unit tests), and the
    Docker-gated integration suite drives the real SQL against Postgres.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        settings: APISettings,
        session_scope: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
    ) -> None:
        super().__init__(app)
        self._cookie_name = settings.session_cookie_name
        self._csrf_cookie_name = settings.csrf_cookie_name
        self._idle_seconds = settings.session_idle_seconds
        self._csrf_max_age = settings.session_absolute_seconds
        self._stub_active = settings.environment == "dev"
        self._session_scope = session_scope or _default_session_scope
        # Match ``_set_session_cookies`` semantics: Secure flag everywhere
        # except ``dev`` env. Required for the ``__Host-`` cookie-name prefix
        # to survive browser-side validation on https origins.
        self._cookie_secure = settings.environment != "dev"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        csrf_cookie_present = self._csrf_cookie_name in request.cookies

        if request.url.path in _SESSION_EXEMPT_PATHS:
            return self._with_csrf_bootstrap(request, await call_next(request), csrf_cookie_present)

        # BotAuthMiddleware (running OUTER of this middleware) may have
        # already injected a service-account session — the bot's bearer-
        # authenticated context wins; don't overwrite and don't demand a
        # cookie the bot doesn't hold.
        if getattr(request.state, "session", None) is not None:
            return await call_next(request)

        if self._stub_active:
            session = _build_phase0_stub_session(self._idle_seconds)
            request.state.session = session
            log.debug(
                "session_stub_injected",
                user_id=session.user_id,
                auth_strength=session.auth_strength,
                cookie_present=self._cookie_name in request.cookies,
                csrf_cookie_present=csrf_cookie_present,
            )
            return self._with_csrf_bootstrap(request, await call_next(request), csrf_cookie_present)

        # --- real validation (paper / live-small / live-scale) -----------
        # Lazy import: services.api.auth.sessions imports SessionContext
        # from this module at load time (same cycle-dodge as BotAuth's
        # SessionContext import; safe at request time).
        from services.api.auth import sessions as sessions_mod

        cookie_val = request.cookies.get(self._cookie_name)
        session_id = sessions_mod.decode_cookie_value(cookie_val) if cookie_val else None
        context: SessionContext | None = None
        if session_id is not None:
            try:
                context = await self._load_session_context_from_db(session_id)
            except Exception:
                log.exception("session_validation_unavailable", path=request.url.path)
                return JSONResponse(
                    status_code=503,
                    content=ErrorEnvelope(
                        error_code="AUTH_UNAVAILABLE",
                        message="Session validation is temporarily unavailable.",
                    ).model_dump(),
                )
        if context is None:
            log.info(
                "session_rejected",
                path=request.url.path,
                cookie_present=cookie_val is not None,
                cookie_decodable=session_id is not None,
            )
            return self._with_csrf_bootstrap(
                request,
                JSONResponse(
                    status_code=401,
                    content=ErrorEnvelope(
                        error_code="AUTH_REQUIRED",
                        message="A valid session is required. Log in and retry.",
                    ).model_dump(),
                ),
                csrf_cookie_present,
            )

        request.state.session = context
        return self._with_csrf_bootstrap(request, await call_next(request), csrf_cookie_present)

    async def _load_session_context_from_db(self, session_id: bytes) -> SessionContext | None:
        """Validate ``session_id`` against ``sessions`` and build the context.

        Returns ``None`` for any invalid session (unknown, evicted, absolute-
        expired, idle-expired, or referencing a missing/malformed accounts
        row). Raises on infrastructure failure — the caller translates to
        503. On success, bumps ``last_activity_utc`` (the idle window per
        backend-spec §8.5.3 is measured request-to-request).
        """
        from services.api.auth import sessions as sessions_mod

        async with self._session_scope() as db:
            row = await sessions_mod.load_active_session(
                db, session_id=session_id, idle_seconds=self._idle_seconds
            )
            if row is None:
                return None
            acct = (
                await db.execute(
                    text(
                        """
                        SELECT a.role,
                               a.external_account_id,
                               EXISTS(
                                   SELECT 1 FROM webauthn_credentials w
                                   WHERE w.account_id = a.id AND w.active = TRUE
                               ) AS webauthn_enrolled,
                               EXISTS(
                                   SELECT 1 FROM totp_secrets t
                                   WHERE t.account_id = a.id
                               ) AS totp_enrolled
                        FROM accounts a
                        WHERE a.id = :acct
                        """
                    ),
                    {"acct": row.account_id},
                )
            ).fetchone()
            if acct is None or acct.role not in ("owner", "reader"):
                # A session pointing at a missing or malformed account row is
                # corruption, not authentication — refuse it.
                log.error(
                    "session_account_row_invalid",
                    account_id=str(row.account_id),
                    role=None if acct is None else acct.role,
                )
                return None
            await sessions_mod.refresh_activity(db, session_id=session_id)
            now = datetime.now(tz=UTC)
            return SessionContext(
                user_id=str(row.account_id),
                # The bootstrap flow writes the username INTO
                # external_account_id (repos/auth.py
                # find_or_create_bootstrap_account) — read the session
                # account's own identity back out rather than assuming the
                # configured operator (a future reader account must never
                # inherit the owner's name).
                username=str(acct.external_account_id),
                role=cast(Literal["owner", "reader"], acct.role),
                auth_strength="weak" if row.totp_bootstrap_only else "strong",
                last_uv_at=row.last_uv_at_utc,
                # Effective expiry = whichever of the absolute deadline or
                # the freshly-reset idle window comes first.
                session_expires_at=min(
                    row.expires_at_utc, now + timedelta(seconds=self._idle_seconds)
                ),
                webauthn_enrolled=bool(acct.webauthn_enrolled),
                totp_enrolled=bool(acct.totp_enrolled),
                is_phase0_stub=False,
            )

    def _with_csrf_bootstrap(
        self,
        request: Request,
        response: Response,
        csrf_cookie_present: bool,
    ) -> Response:
        """Mint the CSRF cookie onto ``response`` iff the request lacked one.

        Skipped for bot-authenticated requests (no cookies in that flow).
        """
        if csrf_cookie_present or getattr(request.state, "is_bot_authenticated", False):
            return response
        _mint_csrf_cookie_on_response(
            response,
            csrf_cookie_name=self._csrf_cookie_name,
            max_age_seconds=self._csrf_max_age,
            secure=self._cookie_secure,
        )
        log.debug("session_csrf_cookie_bootstrapped", path=request.url.path)
        return response


def _mint_csrf_cookie_on_response(
    response: Response,
    *,
    csrf_cookie_name: str,
    max_age_seconds: int,
    secure: bool,
) -> None:
    """Attach a fresh ``__Host-csrf_token`` cookie to the response.

    The real auth flow's ``_set_session_cookies`` in routes/auth.py mints the
    same cookie shape; this helper keeps the attribute set in one place.

    The value is 256 bits of cryptographic randomness via
    :func:`secrets.token_urlsafe`. The CSRF middleware only does a
    constant-time compare against the ``X-CSRF-Token`` header so the
    value is opaque to the server.

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
    paths (or refuses first), so this raises in the rare case of a
    misconfigured middleware order — surfaces the bug loudly during route
    development rather than producing a silent ``AttributeError`` at call
    time.
    """
    session = getattr(request.state, "session", None)
    if session is None:
        raise RuntimeError(
            "SessionContext not set on request.state — "
            "SessionMiddleware must be registered before this route runs"
        )
    if not isinstance(session, SessionContext):
        raise RuntimeError(
            f"request.state.session is {type(session).__name__}, expected SessionContext"
        )
    return session


__all__ = [
    "SessionContext",
    "SessionMiddleware",
    "get_session_context",
]
