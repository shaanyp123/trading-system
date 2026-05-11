"""services/api/auth/ceremony.py — in-memory ceremony-state store.

Two distinct flows share the same store shape but have different lifetimes:

  * **Setup ceremonies** (key: ``ceremony_session_id`` from
    ``POST /api/setup/verify-token``) — span the 4-step setup wizard
    (WebAuthn register → TOTP enroll → backup codes). Lifetime: 30 min
    or until the operator closes the wizard.

  * **Login ceremonies** (key: ``ceremony_id`` from
    ``POST /api/auth/webauthn/challenge``) — single-use; consumed by the
    paired ``/verify`` call. Lifetime: 5 min.

Phase 0 design: in-process ``dict`` guarded by ``asyncio.Lock``. A single-
replica deploy means there's no cross-replica coordination need. When the api
fleet scales (Phase 2+), this store moves to Redis or a small Postgres table
keyed by ``ceremony_id``. The Phase-2 cutover is a drop-in replacement of
this module — route handlers depend on the public functions, not the dict.

A05 N/A (no monetary values). A06 enforced (all timestamps tz-aware UTC).
A22 N/A (pure in-process state; tests inject the store directly).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

import structlog

log = structlog.get_logger()


# Cap on how many concurrent ceremonies a single api process holds. Single-
# operator Phase 0 means realistic peak is 1-2 at a time; the cap protects
# against a misbehaving client (or test) leaking unbounded state.
_MAX_CEREMONIES: Final[int] = 256

# Lifetime caps. Setup ceremony spans the multi-step wizard; login challenge
# is single-use and shorter. Both enforced at lookup time -- expired entries
# are deleted lazily on the first request that finds them, plus eagerly on
# any prune pass.
_SETUP_LIFETIME = timedelta(minutes=30)
_LOGIN_LIFETIME = timedelta(minutes=5)


@dataclass
class CeremonyState:
    """Mutable state carried across the steps of one auth ceremony.

    Setup-ceremony fields (``setup_token_uuid`` + ``intended_role``) are set
    when the ceremony is minted by ``/api/setup/verify-token`` and remain
    set across the wizard.

    Login-ceremony fields (``allowed_credential_ids``) are set when the
    ceremony is minted by ``/api/auth/webauthn/challenge``.

    The intermediate-step fields (``webauthn_challenge``, ``webauthn_user_id``,
    ``totp_secret_base32``, ``account_id``) populate as the operator advances.
    """

    ceremony_key: str
    kind: Literal["setup", "login"]
    created_at_utc: datetime
    expires_at_utc: datetime
    # Setup-ceremony only
    setup_token_uuid: UUID | None = None
    intended_role: Literal["owner", "reader"] | None = None
    # WebAuthn registration
    webauthn_register_challenge: bytes | None = None
    webauthn_user_id: bytes | None = None
    webauthn_nickname: str | None = None
    # WebAuthn login (single-use)
    webauthn_login_challenge: bytes | None = None
    allowed_credential_ids: list[bytes] = field(default_factory=list)
    target_url: str | None = None
    # TOTP enrollment (setup wizard)
    totp_secret_base32: str | None = None
    totp_secret_verified: bool = False
    # WebAuthn-register completion atomically creates the account and links it
    account_id: UUID | None = None


class CeremonyStore:
    """Thread-safe in-memory ceremony state.

    All operations are async-friendly: a single ``asyncio.Lock`` serializes
    mutations. The lock is held only across dict accesses; user-supplied
    work (DB I/O, network calls) happens outside the critical section.
    """

    def __init__(self, *, clock: Iterable[datetime] | None = None) -> None:
        # ``clock`` is a deterministic-time injection point for tests; when
        # None we fall back to ``datetime.now(tz=UTC)``. Pass an iterator
        # over fixed datetimes (or a generator) in tests to advance time.
        self._clock_iter = iter(clock) if clock is not None else None
        self._entries: dict[str, CeremonyState] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        if self._clock_iter is not None:
            return next(self._clock_iter)
        return datetime.now(tz=UTC)

    @staticmethod
    def mint_key() -> str:
        # 128 bits of randomness, base64url-no-padding (~22 chars). Long
        # enough that brute-force discovery is infeasible within the
        # 5-30 min ceremony window.
        return secrets.token_urlsafe(16)

    async def create_setup_ceremony(
        self,
        *,
        setup_token_uuid: UUID,
        intended_role: Literal["owner", "reader"],
    ) -> str:
        async with self._lock:
            self._prune_locked(self._now())
            if len(self._entries) >= _MAX_CEREMONIES:
                log.warning(
                    "ceremony_store_at_capacity",
                    entries=len(self._entries),
                    cap=_MAX_CEREMONIES,
                )
                # Evict oldest to make room; deterministic and safe.
                oldest_key = min(
                    self._entries,
                    key=lambda k: self._entries[k].created_at_utc,
                )
                self._entries.pop(oldest_key, None)
            now = self._now()
            key = self.mint_key()
            self._entries[key] = CeremonyState(
                ceremony_key=key,
                kind="setup",
                created_at_utc=now,
                expires_at_utc=now + _SETUP_LIFETIME,
                setup_token_uuid=setup_token_uuid,
                intended_role=intended_role,
            )
            return key

    async def create_login_ceremony(
        self,
        *,
        allowed_credential_ids: list[bytes],
        target_url: str | None,
    ) -> str:
        async with self._lock:
            self._prune_locked(self._now())
            now = self._now()
            key = self.mint_key()
            self._entries[key] = CeremonyState(
                ceremony_key=key,
                kind="login",
                created_at_utc=now,
                expires_at_utc=now + _LOGIN_LIFETIME,
                allowed_credential_ids=list(allowed_credential_ids),
                target_url=target_url,
            )
            return key

    async def get(self, key: str) -> CeremonyState | None:
        """Return the state if present and unexpired; else None.

        Expired entries are deleted lazily on read. Callers must re-fetch
        after any mutation since the store has no notion of object versions.
        """
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at_utc <= self._now():
                self._entries.pop(key, None)
                return None
            return entry

    async def update(self, state: CeremonyState) -> None:
        """Write back a mutated state object.

        The caller fetched ``state`` via ``get``, mutated fields in-place,
        and now persists. Because ``state`` is the SAME object the dict
        already holds (the store doesn't deep-copy), this is mostly a
        no-op -- but we keep the explicit call for API symmetry and to
        make the lifecycle visible at call sites.
        """
        async with self._lock:
            if state.ceremony_key not in self._entries:
                return
            self._entries[state.ceremony_key] = state

    async def consume(self, key: str) -> CeremonyState | None:
        """Atomically pop the entry; returns the consumed state or None."""
        async with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return None
            if entry.expires_at_utc <= self._now():
                return None
            return entry

    async def delete(self, key: str) -> None:
        """Drop an entry without surfacing the value. Idempotent."""
        async with self._lock:
            self._entries.pop(key, None)

    def _prune_locked(self, now: datetime) -> None:
        """Caller must hold ``self._lock``. Drop all expired entries."""
        stale = [k for k, e in self._entries.items() if e.expires_at_utc <= now]
        for k in stale:
            self._entries.pop(k, None)

    def size(self) -> int:
        """Snapshot count; safe to call without the lock (best-effort)."""
        return len(self._entries)

    def reset(self) -> None:
        """Drop everything. Called from the api lifespan handler at startup
        to keep state deterministic across restarts (matches the SSE
        multiplexer's ``reset_state`` pattern)."""
        self._entries.clear()


# Module-level singleton consumed by route handlers via ``get_ceremony_store``.
# Tests build their own instance and inject it via FastAPI dependency override.
_STORE: Final[CeremonyStore] = CeremonyStore()


def get_ceremony_store() -> CeremonyStore:
    """FastAPI dependency for handlers + override hook for tests."""
    return _STORE


def reset_state() -> None:
    """Called from the api lifespan startup to wipe in-memory state."""
    _STORE.reset()


__all__ = [
    "CeremonyState",
    "CeremonyStore",
    "get_ceremony_store",
    "reset_state",
]
