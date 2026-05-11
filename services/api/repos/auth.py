"""services/api/repos/auth.py — query layer for the Day 21 auth tables.

Wraps ``webauthn_credentials``, ``totp_secrets``, ``backup_codes``, and
related INSERT/SELECT/UPDATE paths used by the WebAuthn / TOTP / backup-code
endpoints. The ``sessions`` table is handled separately by
``services/api/auth/sessions.py`` because session lifecycle has more
intrinsic logic (idle gate, eviction reasons, etc.); the auth repo here is
focused on credential storage.

Protocol-based design mirrors ``services/api/repos/setup_tokens.py`` and
``services/api/repos/phase1.py`` so route handlers can be unit-tested with
stubs.

Uses the ``app_service`` Postgres role per alembic 0006 grants -- the four
new tables inherited the ``ALTER DEFAULT PRIVILEGES ... GRANT INSERT, SELECT,
UPDATE, DELETE`` automatically when migration 20260513_auth_tables ran.

A05 N/A. A06 enforced (every timestamp is tz-aware UTC).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

import structlog
import uuid_utils as uuid7_lib
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Row dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AccountRow:
    id: UUID
    external_account_id: str
    role: Literal["owner", "reader"]


@dataclass(frozen=True, slots=True)
class WebAuthnCredentialRow:
    id: UUID
    account_id: UUID
    credential_id: bytes
    public_key: bytes
    sign_count: int
    nickname: str | None
    active: bool


@dataclass(frozen=True, slots=True)
class TotpSecretRow:
    account_id: UUID
    encrypted_secret: bytes
    enrolled_at_utc: datetime
    last_used_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class BackupCodeRow:
    id: UUID
    account_id: UUID
    code_hash: str
    used_at_utc: datetime | None
    generation_id: UUID


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class AuthRepo(Protocol):
    # --- accounts (Phase 0 bootstrap helper)
    async def find_account_by_username(self, username: str) -> AccountRow | None: ...
    async def find_or_create_bootstrap_account(
        self, username: str, role: Literal["owner", "reader"]
    ) -> AccountRow: ...

    # --- webauthn_credentials
    async def list_webauthn_credentials(
        self, account_id: UUID, *, active_only: bool = True
    ) -> list[WebAuthnCredentialRow]: ...
    async def get_webauthn_credential_by_id(
        self, credential_id: bytes
    ) -> WebAuthnCredentialRow | None: ...
    async def insert_webauthn_credential(
        self,
        *,
        account_id: UUID,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        authenticator_attachment: str | None,
        nickname: str | None,
    ) -> UUID: ...
    async def update_webauthn_sign_count(
        self, *, credential_id: bytes, new_sign_count: int
    ) -> None: ...

    # --- totp_secrets
    async def get_totp_secret(self, account_id: UUID) -> TotpSecretRow | None: ...
    async def upsert_totp_secret(self, *, account_id: UUID, encrypted_secret: bytes) -> None: ...
    async def stamp_totp_last_used(self, account_id: UUID) -> None: ...

    # --- backup_codes
    async def list_unused_backup_codes(self, account_id: UUID) -> list[BackupCodeRow]: ...
    async def insert_backup_codes(
        self,
        *,
        account_id: UUID,
        generation_id: UUID,
        code_hashes: list[str],
    ) -> None: ...
    async def mark_backup_code_used(self, code_row_id: UUID) -> None: ...
    async def invalidate_backup_codes_for_account(self, account_id: UUID) -> None: ...


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------
class PostgresAuthRepo:
    """Backed by the api ``app_service`` Postgres role."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ----- accounts -----
    async def find_account_by_username(self, username: str) -> AccountRow | None:
        # ``accounts`` does not have a ``username`` column; backend-spec §3.1
        # stores ``external_account_id`` as the unique identifier. Phase 0
        # maps ``operator`` (the configured operator_username) to the single
        # bootstrap account's ``external_account_id``. We use external_account_id
        # column as the username surface here.
        row = (
            await self._session.execute(
                text(
                    "SELECT id, external_account_id, role "
                    "FROM accounts "
                    "WHERE external_account_id = :u AND active_to IS NULL"
                ),
                {"u": username},
            )
        ).fetchone()
        if row is None:
            return None
        return AccountRow(
            id=row.id,
            external_account_id=row.external_account_id,
            role=row.role,
        )

    async def find_or_create_bootstrap_account(
        self, username: str, role: Literal["owner", "reader"]
    ) -> AccountRow:
        existing = await self.find_account_by_username(username)
        if existing is not None:
            return existing
        # Account_type defaults to 'individual' for the Phase 0 single-
        # operator path; multi-operator support is Phase 3 (per spec §5.9).
        row = (
            await self._session.execute(
                text(
                    """
                    INSERT INTO accounts
                        (external_account_id, account_type, role, active_from)
                    VALUES
                        (:u, 'individual', :r, now())
                    RETURNING id, external_account_id, role
                    """
                ),
                {"u": username, "r": role},
            )
        ).fetchone()
        await self._session.commit()
        assert row is not None
        return AccountRow(
            id=row.id,
            external_account_id=row.external_account_id,
            role=row.role,
        )

    # ----- webauthn_credentials -----
    async def list_webauthn_credentials(
        self, account_id: UUID, *, active_only: bool = True
    ) -> list[WebAuthnCredentialRow]:
        sql = (
            "SELECT id, account_id, credential_id, public_key, sign_count, "
            "nickname, active "
            "FROM webauthn_credentials "
            "WHERE account_id = :acct"
        )
        if active_only:
            sql += " AND active = TRUE"
        sql += " ORDER BY registered_at_utc ASC"
        rows = (await self._session.execute(text(sql), {"acct": account_id})).fetchall()
        return [
            WebAuthnCredentialRow(
                id=r.id,
                account_id=r.account_id,
                credential_id=bytes(r.credential_id),
                public_key=bytes(r.public_key),
                sign_count=r.sign_count,
                nickname=r.nickname,
                active=r.active,
            )
            for r in rows
        ]

    async def get_webauthn_credential_by_id(
        self, credential_id: bytes
    ) -> WebAuthnCredentialRow | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, account_id, credential_id, public_key, "
                    "sign_count, nickname, active "
                    "FROM webauthn_credentials "
                    "WHERE credential_id = :cid"
                ),
                {"cid": credential_id},
            )
        ).fetchone()
        if row is None:
            return None
        return WebAuthnCredentialRow(
            id=row.id,
            account_id=row.account_id,
            credential_id=bytes(row.credential_id),
            public_key=bytes(row.public_key),
            sign_count=row.sign_count,
            nickname=row.nickname,
            active=row.active,
        )

    async def insert_webauthn_credential(
        self,
        *,
        account_id: UUID,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        authenticator_attachment: str | None,
        nickname: str | None,
    ) -> UUID:
        new_id = UUID(str(uuid7_lib.uuid7()))
        await self._session.execute(
            text(
                """
                INSERT INTO webauthn_credentials
                    (id, account_id, credential_id, public_key, sign_count,
                     authenticator_attachment, nickname)
                VALUES
                    (:id, :acct, :cid, :pk, :sc, :att, :nick)
                """
            ),
            {
                "id": new_id,
                "acct": account_id,
                "cid": credential_id,
                "pk": public_key,
                "sc": sign_count,
                "att": authenticator_attachment,
                "nick": nickname,
            },
        )
        await self._session.commit()
        return new_id

    async def update_webauthn_sign_count(
        self, *, credential_id: bytes, new_sign_count: int
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE webauthn_credentials "
                "SET sign_count = :sc, last_used_at_utc = now() "
                "WHERE credential_id = :cid"
            ),
            {"sc": new_sign_count, "cid": credential_id},
        )
        await self._session.commit()

    # ----- totp_secrets -----
    async def get_totp_secret(self, account_id: UUID) -> TotpSecretRow | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT account_id, encrypted_secret, enrolled_at_utc, "
                    "last_used_at_utc "
                    "FROM totp_secrets WHERE account_id = :acct"
                ),
                {"acct": account_id},
            )
        ).fetchone()
        if row is None:
            return None
        return TotpSecretRow(
            account_id=row.account_id,
            encrypted_secret=bytes(row.encrypted_secret),
            enrolled_at_utc=row.enrolled_at_utc,
            last_used_at_utc=row.last_used_at_utc,
        )

    async def upsert_totp_secret(self, *, account_id: UUID, encrypted_secret: bytes) -> None:
        now = datetime.now(tz=UTC)
        await self._session.execute(
            text(
                """
                INSERT INTO totp_secrets
                    (account_id, encrypted_secret, enrolled_at_utc)
                VALUES (:acct, :sec, :now)
                ON CONFLICT (account_id) DO UPDATE
                SET encrypted_secret = EXCLUDED.encrypted_secret,
                    enrolled_at_utc = EXCLUDED.enrolled_at_utc
                """
            ),
            {"acct": account_id, "sec": encrypted_secret, "now": now},
        )
        await self._session.commit()

    async def stamp_totp_last_used(self, account_id: UUID) -> None:
        await self._session.execute(
            text("UPDATE totp_secrets SET last_used_at_utc = now() WHERE account_id = :acct"),
            {"acct": account_id},
        )
        await self._session.commit()

    # ----- backup_codes -----
    async def list_unused_backup_codes(self, account_id: UUID) -> list[BackupCodeRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, account_id, code_hash, used_at_utc, "
                    "generation_id "
                    "FROM backup_codes "
                    "WHERE account_id = :acct AND used_at_utc IS NULL"
                ),
                {"acct": account_id},
            )
        ).fetchall()
        return [
            BackupCodeRow(
                id=r.id,
                account_id=r.account_id,
                code_hash=r.code_hash,
                used_at_utc=r.used_at_utc,
                generation_id=r.generation_id,
            )
            for r in rows
        ]

    async def insert_backup_codes(
        self,
        *,
        account_id: UUID,
        generation_id: UUID,
        code_hashes: list[str],
    ) -> None:
        # One INSERT per row -- the batch is small (8 codes) so the per-call
        # overhead is negligible, and individual INSERTs let the future
        # caller's transaction context manage commit boundaries.
        for h in code_hashes:
            await self._session.execute(
                text(
                    """
                    INSERT INTO backup_codes
                        (account_id, code_hash, generation_id)
                    VALUES (:acct, :hash, :gen)
                    """
                ),
                {"acct": account_id, "hash": h, "gen": generation_id},
            )
        await self._session.commit()

    async def mark_backup_code_used(self, code_row_id: UUID) -> None:
        await self._session.execute(
            text(
                "UPDATE backup_codes SET used_at_utc = now() WHERE id = :id AND used_at_utc IS NULL"
            ),
            {"id": code_row_id},
        )
        await self._session.commit()

    async def invalidate_backup_codes_for_account(self, account_id: UUID) -> None:
        """Marks all unused codes for the account as used.

        Called on regenerate flows so the old batch can never be reused.
        """
        await self._session.execute(
            text(
                "UPDATE backup_codes "
                "SET used_at_utc = now() "
                "WHERE account_id = :acct AND used_at_utc IS NULL"
            ),
            {"acct": account_id},
        )
        await self._session.commit()


__all__ = [
    "AccountRow",
    "AuthRepo",
    "BackupCodeRow",
    "PostgresAuthRepo",
    "TotpSecretRow",
    "WebAuthnCredentialRow",
]
