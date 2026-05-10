"""services/api/middleware.py — request-scoped middleware.

Day 5 scope (skeleton):

  * `RequestContextMiddleware` — binds a per-request `trace_id` to the
    structlog context (dev-guide §7.1.1) and emits `request_started` /
    `request_completed` log lines with timing.
  * `CSRFMiddleware` — double-submit cookie pattern per backend-spec
    §8.5.4. Enforced on state-changing methods (POST/PUT/PATCH/DELETE)
    EXCEPT for the small allowlist below (bootstrap + watchdog push).

Day 17 addition (IG §3 Week 5 Wed):

  * `RateLimitMiddleware` — fixed-window per-IP rate limiter. Two buckets:
    `general` (100 req / 10s on `/api/**`) and `auth` (5 req / 10s on
    `/api/auth/**`). Long-lived + health-check paths are exempt (otherwise
    the SSE multiplexer's first subscriber would burn its bucket on
    connection). The api enforces this instead of Caddy because the stock
    `caddy:2-alpine` image lacks the `mholt/caddy-ratelimit` community
    module; Phase 1 may move to Caddy-edge filtering if real load justifies
    the custom build. See Docs/decisions-log.md "Day 17 09:00" entry.

Session cookies (`__Host-trading_session`) are NOT issued in Day 5: the
session table doesn't exist yet (Phase 0 Week 2 ships it). The cookie
NAME is reserved in `APISettings.session_cookie_name` so future routes
can share the constant; no middleware reads it yet.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from services.api.config import APISettings
from services.api.errors import ErrorEnvelope

log = structlog.get_logger()


# Routes that are exempt from CSRF enforcement. Bootstrap + machine-to-machine
# endpoints — they authenticate via cryptographic material in the request body
# (raw setup token; bearer auth) rather than via session cookie + CSRF.
_CSRF_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/setup/verify-token",
        "/api/internal/watchdog",
    }
)


# Paths exempt from rate limiting. /api/health is the watchdog's poll target
# (a 5-min cron would chew through the bucket immediately); /api/internal/watchdog
# is the inbound push (single trusted Hetzner Nuremberg IP; no rate-limit
# defence needed). /api/sse/events is a long-lived single-connection stream
# (a tab would burn its 100-req bucket on reconnect bursts otherwise). The N=4
# tab limit in the SSE multiplexer already bounds per-user concurrency.
_RATE_LIMIT_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/internal/watchdog",
        "/api/sse/events",
    }
)
_RATE_LIMIT_AUTH_PREFIX = "/api/auth/"
_RATE_LIMIT_API_PREFIX = "/api/"


# Indirected so tests can monkeypatch a deterministic clock. `time.monotonic`
# is invariant across system clock changes (correct for window math).
def _monotonic() -> float:
    return time.monotonic()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind trace_id to structlog context; log request entry + exit."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
        )
        start = time.perf_counter()
        log.info("request_started")
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request_failed", elapsed_ms=_ms_since(start))
            raise
        else:
            response.headers["X-Trace-Id"] = trace_id
            log.info(
                "request_completed",
                status_code=response.status_code,
                elapsed_ms=_ms_since(start),
            )
            return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF check for state-changing requests."""

    def __init__(self, app: FastAPI, *, settings: APISettings) -> None:
        super().__init__(app)
        self._cookie_name = settings.csrf_cookie_name
        self._header_name = settings.csrf_header_name

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path not in _CSRF_EXEMPT_PATHS
        ):
            cookie_val = request.cookies.get(self._cookie_name)
            header_val = request.headers.get(self._header_name)
            if not cookie_val or not header_val or cookie_val != header_val:
                log.warning(
                    "csrf_rejected",
                    path=request.url.path,
                    cookie_present=bool(cookie_val),
                    header_present=bool(header_val),
                )
                return JSONResponse(
                    status_code=403,
                    content=ErrorEnvelope(
                        error_code="CSRF_REJECTED",
                        message="CSRF token missing or mismatched.",
                    ).model_dump(),
                )
        return await call_next(request)


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 2)


