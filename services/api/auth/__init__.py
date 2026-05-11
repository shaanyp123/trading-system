"""services/api/auth — WebAuthn / TOTP / backup-code / session helpers.

Day 21 (Week 6 Tue) package. Each module wraps one concern; route handlers
in ``services/api/routes/auth.py`` compose them into the ceremony endpoints
listed in backend-spec §4.1.1.

Modules:

  * ``ceremony.py``    — in-memory ceremony-state store (per-process; Phase
                         0 single-replica). Keys ceremony_session_id +
                         ceremony_id; per-entry TTL.
  * ``webauthn.py``    — py_webauthn library wrappers; UV ``required`` LOCKED.
  * ``totp.py``        — pyotp helpers + AES-256-GCM column encryption.
  * ``backup_codes.py``— 10-char base32 generation + Argon2id verification.
  * ``sessions.py``    — session create / verify / refresh / revoke against
                         the ``sessions`` table.

The session middleware (``services/api/session.py``) consumes ``sessions.py``
when a real cookie is present; the Phase-0 stub branch remains for the
``dev`` + ``paper`` envs while frontend ceremonies are smoke-tested.
"""

from __future__ import annotations

__all__: list[str] = []
