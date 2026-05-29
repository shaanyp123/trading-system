"""Unit tests for ``services/version/composite_hash.py``.

Covers the canonical ``parameter_set_hash`` minter (backend-spec §3.11,
``Docs/parameter-sets-bootstrap-design.md`` §11 Q1-A):

* determinism + 64-char lowercase-hex shape
* key-order independence (JCS sorts keys)
* the load-bearing invariant: the hash is STABLE across a
  ``STRATEGY_DECOMMISSIONED`` / ``EXIT_AUTO_APPROVE`` flip (flags excluded)
* a Parameter-Ranges-Table param change DOES move the hash
* ``hash_input`` excludes EXACTLY the two operator-only flags
* the minter uses the in-tree ``services.audit.chain.jcs_serialize`` primitive
  (no second canonicalizer)
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from services.audit.chain import jcs_serialize
from services.version.composite_hash import (
    OPERATOR_ONLY_FLAG_KEYS,
    compute_parameter_set_hash,
    hash_input,
)
from strategies.v1_trend_following.parameters import default_v1_parameters

_HEX = set("0123456789abcdef")


class TestComputeParameterSetHash:
    def test_returns_64_char_lowercase_hex(self) -> None:
        digest = compute_parameter_set_hash(default_v1_parameters().to_canonical_dict())
        assert len(digest) == 64
        assert set(digest) <= _HEX
        assert digest == digest.lower()

    def test_deterministic_across_calls(self) -> None:
        canonical = default_v1_parameters().to_canonical_dict()
        assert compute_parameter_set_hash(canonical) == compute_parameter_set_hash(canonical)

    def test_independent_of_input_key_order(self) -> None:
        """JCS sorts keys, so insertion order must not affect the hash."""
        canonical = default_v1_parameters().to_canonical_dict()
        reordered = dict(reversed(list(canonical.items())))
        # Guard: the reorder really did change insertion order (so the test bites)
        # while leaving the mapping equal.
        assert list(reordered) != list(canonical)
        assert reordered == canonical
        assert compute_parameter_set_hash(reordered) == compute_parameter_set_hash(canonical)

    def test_matches_audit_jcs_primitive(self) -> None:
        """Golden cross-check: the minter is SHA-256(JCS(flags-excluded subset)).

        Proves it uses the single in-tree canonicalizer + a hexdigest, with no
        second canonicalizer slipped in.
        """
        canonical = default_v1_parameters().to_canonical_dict()
        subset = {k: v for k, v in canonical.items() if k not in OPERATOR_ONLY_FLAG_KEYS}
        expected = hashlib.sha256(jcs_serialize(subset)).hexdigest()
        assert compute_parameter_set_hash(canonical) == expected


class TestHashStabilityAcrossFlagFlips:
    """The §10.3 decommission ceremony relies on the hash NOT changing when a
    flag flips — that is what lets the flip be an in-place ``parameters`` UPDATE
    with a stable content-hash primary key (design §11 Q1-A / Q2)."""

    def test_stable_across_strategy_decommissioned_flip(self) -> None:
        base = default_v1_parameters()
        flipped = replace(base, strategy_decommissioned=True)
        assert flipped.strategy_decommissioned is True
        assert compute_parameter_set_hash(base.to_canonical_dict()) == compute_parameter_set_hash(
            flipped.to_canonical_dict()
        )

    def test_stable_across_exit_auto_approve_flip(self) -> None:
        base = default_v1_parameters()
        flipped = replace(base, exit_auto_approve=True)
        assert compute_parameter_set_hash(base.to_canonical_dict()) == compute_parameter_set_hash(
            flipped.to_canonical_dict()
        )

    def test_stable_across_both_flags_flipped(self) -> None:
        base = default_v1_parameters()
        flipped = replace(base, strategy_decommissioned=True, exit_auto_approve=True)
        assert compute_parameter_set_hash(base.to_canonical_dict()) == compute_parameter_set_hash(
            flipped.to_canonical_dict()
        )

    def test_ranges_param_change_does_move_the_hash(self) -> None:
        """Sanity floor: excluding the flags must not mean excluding everything.
        A real Parameter-Ranges-Table change still changes the hash."""
        base = default_v1_parameters()
        changed = replace(base, lookback_days_donchian=base.lookback_days_donchian + 30)
        assert compute_parameter_set_hash(base.to_canonical_dict()) != compute_parameter_set_hash(
            changed.to_canonical_dict()
        )


class TestHashInput:
    def test_excludes_exactly_the_two_operator_flags(self) -> None:
        canonical = default_v1_parameters().to_canonical_dict()
        subset = hash_input(canonical)
        assert set(canonical) - set(subset) == OPERATOR_ONLY_FLAG_KEYS
        assert "STRATEGY_DECOMMISSIONED" not in subset
        assert "EXIT_AUTO_APPROVE" not in subset

    def test_retains_pr_locked_params(self) -> None:
        """The PR-locked params are part of the hash input, not flags."""
        subset = hash_input(default_v1_parameters().to_canonical_dict())
        assert "MIN_HOLDING_DAYS" in subset
        assert "ATR_LOOKBACK_DAYS" in subset

    def test_subset_has_ten_keys(self) -> None:
        subset = hash_input(default_v1_parameters().to_canonical_dict())
        assert len(subset) == 10

    def test_does_not_mutate_input(self) -> None:
        canonical = default_v1_parameters().to_canonical_dict()
        before = dict(canonical)
        hash_input(canonical)
        assert canonical == before

    def test_operator_only_flag_keys_value(self) -> None:
        assert OPERATOR_ONLY_FLAG_KEYS == frozenset(
            {"STRATEGY_DECOMMISSIONED", "EXIT_AUTO_APPROVE"}
        )
