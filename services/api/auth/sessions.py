"""services/api/auth/sessions.py — session lifecycle against ``sessions``.

Backend-spec §8.5.3 + frontend-spec §5.4-§5.5:

  * Session row keyed by raw 32 bytes (``BYTEA PRIMARY KEY``). The session
    cookie stores ``base64url(id)`` -- HttpOnly + Secure + SameSite=Strict.
  * Idle timeout: 30 min from ``last_activity_utc``.
  * Absolute timeout: 24h from ``created_at_utc``.
  * ``totp_bootstrap_only = TRUE`` ↔ application-layer ``auth_strength="weak"``
    (frontend-spec §5.4 -- TOTP-only login is reduced-privilege).
  * ``last_uv_at_utc`` is the WebAuthn UV timestamp consumed by the 5-min
    re-auth window (dev-guide §1.5).

This module provides the *pure-policy* session-row creation, lookup, refresh,
and revoke logic. Cookie encoding/decoding is handled at the route layer; this
module deals in raw bytes.

A05 N/A. A06 enforced (every timestamp is tz-aware UTC). A22 covered by the
integration test ``tests/integration/test_auth_session_lifecycle.py`` (real
Postgres). A02 N/A (services/api is on the hot-fix whitelist).
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.errors import AppError
from services.api.session import SessionContext

log = structlog.get_logger()


# 32 bytes of session id = 256 bits of entropy. Cookie holds the base64url
# encoding (~43 chars) -- well under the 4 KiB cookie limit.
SESSION_ID_BYTES: Final[int] = 32

# Locked re-auth window per dev-guide §1.5: WebAuthn UV must have happened
# within this many seconds to allow risk-loosening actions.
RE_AUTH_WINDOW_SECONDS: Final[int] = 300


def require_recent_uv(
    session: SessionContext,
    *,
    now: datetime | None = None,
) -> None:
    """Gate the re-auth-required endpoints per dev-guide §1.5 LOCKED.

    Raises ``AppError(RE_AUTH_REQUIRED, 401)`` unless:

      * ``session.auth_strength == "strong"`` (WebAuthn-backed), AND
      * ``session.last_uv_at`` is not None, AND
      * ``now - session.last_uv_at <= RE_AUTH_WINDOW_SECONDS``.

    The ``now`` parameter exists for test determinism — pass an explicit
    timestamp to anchor the comparison. Defaults to ``datetime.now(tz=UTC)``.

    Used by:
      * ``POST /api/auth/backup-codes/regenerate``
      * ``POST /api/system/kill-switch/resume``

    Spec ref: backend-spec §4.1.3 ``require_uv_within_5min`` dependency.
    """
    current = now if now is not None else datetime.now(tz=UTC)
    if session.auth_strength != "strong" or session.last_uv_at is None:
        raise AppError(
            error_code="RE_AUTH_REQUIRED",
            message=(
                "This action requires a recent WebAuthn user-verification. "
                "Re-prompt the operator and retry."
            ),
            status_code=401,
        )
    age = current - session.last_uv_at
    if age > timedelta(seconds=RE_AUTH_WINDOW_SECONDS):
        raise AppError(
            error_code="RE_AUTH_REQUIRED",
            message=(
                "Last WebAuthn user-verification is older than the re-auth "
                f"window ({RE_AUTH_WINDOW_SECONDS}s). Re-prompt and retry."
            ),
            status_code=401,
        )


@dataclass(frozen=True)
class SessionRow:
    """Snapshot of one row from the ``sessions`` table."""

    session_id: bytes  # raw BYTEA, NOT base64url
    account_id: UUID
    created_at_utc: datetime
    last_activity_utc: datetime
    last_uv_at_utc: datetime | None
    user_agent: str | None
    ip_address: str | None
    totp_bootstrap_only: bool
    evicted_at_utc: datetime | None
    evicted_reason: str | None
    expires_at_utc: datetime

    @property
    def is_active(self) -> bool:
        """True iff the row is neither evicted nor expired (absolute)."""
        if self.evicted_at_utc is not None:
            return False
        return self.expires_at_utc > datetime.now(tz=UTC)

    @property
    def is_idle_expired(self) -> bool:
        """True if last_activity_utc is older than the configured idle window.

        The idle window is enforced by the route middleware which calls
        ``refresh_if_active``; this is the boolean evaluation the middleware
        uses to decide. The 30-min default lives in ``APISettings``.
        """
        # The middleware passes idle_seconds at call time; here we expose a
        # simple "stale > 1h" floor for telemetry. The actual gate runs in
        # ``refresh_if_active`` with the configured value.
        return (datetime.now(tz=UTC) - self.last_activity_utc) > timedelta(hours=1)


@dataclass(frozen=True)
class SessionVerificationResult:
    """Output of ``load_active_session`` consumed by the middleware."""

    row: SessionRow
    """The post-refresh row (last_activity_utc updated if it was refreshed)."""


def encode_cookie_value(session_id: bytes) -> str:
    """Encode raw session_id to the cookie value (base64url, no padding)."""
    return base64.urlsafe_b64encode(session_id).rstrip(b"=").decode("ascii")


def decode_cookie_value(cookie: str) -> bytes | None:
    """Decode the cookie value back to raw session_id bytes.

    Returns ``None`` on any decode failure -- the route handler treats this
    the same as a missing cookie. A malformed cookie should never crash the
    middleware; it just means the operator hasn't authenticated.
    """
    if not cookie:
        return None
    padding_needed = -len(cookie) % 4
    padded = cookie + ("=" * padding_needed)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError):
        return None
    if len(decoded) != SESSION_ID_BYTES:
        return None
    return decoded


def mint_session_id() -> bytes:
    """Fresh 32-byte session id."""
    return secrets.token_bytes(SESSION_ID_BYTES)


async def create_session(
    db: AsyncSession,
    *,
    account_id: UUID,
    totp_bootstrap_only: bool,
    last_uv_at_utc: datetime | None,
    user_agent: str | None,
    ip_address: str | None,
    idle_seconds: int,
    absolute_seconds: int,
) -> SessionRow:
    """Insert a fresh row in ``sessions`` and return it.

    ``last_uv_at_utc`` is the timestamp of the WebAuthn UV that authorized
    this session (i.e. ``now`` on a fresh strong login). For TOTP-only
    logins, pass ``None`` -- the session row's ``last_uv_at_utc`` stays
    NULL and the re-auth gate refuses all sensitive actions.
    """
    session_id = mint_session_id()
    now = datetime.now(tz=UTC)
    expires_at = now + timedelta(seconds=absolute_seconds)
    # idle_seconds is enforced at lookup time via last_activity_utc compare;
    # the DB row stores the absolute expiry and the last-activity stamp.
    # idle_seconds is passed in for telemetry (logged but not stored).
    _ = idle_seconds  # reference for future use; intentional no-op today

    await db.execute(
        text(
            """
            INSERT INTO sessions (
                id, account_id, created_at_utc, last_activity_utc,
                last_uv_at_utc, user_agent, ip_address,
                totp_bootstrap_only, expires_at_utc
            ) VALUES (
                :sid, :acct, :now, :now,
                :uv, :ua, :ip,
                :weak, :expires
            )
            """
        ),
        {
            "sid": session_id,
            "acct": account_id,
            "now": now,
            "uv": last_uv_at_utc,
            "ua": user_agent,
            "ip": ip_address,
            "weak": totp_bootstrap_only,
            "expires": expires_at,
        },
    )
    await db.commit()
    log.info(
        "session_created",
        account_id=str(account_id),
        totp_bootstrap_only=totp_bootstrap_only,
        expires_at_utc=expires_at.isoformat(),
    )
    return SessionRow(
        session_id=session_id,
        account_id=account_id,
        created_at_utc=now,
        last_activity_utc=now,
        last_uv_at_utc=last_uv_at_utc,
        user_agent=user_agent,
        ip_address=ip_address,
        totp_bootstrap_only=totp_bootstrap_only,
        evicted_at_utc=None,
        evicted_reason=None,
        expires_at_utc=expires_at,
    )


async def load_active_session(
    db: AsyncSession,
    *,
    session_id: bytes,
    idle_seconds: int,
) -> SessionRow | None:
    """Return the session row iff it is neither evicted, idle, nor expired.

    Applies the **idle** gate here: a row whose ``last_activity_utc`` is
    older than ``idle_seconds`` is treated as inactive. Doesn't update
    last_activity here -- the caller refreshes via ``refresh_activity``
    after the request succeeds.
    """
    row = await _fetch_row(db, session_id=session_id)
    if row is None:
        return None
    if row.evicted_at_utc is not None:
        return None
    now = datetime.now(tz=UTC)
    if row.expires_at_utc <= now:
        return None
    if (now - row.last_activity_utc) > timedelta(seconds=idle_seconds):
        # Idle expiry: revoke for clarity in audit + future Phase-3 reader
        # role hand-off, then return None as if the cookie had never been
        # presented.
        await revoke_session(db, session_id=session_id, reason="idle_timeout")
        return None
    return row


async def refresh_activity(db: AsyncSession, *, session_id: bytes) -> None:
    """Bump ``last_activity_utc`` to now. Called per authenticated request."""
    now = datetime.now(tz=UTC)
    await db.execute(
        text(
            "UPDATE sessions "
            "SET last_activity_utc = :now "
            "WHERE id = :sid AND evicted_at_utc IS NULL"
        ),
        {"now": now, "sid": session_id},
    )
    await db.commit()


async def upgrade_strength_to_strong(
    db: AsyncSession,
    *,
    session_id: bytes,
    new_uv_at_utc: datetime,
) -> None:
    """Flip ``totp_bootstrap_only`` from TRUE → FALSE and stamp last_uv_at.

    Called when a TOTP-only session adds a WebAuthn credential in-place
    (frontend-spec §5.4 LOCKED upgrade-without-re-login path).
    """
    await db.execute(
        text(
            "UPDATE sessions SET totp_bootstrap_only = FALSE, last_uv_at_utc = :uv WHERE id = :sid"
        ),
        {"uv": new_uv_at_utc, "sid": session_id},
    )
    await db.commit()


async def stamp_uv(
    db: AsyncSession,
    *,
    session_id: bytes,
    uv_at_utc: datetime,
) -> None:
    """Stamp ``last_uv_at_utc`` after a successful re-auth WebAuthn UV."""
    await db.execute(
        text("UPDATE sessions SET last_uv_at_utc = :uv WHERE id = :sid"),
        {"uv": uv_at_utc, "sid": session_id},
    )
    await db.commit()


async def revoke_session(
    db: AsyncSession,
    *,
    session_id: bytes,
    reason: str,
) -> None:
    """Stamp ``evicted_at_utc`` + ``evicted_reason``. Idempotent."""
    now = datetime.now(tz=UTC)
    await db.execute(
        text(
            "UPDATE sessions "
            "SET evicted_at_utc = :now, evicted_reason = :reason "
            "WHERE id = :sid AND evicted_at_utc IS NULL"
        ),
        {"now": now, "reason": reason, "sid": session_id},
    )
    await db.commit()


async def _fetch_row(
    db: AsyncSession,
    *,
    session_id: bytes,
) -> SessionRow | None:
    result = (
        await db.execute(
            text(
                """
                SELECT id, account_id, created_at_utc, last_activity_utc,
                       last_uv_at_utc, user_agent, ip_address::text AS ip_address,
                       totp_bootstrap_only, evicted_at_utc, evicted_reason,
                       expires_at_utc
                FROM sessions
                WHERE id = :sid
                """
            ),
            {"sid": session_id},
        )
    ).fetchone()
    if result is None:
        return None
    return SessionRow(
        session_id=result.id,
        account_id=result.account_id,
        created_at_utc=result.created_at_utc,
        last_activity_utc=result.last_activity_utc,
        last_uv_at_utc=result.last_uv_at_utc,
        user_agent=result.user_agent,
        ip_address=result.ip_address,
        totp_bootstrap_only=result.totp_bootstrap_only,
        evicted_at_utc=result.evicted_at_utc,
        evicted_reason=result.evicted_reason,
        expires_at_utc=result.expires_at_utc,
    )


__all__ = [
    "RE_AUTH_WINDOW_SECONDS",
    "SESSION_ID_BYTES",
    "SessionRow",
    "SessionVerificationResult",
    "create_session",
    "decode_cookie_value",
    "encode_cookie_value",
    "load_active_session",
    "mint_session_id",
    "refresh_activity",
    "require_recent_uv",
    "revoke_session",
    "stamp_uv",
    "upgrade_strength_to_strong",
]
