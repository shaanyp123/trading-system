"""services/api/auth/totp.py — TOTP enrollment + AES-256-GCM column encryption.

Backend-spec §8.5.2 contract:

  * Library: ``pyotp``.
  * Secret length: 160 bits (32-char base32; matches the de-facto RFC 6238
    default + the ``pyotp.random_base32()`` length).
  * Storage: AES-encrypted at the app layer via a key separate from the
    sops master key. The DB stores ciphertext bytes only.
  * 30-second time-step, 6-digit code (pyotp defaults).
  * Verify allows ``+/-1`` step (a single 30s window of clock drift).

The column-encryption scheme is AES-256-GCM with a random 12-byte nonce per
encrypt; the ciphertext stored in the DB is ``nonce || GCM-ciphertext-tag``
(13-byte minimum). Decryption rejects tampered ciphertext via the GCM
authentication tag.

A05 N/A (no monetary values). A06 N/A (no timestamps). A22 N/A (pure-policy
unit tests).
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from typing import Final

import pyotp
import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = structlog.get_logger()


# Locked nonce length per the AES-GCM IETF profile (96 bits = 12 bytes).
_NONCE_LEN: Final[int] = 12

# RFC 6238 + de-facto defaults; mirror pyotp's TOTP defaults but pin
# explicitly for forward-compatibility (so a future pyotp release that
# changes its default doesn't silently shift behaviour).
_DIGITS: Final[int] = 6
_INTERVAL: Final[int] = 30

# Validity window allowance: TOTP verify accepts the current step plus the
# previous step (clock drift). +1 step alone would allow forward-clock-skew;
# we use ``valid_window=1`` which means "+/- 1 step" -- standard.
_VALID_WINDOW: Final[int] = 1


@dataclass(frozen=True)
class TotpEnrollment:
    """Returned from ``generate_secret`` for the /enroll-challenge handler."""

    secret_base32: str
    provisioning_uri: str


def generate_secret(*, account_name: str, issuer: str) -> TotpEnrollment:
    """Mint a fresh 160-bit base32 secret + ``otpauth://`` provisioning URI.

    ``pyotp.TOTP.provisioning_uri`` URL-encodes both ``name`` and
    ``issuer_name`` internally (verified against pyotp 2.9+); we pass the
    raw strings so we don't double-encode. A ``:`` in either argument is
    safely encoded to ``%3A`` by pyotp -- it would otherwise be the
    otpauth-URI separator and break Google Authenticator's parser.
    """
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret, digits=_DIGITS, interval=_INTERVAL)
    uri = totp.provisioning_uri(name=account_name, issuer_name=issuer)
    return TotpEnrollment(secret_base32=secret, provisioning_uri=uri)


def verify_code(*, secret_base32: str, code: str) -> bool:
    """Constant-time TOTP verify with +/- 1 step drift allowance.

    Returns False for any code that does not match the current or adjacent
    step. pyotp's ``verify`` uses constant-time comparison internally
    (``hmac.compare_digest``).
    """
    totp = pyotp.TOTP(secret_base32, digits=_DIGITS, interval=_INTERVAL)
    return totp.verify(code, valid_window=_VALID_WINDOW)


def encrypt_secret(*, secret_base32: str, key: bytes) -> bytes:
    """AES-256-GCM-encrypt the base32 secret with a fresh nonce per call.

    Output layout: ``nonce (12 bytes) || ciphertext+tag (variable)``. The
    GCM tag is 16 bytes appended to the ciphertext by ``AESGCM.encrypt``,
    so the minimum output size is ``12 + 16 = 28`` bytes for an empty
    plaintext (in practice we always have a 32-char base32 secret).

    ``key`` MUST be 16, 24, or 32 bytes for AES-128/192/256. We standardize
    on 32 bytes (AES-256).
    """
    if len(key) != 32:
        raise ValueError(f"AES-GCM key length must be 32 bytes (AES-256); got {len(key)}")
    aes = AESGCM(key)
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = aes.encrypt(nonce, secret_base32.encode("ascii"), associated_data=None)
    return nonce + ciphertext


def decrypt_secret(*, ciphertext: bytes, key: bytes) -> str:
    """Inverse of ``encrypt_secret``. Raises ``InvalidTag`` on tamper."""
    if len(key) != 32:
        raise ValueError(f"AES-GCM key length must be 32 bytes (AES-256); got {len(key)}")
    if len(ciphertext) < _NONCE_LEN + 16:
        raise ValueError(
            f"ciphertext too short ({len(ciphertext)} bytes); need >= {_NONCE_LEN + 16}"
        )
    nonce = ciphertext[:_NONCE_LEN]
    blob = ciphertext[_NONCE_LEN:]
    aes = AESGCM(key)
    plaintext = aes.decrypt(nonce, blob, associated_data=None)
    return plaintext.decode("ascii")


def load_encryption_key(b64_key: str) -> bytes:
    """Decode the configured key string to raw bytes for AESGCM use.

    Accepts standard base64 OR base64url with or without padding. Raises
    ``ValueError`` if the decoded result is not 32 bytes.
    """
    # Tolerate both standard b64 + urlsafe-b64 + missing padding (common in
    # ad-hoc operator workflows -- ``openssl rand 32 | base64`` vs.
    # ``openssl rand 32 | base64url`` differ by alphabet only).
    padding_needed = -len(b64_key) % 4
    padded = b64_key + ("=" * padding_needed)
    try:
        key = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        # Fall through to standard b64.
        key = base64.b64decode(padded, validate=False)
    if len(key) != 32:
        raise ValueError(f"TOTP encryption key must decode to 32 bytes; got {len(key)}")
    return key


__all__ = [
    "TotpEnrollment",
    "decrypt_secret",
    "encrypt_secret",
    "generate_secret",
    "load_encryption_key",
    "verify_code",
]
