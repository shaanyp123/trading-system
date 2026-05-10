"""services/api/routes/_pagination.py — opaque cursor encoding/decoding.

Backend-spec §4.1.6 mandates cursor-based pagination on all list endpoints.
The cursor is opaque to the client; the server encodes a ``(timestamp, id)``
pair into a base64url string. Day 15 ships the encode/decode primitives;
endpoints use them but Phase 0 tables are empty so the codepaths exercise
only the decode-from-empty path during scaffolding.

Implementation choice: ``base64.urlsafe_b64encode`` of a JSON-serialized
``(iso_ts, str_uuid)`` 2-tuple. Trades a few bytes vs. a tighter binary
encoding for diagnostic readability — operators can base64-decode a stuck
cursor and read its contents.

NOT a security boundary. The cursor is unsigned; an attacker can decode
and craft arbitrary cursors. Authorization on the underlying rows is
enforced by the WHERE clause's ``account_id = :acc`` filter; the cursor
only narrows the page window within the operator's own data.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Final

from services.api.errors import AppError

_MIN_LIMIT: Final[int] = 1
_MAX_LIMIT: Final[int] = 200
_DEFAULT_LIMIT: Final[int] = 50


def encode_cursor(ts: datetime, row_id: str) -> str:
    """Encode a ``(timestamp, row_id)`` pair into an opaque cursor string."""
    payload = json.dumps([ts.isoformat(), row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a cursor produced by ``encode_cursor``.

    Raises ``AppError("INVALID_CURSOR", 400)`` on any decode failure — the
    spec error vocabulary in §4.1.7 doesn't have a dedicated cursor code, so
    we use HTTP 400 with a custom code for clarity. Frontend treats this
    as "drop the cursor, refetch from the top".
    """
    try:
        # Restore padding stripped by encode_cursor.
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parts = json.loads(raw.decode("utf-8"))
        if not (isinstance(parts, list) and len(parts) == 2):
            raise ValueError("expected 2-element list")
        ts_str, row_id = parts
        ts = datetime.fromisoformat(str(ts_str))
        return ts, str(row_id)
    except Exception as exc:
        raise AppError(
            error_code="INVALID_CURSOR",
            message="Pagination cursor is malformed; restart pagination from the top.",
            status_code=400,
            details={"reason": type(exc).__name__},
        ) from exc


def clamp_limit(raw_limit: int | None) -> int:
    """Clamp a route-supplied ``limit`` query param into the spec range.

    Pydantic field-level validation can also enforce this, but routes that
    parse query params manually (rather than via a ``QueryModel``) get the
    same treatment via this helper. Default chosen to match §4.1.6.
    """
    if raw_limit is None:
        return _DEFAULT_LIMIT
    if raw_limit < _MIN_LIMIT:
        return _MIN_LIMIT
    if raw_limit > _MAX_LIMIT:
        return _MAX_LIMIT
    return raw_limit


__all__ = ["clamp_limit", "decode_cursor", "encode_cursor"]
