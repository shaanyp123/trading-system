"""Canonical ``parameter_set_hash`` minting (backend-spec §3.11).

This is the single source of truth for computing
``parameter_sets.parameter_set_hash``. The module was referenced by
``strategies/v1_trend_following/parameters.py`` and ``…/strategy.py`` long
before it existed (the comments there point here); this is its canonical home.

The hash is ``SHA-256(JCS(hash_input))`` where ``hash_input`` is the
"Parameter Ranges Table" parameters of backend-spec §3.11 — i.e. every
canonical V1 parameter EXCEPT the two operator-only boolean kill-switch flags
``STRATEGY_DECOMMISSIONED`` and ``EXIT_AUTO_APPROVE``.

Excluding the flags is a deliberate, signed-off decision
(``Docs/parameter-sets-bootstrap-design.md`` §11 Q1-A, 2026-05-29). It makes
the hash STABLE across a decommission flip, so the §10.3 decommission ceremony
can flip ``STRATEGY_DECOMMISSIONED`` with an in-place ``parameters`` UPDATE that
leaves the content-addressable primary key (the hash) unchanged — no PK churn,
no INSERT-new/retire-old row-swap. The flags are still STORED in
``parameter_sets.parameters``; only the hash ignores them.

JCS canonicalization is delegated to ``services.audit.chain.jcs_serialize`` —
the single in-tree canonicalizer. We do NOT introduce a second one, so this
hash stays byte-compatible with the rest of the system's hashing (e.g. the
per-payload signal hash in ``services/qc_adapter/signal_ingestion.py``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Final

from services.audit.chain import jcs_serialize

#: Canonical UPPER_CASE keys of the operator-only boolean flags EXCLUDED from
#: the hash input (parameter-sets-bootstrap-design.md §11 Q1-A). Keep in sync
#: with the two boolean fields on ``V1Parameters``
#: (``strategies/v1_trend_following/parameters.py``).
OPERATOR_ONLY_FLAG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "STRATEGY_DECOMMISSIONED",
        "EXIT_AUTO_APPROVE",
    }
)


def hash_input(canonical_parameters: Mapping[str, str]) -> dict[str, str]:
    """Return the flags-excluded subset the hash is computed over.

    ``canonical_parameters`` is the full canonical parameter dict — the
    ``V1Parameters.to_canonical_dict()`` shape (UPPER_CASE keys, all values
    stringified, decimals-as-strings). The two operator-only flags in
    :data:`OPERATOR_ONLY_FLAG_KEYS` are dropped; everything else — including the
    PR-locked ``ATR_LOOKBACK_DAYS`` / ``MIN_HOLDING_DAYS`` — is retained.
    """
    return {
        key: value
        for key, value in canonical_parameters.items()
        if key not in OPERATOR_ONLY_FLAG_KEYS
    }


def compute_parameter_set_hash(canonical_parameters: Mapping[str, str]) -> str:
    """Mint the canonical ``parameter_set_hash`` (64-char lowercase hex).

    ``SHA-256(JCS(hash_input(canonical_parameters)))``. Deterministic and stable
    across processes — JCS sorts keys lexicographically and emits no whitespace.
    The two operator-only flags are excluded (see module docstring +
    :func:`hash_input`), so flipping ``STRATEGY_DECOMMISSIONED`` does NOT change
    the returned hash.

    The output matches the ``parameter_sets.parameter_set_hash CHAR(64)`` column
    (RFC-8785 JCS → SHA-256 hexdigest).
    """
    return hashlib.sha256(jcs_serialize(hash_input(canonical_parameters))).hexdigest()


__all__ = [
    "OPERATOR_ONLY_FLAG_KEYS",
    "compute_parameter_set_hash",
    "hash_input",
]
