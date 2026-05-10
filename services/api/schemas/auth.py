"""services/api/schemas/auth.py — `/api/auth/me` Phase-1 schema.

Backend-spec §4.1.1 ``AuthMeResponse``.

Day 15 emits a Phase-0 stub response derived from the session-stub middleware
(``services/api/session.py``). The stub asserts the operator has not yet run
WebAuthn registration (Week 6) — ``webauthn_enrolled=False`` and
``totp_enrolled=False`` — but still returns a valid envelope so the frontend's
landing-page first paint can render the Today tile without an additional 401
round-trip during local development.

When the WebAuthn ceremony lands Week 6, the stub branch is removed; this
schema does not need to change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class AuthMeResponse(BaseModel):
    """Current session info for the logged-in operator.

    ``auth_strength = "weak"`` when the session was issued via a TOTP-only
    fallback path (no WebAuthn UV); ``"strong"`` after a successful WebAuthn
    assertion. Phase-0 stub returns ``"weak"``.

    ``last_uv_at`` is the timestamp of the most recent WebAuthn user-
    verification, used by the API layer to gate risk-loosening endpoints
    behind the 5-minute re-auth window per dev-guide §1.5. ``None`` until the
    operator has done a real WebAuthn ceremony.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str
    username: str
    role: Literal["owner", "reader"]
    auth_strength: Literal["weak", "strong"]
    last_uv_at: datetime | None
    session_expires_at: datetime
    webauthn_enrolled: bool
    totp_enrolled: bool


__all__ = ["AuthMeResponse"]
