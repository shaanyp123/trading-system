"""services/api/repos/setup_tokens.py — `setup_tokens` table repo.

Per backend-spec §3.1.1:

    CREATE TABLE setup_tokens (
        token_uuid UUID PRIMARY KEY,                  -- UUIDv7
        token_hash TEXT NOT NULL,                     -- Argon2id (argon2-cffi)
        intended_role TEXT NOT NULL CHECK (intended_role IN ('owner', 'reader')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,              -- created_at + 24h
        consumed_at TIMESTAMPTZ
    );

The repo handles persistence + Argon2id verification.

Verification flow (called by `routes/setup.py`):
  1. Load every row where consumed_at IS NULL AND expires_at > now().
  2. For each candidate, Argon2id-verify the raw token against token_hash.
  3. On the first match, atomically set consumed_at = now() and return the row.
  4. No match → return None.

Step 1 typically returns 0-1 rows in steady state (the bootstrap command
inserts a single owner token; once consumed, no rows remain). The loop
keeps the implementation honest in the rare race where two simultaneous
unconsumed tokens exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

import structlog
import uuid_utils as uuid7_lib
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


@dataclass(frozen=True)
class SetupTokenRow:
    token_uuid: UUID
    intended_role: Literal["owner", "reader"]
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


# ---------------------------------------------------------------------------
# Argon2id parameters. Match dev-guide §1.5 spec ("Argon2id via argon2-cffi").
# Memory cost is in KiB; values chosen to keep per-verify well under the
# 30s api statement timeout.
# ---------------------------------------------------------------------------
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


class SetupTokenRepo(Protocol):
    """Protocol that the route handler depends on; testable in isolation."""

    async def consume_if_valid(self, raw_token: str) -> SetupTokenRow | None: ...

    async def has_unconsumed_owner_token(self) -> bool: ...

    async def insert_owner_token(self, raw_token: str) -> UUID: ...


class PostgresSetupTokenRepo:
    """Production repo backed by the api `app_service` Postgres role."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def consume_if_valid(self, raw_token: str) -> SetupTokenRow | None:
        # Step 1: candidate rows.
        rows = (
            await self._session.execute(
                text(
                    "SELECT token_uuid, token_hash, intended_role, "
                    "created_at, expires_at, consumed_at "
                    "FROM setup_tokens "
                    "WHERE consumed_at IS NULL AND expires_at > now() "
                    "ORDER BY created_at ASC"
                )
            )
        ).fetchall()

        for row in rows:
            stored_hash: str = row.token_hash
            try:
                _HASHER.verify(stored_hash, raw_token)
            except (VerifyMismatchError, InvalidHashError):
                continue
            # Step 3: atomic single-use consume.
            updated = (
                await self._session.execute(
                    text(
                        "UPDATE setup_tokens "
                        "SET consumed_at = now() "
                        "WHERE token_uuid = :uuid "
                        "  AND consumed_at IS NULL "
                        "RETURNING token_uuid, intended_role, created_at, "
                        "          expires_at, consumed_at"
                    ),
                    {"uuid": row.token_uuid},
                )
            ).fetchone()
            await self._session.commit()
            if updated is None:
                # Lost race against a parallel consumer; treat as miss.
                log.warning(
                    "setup_token_consume_race_lost",
                    token_uuid=str(row.token_uuid),
                )
                continue
            return SetupTokenRow(
                token_uuid=updated.token_uuid,
                intended_role=updated.intended_role,
                created_at=updated.created_at,
                expires_at=updated.expires_at,
                consumed_at=updated.consumed_at,
            )
        return None

    async def has_unconsumed_owner_token(self) -> bool:
        result = (
            await self._session.execute(
                text(
                    "SELECT 1 FROM setup_tokens "
                    "WHERE consumed_at IS NULL "
                    "  AND expires_at > now() "
                    "  AND intended_role = 'owner' "
                    "LIMIT 1"
                )
            )
        ).fetchone()
        return result is not None

    async def insert_owner_token(self, raw_token: str) -> UUID:
        token_uuid = UUID(str(uuid7_lib.uuid7()))
        token_hash = _HASHER.hash(raw_token)
        now = datetime.now(tz=UTC)
        expires = now + timedelta(hours=24)
        await self._session.execute(
            text(
                "INSERT INTO setup_tokens "
                "(token_uuid, token_hash, intended_role, created_at, expires_at) "
                "VALUES (:uuid, :hash, 'owner', :created, :expires)"
            ),
            {
                "uuid": token_uuid,
                "hash": token_hash,
                "created": now,
                "expires": expires,
            },
        )
        await self._session.commit()
        return token_uuid


__all__ = [
    "PostgresSetupTokenRepo",
    "SetupTokenRepo",
    "SetupTokenRow",
]
