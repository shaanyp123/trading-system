"""services/api/routes/internal/ — internal-only endpoints.

Internal endpoints are reached only from inside the Docker `internal` network
(watchdog, other backend services). They authenticate
via shared bearer tokens (no cookies, no CSRF). The `internal` prefix is
purely for code organization — Caddy does NOT additionally restrict the
path (the auth bearer is the security boundary).

Currently hosts: nothing. The LEAN signals ingress (``lean.py``,
``POST /api/internal/lean/signals``) was RETIRED in the crypto-pivot C0
decommission (2026-07-08) — signals are generated in-process by the
crypto strategy worker (delta spec §3.3). The package is kept as the
mount point for future internal-only routes.
"""

from __future__ import annotations
