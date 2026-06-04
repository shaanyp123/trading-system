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
| **P2** | LEAN driver + §6.6 vbt↔LEAN parity + reproduce V1 | **built** (code + parser + parity-logic + V1 cross-check CI-green without LEAN; real-engine run is the operator acceptance gate — see [`lean/README.md`](lean/README.md)) |
| P3 | leverage / margin / liquidation / ruin metrics + sizing schemes | planned |
| P4 | walk-forward + sweep (vectorbt) + anti-overfitting + comparison | planned |
| P5–P6 | intraday minute/tick | **deferred** (daily-only per 2026-06-03 sign-off; no data vendor yet) |
| P7 | isolated live paper-forward | planned |

## P1 layout notes (deltas from design §4.1)

- Metrics live in `eval/metrics.py` (not `risk/metrics.py`) — there is no `risk/`
  dir until P3; they move/extend there with the leverage work.
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
