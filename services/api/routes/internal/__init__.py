"""services/api/routes/internal/ — internal-only endpoints.

Internal endpoints are reached only from inside the Docker `internal` network
(LEAN Local container, watchdog, other backend services). They authenticate
via shared bearer tokens (no cookies, no CSRF). The `internal` prefix is
purely for code organization — Caddy does NOT additionally restrict the
path (the auth bearer is the security boundary).

Currently hosts:

* ``lean.py`` — ``POST /api/internal/lean/signals`` (Pivot-PR-A; post-pivot
  2026-05-12). Receives signal_emitted events from LEAN Local; shared-bearer
  auth via ``LeanAuthMiddleware``.

Future internal routes (not yet shipped):

* ``ibkr_callback.py`` — IBKR-side event callbacks if Pivot-PR-B uses a
  push-based pattern instead of polling.
"""

from __future__ import annotations
