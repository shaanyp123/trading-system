# `research/lean/` — the LEAN driver (P2)

Drives LEAN (the engine of record, design D1) to run daily backtests and normalizes
the output into the shared `research.eval.results.BacktestResult`. Full design:
`Docs/futures-backtester-design.md` §4.3 + the "P2 landed" subsection.

| Module | Role |
|---|---|
| `images.py` | availability probes (lean CLI exe + Docker daemon — never `importlib`) + invocation backends (LEAN CLI default, raw `docker run` fallback) |
| `config_render.py` | render a THROWAWAY per-run `lean.json` into a temp project dir — never edits production `lean/lean.json` |
| `driver.py` | assemble an ISOLATED run, launch LEAN (skip-gated), collect the output dir |
| `results.py` | tolerant parser: LEAN result JSON → `BacktestResult` (+ trades, fills) |
| `projects/` | LEAN `QCAlgorithm` source the harness drives (e.g. the Donchian parity twin) |

The parity rail (`tests/integration/test_vbt_lean_parity.py`) and the reproduce-V1
cross-check (`research/eval/reproduce_v1.py`) build on these.

## ⚠️ Safety — do not pollute production

`lean/v1_strategy.py` POSTs to `LEAN_LOCAL_API_BASE_URL` (default `http://api:8000`)
on **every** daily cycle. A research V1 backtest is therefore run ISOLATED three
ways (`driver.isolation_env()` + `--network none`): no prod network, an unreachable
`LEAN_LOCAL_API_BASE_URL` stub, and a dummy non-empty `LEAN_LOCAL_BEARER_TOKEN`. The
data mount is ALWAYS a read-only COPY (`research/data/cache/lean_bars`) — the live
`trading_lean_data` volume is never mounted (`driver._guard_data_root`). These are
unit-tested invariants. **Never** run a research V1 backtest joined to the prod
docker network.

## Authoritative V1 P&L — the backtest-only order path (PR #335)

The numpy / vbt screen (research/README.md) is a *daily screen*; LEAN is the
authority for fills, stops, margin, and the equity curve (design D1). PR #335
(2026-06-09, `a10013c`) makes that authoritative run the LIVE strategy itself:

- **Mechanism.** In a research backtest (`not self.live_mode`), `lean/v1_strategy.py`
  places real LEAN **market orders** mirroring its computed per-bar signals, sized by
  the **production Stage 0-5 sizer** (`services/risk/sizing.py`, loaded by file path
  inside the LEAN container — the harness mounts `services/` read-only). Orders fill
  next-bar; the resulting equity curve, drawdown, and margin events are LEAN's, not a
  reconstruction. Reconcile/roll logic lives in `_place_backtest_orders`.
- **Production is byte-for-byte unchanged.** The path is gated by
  `self._backtest_orders_enabled = not bool(self.live_mode)` (set once in
  `initialize`, never mutated). In paper/live V1 stays POST-only and places **ZERO**
  LEAN orders — a live-safety unit test (`tests/unit/test_v1_backtest_orders.py`)
  locks that the order path is provably unreachable when `live_mode` is true.
- **Isolation is identical to the POST path.** The acceptance run is `--network none`,
  POST stub `http://127.0.0.1:9`, dummy non-empty bearer, read-only data COPY — never
  the live `trading_lean_data` volume.

**Acceptance (real engine, isolated container, 2023-09-01 → 2026-06-08; post
order-routing fix "PR A.2" + explicit cost model "PR B", 2026-06-11):** 1013 bars,
**85 fills, 40 closed trades, +3.42% total return** ($100k → $103,420.76), **Sharpe
0.14**, realized vol **9.1% annualized**, max drawdown **11.61%**, **0 margin
events**, no liquidation, **total fees $223.03** (per-fill census matches the cost
table exactly). Time-in-market 65% of bars; zero-price order skips down **2,165 → 56
market-days (−97%)**; **18/18 rolls carried** (record-at-event →
consolidate-next-cycle); zero same-market entry re-emits. Driver:
`/tmp/v1_acceptance_run.py` (env `V1_START` / `V1_END` / `V1_TIMEOUT`); run from a
worktree with `cwd` AND `PYTHONPATH` set to that worktree. *(Delta chain: the #335
baseline — 45 fills / 18 trades / +4.10% / Sharpe −1.00 / vol 4.4% — was an artifact
of NON-execution; the PR A.2 routing fix made it 85 fills / +3.45% / vol 9.1%; PR B's
honest costs move it −$33.92 to +3.42% — commissions DOWN $42.01 (LEAN had been
overcharging MBT ~39%) against ≈$76 of explicit 1-tick slippage. 84/85 fills are
byte-identical to PR A.2; the one difference is the TLT short sized 314→313 shares
because Stage 0-5 reads the slippage-bearing equity.)*

## Cost model & fill conventions (charter PR B, 2026-06-11)

Probe-measured in THIS image (`trading-lean-local:latest`), then made explicit:

