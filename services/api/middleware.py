"""services/api/middleware.py — request-scoped middleware.

Day 5 scope (skeleton):

  * `RequestContextMiddleware` — binds a per-request `trace_id` to the
    structlog context (dev-guide §7.1.1) and emits `request_started` /
    `request_completed` log lines with timing.
  * `CSRFMiddleware` — double-submit cookie pattern per backend-spec
    §8.5.4. Enforced on state-changing methods (POST/PUT/PATCH/DELETE)
    EXCEPT for the small allowlist below (bootstrap + watchdog push).

Session cookies (`__Host-trading_session`) are NOT issued in Day 5: the
session table doesn't exist yet (Phase 0 Week 2 ships it). The cookie
NAME is reserved in `APISettings.session_cookie_name` so future routes
can share the constant; no middleware reads it yet.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

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


def register_middleware(app: FastAPI, settings: APISettings) -> None:
    """Wire middleware in execution order (last-added = outermost).

    Order matters: RequestContext must wrap CSRF so that a CSRF rejection
    still emits a `request_completed` log line with the trace_id bound.
    """
    # mypy is strict about the kwargs signature of the middleware factory;
    # starlette's runtime accepts kwargs that BaseHTTPMiddleware subclasses
    # consume in __init__, but the annotation doesn't capture that.
    app.add_middleware(CSRFMiddleware, settings=settings)  # type: ignore[arg-type]
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
    "RequestContextMiddleware",
    "register_middleware",
]
