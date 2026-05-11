"""services/api/schemas/auth.py — request + response models for `/api/auth/**`.

Backend-spec §4.1.1 declares the canonical endpoint contracts; the models in
this file are the wire shapes for each one. Day 15 (PR #56) shipped
``AuthMeResponse`` only; Day 21 (Week 6 Tue) adds every other model the
WebAuthn / TOTP / backup-code / setup-token endpoints need.

All models declare ``extra="forbid"`` per dev-guide §3 — an unknown field on a
request body is a 422 (RequestValidationError converts to the canonical
envelope via ``services/api/errors.py``), and an unknown field on a response
body fails the FastAPI ``response_model`` round-trip at test time.

WebAuthn JSON shapes (``options`` for challenge endpoints, ``credential`` for
verify endpoints) are typed as ``dict[str, Any]`` here. The actual structure
is fixed by the W3C WebAuthn spec and exercised end-to-end by
``@simplewebauthn/browser`` on the client and ``py_webauthn`` on the server;
we don't re-validate the inner shape here because that's the library's job
and any mismatch shows up as a 401 ``WEBAUTHN_VERIFY_FAILED`` at verify time.

Cross-references:
  * Frontend-spec §5.1 — WebAuthn ceremony
  * Frontend-spec §5.2 — TOTP backup
  * Frontend-spec §5.3 — 8 single-use backup codes (LOCKED)
  * Dev-guide §1.5 — locked auth decisions (UV required, 8 codes, Argon2id)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# GET /api/auth/me (Day 15 — kept for backwards compatibility)
# ---------------------------------------------------------------------------
class AuthMeResponse(BaseModel):
    """Current session info for the logged-in operator.

    ``auth_strength = "weak"`` when the session was issued via a TOTP-only
    fallback path (no WebAuthn UV); ``"strong"`` after a successful WebAuthn
    assertion. ``last_uv_at`` is the timestamp of the most recent WebAuthn
    user-verification, used by the API layer to gate risk-loosening endpoints
    behind the 5-minute re-auth window per dev-guide §1.5.
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


# ---------------------------------------------------------------------------
# POST /api/setup/verify-token (Day 5 — kept; declared here for spec parity)
# ---------------------------------------------------------------------------
class SetupTokenVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=8, max_length=256)


class SetupTokenVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: Literal[True]
    intended_role: Literal["owner", "reader"]
    ceremony_session_id: str


# ---------------------------------------------------------------------------
# WebAuthn — registration ceremony (auth: ceremony_session_id from /setup)
# ---------------------------------------------------------------------------
class WebAuthnRegisterChallengeRequest(BaseModel):
    """Body for ``POST /api/auth/webauthn/register/challenge``.

    The ceremony_session_id was minted by ``/api/setup/verify-token``. It
    proves the caller knows a valid setup token without re-submitting the
    token raw on every wizard step.
    """

    model_config = ConfigDict(extra="forbid")

    ceremony_session_id: str = Field(min_length=8, max_length=128)
    nickname: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description='User-friendly device label, e.g. "YubiKey 5C" or "iPhone Touch ID".',
    )


class WebAuthnRegisterChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: dict[str, Any]
    """W3C ``PublicKeyCredentialCreationOptions`` as JSON (base64url encoded
    challenge + user_id per py_webauthn ``options_to_json``)."""


class WebAuthnRegisterVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_session_id: str = Field(min_length=8, max_length=128)
    credential: dict[str, Any]
    """The raw ``PublicKeyCredential`` JSON returned by
    ``navigator.credentials.create()`` (base64url encoded blobs)."""


class WebAuthnRegisterVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[True]
    credential_id_b64: str
    """The newly registered credential's ID, base64url encoded. Frontend
    persists nothing — this is informational only."""


# ---------------------------------------------------------------------------
# WebAuthn — login ceremony (no auth required)
# ---------------------------------------------------------------------------
class WebAuthnChallengeRequest(BaseModel):
    """Body for ``POST /api/auth/webauthn/challenge``.

    Day 21 ignores ``target_url`` server-side (the frontend keeps the intended
    target in its own state); the field is accepted for spec compatibility.
    """

    model_config = ConfigDict(extra="forbid")

    target_url: str | None = Field(default=None, max_length=512)


class WebAuthnChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_id: str
    options: dict[str, Any]


class WebAuthnVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_id: str = Field(min_length=8, max_length=128)
    credential: dict[str, Any]


class WebAuthnVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[True]
    target_url: str | None


# ---------------------------------------------------------------------------
# TOTP — enrollment (auth: ceremony_session_id)
# ---------------------------------------------------------------------------
class TotpEnrollChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_session_id: str = Field(min_length=8, max_length=128)


class TotpEnrollChallengeResponse(BaseModel):
    """Returned exactly once per ceremony; the secret is the only material the
    operator needs to add the account to their authenticator app."""

    model_config = ConfigDict(extra="forbid")

    secret_base32: str
    """Base32-encoded TOTP secret (160 bits = 32 chars). The operator types
    this into their authenticator app if they can't scan the QR code."""

    provisioning_uri: str
    """RFC 6238 ``otpauth://`` URI. The frontend renders this as a QR code
    via the ``qrcode`` npm package."""


class TotpSetupVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_session_id: str = Field(min_length=8, max_length=128)
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TotpSetupVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[True]


# ---------------------------------------------------------------------------
# TOTP — login fallback (no auth required; issues weak session)
# ---------------------------------------------------------------------------
class TotpLoginVerifyRequest(BaseModel):
    """Body for ``POST /api/auth/totp/verify`` — TOTP-only login path.

    Username is required because there is no session cookie yet; the server
    looks up the account by username (Phase 0: ``"operator"`` for the single-
    operator system) and checks the 6-digit code against the encrypted secret.
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TotpLoginVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[True]
    auth_strength: Literal["weak"]
    """Always ``"weak"`` per frontend-spec §5.4 — TOTP-only login is reduced-
    privilege until the operator adds WebAuthn."""


# ---------------------------------------------------------------------------
# Backup codes — generation (during setup wizard) + recovery + regeneration
# ---------------------------------------------------------------------------
class BackupCodesGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_session_id: str = Field(min_length=8, max_length=128)


class BackupCodesGenerateResponse(BaseModel):
    """Returned exactly once; raw codes are never persisted server-side."""

    model_config = ConfigDict(extra="forbid")

    codes: list[str] = Field(min_length=8, max_length=8)
    """Eight codes in locked format ``ABCDE-FGHIJ`` (10-char base32 in 2
    groups of 5) per dev-guide §1.5 + frontend-spec §5.3."""

    generation_id: str
    """UUID grouping the batch; frontend may display alongside the codes for
    audit reference."""


class RecoverRequest(BaseModel):
    """Body for ``POST /api/auth/recover`` — backup-code recovery path."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    backup_code: str = Field(min_length=10, max_length=20)
    """Operator types ``ABCDE-FGHIJ`` (with or without hyphen); the server
    normalizes by stripping non-base32 chars before Argon2id-verifying."""


class RecoverResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[True]
    auth_strength: Literal["weak"]
    """Always ``"weak"`` — recovery puts the operator into a reduced-privilege
    session and the frontend immediately walks them through re-enrollment of
    WebAuthn + TOTP + a fresh batch of 8 backup codes."""


class BackupCodesRegenerateResponse(BaseModel):
    """Re-auth-required regeneration of the 8 codes. Old codes invalidated."""

    model_config = ConfigDict(extra="forbid")

    codes: list[str] = Field(min_length=8, max_length=8)
    generation_id: str


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
class LogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[True]


__all__ = [
    "AuthMeResponse",
    "BackupCodesGenerateRequest",
    "BackupCodesGenerateResponse",
    "BackupCodesRegenerateResponse",
    "LogoutResponse",
    "RecoverRequest",
    "RecoverResponse",
    "SetupTokenVerifyRequest",
    "SetupTokenVerifyResponse",
    "TotpEnrollChallengeRequest",
    "TotpEnrollChallengeResponse",
    "TotpLoginVerifyRequest",
    "TotpLoginVerifyResponse",
    "TotpSetupVerifyRequest",
    "TotpSetupVerifyResponse",
    "WebAuthnChallengeRequest",
    "WebAuthnChallengeResponse",
    "WebAuthnRegisterChallengeRequest",
    "WebAuthnRegisterChallengeResponse",
    "WebAuthnRegisterVerifyRequest",
    "WebAuthnRegisterVerifyResponse",
    "WebAuthnVerifyRequest",
    "WebAuthnVerifyResponse",
]
