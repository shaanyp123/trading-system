"""services/api/routes/auth.py — `/api/auth/me` Phase-1 endpoint.

Backend-spec §4.1.1 ``GET /api/auth/me``. Day 15 returns the Phase-0 stub
``SessionContext`` injected by ``SessionStubMiddleware``. WebAuthn / TOTP /
recover / logout / backup-codes endpoints from §4.1.1 are deferred to Week 6
when the frontend auth flow lands.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from services.api.schemas.auth import AuthMeResponse
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


@router.get(
    "/api/auth/me",
    tags=["auth"],
    response_model=AuthMeResponse,
)
async def auth_me(
    session: SessionContext = Depends(get_session_context),
) -> AuthMeResponse:
    return AuthMeResponse(
        user_id=session.user_id,
        username=session.username,
        role=session.role,
        auth_strength=session.auth_strength,
        last_uv_at=session.last_uv_at,
        session_expires_at=session.session_expires_at,
        webauthn_enrolled=session.webauthn_enrolled,
        totp_enrolled=session.totp_enrolled,
    )


__all__ = ["router"]
