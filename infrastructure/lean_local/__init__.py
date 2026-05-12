"""infrastructure/lean_local/ — Dockerized LEAN Local algorithm engine.

Hosts ``quantconnect/lean:latest`` on the operator's VPS with the v1
trend-following algorithm. Receives market data via QC bundled bars
(cached in volume) + live ticks via ``ib_gateway`` (Pivot-PR-B). POSTs
signal events to the backend at ``POST /api/internal/lean/signals``.

Pivot-PR-A (post-pivot 2026-05-12) ships:

* ``Dockerfile`` — extends ``quantconnect/lean:latest`` with a custom
  entrypoint that reads sops secrets + maps to LEAN config + execs
  LEAN's runtime.
* ``entrypoint.sh`` — sops yaml → env vars + lean.json config-file
  rendering.
* ``lean.json.template`` — LEAN configuration template (rendered with
  env vars before LEAN starts; brokerage = InteractiveBrokersBrokerage
  or PaperBrokerage based on `LEAN_LIVE_MODE`).

See ``deploy/lean_local/README.md`` for the operator runbook.
"""

from __future__ import annotations