- **LEAN's bundled `InteractiveBrokersFeeModel` is stale for micros**: it charges
  $0.57/contract/side for ALL CME/COMEX micros and $4.77 for MBT. Reality (IBKR
  fixed, non-member, as of 2026-06, ±$0.10/side stated tolerance): index micros
  **$0.62**, MGC **$1.37**, MBT **$3.42**. ETFs: the bundled model matches IBKR
  fixed exactly ($0.005/share, $1.00 min) and is kept.
- **The explicit tables live in `research/data/contract_specs.py`** (canonical:
  `commission_per_side`, `slippage_ticks`, `FILL_CONVENTION`, `COSTS_AS_OF`) and are
  mirrored inline in `research/lean/projects/research_runner.py` (`COSTS_MODEL=ibkr`,
  default) and `lean/v1_strategy.py` (backtest order path, master-gated) — neither
  can import `research/` in-container; `tests/unit/test_research_contract_specs.py`
  AST-pins all three tables in sync.
- **Slippage: futures 1 tick adverse per side, explicit** (probe: a buy fills at
  close + 1 tick). Rationale: the backtest fills at the SAME session's close — the
  bar the signal was computed from — while the live ceremony dispatches ~17:30 ET
  and really fills at the 18:00 ET reopen; 1 tick stands in for that gap +
  half-spread. ETFs: 0 ticks — they fill at the NEXT session's official open (an
  achievable auction print), stated rather than hidden.
- **Fill conventions (empirical):** futures market orders fill same-instant at the
  session close (`MGC 2024-04-01 17:30 → fill == that bar's close`); ETF orders pend
  overnight and fill at the next session's open (TLT/IEF/SHY each exactly the next
  open). The §6.6 parity rail's next-open assumption is confirmed for equities.
- **Models land on the TRADED CONTRACT security** (fills never happen on the
  canonical continuous): the runner costs each mapped contract before ordering;
  V1 costs contracts at its two gated subscription sites. `costs: zero` now zeroes
  mapped-contract fills too (the old canonical-only application was inert for
  futures fills).
- **Every report surfaces the assumptions** (md/html header + `result.json
  cost_model` block), rendered from `contract_specs.py` so report and engine can't
  drift.

**Follow-up findings (post-#335, in `Docs/futures-backtester-design.md` charter PR A
follow-up):** nit (a) `invested_since`-reset was *structurally inert* only because the
MIN_HOLDING gate never engaged in backtest — the order-routing fix now tracks entry
dates in-strategy, so the 14-day gate is live-faithful (and preserved across rolls).
Nit (b)'s chain-edge gap root-caused to the price-source artifact and is **FIXED** by
the order-routing fix (explicit front subscriptions + market-level position state +
record-then-consolidate rolls — design doc "PR A.2" has the full probe evidence and
the residual-vol-gap analysis). Remaining known divergences (documented, chartered):
no stop-order simulation in backtest (live brackets every entry at 3-ATR); Stage 0-5
sizing in backtest vs single-lot live dispatch; the map_file historical-roll horizon
starts mid-2024 (a `services/data` backfill follow-up — /MYM is pinned to its
June-2024 contract for the first ~9 months of the window).

## Operator acceptance (the real-engine gate)

The code + parser + parity-logic + V1 cross-check are CI-green on committed fixtures
WITHOUT Docker/LEAN. The items below need a real engine and are the P2 acceptance
gate; until they run, the integration tests SKIP visibly (never a silent pass).

### 0. Prereqs

```bash
pip install lean            # the CLI (provides the `lean` executable)
# + Docker Desktop running, OR build/tag the production-faithful image:
docker build -t trading-lean-local:latest -f infrastructure/lean_local/Dockerfile .
```

### 1. Snapshot the on-disk bars (a COPY — never the live volume)

```bash
# on the VPS (read-only; doesn't touch running containers):
ssh root@178.156.239.84 'docker run --rm -v trading_lean_data:/src:ro -v /tmp:/out \
  alpine tar czf /out/lean_bars_snapshot.tgz -C /src .'
scp root@178.156.239.84:/tmp/lean_bars_snapshot.tgz /tmp/
mkdir -p research/data/cache/lean_bars
tar xzf /tmp/lean_bars_snapshot.tgz -C research/data/cache/lean_bars
ssh root@178.156.239.84 'rm /tmp/lean_bars_snapshot.tgz'
```

### 2. Run the §6.6 parity rail (Donchian, a clean ETF series)

```bash
make test-integration            # or:
python -m pytest tests/integration/test_vbt_lean_parity.py -m integration -rs
# Knobs (env): RESEARCH_PARITY_SYMBOL (default TLT), RESEARCH_PARITY_CHANNEL (20),
# RESEARCH_PARITY_START/END, RESEARCH_PARITY_CASH.
```