@dataclass
class _Bucket:
    """Fixed-window counter state. Reset when `_monotonic() - started_at >= window_seconds`."""

    count: int
    started_at: float


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP fixed-window rate limiter.

    Two buckets per IG §3 Week 5 Wed:
      * `general` — 100 req / 10s on `/api/**` (excludes auth, SSE, health,
        internal/watchdog).
      * `auth`    —   5 req / 10s on `/api/auth/**`. Tighter because the
        threat model is credential-stuffing / brute-force.

    Implementation notes:
      * Fixed-window counter, NOT sliding-window or token-bucket. The
        spec contract is "N per window" which the fixed-window model
        satisfies exactly. The known burst-at-window-edge property (2N
        requests possible in a 2-second span straddling a window boundary)
        is acceptable for Phase 0; we can move to sliding-window in Phase 1
        if real attack data shows the edge-burst being exploited.
      * `_monotonic` (module-level) is indirected for test injection so a
        unit test can advance the clock deterministically without sleeping.
      * In-process `dict` is the entire store: single-replica Phase 0
        deploy means no cross-replica coordination is needed. When the
        api fleet scales out (Phase 2+), this middleware would need to
        move to Redis or postgres-backed state (or move to Caddy via the
        ratelimit module).
      * `request.client.host` is the resolved IP because uvicorn runs with
        `--proxy-headers --forwarded-allow-ips *` (services/api/entrypoint.py),
        which trusts Caddy's `X-Forwarded-For`. With the api on the internal
        Docker network and Caddy on `[internal, egress]`, Caddy is the only
        reachable upstream — the trust boundary is sound.
    """

    def __init__(self, app: FastAPI, *, settings: APISettings) -> None:
        super().__init__(app)
        self._window_seconds = float(settings.rate_limit_window_seconds)
        self._limit_general = int(settings.rate_limit_general_per_window)
        self._limit_auth = int(settings.rate_limit_auth_per_window)
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = asyncio.Lock()

    def _select_bucket(self, path: str) -> tuple[str, int] | None:
        """Return (bucket_name, limit) for the given path, or None if exempt.

        Exempt paths short-circuit BEFORE we touch the dict so the counter
        isn't even allocated for /api/health (which the watchdog hits every
        5 min from a single IP) or /api/sse/events (which a single tab holds
        open for hours).
        """
        if path in _RATE_LIMIT_EXEMPT_PATHS:
            return None
        if not path.startswith(_RATE_LIMIT_API_PREFIX):
            return None
        if path.startswith(_RATE_LIMIT_AUTH_PREFIX):
            return ("auth", self._limit_auth)
        return ("general", self._limit_general)

    def _client_key(self, request: Request) -> str:
        # request.client is None for in-memory ASGI transports; tests cover the
        # unknown-host path explicitly. Real traffic always has it set via
        # uvicorn + --proxy-headers.
        if request.client and request.client.host:
            return request.client.host
        return "unknown"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        selection = self._select_bucket(request.url.path)
        if selection is None:
            return await call_next(request)
        bucket_name, limit = selection
        key = (self._client_key(request), bucket_name)
        now = _monotonic()

        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or now - bucket.started_at >= self._window_seconds:
                bucket = _Bucket(count=0, started_at=now)
                self._buckets[key] = bucket
            bucket.count += 1
            allowed = bucket.count <= limit
            # Retry-After (seconds until the current window resets). Always
            # >= 1 to give clients a sensible minimum backoff.
            seconds_left = max(0.0, self._window_seconds - (now - bucket.started_at))
            retry_after = max(1, int(seconds_left) + 1) if not allowed else 0

        if allowed:
            return await call_next(request)

        log.warning(
            "rate_limit_exceeded",
            client_host=key[0],
            bucket=bucket_name,
            count=bucket.count,
            limit=limit,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=429,
            content=ErrorEnvelope(
                error_code="RATE_LIMITED",
                message=(
                    f"Too many requests. Rate limit for this endpoint is "
                    f"{limit} per {int(self._window_seconds)}s."
                ),
                details={"bucket": bucket_name, "limit": limit, "retry_after_s": retry_after},
            ).model_dump(),
            headers={"Retry-After": str(retry_after)},
        )


def register_middleware(app: FastAPI, settings: APISettings) -> None:
    """Wire middleware. Starlette's `add_middleware` makes the LAST-ADDED
    layer OUTERMOST in the request path (verified empirically; see Day 17
    PR description). Resulting request path, outermost → innermost:

        RequestContext → RateLimit → CSRF → (SessionStub*) → routes

    (* SessionStub is added separately in main.py AFTER register_middleware,
    so it ends up outermost of all four — request flow becomes
    SessionStub → RequestContext → RateLimit → CSRF → routes. The SessionStub
    only mutates `request.state` and never short-circuits, so it does not
    affect the gate semantics below.)

    Order rationale:
      * `RequestContext` outermost: binds `trace_id` to structlog so a
        rejection by RateLimit OR CSRF still emits `request_completed` /
        `csrf_rejected` / `rate_limit_exceeded` lines with `trace_id`
        attached for ops grep-ability.
      * `RateLimit` outer of `CSRF`: a flood of CSRF-failing POSTs from a
        single attacker IP would otherwise bypass the limiter entirely
        (CSRF rejection short-circuits before RateLimit's counter
        increment). Putting RateLimit outer ensures every request — valid
        or not — consumes one bucket slot.
      * `CSRF` innermost of the gates: by the time CSRF runs, RateLimit
        has cleared, so CSRF's constant-time cookie+header comparison only
        executes for requests that survived the per-IP budget.
    """
    # mypy is strict about the kwargs signature of the middleware factory;
    # starlette's runtime accepts kwargs that BaseHTTPMiddleware subclasses
    # consume in __init__, but the annotation doesn't capture that.
    app.add_middleware(CSRFMiddleware, settings=settings)  # type: ignore[arg-type]
    app.add_middleware(RateLimitMiddleware, settings=settings)  # type: ignore[arg-type]
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", settings.csrf_header_name, "X-Trace-Id"],
        )


__all__ = [
    "CSRFMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "register_middleware",
]
