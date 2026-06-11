# `research/` — futures backtesting + research harness

A research / iteration / leverage-modeling / reporting layer that **drives LEAN**
(the engine of record). Full design + rationale: `Docs/futures-backtester-design.md`
(SIGNED OFF 2026-06-03). The operator edits a config and runs one command — never
engine code.

## Quickstart (P1 — daily spine)

```bash
# 1. Copy a snapshot of the on-disk LEAN bars (NEVER point at the live volume).
#    On the VPS:
docker run --rm -v trading_lean_data:/src -v "$PWD/research/data/cache/lean_bars":/dst \
  alpine sh -c 'cp -a /src/. /dst/'

# 2. Edit a config (universe, dates, strategy) and run it.
make research RUN=research/config/examples/p1_buy_and_hold.yaml

# 3. Open the report.
open research/runs/<UTC-ts>/report.html
```

## What's built vs deferred

| Phase | Scope | Status |
|---|---|---|
| **P1** | daily loader + contract specs + strategy contract + buy-and-hold + report | **built** |
| **P2** | LEAN driver + §6.6 vbt↔LEAN parity + reproduce V1 | **built + real-engine accepted** (parity PASS + V1-repro PASS vs real LEAN 2026-06-04; see [`lean/README.md`](lean/README.md)) |
| **P3** | leverage / margin / liquidation / ruin metrics + sizing schemes | **built** (`research/risk/`; `make research RUN=research/config/examples/p3_leverage_sweep.yaml` → ruin report with a RED liquidation banner) |
| **V1 adapter** | backtest the PRODUCTION strategy (`strategy.ref: v1_adapter`) | **built** (`research/strategy/v1_adapter.py` replays the REAL V1 logic; `make research RUN=research/config/examples/v1_backtest.yaml`) |
| **V1 P&L (LEAN-native)** | authoritative multi-year V1 equity curve — the LIVE `lean/v1_strategy.py` places real LEAN orders in a backtest, sized by the production Stage 0-5 engine | **built + accepted** (PR #335, 2026-06-09; order-routing fix 2026-06-10 makes the fills real; explicit cost model "PR B" 2026-06-11 makes them honestly priced — 85 fills / 40 trades / **+3.42% / vol 9.1% / fees $223.03**; see [`lean/README.md`](lean/README.md) "Authoritative V1 P&L" + "Cost model & fill conventions") |
| P4 | walk-forward + sweep (vectorbt) + anti-overfitting + comparison | planned |
| P5–P6 | intraday minute/tick | **deferred** (daily-only per 2026-06-03 sign-off; no data vendor yet) |
| P7 | isolated live paper-forward | planned |

## Backtesting a strategy

`strategy.ref` in a run config selects the strategy:

- `buy_and_hold` — constant exposure (the leverage/ruin baseline).
- `donchian` — Donchian breakout (`strategy.channel`).
- `v1_adapter` — **the production V1 strategy**, replayed via the REAL
  `strategies/v1_trend_following` logic (entry: Donchian + MA filter + Kaufman ER
  gate; indicator exits: reversal/trend-flip/decommission, MIN_HOLDING-gated). It
  emits V1's per-bar DIRECTION; pair it with `sizing.scheme: vol_target` (V1's
  Clenow sizing) for V1's magnitude. Example: `research/config/examples/v1_backtest.yaml`.

To backtest a **new** daily strategy, add a `ResearchStrategy` subclass (the
`donchian` / `v1_adapter` files are the templates) and reference it by name.

**Fidelity limits (numpy screen — LEAN is the authority for fills, design D1):**
- V1's ATR protective stop is intrabar; the adapter approximates it close-based.
  The authoritative fill/stop/margin run is the **LEAN-native order path (landed
  PR #335)** — see [`lean/README.md`](lean/README.md) "Authoritative V1 P&L".
- The 200-day MA-slow warmup consumes most of a short single-series window (an ETF
  ~250 bars → ~50 tradeable; a single futures contract ~314 → ~114). A meaningful
  multi-year futures backtest needs the **continuous-contract LEAN path** (LEAN
  stitches ~3 years from the on-disk expiries); the numpy path is single-series.

## P1 layout notes (deltas from design §4.1)

- Metrics moved to `risk/metrics.py` in P3 (the full §6.5 ruin suite); `eval/metrics.py`
  is gone. `run.py`'s P1 daily-spine report imports `summarize` from there now.
- `run.py`, `screen/daily_eval.py`, `data/bars.py`, and `eval/results.py` are P1
  primitives not spelled out in the §4.1 tree. `eval/results.py` holds the single
  canonical `BacktestResult`; the P2 LEAN driver normalizes into that same type.
- The copy step above uses `cp -a`; `rsync` (design §4.2) works equally — both are
  read-only snapshot mechanisms.

## Guardrails (do not violate)

- **Never** mount or write the live `trading_lean_data` Docker volume — read a copy
  (design §4.2, R1).
- **Never** touch a forbidden path (dev-guide §2.2), the production DB, or the audit
  chain. The harness FEEDS governance; a winner graduates via a normal `strategies/`
  PR + `risk-review-approved` (design D6).
- LEAN is authoritative; vectorbt is a daily-only screen, parity-gated (design D1/D2).
- House rules hold (structlog, no print). The Decimal-for-money rule is exempted
  inside this package's float analytics, with Decimal/string at every governance
  boundary (design D8 / §13).