If the per-trade slippage criterion fails on the first real run, tune the fill model
in `projects/donchian_reference.py` (LEAN fills next-open; the numpy screen is
close-to-close) — a one-line change. The aggregate-P&L + trade-count criteria are
robust to that gap.

### 3. Reproduce production V1 + cross-check against the live oracle

Capture the live paper decision oracle (read-only; prints no secrets):

```bash
ssh root@178.156.239.84 'docker exec -i -w /app trading-api-1 python -' <<'PY' > /tmp/v1_oracle.json
import asyncio, json, os
from pathlib import Path
import asyncpg
from services.api import entrypoint
_sec = entrypoint._load_secrets(Path(os.environ.get("API_SECRETS_PATH", str(entrypoint.DEFAULT_SECRETS_PATH))))
_pw = (_sec.get("postgres") or {}).get("app_service_password"); assert isinstance(_pw, str)
_url = entrypoint._build_database_url(_pw).replace("+asyncpg", "")
async def main():
    conn = await asyncpg.connect(_url)
    try:
        rows = await conn.fetch("""
            SELECT to_char(session_date,'YYYY-MM-DD') AS session_date, market,
                   lower(direction) AS direction, left(parameter_set_hash,12) AS phash
            FROM signals WHERE env='paper' AND signal_type='entry'
            ORDER BY session_date, market""")
        print(json.dumps({"env": "paper", "signal_type": "entry",
                          "entries": [dict(r) for r in rows]}, indent=2))
    finally:
        await conn.close()
asyncio.run(main())
PY
```

Then run the reproduce-V1 test over a single parameter-REGIME window, cut by
DATE. ⚠️ Measured (charter PR D): prod stamps a **distinct `parameter_set_hash`
on every signal**, so "restrict to one phash" is not a usable filter — the live
regime boundary is the **ER gate landing 2026-06-02** (before it live ran without
the gate the current code applies):

```bash
RESEARCH_V1_ORACLE=/tmp/v1_oracle.json \
RESEARCH_V1_WINDOW=2026-06-02:2026-06-08 \
python -m pytest tests/integration/test_reproduce_v1.py -m integration -rs
```

Reproduce at the DECISION level (same market + direction + session_date);
`decision_price ≠ backtest fill` is expected (the §6.6 tolerances cover that).
The committed fixtures (`tests/fixtures/v1_repro_log/` + `tests/fixtures/v1_oracle/`)
ARE captured real artifacts (real-engine run 2026-05-01→2026-06-08 + the prod
signals table as of 2026-06-11); the golden test guards the measured result.

## Measured trust bridge (charter PR D, 2026-06-11)

Real-engine run (isolated) over 2026-05-01→2026-06-08 vs the live paper oracle
(19 signal rows → 10 unique decisions, all long):

| View | Match | Exact 95% CI |
|---|---|---|
| **Market-level** (did each live-flagged market flag?) | **4/5 = 80%** | [0.28, 0.99] |
| Strict decision (date+market+direction), full span | 4/10 = 40% | [0.12, 0.74] |
| Stabilization window (2026-05-26→06-08) | 2/3 = 67% | [0.09, 0.99] |
| **ER-aligned regime** (2026-06-02→06-08, gate live both sides) | **1/1 = 100%** | [0.02, 1.00] |

**After attribution the unexplained residual is ZERO decisions.** Every miss/extra
has a verified cause (log lines in the committed fixture):

1. **Live dormant anti-pyramiding re-emission (pre-#312, deployed 2026-06-01):**
   9 of 19 oracle rows are duplicate re-emissions of a held breakout; the backtest
   is position-aware and correctly suppresses (`position_already_same_direction` —
   e.g. TLT 05-18).
2. **ER-gate regime flip (live boundary 2026-06-02):** the backtest applies the
   current ER(0.20) gate to pre-boundary dates where live had no gate — /M2K
   2026-05-26 logs `efficiency_below_threshold`; this is the single market-level
   miss.
3. **Bar data revised since live decided:** bar_sync overwrites daily + map-file
   re-synthesis (#326); /MES 05-18/19 + /MNQ 05-13/18 log `no_breakout` on today's
   bars where live saw a breakout; same cause for the IEF/SHY/TLT-05-15 extras and
   /MNQ first flagging 05-06 vs live 05-13.
4. **Sizing-to-zero re-emission:** the 25%/name cap clips one /MNQ contract
   (≈$43k notional) to 0 at $100k → the backtest stays flat and re-emits while the
   trend persists (`v1_backtest_sizing_empty`, 05-26→06-02 extras). Live /MNQ
   orders also never filled (no live /MNQ position).

Residual bounds (documented, not fixable research-side): the LEAN log carries no
direction → log-derived entries are labeled `long`; the oracle is all-long as of
capture (the golden pins that), so this measurement is direction-unambiguous, but a
future short entry needs POST-body capture to verify side. The error bars are wide
because clean paper history is short — each new live entry tightens them; re-run
this ceremony after more paper weeks for a tighter bound.
