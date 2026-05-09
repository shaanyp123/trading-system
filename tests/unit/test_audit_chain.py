"""Unit tests for the audit-chain primitives in :mod:`services.audit.chain`.

These run without Docker — they exercise pure Python code (JCS
canonicalization, SHA-256 record-hash math, error paths). Database-bound
tests for the writer live in ``tests/integration/test_audit_writer.py``.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from services.audit.chain import (
    GENESIS_HASH,
    HASH_LENGTH_BYTES,
    compute_record_hash,
    jcs_serialize,
)


class TestGenesisHash:
    def test_genesis_hash_is_32_zero_bytes(self) -> None:
        assert GENESIS_HASH == bytes(32)
        assert len(GENESIS_HASH) == HASH_LENGTH_BYTES


class TestJcsSerialize:
    def test_simple_dict_sorts_keys(self) -> None:
        result = jcs_serialize({"b": 1, "a": 2})
        assert result == b'{"a":2,"b":1}'

    def test_nested_dict_sorts_recursively(self) -> None:
        result = jcs_serialize({"outer": {"z": 1, "a": 2}, "alpha": 3})
        assert result == b'{"alpha":3,"outer":{"a":2,"z":1}}'

    def test_list_preserves_order(self) -> None:
        # Lists are NOT sorted — RFC 8785 only sorts object keys, list order
        # is meaningful and preserved.
        result = jcs_serialize({"events": [3, 1, 2]})
        assert result == b'{"events":[3,1,2]}'

    def test_decimal_serializes_as_string_canonical_repr(self) -> None:
        # Decimal -> str() preserves significant digits; JSON quotes it.
        result = jcs_serialize({"price": Decimal("100.50")})
        assert result == b'{"price":"100.50"}'

    def test_decimal_trailing_zeros_preserved(self) -> None:
        # Caller-visible: "100.50" and "100.5" produce DIFFERENT bytes even
        # though they represent the same number. Caller must normalize
        # (.quantize) before passing.
        a = jcs_serialize({"x": Decimal("100.50")})
        b = jcs_serialize({"x": Decimal("100.5")})
        assert a != b

    def test_bool_emits_true_false_not_one_zero(self) -> None:
        # bool is a subclass of int in Python; chain.py must check bool
        # FIRST so it doesn't fall through to the int branch.
        result = jcs_serialize({"flag": True, "other": False})
        assert result == b'{"flag":true,"other":false}'

    def test_none_emits_null(self) -> None:
        result = jcs_serialize({"value": None})
        assert result == b'{"value":null}'

    def test_int_emits_unquoted(self) -> None:
        result = jcs_serialize({"n": 42, "m": -7})
        assert result == b'{"m":-7,"n":42}'

    def test_string_uses_json_escaping(self) -> None:
        result = jcs_serialize({"msg": 'hello "world"'})
        assert result == b'{"msg":"hello \\"world\\""}'

    def test_no_whitespace_in_output(self) -> None:
        result = jcs_serialize({"a": 1, "b": [1, 2, 3], "c": {"d": 4}})
        assert b" " not in result
        assert b"\n" not in result
        assert b"\t" not in result

    def test_float_raises_per_a05(self) -> None:
        with pytest.raises(TypeError, match=r"float values are forbidden.*A05"):
            jcs_serialize({"price": 100.5})

    def test_float_in_nested_dict_raises(self) -> None:
        with pytest.raises(TypeError, match=r"float"):
            jcs_serialize({"outer": {"inner": 1.5}})

    def test_float_in_list_raises(self) -> None:
        with pytest.raises(TypeError, match=r"float"):
            jcs_serialize({"prices": [Decimal("1"), 2.5, Decimal("3")]})

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError, match=r"non-JCS-serializable"):
            jcs_serialize({"data": object()})

    def test_bytes_in_payload_raises(self) -> None:
        # Audit payloads must not contain raw bytes — caller base64-encodes
        # upstream if needed.
        with pytest.raises(TypeError, match=r"non-JCS-serializable"):
            jcs_serialize({"hash": b"\x00\x01"})

    def test_deterministic_across_input_orderings(self) -> None:
        a = jcs_serialize({"a": 1, "b": 2, "c": 3})
        b = jcs_serialize({"c": 3, "a": 1, "b": 2})
        assert a == b


class TestComputeRecordHash:
    def test_genesis_chain_root(self) -> None:
        payload = jcs_serialize({"event": "system_started"})
        record_hash = compute_record_hash(GENESIS_HASH, payload)
        # SHA-256 outputs are 32 bytes.
        assert len(record_hash) == HASH_LENGTH_BYTES
        # Match a manual computation to lock the algorithm.
        expected = hashlib.sha256(GENESIS_HASH + payload).digest()
        assert record_hash == expected

    def test_chain_propagation(self) -> None:
        # Row 1's record_hash becomes row 2's prev_hash.
        payload_1 = jcs_serialize({"i": 1})
        hash_1 = compute_record_hash(GENESIS_HASH, payload_1)
        payload_2 = jcs_serialize({"i": 2})
        hash_2 = compute_record_hash(hash_1, payload_2)
        # hash_2 differs from hash_1 with the same prev_hash — confirms
        # payload affects output.
        hash_2_alt_payload = compute_record_hash(hash_1, payload_1)
        assert hash_2 != hash_2_alt_payload
        # hash_2 differs from a hash computed against GENESIS — confirms
        # prev_hash affects output.
        hash_2_genesis = compute_record_hash(GENESIS_HASH, payload_2)
        assert hash_2 != hash_2_genesis

    def test_short_prev_hash_raises(self) -> None:
        with pytest.raises(ValueError, match=r"32 bytes"):
            compute_record_hash(b"\x00" * 16, b"{}")

    def test_long_prev_hash_raises(self) -> None:
        with pytest.raises(ValueError, match=r"32 bytes"):
            compute_record_hash(b"\x00" * 33, b"{}")

    def test_empty_prev_hash_raises(self) -> None:
        with pytest.raises(ValueError, match=r"32 bytes"):
            compute_record_hash(b"", b"{}")
