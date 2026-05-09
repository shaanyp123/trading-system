"""Audit-chain primitives: JCS canonicalization, record-hash math, chain verification.

The hash chain is the single load-bearing integrity property of the audit
log (backend-spec §2.10). Every row in ``audit_log`` carries::

    record_hash = SHA-256(prev_hash || payload_jcs)

where ``prev_hash`` is the previous row's ``record_hash`` (or
:data:`GENESIS_HASH` for the very first row) and ``payload_jcs`` is the
RFC 8785 canonical-JSON serialization of the event payload.

The two functions called from the write path are:

* :func:`jcs_serialize` — canonicalize a Python dict to deterministic bytes.
* :func:`compute_record_hash` — combine ``prev_hash`` and ``payload_jcs``
  with SHA-256.

The verification function :func:`verify_chain` is called from
``services/audit/verify_export.py`` and from periodic integrity checks; it
is not on the write path.

JCS implementation choice
-------------------------

Dev-guide §7.3 illustrates ``jcs.canonicalize(payload)`` from the PyJCS
package. This module ships an in-tree canonicalizer instead because:

1. PyJCS raises ``TypeError`` on :class:`~decimal.Decimal` values; the
   trading system uses Decimal pervasively for money/price (anti-pattern
   A05). We need to convert Decimal → ``str()`` (canonical repr) before
   canonicalization.
2. Anti-pattern A05 forbids ``float`` values in audit payloads; rejecting
   them loudly here surfaces accidental float leakage at write time
   instead of letting them silently round-trip through JSON.

The canonicalization output is byte-for-byte deterministic across runs
and processes for our payload domain (str / int / bool / None / Decimal-as-
str / nested dicts and lists). It uses Python's ``json.dumps`` with
``sort_keys=True`` and ``separators=(",", ":")``; all our payload keys are
ASCII so Python's natural string sort matches RFC 8785's UTF-16 code-unit
ordering for BMP characters.

If a payload ever needs non-ASCII keys or float values, this module's
``_to_jcs_compatible`` is the right place to extend (and the spec § needs
updating accordingly).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: 32-byte all-zero sentinel used as ``prev_hash`` for the very first row in
#: the chain. Backend-spec §2.10.1 calls for ``b"\x00" * 32`` explicitly so
#: the genesis row's hash is well-defined and verifiable.
GENESIS_HASH: Final[bytes] = b"\x00" * 32

#: SHA-256 output length in bytes; the audit_log CHECK constraint enforces
#: ``octet_length(record_hash) = 32`` (and likewise for ``prev_hash``).
HASH_LENGTH_BYTES: Final[int] = 32


def _to_jcs_compatible(value: Any) -> Any:
    """Recursively convert Decimal → str so :func:`json.dumps` accepts the payload.

    Decimal's ``str()`` preserves significant digits: ``str(Decimal("100.50"))``
    is ``"100.50"`` while ``str(Decimal("100.5"))`` is ``"100.5"``. Both
    represent the same number but differ in trailing zeros — callers MUST
    normalize Decimal values consistently before passing them in (e.g.
    ``.quantize()`` to a fixed precision) so the same logical value always
    canonicalizes identically.

    Floats are rejected with a clear error pointing to anti-pattern A05.

    Bytes are not supported in audit payloads — caller must base64-encode
    or hex-encode upstream (the BYTEA columns are ``payload_jcs`` and
    ``record_hash``/``prev_hash``, never user-payload data).
    """
    if isinstance(value, dict):
        return {k: _to_jcs_compatible(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jcs_compatible(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        # bool MUST be checked before int since bool is a subclass of int in
        # Python and would otherwise pass the int branch and emit "1"/"0"
        # instead of "true"/"false" through json.dumps.
        return value
    if isinstance(value, int | str) or value is None:
        return value
    if isinstance(value, float):
        raise TypeError(
            f"float values are forbidden in audit payloads "
            f"(anti-pattern A05); got {value!r}. Convert to Decimal upstream."
        )
    raise TypeError(
        f"non-JCS-serializable type {type(value).__name__!r} in audit payload; value={value!r}"
    )


def jcs_serialize(payload: dict[str, Any]) -> bytes:
    """Canonical JSON-serialize ``payload`` per RFC 8785 (restricted domain).

    Returns deterministic UTF-8 bytes suitable for hashing. The output
    invariants:

    * Object keys sorted in ascending string order (ASCII keys only in our
      domain → matches RFC 8785's UTF-16 code-unit ordering for BMP).
    * No whitespace between tokens.
    * Standard JSON string escaping via :func:`json.dumps`.
    * Booleans emitted as ``true``/``false``; ``None`` as ``null``.
    * Integers as integer literals; Decimals as JSON-string-quoted (because
      they are converted to ``str()`` before serialization).

    Raises
    ------
    TypeError
        If the payload contains a ``float`` (anti-pattern A05) or any
        type other than ``dict`` / ``list`` / ``tuple`` / ``str`` / ``int``
        / ``bool`` / ``None`` / :class:`~decimal.Decimal`.
    """
    normalized = _to_jcs_compatible(payload)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_record_hash(prev_hash: bytes, payload_jcs: bytes) -> bytes:
    """Compute ``SHA-256(prev_hash || payload_jcs)`` (backend-spec §2.10.1).

    Raises
    ------
    ValueError
        If ``prev_hash`` is not exactly 32 bytes — this catches caller bugs
        before the row would otherwise be rejected by the audit_log CHECK
        constraint (``octet_length(prev_hash) = 32``).
    """
    if len(prev_hash) != HASH_LENGTH_BYTES:
        raise ValueError(f"prev_hash must be {HASH_LENGTH_BYTES} bytes; got {len(prev_hash)}")
    return hashlib.sha256(prev_hash + payload_jcs).digest()


async def verify_chain(session: AsyncSession) -> tuple[bool, int | None]:
    """Walk the entire ``audit_log`` and verify hash-chain integrity.

    Returns ``(True, None)`` on a clean walk. On failure returns
    ``(False, sequence_no)`` where ``sequence_no`` is the first row whose
    ``record_hash`` does not match ``SHA-256(expected_prev || payload_jcs)``
    OR whose ``prev_hash`` does not match the previous row's
    ``record_hash``.

    This is O(N) over the entire audit log and is intended for periodic
    integrity checks and the export-verification tool, not for every write.
    """
    rows = (
        await session.execute(
            text(
                "SELECT sequence_no, prev_hash, record_hash, payload_jcs "
                "FROM audit_log ORDER BY sequence_no ASC"
            )
        )
    ).fetchall()

    expected_prev: bytes = GENESIS_HASH
    for row in rows:
        prev_hash = bytes(row.prev_hash)
        record_hash = bytes(row.record_hash)
        payload_jcs = bytes(row.payload_jcs)

        if prev_hash != expected_prev:
            return False, row.sequence_no
        computed = compute_record_hash(prev_hash, payload_jcs)
        if record_hash != computed:
            return False, row.sequence_no
        expected_prev = record_hash
    return True, None


__all__ = [
    "GENESIS_HASH",
    "HASH_LENGTH_BYTES",
    "compute_record_hash",
    "jcs_serialize",
    "verify_chain",
]
