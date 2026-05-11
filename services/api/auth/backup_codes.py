"""services/api/auth/backup_codes.py — 8 single-use backup codes (LOCKED).

Locked decisions per dev-guide §1.5 + frontend-spec §5.3 + backend-spec §8.5.2:

  * Generate 8 codes per batch.
  * Format: **10-char base32 (uppercase, RFC 4648), hyphen-separated as
    ``ABCDE-FGHIJ`` (2 groups of 5)**.
  * Generated server-side via ``secrets.token_bytes(8)`` then base32-encoded
    and split. Base32 of 8 bytes is 16 chars; we take the first 10 to fit
    the locked format. (Effective entropy: 10 base32 chars = 50 bits.)
  * Stored as **Argon2id hashes** via ``argon2-cffi`` -- never as plaintext.
  * Raw codes returned exactly once in the response body of the
    generation endpoint; subsequent retrieval is impossible.
  * On recovery use: the matching ``backup_codes`` row's ``used_at_utc`` is
    set and the row is treated as consumed -- a code cannot be re-used.

A05 N/A. A06 N/A (no timestamps in this module; the route handler sets
``used_at_utc`` via the repo). A22 N/A (pure-policy unit tests against
deterministic byte streams).
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from typing import Final

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

log = structlog.get_logger()


# LOCKED — do not change without a Phase-X migration. The setup wizard's
# verbatim acknowledgment string ("I have saved my backup codes") sets
# operator expectations around the count + length.
NUM_CODES: Final[int] = 8
CODE_BYTES_RAW: Final[int] = 8  # 8 bytes -> 16 base32 chars; we slice to 10
CODE_LENGTH: Final[int] = 10  # 10 base32 chars displayed
CODE_GROUP_SIZE: Final[int] = 5  # ABCDE-FGHIJ
HYPHEN_AT_INDEX: Final[int] = CODE_GROUP_SIZE


# Argon2id parameters match the setup_tokens module (same security posture):
# 3 iterations, 64 MiB, parallelism=4. Per-verify cost ~80ms on a 2-core VPS.
_HASHER: Final[PasswordHasher] = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


@dataclass(frozen=True)
class BackupCode:
    """One generated code in its display + hashed forms."""

    display: str  # "ABCDE-FGHIJ"
    hash: str


def _format_display(raw_bytes: bytes) -> str:
    """Map ``CODE_BYTES_RAW`` random bytes to the locked display format.

    base32-encode (16 chars) → uppercase → take first 10 → split at index 5
    with a hyphen. The base32 encoding is RFC 4648 (uppercase, no padding
    after the slice -- ``=`` padding only appears at fixed offsets so the
    first 10 chars never hold padding for 8 input bytes).
    """
    if len(raw_bytes) != CODE_BYTES_RAW:
        raise ValueError(f"raw_bytes must be exactly {CODE_BYTES_RAW} bytes; got {len(raw_bytes)}")
    encoded = base64.b32encode(raw_bytes).decode("ascii").upper()
    sliced = encoded[:CODE_LENGTH]
    return f"{sliced[:HYPHEN_AT_INDEX]}-{sliced[HYPHEN_AT_INDEX:]}"


def generate_codes() -> list[BackupCode]:
    """Mint 8 codes; return each in (display, hash) form.

    Caller persists ONLY the hashes -- the displays go to the operator in the
    response body exactly once and are then unrecoverable.
    """
    out: list[BackupCode] = []
    for _ in range(NUM_CODES):
        raw = secrets.token_bytes(CODE_BYTES_RAW)
        display = _format_display(raw)
        out.append(BackupCode(display=display, hash=_HASHER.hash(display)))
    return out


def normalize(input_code: str) -> str:
    """Operator-input normalization before verification.

    Strip whitespace + hyphens + lowercase-input → uppercase, leaving only
    base32 alphabet chars (A-Z, 2-7). Output is the canonical comparison
    form; we apply the same hyphen-split when re-displaying.

    Returns an empty string if the input has no usable chars; the verify
    path treats that as a no-match.
    """
    cleaned = "".join(c for c in input_code.upper() if c.isalnum())
    if not cleaned:
        return ""
    # Restore the canonical 10-char hyphenated form if exactly 10 chars
    # survived; otherwise return verbatim (verification will fail-match).
    if len(cleaned) == CODE_LENGTH:
        return f"{cleaned[:HYPHEN_AT_INDEX]}-{cleaned[HYPHEN_AT_INDEX:]}"
    return cleaned


def verify(*, raw_code: str, code_hash: str) -> bool:
    """Argon2id-verify a single raw code against a stored hash.

    Returns ``True`` only when the hash + raw match. Mis-formed hashes
    (corrupted column) return ``False`` rather than raising -- a corrupted
    row should not crash the recovery flow.
    """
    try:
        _HASHER.verify(code_hash, raw_code)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:
        # Defensive: if argon2-cffi raises an unexpected exception, log and
        # return False rather than 500ing on the recover endpoint.
        log.exception("backup_code_verify_unexpected_exception")
        return False
    return True


__all__ = [
    "NUM_CODES",
    "BackupCode",
    "generate_codes",
    "normalize",
    "verify",
]
