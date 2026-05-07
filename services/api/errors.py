"""services/api/errors.py — canonical error envelope per dev-guide §3.6.

Every FastAPI error response carries the same shape:

    {"error_code": str, "message": str, "details": dict | None}

`AppError` is the only sanctioned way for route handlers to signal a
typed failure; the registered exception handler converts it to a
`JSONResponse` without leaking stack traces.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorEnvelope(BaseModel):
    error_code: str
    message: str
    details: dict[str, Any] | None = None


class AppError(Exception):
    """Typed app error. Always prefer this over raw HTTPException."""

    def __init__(
        self,
        error_code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.details = details


def _envelope(
    error_code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ErrorEnvelope(
        error_code=error_code,
        message=message,
        details=details,
    ).model_dump(exclude_none=False)


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.error_code, exc.message, details=exc.details),
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_envelope(
            "VALIDATION_ERROR",
            "Request body failed validation.",
            details={"errors": exc.errors()},
        ),
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _http_status_to_error_code(exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, str(exc.detail) if exc.detail else code),
    )


def _http_status_to_error_code(status: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
        503: "SERVICE_UNAVAILABLE",
    }.get(status, f"HTTP_{status}")


def register_error_handlers(app: FastAPI) -> None:
    """Wire AppError, RequestValidationError, and HTTPException to the envelope."""
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]


__all__ = ["AppError", "ErrorEnvelope", "register_error_handlers"]
