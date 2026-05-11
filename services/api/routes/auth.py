"""services/api/routes/auth.py — WebAuthn / TOTP / backup-code endpoints.

Day 21 (Week 6 Tue) wires the full auth ceremony surface per backend-spec
§4.1.1. The Day 15 ``GET /api/auth/me`` stub remains compatible.

Endpoint inventory (matches backend-spec §4.1.1 line 1700-1710):

  * ``POST /api/auth/webauthn/challenge``           — start login
  * ``POST /api/auth/webauthn/verify``              — complete login
  * ``POST /api/auth/webauthn/register/challenge``  — start registration
  * ``POST /api/auth/webauthn/register/verify``     — complete registration
  * ``POST /api/auth/totp/enroll-challenge``        — start TOTP enrollment
  * ``POST /api/auth/totp/setup-verify``            — finish TOTP enrollment
  * ``POST /api/auth/totp/verify``                  — TOTP login fallback
  * ``POST /api/auth/backup-codes/generate``        — mint 8 codes (setup)
  * ``POST /api/auth/backup-codes/regenerate``      — mint 8 codes (re-auth)
  * ``POST /api/auth/recover``                      — backup-code login
  * ``POST /api/auth/logout``                       — invalidate session
  * ``GET  /api/auth/me``                           — session info

The setup-wizard endpoints (``register/*``, ``totp/enroll-challenge``,
``totp/setup-verify``, ``backup-codes/generate``) authenticate via the
``ceremony_session_id`` minted by ``POST /api/setup/verify-token`` and stored
in the in-memory ``CeremonyStore``. They MUST NOT depend on the session
cookie -- the operator hasn't been issued one until the wizard completes.

The login endpoints (``webauthn/challenge``, ``webauthn/verify``,
``totp/verify``, ``recover``) issue the session cookie + CSRF cookie on
success. The session cookie name is ``__Host-trading_session`` per
backend-spec §8.5.3.

A05 N/A (auth code doesn't touch money). A06 enforced (all timestamps
tz-aware UTC). A22 covered by integration tests where the session round-trip
hits real Postgres. A01/A04: when audit-log writes wire up (Phase 1+ via the
risk dispatcher), the audit event types `WEBAUTHN_REGISTERED` /
`WEBAUTHN_LOGIN` / `TOTP_LOGIN` / `BACKUP_CODE_USED` are already in the
locked taxonomy (services/audit/event_types.py:133-136); no new enum
required.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers.exceptions import WebAuthnException

from services.api.auth import backup_codes as backup_codes_mod
from services.api.auth import sessions as sessions_mod
from services.api.auth import totp as totp_mod
from services.api.auth import webauthn as webauthn_mod
from services.api.auth.ceremony import (
    CeremonyState,
    CeremonyStore,
    get_ceremony_store,
)
from services.api.config import APISettings, get_settings
from services.api.db import get_session
from services.api.errors import AppError
from services.api.repos.auth import AuthRepo, PostgresAuthRepo
from services.api.schemas.auth import (
    AuthMeResponse,
    BackupCodesGenerateRequest,
    BackupCodesGenerateResponse,
    BackupCodesRegenerateResponse,
    LogoutResponse,
    RecoverRequest,
    RecoverResponse,
    TotpEnrollChallengeRequest,
    TotpEnrollChallengeResponse,
    TotpLoginVerifyRequest,
    TotpLoginVerifyResponse,
    TotpSetupVerifyRequest,
    TotpSetupVerifyResponse,
    WebAuthnChallengeRequest,
    WebAuthnChallengeResponse,
    WebAuthnRegisterChallengeRequest,
    WebAuthnRegisterChallengeResponse,
    WebAuthnRegisterVerifyRequest,
    WebAuthnRegisterVerifyResponse,
    WebAuthnVerifyRequest,
    WebAuthnVerifyResponse,
)
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def _get_auth_repo(
    db: AsyncSession = Depends(get_session),
) -> AuthRepo:
    """Override-able dep for unit tests via FastAPI ``dependency_overrides``."""
    return PostgresAuthRepo(db)


def _get_db(
    db: AsyncSession = Depends(get_session),
) -> AsyncSession:
    """Pass-through dep so the route can also reach the raw session for
    ``sessions_mod.create_session`` (which writes to ``sessions`` directly)."""
    return db


def _require_totp_key(settings: APISettings) -> bytes:
    if settings.totp_encryption_key is None:
        raise AppError(
            error_code="TOTP_KEY_MISSING",
            message=(
                "TOTP enrollment requires API_TOTP_ENCRYPTION_KEY to be "
                "configured. Operator must populate the sops bundle before "
                "running the setup wizard."
            ),
            status_code=503,
        )
    return totp_mod.load_encryption_key(settings.totp_encryption_key.get_secret_value())


async def _get_ceremony(
    store: CeremonyStore,
    ceremony_session_id: str,
    *,
    expected_kind: str,
) -> CeremonyState:
    state = await store.get(ceremony_session_id)
    if state is None or state.kind != expected_kind:
        raise AppError(
            error_code="INVALID_CEREMONY_SESSION",
            message="Ceremony session is invalid or expired.",
            status_code=401,
        )
    return state


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------
def _set_session_cookies(
    response: Response,
    *,
    settings: APISettings,
    session_id: bytes,
) -> str:
    """Set the `__Host-trading_session` + CSRF cookies on the response.

    Returns the CSRF token value (the frontend reads it from the cookie via
    document.cookie; the value is also returned here so route handlers can
    log it if needed).
    """
    secure = settings.environment != "dev"
    cookie_value = sessions_mod.encode_cookie_value(session_id)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=cookie_value,
        max_age=settings.session_absolute_seconds,
        secure=secure,
        httponly=True,
        samesite="strict",
        path="/",
    )
    csrf_value = secrets.token_urlsafe(32)
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_value,
        max_age=settings.session_absolute_seconds,
        secure=secure,
        httponly=False,  # frontend JS reads it
        samesite="strict",
        path="/",
    )
    return csrf_value


def _clear_session_cookies(response: Response, *, settings: APISettings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


# ---------------------------------------------------------------------------
# GET /api/auth/me — Day 15-compatible (returns session info)
# ---------------------------------------------------------------------------
@router.get("/api/auth/me", tags=["auth"], response_model=AuthMeResponse)
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


# ---------------------------------------------------------------------------
# WebAuthn registration (setup wizard)
# ---------------------------------------------------------------------------
@router.post(
    "/api/auth/webauthn/register/challenge",
    tags=["auth"],
    response_model=WebAuthnRegisterChallengeResponse,
)
async def webauthn_register_challenge(
    body: WebAuthnRegisterChallengeRequest,
    store: CeremonyStore = Depends(get_ceremony_store),
    repo: AuthRepo = Depends(_get_auth_repo),
    settings: APISettings = Depends(get_settings),
) -> WebAuthnRegisterChallengeResponse:
    state = await _get_ceremony(store, body.ceremony_session_id, expected_kind="setup")

    # Ensure the bootstrap account exists; subsequent steps reference its id.
    role = state.intended_role or "owner"
    account = await repo.find_or_create_bootstrap_account(settings.operator_username, role)

    # Build exclude-list so the authenticator refuses to re-register the same
    # credential the operator already holds (W3C spec calls this a "wrong-
    # authenticator-attached" UX guard).
    existing = await repo.list_webauthn_credentials(account.id, active_only=True)
    exclude_ids = [c.credential_id for c in existing]

    challenge = webauthn_mod.generate_registration_challenge(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_name=settings.operator_username,
        user_display_name=settings.operator_username,
        exclude_credential_ids=exclude_ids,
    )
    state.webauthn_register_challenge = challenge.challenge_bytes
    state.webauthn_user_id = challenge.user_id_bytes
    state.webauthn_nickname = body.nickname
    state.account_id = account.id
    await store.update(state)
    log.info(
        "webauthn_register_challenge_issued",
        ceremony_session_id=state.ceremony_key,
        account_id=str(account.id),
    )
    return WebAuthnRegisterChallengeResponse(options=challenge.options_dict)


@router.post(
    "/api/auth/webauthn/register/verify",
    tags=["auth"],
    response_model=WebAuthnRegisterVerifyResponse,
)
async def webauthn_register_verify(
    body: WebAuthnRegisterVerifyRequest,
    response: Response,
    request: Request,
    store: CeremonyStore = Depends(get_ceremony_store),
    repo: AuthRepo = Depends(_get_auth_repo),
    db: AsyncSession = Depends(_get_db),
    settings: APISettings = Depends(get_settings),
) -> WebAuthnRegisterVerifyResponse:
    state = await _get_ceremony(store, body.ceremony_session_id, expected_kind="setup")
    if state.webauthn_register_challenge is None or state.account_id is None:
        raise AppError(
            error_code="CEREMONY_OUT_OF_ORDER",
            message="WebAuthn register/challenge must be called before /verify.",
            status_code=400,
        )

    try:
        verified = webauthn_mod.verify_registration(
            credential=body.credential,
            expected_challenge=state.webauthn_register_challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
        )
    except WebAuthnException as exc:
        log.warning("webauthn_register_verify_failed", reason=str(exc))
        raise AppError(
            error_code="WEBAUTHN_VERIFY_FAILED",
            message="WebAuthn registration could not be verified.",
            status_code=401,
        ) from exc

    new_credential_id = await repo.insert_webauthn_credential(
        account_id=state.account_id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        authenticator_attachment=verified.authenticator_attachment,
        nickname=state.webauthn_nickname,
    )

    # Issue a session immediately so the operator is signed in after the
    # wizard's WebAuthn step (the remaining wizard steps -- TOTP enroll and
    # backup-code generate -- are session-authenticated via this cookie OR
    # via the ceremony_session_id, whichever the frontend uses).
    now = datetime.now(tz=UTC)
    session_row = await sessions_mod.create_session(
        db,
        account_id=state.account_id,
        totp_bootstrap_only=False,
        last_uv_at_utc=now,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    _set_session_cookies(response, settings=settings, session_id=session_row.session_id)

    # Don't drop the ceremony yet -- the remaining wizard steps still need
    # state.account_id. The store TTL handles eventual cleanup.
    log.info(
        "webauthn_registered",
        account_id=str(state.account_id),
        credential_uuid=str(new_credential_id),
    )

    import base64

    cred_b64 = base64.urlsafe_b64encode(verified.credential_id).rstrip(b"=").decode("ascii")
    return WebAuthnRegisterVerifyResponse(success=True, credential_id_b64=cred_b64)


# ---------------------------------------------------------------------------
# WebAuthn login
# ---------------------------------------------------------------------------
@router.post(
    "/api/auth/webauthn/challenge",
    tags=["auth"],
    response_model=WebAuthnChallengeResponse,
)
async def webauthn_login_challenge(
    body: WebAuthnChallengeRequest,
    store: CeremonyStore = Depends(get_ceremony_store),
    repo: AuthRepo = Depends(_get_auth_repo),
    settings: APISettings = Depends(get_settings),
) -> WebAuthnChallengeResponse:
    # Phase 0 single-operator: load credentials for the configured username.
    account = await repo.find_account_by_username(settings.operator_username)
    if account is None:
        raise AppError(
            error_code="ACCOUNT_NOT_FOUND",
            message="No registered operator account. Run /setup first.",
            status_code=404,
        )
    credentials = await repo.list_webauthn_credentials(account.id, active_only=True)
    if not credentials:
        raise AppError(
            error_code="NO_WEBAUTHN_CREDENTIALS",
            message=(
                "No WebAuthn credentials are registered for this operator. "
                "Use the TOTP fallback or backup-code recovery."
            ),
            status_code=404,
        )

    challenge = webauthn_mod.generate_authentication_challenge(
        rp_id=settings.webauthn_rp_id,
        allow_credential_ids=[c.credential_id for c in credentials],
    )
    ceremony_id = await store.create_login_ceremony(
        allowed_credential_ids=[c.credential_id for c in credentials],
        target_url=body.target_url,
    )
    # Stash the challenge on the just-minted state.
    state = await store.get(ceremony_id)
    assert state is not None, "login ceremony missing immediately after creation"
    state.webauthn_login_challenge = challenge.challenge_bytes
    await store.update(state)
    log.info("webauthn_login_challenge_issued", ceremony_id=ceremony_id)
    return WebAuthnChallengeResponse(ceremony_id=ceremony_id, options=challenge.options_dict)


@router.post(
    "/api/auth/webauthn/verify",
    tags=["auth"],
    response_model=WebAuthnVerifyResponse,
)
async def webauthn_login_verify(
    body: WebAuthnVerifyRequest,
    response: Response,
    request: Request,
    store: CeremonyStore = Depends(get_ceremony_store),
    repo: AuthRepo = Depends(_get_auth_repo),
    db: AsyncSession = Depends(_get_db),
    settings: APISettings = Depends(get_settings),
) -> WebAuthnVerifyResponse:
    state = await store.consume(body.ceremony_id)
    if state is None or state.kind != "login" or state.webauthn_login_challenge is None:
        raise AppError(
            error_code="INVALID_CEREMONY",
            message="WebAuthn login ceremony is invalid or expired.",
            status_code=401,
        )

    # Pull the credential row by the credential id the assertion identifies.
    # The frontend sends ``credential.id`` (base64url-encoded); py_webauthn
    # decodes that internally during verify, so we look up by what we sent.
    raw_id_b64 = body.credential.get("id") or body.credential.get("rawId")
    if not isinstance(raw_id_b64, str):
        raise AppError(
            error_code="MALFORMED_CREDENTIAL",
            message="WebAuthn credential payload missing 'id'/'rawId'.",
            status_code=400,
        )
    credential_id = _b64url_decode(raw_id_b64)
    row = await repo.get_webauthn_credential_by_id(credential_id)
    if row is None or not row.active:
        raise AppError(
            error_code="WEBAUTHN_VERIFY_FAILED",
            message="No matching WebAuthn credential for that assertion.",
            status_code=401,
        )

    try:
        verified = webauthn_mod.verify_authentication(
            credential=body.credential,
            expected_challenge=state.webauthn_login_challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=row.public_key,
            current_sign_count=row.sign_count,
        )
    except WebAuthnException as exc:
        log.warning("webauthn_login_verify_failed", reason=str(exc))
        raise AppError(
            error_code="WEBAUTHN_VERIFY_FAILED",
            message="WebAuthn assertion could not be verified.",
            status_code=401,
        ) from exc

    await repo.update_webauthn_sign_count(
        credential_id=verified.credential_id,
        new_sign_count=verified.new_sign_count,
    )

    now = datetime.now(tz=UTC)
    session_row = await sessions_mod.create_session(
        db,
        account_id=row.account_id,
        totp_bootstrap_only=False,
        last_uv_at_utc=now,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    _set_session_cookies(response, settings=settings, session_id=session_row.session_id)
    log.info("webauthn_login_success", account_id=str(row.account_id))
    return WebAuthnVerifyResponse(success=True, target_url=state.target_url)


# ---------------------------------------------------------------------------
# TOTP enrollment (setup wizard) + login fallback
# ---------------------------------------------------------------------------
@router.post(
    "/api/auth/totp/enroll-challenge",
    tags=["auth"],
    response_model=TotpEnrollChallengeResponse,
)
async def totp_enroll_challenge(
    body: TotpEnrollChallengeRequest,
    store: CeremonyStore = Depends(get_ceremony_store),
    settings: APISettings = Depends(get_settings),
) -> TotpEnrollChallengeResponse:
    _require_totp_key(settings)  # fail fast if column-encryption key missing
    state = await _get_ceremony(store, body.ceremony_session_id, expected_kind="setup")
    enrollment = totp_mod.generate_secret(
        account_name=settings.operator_username,
        issuer=settings.webauthn_rp_name,
    )
    state.totp_secret_base32 = enrollment.secret_base32
    state.totp_secret_verified = False
    await store.update(state)
    log.info("totp_enroll_challenge_issued", ceremony_session_id=state.ceremony_key)
    return TotpEnrollChallengeResponse(
        secret_base32=enrollment.secret_base32,
        provisioning_uri=enrollment.provisioning_uri,
    )


@router.post(
    "/api/auth/totp/setup-verify",
    tags=["auth"],
    response_model=TotpSetupVerifyResponse,
)
async def totp_setup_verify(
    body: TotpSetupVerifyRequest,
    store: CeremonyStore = Depends(get_ceremony_store),
    repo: AuthRepo = Depends(_get_auth_repo),
    settings: APISettings = Depends(get_settings),
) -> TotpSetupVerifyResponse:
    key = _require_totp_key(settings)
    state = await _get_ceremony(store, body.ceremony_session_id, expected_kind="setup")
    if state.totp_secret_base32 is None or state.account_id is None:
        raise AppError(
            error_code="CEREMONY_OUT_OF_ORDER",
            message="TOTP enroll-challenge must succeed before setup-verify.",
            status_code=400,
        )

    if not totp_mod.verify_code(secret_base32=state.totp_secret_base32, code=body.totp_code):
        log.warning("totp_setup_verify_failed_code", account_id=str(state.account_id))
        raise AppError(
            error_code="TOTP_CODE_INVALID",
            message="TOTP code did not verify.",
            status_code=401,
        )

    encrypted = totp_mod.encrypt_secret(secret_base32=state.totp_secret_base32, key=key)
    await repo.upsert_totp_secret(account_id=state.account_id, encrypted_secret=encrypted)
    state.totp_secret_verified = True
    state.totp_secret_base32 = None  # zero out from ceremony memory
    await store.update(state)
    log.info("totp_enrolled", account_id=str(state.account_id))
    return TotpSetupVerifyResponse(success=True)


@router.post(
    "/api/auth/totp/verify",
    tags=["auth"],
    response_model=TotpLoginVerifyResponse,
)
async def totp_login_verify(
    body: TotpLoginVerifyRequest,
    response: Response,
    request: Request,
    repo: AuthRepo = Depends(_get_auth_repo),
    db: AsyncSession = Depends(_get_db),
    settings: APISettings = Depends(get_settings),
) -> TotpLoginVerifyResponse:
    key = _require_totp_key(settings)
    account = await repo.find_account_by_username(body.username)
    if account is None:
        log.warning("totp_login_account_not_found", username=body.username)
        raise AppError(
            error_code="TOTP_LOGIN_FAILED",
            message="Username or TOTP code did not verify.",
            status_code=401,
        )
    secret_row = await repo.get_totp_secret(account.id)
    if secret_row is None:
        log.warning("totp_login_no_secret", account_id=str(account.id))
        raise AppError(
            error_code="TOTP_LOGIN_FAILED",
            message="Username or TOTP code did not verify.",
            status_code=401,
        )
    secret_b32 = totp_mod.decrypt_secret(ciphertext=secret_row.encrypted_secret, key=key)
    if not totp_mod.verify_code(secret_base32=secret_b32, code=body.totp_code):
        log.warning("totp_login_code_invalid", account_id=str(account.id))
        raise AppError(
            error_code="TOTP_LOGIN_FAILED",
            message="Username or TOTP code did not verify.",
            status_code=401,
        )

    await repo.stamp_totp_last_used(account.id)

    # TOTP-only login → weak session (no last_uv_at -- WebAuthn UV is the only
    # path to "strong"). The frontend banner reflects this and the route-layer
    # re-auth gate refuses sensitive endpoints.
    session_row = await sessions_mod.create_session(
        db,
        account_id=account.id,
        totp_bootstrap_only=True,
        last_uv_at_utc=None,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    _set_session_cookies(response, settings=settings, session_id=session_row.session_id)
    log.info("totp_login_success", account_id=str(account.id))
    return TotpLoginVerifyResponse(success=True, auth_strength="weak")


# ---------------------------------------------------------------------------
# Backup codes — generation (setup), recovery, regeneration
# ---------------------------------------------------------------------------
@router.post(
    "/api/auth/backup-codes/generate",
    tags=["auth"],
    response_model=BackupCodesGenerateResponse,
)
async def backup_codes_generate(
    body: BackupCodesGenerateRequest,
    store: CeremonyStore = Depends(get_ceremony_store),
    repo: AuthRepo = Depends(_get_auth_repo),
) -> BackupCodesGenerateResponse:
    state = await _get_ceremony(store, body.ceremony_session_id, expected_kind="setup")
    if state.account_id is None:
        raise AppError(
            error_code="CEREMONY_OUT_OF_ORDER",
            message="WebAuthn register must succeed before backup-codes/generate.",
            status_code=400,
        )

    codes = backup_codes_mod.generate_codes()
    generation_id = uuid4()
    # Invalidate any prior batch (defensive — re-running /setup mid-flight).
    await repo.invalidate_backup_codes_for_account(state.account_id)
    await repo.insert_backup_codes(
        account_id=state.account_id,
        generation_id=generation_id,
        code_hashes=[c.hash for c in codes],
    )
    # Drop the ceremony now -- the wizard is complete after this call.
    await store.delete(state.ceremony_key)
    log.info(
        "backup_codes_generated",
        account_id=str(state.account_id),
        generation_id=str(generation_id),
    )
    return BackupCodesGenerateResponse(
        codes=[c.display for c in codes],
        generation_id=str(generation_id),
    )


@router.post(
    "/api/auth/recover",
    tags=["auth"],
    response_model=RecoverResponse,
)
async def recover(
    body: RecoverRequest,
    response: Response,
    request: Request,
    repo: AuthRepo = Depends(_get_auth_repo),
    db: AsyncSession = Depends(_get_db),
    settings: APISettings = Depends(get_settings),
) -> RecoverResponse:
    account = await repo.find_account_by_username(body.username)
    if account is None:
        log.warning("recover_account_not_found", username=body.username)
        raise AppError(
            error_code="RECOVER_FAILED",
            message="Username or backup code did not verify.",
            status_code=401,
        )
    normalized = backup_codes_mod.normalize(body.backup_code)
    if not normalized:
        log.warning("recover_normalize_empty", account_id=str(account.id))
        raise AppError(
            error_code="RECOVER_FAILED",
            message="Username or backup code did not verify.",
            status_code=401,
        )

    unused = await repo.list_unused_backup_codes(account.id)
    matched_row_id: UUID | None = None
    for row in unused:
        if backup_codes_mod.verify(raw_code=normalized, code_hash=row.code_hash):
            matched_row_id = row.id
            break
    if matched_row_id is None:
        log.warning("recover_no_match", account_id=str(account.id))
        raise AppError(
            error_code="RECOVER_FAILED",
            message="Username or backup code did not verify.",
            status_code=401,
        )

    await repo.mark_backup_code_used(matched_row_id)
    # Recovery → weak session; frontend immediately walks the operator through
    # re-enrollment (WebAuthn + TOTP + new batch of 8 codes).
    session_row = await sessions_mod.create_session(
        db,
        account_id=account.id,
        totp_bootstrap_only=True,
        last_uv_at_utc=None,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    _set_session_cookies(response, settings=settings, session_id=session_row.session_id)
    log.info("recover_success", account_id=str(account.id))
    return RecoverResponse(success=True, auth_strength="weak")


@router.post(
    "/api/auth/backup-codes/regenerate",
    tags=["auth"],
    response_model=BackupCodesRegenerateResponse,
)
async def backup_codes_regenerate(
    session: SessionContext = Depends(get_session_context),
    repo: AuthRepo = Depends(_get_auth_repo),
) -> BackupCodesRegenerateResponse:
    # Re-auth gate: WebAuthn UV within last 5 min (dev-guide §1.5).
    _require_recent_uv(session)
    # Phase 0 stub session uses ``user_id = "phase0-stub-owner"`` -- not a
    # UUID, so we look up the actual operator account by username for now.
    # When real sessions land, ``session.user_id`` is the account UUID
    # directly; the lookup is still safe since find_account_by_username is
    # idempotent.
    account = await repo.find_account_by_username(session.username)
    if account is None:
        raise AppError(
            error_code="ACCOUNT_NOT_FOUND",
            message="No registered account for this session.",
            status_code=404,
        )
    codes = backup_codes_mod.generate_codes()
    generation_id = uuid4()
    await repo.invalidate_backup_codes_for_account(account.id)
    await repo.insert_backup_codes(
        account_id=account.id,
        generation_id=generation_id,
        code_hashes=[c.hash for c in codes],
    )
    log.info(
        "backup_codes_regenerated",
        account_id=str(account.id),
        generation_id=str(generation_id),
    )
    return BackupCodesRegenerateResponse(
        codes=[c.display for c in codes],
        generation_id=str(generation_id),
    )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
@router.post("/api/auth/logout", tags=["auth"], response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    settings: APISettings = Depends(get_settings),
    db: AsyncSession = Depends(_get_db),
) -> LogoutResponse:
    cookie_val = request.cookies.get(settings.session_cookie_name)
    if cookie_val:
        session_id = sessions_mod.decode_cookie_value(cookie_val)
        if session_id is not None:
            await sessions_mod.revoke_session(db, session_id=session_id, reason="explicit_logout")
    _clear_session_cookies(response, settings=settings)
    log.info("logout_completed")
    return LogoutResponse(success=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Backwards-compat: Day 25 extracted the canonical re-auth gate into
# ``services.api.auth.sessions.require_recent_uv`` so ``routes/system.py``
# (kill-switch resume) can share it without a cross-route import. The alias
# keeps the local call sites in this module unchanged.
_require_recent_uv = sessions_mod.require_recent_uv


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _b64url_decode(value: str) -> bytes:
    import base64

    padding_needed = -len(value) % 4
    return base64.urlsafe_b64decode(value + "=" * padding_needed)


def _unused() -> None:
    # Silence unused-import warnings while these symbols stay reserved for
    # the integration test imports (services.api.auth.* round-tripped).
    _ = (UUID, Any)


__all__ = ["router"]
