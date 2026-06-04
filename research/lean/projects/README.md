# `research/lean/projects/` — LEAN algorithms the harness drives

These are **QuantConnect LEAN `QCAlgorithm` source files**, loaded by the LEAN
engine via `algorithm-location` (not imported as Python modules by the harness).
Like `lean/`, they `from AlgorithmImports import *` — which only resolves inside
LEAN's runtime — so this directory is **excluded from ruff + mypy**
(`pyproject.toml`). The harness references them by file PATH (the driver mounts the
chosen file into the container); it never imports them.

| File | Purpose | POSTs to api? |
|---|---|---|
| `donchian_reference.py` | Long-only Donchian breakout — the LEAN twin of `research/strategy/donchian.py`; the §6.6 parity rail runs both and compares within tolerance. | **No** (safe for the parser fixture + parity) |

To reproduce **production V1**, the driver mounts the repo's `lean/v1_strategy.py`
(NOT copied here — it is a read-only input) and the `strategies/` package. That
algorithm DOES POST every cycle, so the driver runs it ISOLATED (`--network none`,
unreachable `LEAN_LOCAL_API_BASE_URL` stub, dummy bearer) — see
`research/lean/driver.py` and design §9 P2.

Reference algorithms read their window / cash / channel / symbol from the LEAN
`parameters` block (rendered by `research/lean/config_render.py`), so the driver —
not the algorithm — owns the backtest window.
