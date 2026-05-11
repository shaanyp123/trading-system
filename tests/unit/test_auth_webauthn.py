"""Unit tests for ``services.api.auth.webauthn`` — py_webauthn wrappers.

Exercises the deterministic surfaces of the module:

  * Challenge generation returns options with the LOCKED contracts
    (UV=required, attestation=none, RFC-recommended algorithm set).
  * ``options_to_dict`` round-trips through ``webauthn.options_to_json``.
  * Verification error paths surface ``InvalidRegistrationResponse`` /
    ``InvalidAuthenticationResponse`` correctly.

The full ceremony (real authenticator response → verify success) is
exercised by the integration test against the live frontend during the
Day 24 operator E2E run; the unit tests stop at the library's input
validation boundary.

A05 / A06 N/A. A22 N/A (no Postgres I/O).
"""

from __future__ import annotations

import pytest
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    UserVerificationRequirement,
)

from services.api.auth import webauthn as webauthn_mod


# ---------------------------------------------------------------------------
# Locked constants
# ---------------------------------------------------------------------------
class TestLockedConstants:
    def test_user_verification_required_is_locked(self) -> None:
        assert webauthn_mod.USER_VERIFICATION_REQUIRED is UserVerificationRequirement.REQUIRED

    def test_attestation_none_is_locked(self) -> None:
        assert webauthn_mod.ATTESTATION_NONE is AttestationConveyancePreference.NONE


# ---------------------------------------------------------------------------
# Registration challenge
# ---------------------------------------------------------------------------
class TestRegistrationChallenge:
    def test_returns_challenge_bytes_and_user_id_bytes(self) -> None:
        challenge = webauthn_mod.generate_registration_challenge(
            rp_id="localhost",
            rp_name="trading-system",
            user_name="operator",
            user_display_name="operator",
        )
        assert isinstance(challenge.challenge_bytes, bytes)
        assert len(challenge.challenge_bytes) >= 16  # py_webauthn default: 64
        assert challenge.user_id_bytes is not None
        assert len(challenge.user_id_bytes) >= 16

    def test_options_dict_has_required_keys(self) -> None:
        challenge = webauthn_mod.generate_registration_challenge(
            rp_id="localhost",
            rp_name="trading-system",
            user_name="operator",
            user_display_name="operator",
        )
        opts = challenge.options_dict
        # Per W3C the JSON keys are camelCase even though py_webauthn uses
        # snake_case internally; options_to_json handles the mapping.
        for key in ("rp", "user", "challenge", "pubKeyCredParams"):
            assert key in opts, f"missing required option key: {key}"

    def test_options_assert_uv_required(self) -> None:
        challenge = webauthn_mod.generate_registration_challenge(
            rp_id="localhost",
            rp_name="trading-system",
            user_name="operator",
            user_display_name="operator",
        )
        opts = challenge.options_dict
        # ``authenticatorSelection.userVerification`` must equal "required".
        sel = opts.get("authenticatorSelection") or {}
        assert sel.get("userVerification") == "required"

    def test_options_assert_attestation_none(self) -> None:
        challenge = webauthn_mod.generate_registration_challenge(
            rp_id="localhost",
            rp_name="trading-system",
            user_name="operator",
            user_display_name="operator",
        )
        assert challenge.options_dict.get("attestation") == "none"

    def test_exclude_credentials_passed_through(self) -> None:
        excluded = [b"\x01" * 16, b"\x02" * 16]
        challenge = webauthn_mod.generate_registration_challenge(
            rp_id="localhost",
            rp_name="trading-system",
            user_name="operator",
            user_display_name="operator",
            exclude_credential_ids=excluded,
        )
        opts = challenge.options_dict
        exclude_list = opts.get("excludeCredentials") or []
        assert len(exclude_list) == 2

    def test_supported_pub_key_algs_includes_es256(self) -> None:
        challenge = webauthn_mod.generate_registration_challenge(
            rp_id="localhost",
            rp_name="trading-system",
            user_name="operator",
            user_display_name="operator",
        )
        algs = challenge.options_dict.get("pubKeyCredParams") or []
        # ES256 alg = -7. The W3C spec uses ``alg`` as the field name.
        assert any(a.get("alg") == -7 for a in algs)


# ---------------------------------------------------------------------------
# Authentication challenge
# ---------------------------------------------------------------------------
class TestAuthenticationChallenge:
    def test_returns_challenge_bytes_user_id_none(self) -> None:
        challenge = webauthn_mod.generate_authentication_challenge(
            rp_id="localhost",
            allow_credential_ids=[b"\x10" * 16],
        )
        assert isinstance(challenge.challenge_bytes, bytes)
        assert challenge.user_id_bytes is None  # auth ceremony has no user_id

    def test_allow_credentials_passed_through(self) -> None:
        allowed = [b"\xa1" * 16, b"\xa2" * 16, b"\xa3" * 16]
        challenge = webauthn_mod.generate_authentication_challenge(
            rp_id="localhost",
            allow_credential_ids=allowed,
        )
        opts = challenge.options_dict
        allow_list = opts.get("allowCredentials") or []
        assert len(allow_list) == 3

    def test_options_assert_uv_required(self) -> None:
        challenge = webauthn_mod.generate_authentication_challenge(
            rp_id="localhost",
            allow_credential_ids=[b"\x10" * 16],
        )
        assert challenge.options_dict.get("userVerification") == "required"


# ---------------------------------------------------------------------------
# Verification error paths
# ---------------------------------------------------------------------------
class TestVerifyRegistrationFailures:
    def test_malformed_credential_raises_webauthn_exception(self) -> None:
        # Empty dict isn't a valid WebAuthn credential; py_webauthn raises
        # a subclass of WebAuthnException (InvalidJSONStructure or
        # InvalidRegistrationResponse depending on the failure mode). The
        # route handler catches the base class so we cover both.
        with pytest.raises(WebAuthnException):
            webauthn_mod.verify_registration(
                credential={},
                expected_challenge=b"\x00" * 32,
                expected_rp_id="localhost",
                expected_origin="http://localhost:3000",
            )

    def test_wrong_shape_credential_raises(self) -> None:
        bad = {"id": "x", "rawId": "x", "type": "public-key", "response": {}}
        with pytest.raises(WebAuthnException):
            webauthn_mod.verify_registration(
                credential=bad,
                expected_challenge=b"\x00" * 32,
                expected_rp_id="localhost",
                expected_origin="http://localhost:3000",
            )


class TestVerifyAuthenticationFailures:
    def test_malformed_credential_raises_webauthn_exception(self) -> None:
        with pytest.raises(WebAuthnException):
            webauthn_mod.verify_authentication(
                credential={},
                expected_challenge=b"\x00" * 32,
                expected_rp_id="localhost",
                expected_origin="http://localhost:3000",
                credential_public_key=b"\x00" * 65,
                current_sign_count=0,
            )


# ---------------------------------------------------------------------------
# options_to_dict helper
# ---------------------------------------------------------------------------
class TestOptionsToDict:
    def test_round_trips_registration_options(self) -> None:
        # Build options via py_webauthn directly; ensure our helper returns
        # the same dict shape ``options_to_json`` produces.
        from webauthn import generate_registration_options

        options = generate_registration_options(
            rp_id="localhost",
            rp_name="trading-system",
            user_name="operator",
        )
        produced = webauthn_mod.options_to_dict(options)
        assert isinstance(produced, dict)
        assert "challenge" in produced
        assert "user" in produced
