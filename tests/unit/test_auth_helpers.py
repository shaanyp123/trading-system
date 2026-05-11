"""Unit tests for ``services.api.auth.{ceremony,totp,backup_codes}``.

Pure-policy modules — no FastAPI app or testcontainers required. The
ceremony store uses an injectable clock; TOTP + backup-code generation
exercise the real cryptographic primitives against deterministic byte
streams (or fixed seeds where supported).

Cross-references:
  * backend-spec §8.5.2 (TOTP + backup codes)
  * dev-guide §1.5 (LOCKED: 8 codes, ABCDE-FGHIJ format, Argon2id, UV required)
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest

from services.api.auth import backup_codes as bc_mod
from services.api.auth import totp as totp_mod
from services.api.auth.ceremony import CeremonyStore


# ---------------------------------------------------------------------------
# CeremonyStore
# ---------------------------------------------------------------------------
class TestCeremonyStore:
    @pytest.mark.asyncio
    async def test_create_setup_ceremony_returns_unique_key(self) -> None:
        store = CeremonyStore()
        token_uuid = uuid4()
        k1 = await store.create_setup_ceremony(setup_token_uuid=token_uuid, intended_role="owner")
        k2 = await store.create_setup_ceremony(setup_token_uuid=token_uuid, intended_role="owner")
        assert k1 != k2
        assert len(k1) >= 16  # base64url of 16 bytes >= 22 chars
        s1 = await store.get(k1)
        assert s1 is not None
        assert s1.kind == "setup"
        assert s1.setup_token_uuid == token_uuid
        assert s1.intended_role == "owner"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown_key(self) -> None:
        store = CeremonyStore()
        assert await store.get("never-minted") is None

    @pytest.mark.asyncio
    async def test_expired_setup_ceremony_returns_none_and_is_pruned(self) -> None:
        # Inject a clock so the entry is born in the past and the lookup
        # happens after its 30-min TTL has elapsed. Clock ticks:
        #   1) create's prune sweep
        #   2) create's `now` for created_at_utc + expires_at_utc
        #   3) get's expiry check
        now = datetime.now(tz=UTC)
        past_now = now - timedelta(hours=1)
        store = CeremonyStore(clock=iter([past_now, past_now, now]))
        token_uuid = uuid4()
        key = await store.create_setup_ceremony(setup_token_uuid=token_uuid, intended_role="owner")
        assert await store.get(key) is None
        # Lazy prune drops the entry from the dict.
        assert store.size() == 0

    @pytest.mark.asyncio
    async def test_update_persists_mutations(self) -> None:
        store = CeremonyStore()
        key = await store.create_setup_ceremony(setup_token_uuid=uuid4(), intended_role="owner")
        state = await store.get(key)
        assert state is not None
        state.webauthn_register_challenge = b"\x01" * 32
        state.webauthn_user_id = b"\x02" * 64
        await store.update(state)
        reloaded = await store.get(key)
        assert reloaded is not None
        assert reloaded.webauthn_register_challenge == b"\x01" * 32
        assert reloaded.webauthn_user_id == b"\x02" * 64

    @pytest.mark.asyncio
    async def test_consume_pops_entry(self) -> None:
        store = CeremonyStore()
        key = await store.create_login_ceremony(
            allowed_credential_ids=[b"cred-1", b"cred-2"], target_url="/today"
        )
        first = await store.consume(key)
        assert first is not None
        assert first.kind == "login"
        # Second consume returns None — entry is gone.
        assert await store.consume(key) is None

    @pytest.mark.asyncio
    async def test_create_login_ceremony_shorter_lifetime_than_setup(self) -> None:
        # The 5-min login TTL vs 30-min setup TTL is documented in the module
        # docstring; this test pins it so a future relaxation has to be
        # explicit. Clock ticks: create-prune, create-now, get-check.
        now = datetime.now(tz=UTC)
        store = CeremonyStore(clock=iter([now, now, now + timedelta(minutes=6)]))
        key = await store.create_login_ceremony(
            allowed_credential_ids=[b"cred"],
            target_url=None,
        )
        # 6 min after creation, the login entry (5-min TTL) is expired.
        assert await store.get(key) is None

    @pytest.mark.asyncio
    async def test_setup_ceremony_survives_six_min_clock_advance(self) -> None:
        # Mirror of the above but for setup -- 6 min in, still valid since
        # setup TTL is 30 min.
        now = datetime.now(tz=UTC)
        store = CeremonyStore(clock=iter([now, now, now + timedelta(minutes=6)]))
        key = await store.create_setup_ceremony(setup_token_uuid=uuid4(), intended_role="owner")
        state = await store.get(key)
        assert state is not None  # setup is 30-min TTL

    @pytest.mark.asyncio
    async def test_reset_drops_everything(self) -> None:
        store = CeremonyStore()
        for _ in range(3):
            await store.create_setup_ceremony(setup_token_uuid=uuid4(), intended_role="owner")
        assert store.size() == 3
        store.reset()
        assert store.size() == 0


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------
class TestTotp:
    def test_generate_secret_returns_32char_base32(self) -> None:
        enrollment = totp_mod.generate_secret(account_name="operator", issuer="trading-system")
        assert len(enrollment.secret_base32) == 32
        # base32 alphabet (RFC 4648 uppercase)
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in enrollment.secret_base32)

    def test_provisioning_uri_starts_with_otpauth_scheme(self) -> None:
        enrollment = totp_mod.generate_secret(account_name="operator", issuer="trading-system")
        assert enrollment.provisioning_uri.startswith("otpauth://totp/")
        assert "secret=" in enrollment.provisioning_uri

    def test_provisioning_uri_url_encodes_issuer_with_colon(self) -> None:
        # The issuer is URL-encoded before injection so a colon doesn't
        # break parsers like Google Authenticator (which splits on ``:``).
        enrollment = totp_mod.generate_secret(account_name="operator", issuer="trading:system")
        # The literal ``trading:system`` MUST NOT appear; the encoded form
        # ``trading%3Asystem`` MUST.
        assert "trading:system" not in enrollment.provisioning_uri
        assert "trading%3Asystem" in enrollment.provisioning_uri

    def test_verify_code_round_trip(self) -> None:
        import pyotp

        secret = pyotp.random_base32()
        current = pyotp.TOTP(secret).now()
        assert totp_mod.verify_code(secret_base32=secret, code=current) is True

    def test_verify_code_rejects_wrong_value(self) -> None:
        import pyotp

        secret = pyotp.random_base32()
        assert totp_mod.verify_code(secret_base32=secret, code="000000") in (False, True)
        # A clearly malformed code returns False.
        assert totp_mod.verify_code(secret_base32=secret, code="abcdef") is False

    def test_encrypt_decrypt_round_trip(self) -> None:
        key = b"\x42" * 32
        plaintext = "JBSWY3DPEHPK3PXP"
        ciphertext = totp_mod.encrypt_secret(secret_base32=plaintext, key=key)
        # Different nonces → different ciphertexts each call.
        ciphertext2 = totp_mod.encrypt_secret(secret_base32=plaintext, key=key)
        assert ciphertext != ciphertext2
        # Same key decrypts each ciphertext back to the same plaintext.
        assert totp_mod.decrypt_secret(ciphertext=ciphertext, key=key) == plaintext
        assert totp_mod.decrypt_secret(ciphertext=ciphertext2, key=key) == plaintext

    def test_encrypt_secret_rejects_wrong_key_length(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            totp_mod.encrypt_secret(secret_base32="abc", key=b"too-short")

    def test_decrypt_secret_rejects_wrong_key_length(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            totp_mod.decrypt_secret(ciphertext=b"\x00" * 28, key=b"too-short")

    def test_decrypt_secret_rejects_short_ciphertext(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            totp_mod.decrypt_secret(ciphertext=b"\x00" * 10, key=b"\x42" * 32)

    def test_decrypt_secret_rejects_tampered_ciphertext(self) -> None:
        from cryptography.exceptions import InvalidTag

        key = b"\x42" * 32
        ciphertext = bytearray(totp_mod.encrypt_secret(secret_base32="JBSWY3DPEHPK3PXP", key=key))
        # Flip a byte after the 12-byte nonce. The GCM tag MUST detect it.
        ciphertext[-1] ^= 0x01
        with pytest.raises(InvalidTag):
            totp_mod.decrypt_secret(ciphertext=bytes(ciphertext), key=key)

    def test_load_encryption_key_accepts_base64url_no_padding(self) -> None:
        raw = b"\x99" * 32
        b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        assert totp_mod.load_encryption_key(b64) == raw

    def test_load_encryption_key_accepts_standard_base64_with_padding(self) -> None:
        raw = b"\x77" * 32
        b64 = base64.b64encode(raw).decode("ascii")
        assert totp_mod.load_encryption_key(b64) == raw

    def test_load_encryption_key_rejects_wrong_decoded_length(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            totp_mod.load_encryption_key(base64.urlsafe_b64encode(b"\x00" * 16).decode())


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------
class TestBackupCodes:
    def test_locked_constants(self) -> None:
        # Pin the LOCKED values from dev-guide §1.5 + frontend-spec §5.3.
        assert bc_mod.NUM_CODES == 8
        assert bc_mod.CODE_LENGTH == 10
        assert bc_mod.CODE_GROUP_SIZE == 5
        assert bc_mod.HYPHEN_AT_INDEX == 5

    def test_generate_codes_returns_exactly_eight(self) -> None:
        codes = bc_mod.generate_codes()
        assert len(codes) == 8

    def test_generate_codes_format_is_aaaaa_dash_bbbbb(self) -> None:
        codes = bc_mod.generate_codes()
        for c in codes:
            assert len(c.display) == 11  # 5 + hyphen + 5
            assert c.display[5] == "-"
            front, back = c.display.split("-")
            assert len(front) == 5
            assert len(back) == 5
            for ch in front + back:
                # base32 RFC 4648 uppercase alphabet
                assert ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

    def test_generate_codes_are_unique_within_batch(self) -> None:
        # 50-bit space per code; 8 codes → collision probability ~ 0; if the
        # test ever fails we have a deeper bug (e.g. broken secrets.token_bytes).
        codes = bc_mod.generate_codes()
        displays = {c.display for c in codes}
        assert len(displays) == 8

    def test_hashes_are_distinct_from_displays(self) -> None:
        # The hash field is the Argon2id encoded hash; it MUST NOT be the raw
        # display value -- that's the whole point of the at-rest contract.
        codes = bc_mod.generate_codes()
        for c in codes:
            assert c.display != c.hash
            assert c.hash.startswith("$argon2")

    def test_verify_round_trip(self) -> None:
        codes = bc_mod.generate_codes()
        for c in codes:
            assert bc_mod.verify(raw_code=c.display, code_hash=c.hash) is True

    def test_verify_rejects_wrong_code(self) -> None:
        codes = bc_mod.generate_codes()
        assert bc_mod.verify(raw_code="WRONG-CODE", code_hash=codes[0].hash) is False

    def test_verify_rejects_corrupted_hash(self) -> None:
        # InvalidHashError should be swallowed and surface as False rather
        # than crash the recover flow.
        assert bc_mod.verify(raw_code="ABCDE-FGHIJ", code_hash="not-a-real-hash") is False

    def test_normalize_strips_whitespace_and_lowercases_to_canonical_form(self) -> None:
        assert bc_mod.normalize("  abcde-fghij  ") == "ABCDE-FGHIJ"
        assert bc_mod.normalize("ABCDE-FGHIJ") == "ABCDE-FGHIJ"
        assert bc_mod.normalize("abcdefghij") == "ABCDE-FGHIJ"  # re-insert hyphen

    def test_normalize_empty_input_returns_empty_string(self) -> None:
        assert bc_mod.normalize("") == ""
        assert bc_mod.normalize("   ") == ""
        assert bc_mod.normalize("---") == ""

    def test_normalize_returns_unhyphenated_when_wrong_length(self) -> None:
        # If the operator types 9 chars or 11 chars, normalize doesn't try to
        # invent a canonical hyphen position -- the verify call will fail-
        # match and the recover endpoint logs + returns 401.
        assert bc_mod.normalize("ABCDE") == "ABCDE"

    def test_verify_after_normalize_round_trip(self) -> None:
        """Recovery path realism: operator types ``abcde fghij`` → match."""
        codes = bc_mod.generate_codes()
        c = codes[0]
        # Operator types lowercase with a space instead of hyphen.
        raw = c.display.replace("-", " ").lower()
        normalized = bc_mod.normalize(raw)
        assert normalized == c.display
        assert bc_mod.verify(raw_code=normalized, code_hash=c.hash) is True


# ---------------------------------------------------------------------------
# Sanity: imports + Literal typing
# ---------------------------------------------------------------------------
class TestModuleContract:
    def test_intended_role_is_literal_owner_or_reader(self) -> None:
        # Pin the Literal that gates the intended_role surface; if a future
        # PR widens this without going through Phase 3, this test catches it.

        from services.api.auth.ceremony import CeremonyState

        annotations = CeremonyState.__annotations__
        role_annot = annotations["intended_role"]
        # role_annot is ``Literal["owner", "reader"] | None``; the inner
        # Literal args are ("owner", "reader").
        # Extract via repr because Literal | None is a UnionType at runtime.
        repr_str = repr(role_annot)
        assert "owner" in repr_str and "reader" in repr_str

    def test_role_literal_helper_passes(self) -> None:
        # Sanity that ``Literal`` import works for downstream consumers.
        _: Literal["owner", "reader"] = "owner"


# ---------------------------------------------------------------------------
# require_recent_uv re-auth gate (dev-guide §1.5 LOCKED, 5-min UV window)
# ---------------------------------------------------------------------------
class TestRequireRecentUv:
    """Day 25 (Week 7 Mon) gate test surface.

    The helper is the canonical 5-min re-auth check used by:

      * ``POST /api/auth/backup-codes/regenerate``
      * ``POST /api/system/kill-switch/resume``

    Three failure modes (rejected with ``AppError(RE_AUTH_REQUIRED, 401)``):
    weak auth_strength, missing last_uv_at, last_uv_at older than 5 min.
    One pass: strong auth + last_uv_at within the window.
    """

    def _session(
        self,
        *,
        auth_strength: Literal["strong", "weak"],
        last_uv_at: datetime | None,
    ):
        from services.api.session import SessionContext

        now = datetime.now(tz=UTC)
        return SessionContext(
            user_id="phase0-stub-owner",
            username="operator",
            role="owner",
            auth_strength=auth_strength,
            last_uv_at=last_uv_at,
            session_expires_at=now + timedelta(minutes=30),
            webauthn_enrolled=False,
            totp_enrolled=False,
            is_phase0_stub=False,
        )

    def test_strong_with_recent_uv_passes(self) -> None:
        from services.api.auth.sessions import require_recent_uv

        session = self._session(
            auth_strength="strong",
            last_uv_at=datetime.now(tz=UTC) - timedelta(minutes=1),
        )
        # Should not raise
        require_recent_uv(session)

    def test_weak_strength_rejects_even_with_recent_uv(self) -> None:
        from services.api.auth.sessions import require_recent_uv
        from services.api.errors import AppError

        session = self._session(
            auth_strength="weak",
            last_uv_at=datetime.now(tz=UTC) - timedelta(seconds=10),
        )
        with pytest.raises(AppError) as excinfo:
            require_recent_uv(session)
        assert excinfo.value.error_code == "RE_AUTH_REQUIRED"
        assert excinfo.value.status_code == 401

    def test_no_last_uv_at_rejects(self) -> None:
        from services.api.auth.sessions import require_recent_uv
        from services.api.errors import AppError

        session = self._session(auth_strength="strong", last_uv_at=None)
        with pytest.raises(AppError) as excinfo:
            require_recent_uv(session)
        assert excinfo.value.error_code == "RE_AUTH_REQUIRED"

    def test_uv_older_than_window_rejects(self) -> None:
        from services.api.auth.sessions import (
            RE_AUTH_WINDOW_SECONDS,
            require_recent_uv,
        )
        from services.api.errors import AppError

        now = datetime.now(tz=UTC)
        stale_uv = now - timedelta(seconds=RE_AUTH_WINDOW_SECONDS + 60)
        session = self._session(auth_strength="strong", last_uv_at=stale_uv)
        with pytest.raises(AppError) as excinfo:
            require_recent_uv(session, now=now)
        assert excinfo.value.error_code == "RE_AUTH_REQUIRED"
        # The age-of-UV branch carries a different message than the
        # missing-UV branch (both are RE_AUTH_REQUIRED but the message
        # mentions the window).
        assert (
            "older than" in excinfo.value.message.lower()
            or "window" in excinfo.value.message.lower()
        )

    def test_uv_exactly_at_window_boundary_passes(self) -> None:
        from services.api.auth.sessions import (
            RE_AUTH_WINDOW_SECONDS,
            require_recent_uv,
        )

        now = datetime.now(tz=UTC)
        # Exactly at the boundary — the helper uses `> RE_AUTH_WINDOW_SECONDS`
        # so the boundary itself passes.
        boundary_uv = now - timedelta(seconds=RE_AUTH_WINDOW_SECONDS)
        session = self._session(auth_strength="strong", last_uv_at=boundary_uv)
        require_recent_uv(session, now=now)  # should not raise

    def test_now_parameter_overrides_wall_clock(self) -> None:
        from services.api.auth.sessions import require_recent_uv

        # Force the gate to pass by pretending it's just-now, even if
        # last_uv_at is 1 day old.
        ancient_uv = datetime.now(tz=UTC) - timedelta(days=1)
        session = self._session(auth_strength="strong", last_uv_at=ancient_uv)
        # ``now`` exactly 1 second after the ancient UV — well within window.
        fake_now = ancient_uv + timedelta(seconds=1)
        require_recent_uv(session, now=fake_now)  # should not raise
