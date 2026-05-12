"""infrastructure/lean_local/ — Dockerized LEAN Local algorithm engine.

Hosts ``quantconnect/lean:latest`` on the operator's VPS with the v1
trend-following algorithm. Receives historical bars via the on-disk
``/Lean/Data/`` volume (read by ``SubscriptionDataReaderHistoryProvider``);
fake ticks from ``FakeDataQueue`` keep LEAN's live-trading event loop
moving in live-mode. POSTs signal events to the backend at
``POST /api/internal/lean/signals``.

Pivot-PR-A (post-pivot 2026-05-12) ships:

* ``Dockerfile`` — extends ``quantconnect/lean:latest`` with a custom
  entrypoint that reads sops secrets + maps to LEAN config + execs
  LEAN's runtime.
* ``entrypoint.sh`` — sops yaml → env vars + lean.json config-file
  rendering.
* ``lean.json`` (in ``lean/``) — LEAN configuration template
  (deep-merged on top of the upstream LEAN config; environment selected
  at boot from ``LEAN_LIVE_MODE``).

**Post-ceremony 2026-05-12:** LEAN-side brokerage swapped from
``InteractiveBrokersBrokerage`` → ``PaperBrokerage``. The bare
``quantconnect/lean:latest`` image does NOT ship
``QuantConnect.Brokerages.InteractiveBrokers.dll``, so live-mode boot
crash-looped on LEAN's Composer broker-factory lookup
(``Sequence contains no matching element``). PaperBrokerage is LEAN's
built-in zero-dependency simulator. The api owns the real IBKR contract
via ``services/execution/ibkr_adapter.py``; LEAN never talks to a real
broker. See ``Docs/decisions-log.md`` 2026-05-12 entry "Post-ceremony
session — LEAN container's IBKR DLL gap".

See ``deploy/lean_local/README.md`` for the operator runbook.
"""

from __future__ import annotations
