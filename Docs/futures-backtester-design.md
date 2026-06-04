# Futures Backtesting + Research System Design

> **Status: SIGNED OFF 2026-06-03 — DAILY-resolution build; intraday deferred.**
> This doc defines a new top-level `research/` package that DRIVES LEAN (the
> locked engine of record) to let a non-coding operator test many futures
> strategies quickly, model leverage/margin/liquidation honestly, and graduate
> winners into the live system through existing governance. Nothing here touches
> a forbidden path.
>
> **Sign-off resolutions (2026-06-03):** Q-A LEAN engine of record — CONFIRMED.
> Q-B intraday vendor — **DEFERRED: daily bars only for now** (no vendor
> commitment; P5/P6 documented as the future path but NOT built; the harness
> stays resolution-agnostic so there is no ENGINE ceiling — only a deferred DATA
> decision the operator can take later). Q-C float-for-money exemption inside
> `research/` — GRANTED (bounded; §13). Q-D P7 intraday isolation — moot while
> intraday is deferred; P7 daily paper-forward needs no separate account.
> **Build order: P1→P4 (daily) now; P5–P7 deferred.** P1 in progress.
>
> **Read before reviewing:** `CLAUDE.md`, `Docs/recent-architecture-changes.md`,
> `Docs/claude-dev-guide.md` §1.5 (LEAN authoritative / vectorbt research-only)
> + §6.6 (parity rail). This doc does not re-litigate any of those.

---

## 1. Motivation & Problem Statement

The operator wants to **test many futures strategies quickly**, see the **full
distribution of outcomes including ruin under leverage**, and graduate winners
into the live paper/real-money system **without rewriting the strategy**. Today:

- Production `lean/v1_strategy.py` runs ONE strategy (`V1TrendFollowingAlgorithm`)
  at `Resolution.DAILY`, fed by the api's `bar_sync` on-disk bars. There is no
  iteration loop: changing a parameter, adding a candidate strategy, or asking
  "at what leverage would this have been liquidated?" requires hand-editing the
  live algorithm and watching the 21:30 UTC cycle — slow, serial, and entangled
  with production.
- There is **no research archive below daily resolution.** `bar_sync` fetches
  daily bars via IBKR `reqHistoricalData` (clientId=3) once per ET day. IBKR
  intraday history is pacing-limited and shallow — usable as a live feed, not as
  a research corpus.
- There is **no honest leverage/ruin reporting.** The live risk engine
  (`services/risk/sizing.py`) caps size, but nothing shows the operator the
  drawdown / margin-call / liquidation distribution a leveraged variant would
  have produced.

The job of this system is **not to flatter a strategy**. It is to make it fast
to falsify one, and to surface ruin before real money does.

### Why this is a research LAYER, not a new engine

The "no resolution ceiling + same code backtest→live" requirement has exactly
one correct consequence given the locked stack: **LEAN is the engine of record
for backtest AND live, across all resolutions.** LEAN natively runs
tick/second/minute/hour/daily and executes the IDENTICAL `QCAlgorithm` in
backtest and live mode — that IS "no ceiling + same code." It already ships the
margin/leverage/fill/liquidation models that make leveraged intraday realistic,
and `v1_strategy.py` already runs in it (backtest + live). Building a
from-scratch event engine would mean re-deriving fills, margin, rolls, and
liquidation — and would BREAK the trust bridge, because the thing you backtested
would no longer be the thing that trades.

So we build a **research / iteration / leverage-modeling / reporting layer that
DRIVES LEAN** (and optionally vectorbt for daily idea triage). `vectorbt` stays
exactly where §1.5 locks it: an OPTIONAL fast daily/low-frequency screen for
idea triage, never the authority, never intraday, never live.

---

## 2. Proposed Decisions (LOCKED on sign-off)

Encode these; they are the spine of every phase. Numbered `D-N` for reference.

- **D1 — LEAN is the engine of record for the harness.** The research layer
  programmatically launches and parametrizes LEAN backtests + live/paper-forward
  runs and normalizes their results. It never re-implements fills/margin/
  liquidation. (Confirms §1.5; §14 Q-A asks the operator to ratify the engine
  shift explicitly.)
- **D2 — vectorbt is an OPTIONAL daily-only fast screen.** Used for big
  parameter sweeps on daily data to triage ideas before the slow LEAN confirm.
  Any vbt result that informs a graduation decision MUST pass the §6.6 parity
  rail against LEAN first. vbt never runs intraday and never runs live.
- **D3 — New top-level `research/` package.** Offline-by-default. Imports the
  existing `strategies/` and the read-only data utilities in `services/data/`
  (`bar_sync.py`, `map_file_synthesis.py`) as libraries; touches NO forbidden
  path; writes nothing to the production DB or the production `lean_data` volume.
- **D4 — Daily data reuses the api's on-disk LEAN bars; intraday is DEFERRED.**
  Daily research reads the same on-disk format `bar_sync` already produces (a
  read-only *copy*, never the live volume). **The build is daily-resolution
  (operator sign-off 2026-06-03).** Minute/tick would need a vendor →
  LEAN-intraday-on-disk ingestion path (§5.4); that is documented as the future
  P5/P6 path but is NOT built now and carries no vendor commitment. The harness
  is resolution-agnostic (D7), so adding intraday later is a data + ingestion
  job, not an engine change. The "no DataBento" rule governs the **live data
  path**; an intraday RESEARCH archive remains a distinct future sourcing
  decision.
- **D5 — Leverage is a first-class, sweepable dial with a hard cap.** Size in
  integer contracts; `leverage = gross_notional / equity` is tracked, reported,
  and capped. Liquidation is SIMULATED by LEAN (not assumed away), and reported
  with explicit residual-uncertainty at daily resolution where intrabar path is
  unknown.
- **D6 — The harness FEEDS governance; it never bypasses it.** A winning idea
  graduates via a normal `strategies/` PR with a LEAN backtest delta + the
  `risk-review-approved` label. No research code can place a production order,
  mutate `parameter_sets`, or write the audit chain.
- **D7 — Resolution-agnostic strategy contract.** Reference strategies
  (buy-and-hold, time-series momentum, Donchian breakout, a V1 adapter) implement
  ONE interface that LEAN drives at any resolution. The operator picks resolution
  in config; strategy code does not change.
- **D8 — House rules hold, with ONE scoped exemption.** `structlog` (no `print`/
  stdlib logging), UTC storage / ET presentation, UUIDv7 where we persist. The
  Decimal-for-money rule (§3.8) is **explicitly exempted inside `research/`
  analytics/screen modules** (numpy/pandas/vbt are float by nature); values are
  re-materialized as `Decimal`/strings at every governance boundary. Escalated
  in §14 Q-C.
- **D9 — Live-forward candidates are ISOLATED from production.** A research LEAN
  instance never shares production's order flow, audit chain, or write path. P7
  isolation ladder in §6.4 (default: read-only data + PaperBrokerage internal
  fills, zero IBKR session; intraday real-time later via a dedicated clientId in
  80–99 + a separate paper account).

---

## 3. Scope

**In scope**
- CME micro futures in the active V1 universe (`parameters.py::V1_CANDIDATE_UNIVERSE`):
  `/MES /MNQ /MYM /M2K /MGC /MBT` + the 4 bond ETFs `TLT IEF SHY TIP`. `/MCL` and
  any `V1_SIDELINED_MARKETS` stay excluded (re-enable via the existing runbook).
- ALL resolutions where data exists: daily (now), minute/tick (after the §14 Q-B
  vendor decision). Single-contract and multi-contract portfolios.
- Historical backtest AND live/paper-forward on real-time data, via the SAME LEAN
  algorithm code path.
- Leverage / margin / liquidation modeling and ruin reporting.
- Anti-overfitting tooling (IS/OOS, walk-forward, sweep, parameter sensitivity,
  multiple-testing awareness) and operator-legible reports.

**Out of scope (this system)**
- Equities (beyond the 4 bond ETFs already in V1), options, FX, single-name
  stocks. No new asset classes.
- Any change to the live strategy, the live risk engine, the audit chain, or the
  production data pipeline. The harness reads from those; it does not modify them.
- A from-scratch backtest engine (D1).
- Auto-graduation. A human PR + `risk-review-approved` is the only graduation
  path (D6). The harness produces the artifact that justifies the PR; it does not
  open it.

---

## 4. Architecture

### 4.1 Package layout (`research/`, new, hot-fix-class but PR-reviewed)

```
research/
├── README.md                  # operator quickstart: "edit a config, run one command"
├── config/
│   ├── schema.py              # RunConfig dataclass + validation (resolution, dates,
│   │                          #   universe, strategy, params, sizing/leverage, costs,
│   │                          #   engine=lean|vbt, mode=backtest|forward)
│   └── examples/*.yaml        # ready-to-edit operator configs (one per phase)
├── data/
│   ├── daily_loader.py        # read-only loader over a COPY of bar_sync's on-disk bars
│   ├── intraday_ingest.py     # vendor → LEAN intraday on-disk format (P5+; §5.4)
│   ├── contract_specs.py      # per-symbol reference data (mult, tick, margin, calendar)
│   ├── sessions.py            # RTH/ETH, CME CT→UTC, half-days, session breaks (P5+)
│   └── cache.py               # parquet cache layer (idempotent, content-hashed)
├── strategy/
│   ├── contract.py            # ResearchStrategy interface (resolution-agnostic)
│   ├── buy_and_hold.py        # reference + the P1 exactness sanity check
│   ├── tsmom.py               # time-series momentum reference
│   ├── donchian.py            # Donchian breakout reference
│   └── v1_adapter.py          # wraps strategies/v1_trend_following for apples-to-apples
├── lean/
│   ├── driver.py              # build a per-run LEAN config + launch + collect results
│   ├── config_render.py       # render lean.json variant (env, resolution, params, cash)
│   ├── results.py             # parse LEAN output → normalized ResultBundle
│   └── images.py              # which LEAN container/CLI invocation (§4.3)
├── screen/
│   └── vbt_screen.py          # OPTIONAL vectorbt daily sweep (D2; parity-gated)
├── risk/
│   ├── sizing_schemes.py      # fixed / fixed-fractional / vol-target / ATR / risk-parity
│   ├── leverage.py            # leverage = gross_notional/equity; hard cap enforcement
│   ├── liquidation.py         # daily-bar intrabar liquidation ESTIMATOR + uncertainty
│   └── metrics.py             # CAGR, Sharpe, Sortino, MaxDD, Calmar, vol-drag, RoR, CVaR…
├── eval/
│   ├── walk_forward.py        # rolling IS/OOS windows; report OOS not IS
│   ├── sweep.py               # parameter grid → ranked-on-OOS table
│   ├── compare.py             # candidate vs benchmark vs live-V1 normalization
│   └── report.py              # operator-legible HTML/markdown artifact writer
└── runs/                      # gitignored; per-run outputs at runs/<UTC-ts>/
```

`tests/integration/test_vbt_lean_parity.py` — the parity rail (already sketched
in dev-guide §6.6; P2 makes it real).

### 4.2 Data flow

```
                          OPERATOR
                             │  edits research/config/examples/<x>.yaml
                             ▼
                    ┌──────────────────┐
                    │ research/driver  │  (one command: `make research RUN=<config>`)
                    └────────┬─────────┘
            ┌────────────────┼───────────────────────────┐
            ▼ (daily triage) ▼ (authority)               ▼ (forward)
   ┌─────────────────┐ ┌──────────────────┐   ┌────────────────────────┐
   │ screen/vbt      │ │ lean/driver →    │   │ lean/driver (live-mode │
   │ (D2, optional)  │ │ LEAN backtest    │   │ PaperBrokerage, P7)    │
   └────────┬────────┘ └─────────┬────────┘   └───────────┬────────────┘
            │   §6.6 parity gate  │                        │
            └─────────►◄──────────┘                        │
                       ▼                                    ▼
              ┌───────────────────┐               isolated read-only data
              │ eval/ + risk/     │               (no prod order flow, D9)
              │ metrics+ruin+WFO  │
              └─────────┬─────────┘
                        ▼
            research/runs/<ts>/report.html
            (equity curve, drawdown, leverage-over-time, margin usage,
             ruin metrics, vs buy-&-hold + vs live-V1)
                        │
                        ▼ operator decides
            strategies/ PR + LEAN backtest delta + risk-review-approved  (D6)
```

**Data isolation (critical).** The daily loader reads a **copy** of the on-disk
bars, never the live `trading_lean_data` volume. The canonical copy mechanism is
a one-shot `docker run --rm -v trading_lean_data:/src ... rsync` into
`research/data/cache/lean_bars/` (or an operator `scp` from the VPS). The harness
treats this as immutable input. Reasoning: the live volume is single-writer
(bar_sync uid=1000) and load-bearing; a research process must never race it or
chown it (cf. the recurring ownership bug, memory `bar_sync_universe_permission`).

### 4.3 How the harness drives LEAN

LEAN runs as the `quantconnect/lean:latest`-derived image (same base as
`lean_local`). The driver supports two invocation backends, chosen in config:

1. **LEAN CLI (`lean backtest` / `lean live`)** — *proposed default.* The CLI
   wraps the Launcher + Docker, takes a project dir + a `lean.json`, and writes
   results to a known output dir. Cleanest programmatic surface; the driver
   renders a per-run `lean.json` (via `config_render.py`) into a temp project dir
   that mounts `v1_strategy.py` (or a reference strategy) + the data copy, then
   shells out and parses the output.
2. **Raw `docker run` against the Launcher** — fallback if the CLI is
   unavailable in CI; same config, lower-level.

Per-run the driver sets: `algorithm-location`, `resolution`, the `parameters`
block (V1 params + sizing/leverage/cost knobs), `set_cash`, start/end dates, and
selects the `backtesting` environment (historical) or a research-only live
environment (P7). Production's `lean.json` is never edited; the driver renders a
throwaway copy.

> **Stale-doc note for implementers:** `lean/README.md` still describes the
> retired clientId=10 `InteractiveBrokersBrokerage` data-queue path and calls
> `lean_data` "deprecated/unused." That is wrong post-Option-C. Trust
> `lean/lean.json` (FakeDataQueue + `SubscriptionDataReaderHistoryProvider`),
> `CLAUDE.md`, and `recent-architecture-changes.md`. Fixing the README is out of
> scope here (see §12).

### 4.4 Operator interface

The operator NEVER edits engine code. A run is a config file:

```yaml
# research/config/examples/p3_leverage_sweep.yaml
name: mes_mnq_vol_target_leverage_sweep
engine: lean                 # lean (authority) | vbt (daily screen only)
mode: backtest               # backtest | forward
resolution: daily            # daily | minute | tick   (minute/tick require P5+)
universe: ["/MES", "/MNQ"]
date_range: { start: 2018-01-01, end: 2025-12-31 }
strategy: { ref: v1_adapter, params_overrides: { LOOKBACK_DAYS_DONCHIAN: 80 } }
sizing: { scheme: vol_target, vol_target_pct_annual: 0.15 }
leverage: { cap: 3.0, sweep: [1.0, 2.0, 3.0, 4.0, 6.0] }
costs: { model: ibkr }       # mirrors set_brokerage_model(IB, MARGIN)
validity: { scheme: walk_forward, is_months: 24, oos_months: 6 }
benchmarks: ["buy_and_hold", "live_v1"]
```

`make research RUN=research/config/examples/p3_leverage_sweep.yaml` →
`research/runs/<UTC-ts>/report.html` + `result.json` + the raw LEAN output.

---

## 5. Futures Data Correctness + Intraday Hard Parts

This is the #1 source of silent, fatal bugs. The harness matches the production
LEAN conventions exactly so parity holds.

### 5.1 Continuous-contract construction & rolls

Production already solved this; we REUSE it, we do not re-derive it.

- **Mapping mode = `OPEN_INTEREST` (=2), normalization = `RAW`, `contract_depth_offset=0`.**
  These are the exact settings on every `add_future` in
  `lean/v1_strategy.py:370` and the `DATA_MAPPING_MODE_OPEN_INTEREST` constant in
  `services/data/map_file_synthesis.py`. RAW = un-adjusted per-expiry prices;
  LEAN's resolver stitches the active contract per session date via the
  synthesized map_file. The research daily loader MUST present data the same way,
  or vbt-vs-LEAN parity (§6.6) will fail spuriously.
- **Roll detection reuse.** `map_file_synthesis.py` already collapses bar_sync's
  noisy front-month flip-flops (~66 raw transitions for /MES over 2y) into ~6
  genuine quarterly rolls via the `MAP_FILE_PERSISTENCE_DAYS = 15` filter, and
  reproduces LEAN's `SecurityIdentifier.GenerateFuture` byte-for-byte
  (`oadate` / `encode_base36` / `compute_future_sid_hash`, validated 55/55 vs
  bundled `es.csv`). The intraday archive (P5) reuses these helpers to synthesize
  intraday map_files — we do not write a second roll engine.
- **Price treatment documented per run.** The report header states RAW
  per-expiry (the V1 default). If a future reference strategy needs back-/ratio-
  adjusted prices for SIGNALS, that is an explicit, reported choice — never a
  silent normalization swap.
- **`/MYM` routes via `cbot` market_dir, not `cme`.** Mirror
  `bar_sync.py::PHASE1_UNIVERSE_METADATA` + `universe_freshness.py::V1_FUTURES_MARKET_PATHS`.
  Getting this wrong yields `MapFile.Count: 0` and a silent empty history (the
  2026-05-25 PR #226 bug). `contract_specs.py` encodes the market_dir per symbol.

### 5.2 Intraday-specific hard parts (P5/P6)

These do not exist at daily resolution and are where intraday silently fabricates
returns:

- **Session handling (RTH vs ETH / overnight).** CME micros trade nearly 23h.
  The research config picks a session policy; `sessions.py` encodes CME calendars
  (RTH 08:30–15:00 CT for index; metals/crypto differ) and feeds LEAN the right
  `extended_market_hours` flag. Production runs `extended_market_hours=False`
  (`v1_strategy.py:373`); intraday research must state its choice explicitly.
- **Timezone correctness.** CME settles in CT; LEAN stores UTC; production sets
  `set_time_zone("America/New_York")` for the 17:30 ET cycle. Intraday bars MUST
  be unambiguous UTC on disk. DST transitions (CT has them) are the classic
  off-by-one-hour return fabricator — `sessions.py` is DST-aware and tested
  against known boundaries.
- **Session breaks & half-days.** Daily maintenance halt (CME 16:00–17:00 CT),
  holiday half-days. A minute bar that straddles a break is dropped, not
  interpolated.
- **Daily settlement vs the minute stream.** The settlement print is not the
  last traded minute. The harness reconciles the synthesized daily bar (used by
  the daily path) against the minute close and reports any divergence rather than
  silently trusting one.

### 5.3 Contract reference data (`contract_specs.py`)

| Symbol | Name | Multiplier (point value) | Tick | $/tick | Exchange / market_dir | Roll |
|---|---|---|---|---|---|---|
| `/MES` | E-mini S&P 500 Micro | $5 × index | 0.25 | $1.25 | CME / `cme` | quarterly |
| `/MNQ` | E-mini Nasdaq-100 Micro | $2 × index | 0.25 | $0.50 | CME / `cme` | quarterly |
| `/MYM` | E-mini Dow Micro | $0.50 × index | 1.0 | $0.50 | CBOT / **`cbot`** | quarterly |
| `/M2K` | E-mini Russell 2000 Micro | $5 × index | 0.10 | $0.50 | CME / `cme` | quarterly |
| `/MGC` | Gold Micro | $10 × /oz | 0.10 | $1.00 | COMEX / `comex` | monthly-ish |
| `/MBT` | Bitcoin Micro | 0.1 × BTC | 5.0 | $0.50 | CME / `cme` | monthly |
| `TLT`,`IEF`,`SHY`,`TIP` | bond ETFs | $1 × price (1 share) | 0.01 | $0.01 | NYSE Arca / `usa` | none (cash) |

Each row also carries: currency (USD), initial & maintenance margin (sourced
from LEAN's symbol-properties DB and overridable per run), trading calendar, and
expiry schedule. The futures rows pull expiry rules from
`map_file_synthesis.compute_future_expiry`; ETF rows have no roll. This table is
the single source of truth for notional/leverage math (§6.1) and is unit-tested
against the multipliers in `parameters.py`'s universe comments.

### 5.4 Intraday ingestion path (P5, gated on §14 Q-B)

`intraday_ingest.py` writes the vendor's minute/tick bars into LEAN's intraday
on-disk layout (the minute analog of bar_sync's daily zips), reusing
`map_file_synthesis.py` for map_files/rolls and `contract_specs.py` for symbol
properties. Offline, idempotent, parquet-cached. **This is a separate research
archive, not the live feed** — it never touches the production `lean_data`
volume. Vendor + cost are escalated in §14 Q-B (proposed default: Databento CME
historical, with a small bounded backfill before any subscription commitment).

### 5.5 Look-ahead avoidance

- Signal on bar `t` fills at `open[t+1]` by default (next bar's open); `close[t]`
  fills are an explicit opt-in, reported. This matches V1's "decide at 17:30 ET
  settlement, act next session" cadence.
- At minute resolution: NEVER use bar `t`'s close to act within bar `t`. LEAN's
  event model enforces this when the algorithm subscribes correctly; the harness
  adds a static check in `v1_adapter`/reference strategies that flags any
  same-bar close→act access in review.

---

## 6. Leverage / Margin / Liquidation / Risk Model

The operator WANTS leverage for outsized returns. We model it honestly — the
point is to show the leverage at which the strategy would have been **liquidated**
and how often, not to hide it.

### 6.1 Sizing & leverage (`research/risk/`)

- Size in **integer contracts**. `notional = contracts × multiplier × price`
  (multiplier from §5.3). `leverage = gross_notional / equity`, tracked per bar
  and reported as a time series (peak, mean, terminal).
- **Sizing schemes** (`sizing_schemes.py`): fixed contracts, fixed-fractional,
  vol-targeting (V1's Clenow-style `VOL_TARGET_PCT_ANNUAL`), ATR-based (V1's stop
  logic), risk-parity across contracts. Each is a pure function the operator
  selects in config; each runs under a **hard `leverage.cap`**.
- **Parity to the live sizing authority.** The live sizer is
  `services/risk/sizing.py` (Stages 0–5, FORBIDDEN path). The research
  `sizing_schemes.py` re-implements the same vol-target/ATR math for sweeping but
  is NOT the authority. A unit test pins the research vol-target output against a
  recorded `sizing.py` Stage-1 vector for the V1 params so the two never silently
  diverge. (We import the public result, we do not modify `services/risk/`.)

### 6.2 Margin & forced liquidation (LEAN-native, configured + reported here)

- LEAN's `set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE,
  AccountType.MARGIN)` (`v1_strategy.py:298`) already models initial &
  maintenance margin, available margin, margin calls, and forced liquidation via
  its `MarginCallModel`. The harness CONFIGURES this (margin overrides per run)
  and REPORTS it (margin-call frequency, time-in-margin-call, liquidation events).
  We do not write our own margin engine — that would break the trust bridge.
- **A backtest without liquidation lets a strategy "ride through" a drawdown that
  would have wiped the account out.** That is the most dangerous way to be wrong
  about leverage. So liquidation is always ON, and the report makes a liquidation
  event impossible to miss (red banner + the equity point at which it triggered).

### 6.3 The honest daily-resolution caveat (and how we handle it)

At `Resolution.DAILY`, LEAN can only check margin at the **daily bar boundary** —
it cannot see the intrabar path, so a margin breach that happened and recovered
*within* a day is invisible. This is a real limit, not a bug.

`research/risk/liquidation.py` adds an **offline intrabar liquidation ESTIMATOR**
for daily runs: using each daily bar's high/low and the position/leverage, it
flags "equity would have crossed maintenance margin intraday on these N days" as
a **WARNING with explicit residual uncertainty** (a minute bar doesn't reveal the
path either; tick reduces but never eliminates it). The estimator is a
flag-for-confirmation, not a verdict. The honest resolution of the warning is to
**re-run that window at minute resolution (P5)** — which the report links to
directly. We REPORT the uncertainty; we never pretend daily bars settle it.

### 6.4 Live-forward isolation ladder (P7, D9)

1. **Default (daily paper-forward).** A research LEAN instance in live-mode with
   `live-mode-brokerage = PaperBrokerage` reads a **read-only copy** of the
   on-disk daily bars and simulates fills internally — exactly as production LEAN
   does (it never places IBKR orders; the api does). **Zero new IBKR session,
   zero order flow, zero production interference.** This is the cheapest correct
   isolation and needs no new account.
2. **Intraday real-time (later).** Needs its own live market-data feed. Use a
   **dedicated clientId in 80–99** (market-data-only; never 1/3/4/10; avoid 99
   which `replay_executions.py` uses — proposed default **88**) AND a **separate
   IBKR paper account** so research positions can never commingle with the
   production paper account (`DUQ825170`) or pollute EOD recon. Surface for
   sign-off when P7-intraday is actually reached (it is not in the P1–P5 critical
   path).

### 6.5 Risk metrics (`research/risk/metrics.py`) — surface ruin, don't hide it

Every report computes and shows: CAGR, annualized vol, Sharpe, Sortino, **max
drawdown**, Calmar/MAR, **volatility drag** (geometric vs arithmetic),
**risk-of-ruin / P(drawdown > X%)**, time-to-recovery, worst day/week, downside
deviation, CVaR / tail loss, **margin-call frequency**, **peak leverage used**,
and a **Kelly / fractional-Kelly** context line (what fraction of full Kelly the
chosen sizing implies). The deliverable must let the operator SEE the leverage at
which liquidation occurs and how often — that is the headline of the leverage
report, not a footnote.

---

## 7. Anti-Overfitting & Validity

Higher frequency = more data points, more params, more microstructure noise =
more ways to fool yourself. Discipline matters MORE intraday, not less.

- **IS/OOS split + walk-forward (`eval/walk_forward.py`).** Rolling
  in-sample/out-of-sample windows (config `is_months`/`oos_months`). **Sweeps
  rank on OOS, never IS** (`eval/sweep.py` sorts by OOS metric and shows the IS→OOS
  degradation explicitly).
- **Parameter sensitivity.** Report the metric surface around the chosen params,
  not just the peak — a sharp peak is overfit; a broad plateau is robust.
- **Multiple-testing awareness.** When N parameter combos are tried, the best
  Sharpe is inflated. Report **deflated-Sharpe / reality-check** style context
  (how many combos were tried, expected max-Sharpe under the null). Critical at
  high frequency.
- **Regime analysis.** Break results by regime (trend/chop, vol buckets) so a
  result that only works in one regime is visible.
- **Benchmarks, always.** Every candidate is shown against **buy-and-hold per
  contract** and **the live V1** (`v1_adapter`), normalized for comparison.

---

## 8. Trust Bridge to Live

"Trust the backtest" reduces to "trust LEAN + the data," because backtest→live is
the SAME LEAN code path. So data validation stays front and center, and we prove
the bridge twice:

- **§6.6 parity rail (`tests/integration/test_vbt_lean_parity.py`).** For daily
  strategies the vbt screen must match LEAN within: per-trade slippage ≤ 5bps,
  aggregate P&L ≤ 0.5% of starting equity, trade count within 5%. A vbt result
  that fails parity cannot inform a graduation decision.
- **Reproduce V1 end-to-end (P2 acceptance).** The first real validation is the
  harness driving LEAN to reproduce the production `V1TrendFollowingAlgorithm`
  result on the same window — same signals, same trades. If the harness can't
  reproduce the thing that already trades, it can't be trusted to evaluate
  anything new.
- **Buy-and-hold exactness (P1 acceptance).** The daily loader + buy-and-hold
  reference must reproduce a hand-computable buy-and-hold return exactly (no
  fees) — the cheapest possible proof the data plumbing is correct before any
  strategy logic enters.

---

## 9. Phase / PR Breakdown

Each phase is independently useful and ships as one PR with tests passing before
merge. None touches a forbidden path; each PR that adds research code is normal
review (no `risk-review-approved` needed unless it edits `strategies/**` or a
forbidden path — only the eventual graduation PR does).

- **P1 — Daily spine.** `research/config/` + `data/daily_loader.py` +
  `data/contract_specs.py` + `strategy/contract.py` + `strategy/buy_and_hold.py` +
  `eval/report.py` skeleton + optional `screen/vbt_screen.py`.
  **Acceptance:** reproduces buy-and-hold exactly on a daily window; report
  artifact renders.
- **P2 — LEAN driver.** `research/lean/{driver,config_render,results,images}.py` +
  make `tests/integration/test_vbt_lean_parity.py` real.
  **Acceptance:** programmatic LEAN daily backtest runs end-to-end; §6.6 parity
  passes; reproduces production V1 on a fixed window.
- **P3 — Leverage.** `research/risk/{sizing_schemes,leverage,liquidation,metrics}.py`.
  **Acceptance:** leverage sweep produces a ruin report; LEAN liquidation events
  surface; daily intrabar-liquidation estimator flags + reports uncertainty.
- **P4 — Validity.** `research/eval/{walk_forward,sweep,compare}.py` + ranked-OOS
  reports. **Acceptance:** a walk-forward run ranks combos on OOS with IS→OOS
  degradation shown; multiple-testing context printed.
- **P5 — Intraday. DEFERRED (operator sign-off 2026-06-03: daily-only for now;
  no vendor commitment).** When taken up: `data/intraday_ingest.py` +
  `data/sessions.py` + minute backtests + intrabar fill realism.
  **Acceptance:** a minute backtest runs on ingested vendor data; session/TZ
  correctness tests pass; a P3 daily liquidation warning is confirmed/denied at
  minute resolution.
- **P6 — Tick. DEFERRED (follows P5).** When taken up: tick-resolution backtests
  where data exists. **Acceptance:** a tick backtest runs; residual intrabar
  uncertainty is quantified vs minute.
- **P7 — Live paper-forward.** Run a candidate in LEAN live-mode in an isolated
  instance per §6.4. **Acceptance:** a candidate runs forward on real-time data
  with zero production interference (verified: no new order flow, no recon delta,
  no audit-chain write).

---

## 10. Test Plan

- **Unit (per phase, `tests/unit/test_research_*.py`).** Contract-spec math
  (notional/leverage/$-per-tick per §5.3); sizing-scheme outputs incl. the
  parity pin against `services/risk/sizing.py` Stage-1; metrics math on known
  series (a fixed equity curve with a known MaxDD/Sharpe/CVaR); session/DST
  boundary handling (P5); look-ahead static check.
- **Integration.** `test_vbt_lean_parity.py` (the §6.6 rail); the P2 V1-repro
  test (harness-driven LEAN vs a recorded production V1 result on a fixed
  window); intraday ingest → LEAN-reads-it round-trip (P5).
- **Golden.** A frozen `research/runs/` fixture for buy-and-hold and V1-repro so
  report regressions are caught (mirrors the dev-guide §6.5 golden-test pattern).
- **What we CANNOT test pre-vendor.** Minute/tick correctness until §14 Q-B is
  resolved and a data sample exists. P1–P4 are fully testable on daily today.
- `make test` green before every PR (dev-guide §1.4). Note: the local venv
  currently has ~54 pre-existing `ib_async`-missing failures (memory
  `bar_sync_universe_permission`); research tests must not depend on `ib_async`
  so they run clean locally and in CI.

---

## 11. Risks + Mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | Research process races/chowns the live `lean_data` volume → breaks production bar_sync | Read-only **copy** only (§4.2); harness never mounts the live volume writable; documented in `research/README.md` |
| R2 | vbt screen lies vs LEAN, a bad idea graduates | §6.6 parity rail is a hard gate; vbt results carry a "PARITY: pass/fail" stamp; fail ⇒ cannot inform graduation (D2) |
| R3 | Daily backtest hides intrabar liquidation under leverage | LEAN liquidation always on (§6.2) + daily intrabar estimator WARNING (§6.3) + link to minute re-run; report makes ruin un-missable (§6.5) |
| R4 | Intraday TZ/session bug fabricates returns | `sessions.py` DST-aware + tested vs known boundaries; settlement-vs-minute reconciliation reported (§5.2); minute correctness gated behind explicit tests (P5) |
| R5 | Overfitting at high frequency | OOS-ranked sweeps, walk-forward, deflated-Sharpe context, parameter-sensitivity plateau check, regime split (§7) |
| R6 | Research live-forward interferes with production (orders/recon/audit) | §6.4 isolation ladder; default needs no IBKR session; intraday uses a dedicated 80–99 clientId + separate paper account (§14 Q-B/Q-D); P7 acceptance verifies zero deltas |
| R7 | Float exemption leaks into a governance artifact | Exemption scoped to `research/` analytics only; Decimal/string re-materialization at every boundary (D8); a boundary test asserts governance outputs are strings (§14 Q-C) |
| R8 | LEAN driver brittle across LEAN releases | Pin the LEAN image tag per run in `result.json`; CLI default + raw-docker fallback (§4.3); golden V1-repro catches engine drift |
| R9 | Intraday data cost balloons (minute ≈ ~1000× daily volume) | Bounded backfill before any subscription (§14 Q-B); parquet cache + symbol/date scoping; cost stated in the run header |

---

## 12. Out of Scope (Deferred)

- New asset classes (equities beyond bond ETFs, options, FX) — §3.
- Editing the live strategy / risk engine / audit chain / production data
  pipeline — the harness reads these.
- Auto-graduation / agent-driven parameter changes from research results — human
  PR only (D6).
- Fixing `lean/README.md`'s stale Option-C sections — real but separate; flagged
  for a follow-up docs PR, not bundled here.
- A research web UI — the operator interface is config-in / report-out (§4.4);
  surfacing reports in the existing web app is a future nice-to-have.

---

## 13. Governance & House-Rule Exemptions

- **Forbidden paths (dev-guide §2.2): untouched.** `research/` imports
  `services/data/` and `strategies/` as read-only libraries. The only files that
  ever require `risk-review-approved` are the eventual graduation PRs that edit
  `strategies/**` (and never `services/risk|signal|audit|execution|
  reconciliation|calibration`, `services/agent/*`, or `alembic/**`).
- **structlog only** (no `print`/stdlib logging) — research scripts included.
- **clientId discipline (§1.5).** Research live-forward uses 80–99 only;
  proposed default 88 (avoiding 99/replay); documented in every connecting path.
- **Decimal exemption (D8, escalated §14 Q-C).** numpy/pandas/vbt are float;
  forcing Decimal through them is infeasible. Exemption is bounded to `research/`
  analytics/screen/metrics; every value crossing into a PR backtest delta,
  `parameter_sets`, or any persisted governance artifact is re-materialized as
  `Decimal` → string at the boundary, enforced by a test (R7).

---

## 14. Open Decisions for Sign-off

**RESOLVED 2026-06-03:** Q-A CONFIRMED · Q-B **DEFERRED (daily-only; no vendor)**
· Q-C GRANTED (bounded) · Q-D moot while intraday deferred. The originals are
retained below for the record.

The defaults below are PROPOSED; everything else in this doc is a default I'll
proceed on once the doc is ratified. These four are the genuinely consequential /
cost-bearing / strategy-ambiguous ones.

- **Q-A — Confirm LEAN as the engine of record for the harness (D1).** The "no
  ceiling + same code backtest→live" requirement forces this; vbt stays a
  daily-only research screen. *Proposed: CONFIRM (it is the only choice
  consistent with §1.5 and the trust bridge).* This is a confirm, not a
  re-litigation.
- **Q-B — Intraday research data vendor + cost (gates P5/P6).** Deep minute/tick
  CME history needs a vendor; IBKR `reqHistoricalData` is pacing-limited and
  shallow (fine as a live feed, not a research archive). *Proposed: **Databento**
  CME historical (precedent exists — it seeded futures pre-Option-C at ~$0.96/run
  and the `/MCL` re-enable runbook already lists it as an alt feed), starting
  with a small **bounded one-time backfill** (a few symbols × a few years of
  minute) before any subscription commitment.* Note: this is a RESEARCH-archive
  decision, distinct from the locked "no DataBento on the live path." Alternative:
  defer intraday entirely and ship P1–P4 daily-only first.
- **Q-C — Float-for-money exemption inside `research/` (D8).** *Proposed: GRANT,
  bounded to research analytics, with Decimal/string re-materialization at every
  governance boundary (R7 test enforces).* Without it, numpy/vbt can't be used.
- **Q-D — P7 isolation: separate IBKR paper account for intraday real-time.**
  *Proposed: daily paper-forward needs NONE (read-only data + PaperBrokerage,
  §6.4-1); intraday real-time SHOULD get a dedicated clientId (88) + a separate
  paper account so it can never touch production recon.* Only relevant when
  P7-intraday is reached; not on the P1–P5 path.

---

## Appendix: Files Touched Summary

**New (all under `research/`, plus the parity test):** `research/README.md`,
`research/config/{schema.py,examples/*.yaml}`,
`research/data/{daily_loader,intraday_ingest,contract_specs,sessions,cache}.py`,
`research/strategy/{contract,buy_and_hold,tsmom,donchian,v1_adapter}.py`,
`research/lean/{driver,config_render,results,images}.py`,
`research/screen/vbt_screen.py`,
`research/risk/{sizing_schemes,leverage,liquidation,metrics}.py`,
`research/eval/{walk_forward,sweep,compare,report}.py`,
`tests/unit/test_research_*.py`, `tests/integration/test_vbt_lean_parity.py`
(promote from sketch), a `make research` target in `Makefile`, and a
`research/runs/` gitignore entry.

**Read-only imports (NOT modified):** `services/data/{bar_sync,map_file_synthesis}.py`,
`strategies/v1_trend_following/**`, `lean/{v1_strategy.py,lean.json}`,
`services/risk/sizing.py` (public result only, for the parity pin).

**Never touched:** any forbidden path (dev-guide §2.2), the production
`trading_lean_data` volume, the production DB, the audit chain.

---

## Sign-off Checklist — SIGNED OFF 2026-06-03

- [x] Q-A: LEAN as engine of record for the harness — CONFIRMED.
- [x] Q-B: intraday vendor + cost — **DEFERRED: daily bars only for now** (no
      vendor commitment; P5/P6 documented as the future path, not built).
- [x] Q-C: `research/` float-for-money exemption — GRANTED (bounded).
- [x] Q-D: P7 intraday isolation — moot while intraday deferred; daily
      paper-forward needs no separate account.
- [x] §3 scope (micro futures + 4 bond ETFs; `/MCL` + sidelined excluded) accepted.
- [x] §9 phase/PR breakdown — **P1→P4 daily now; P5–P7 deferred** — accepted.
- [x] §11 risk-mitigation review accepted.

**Status:** SIGNED OFF. P1 (daily spine) in progress. See "P1 Kickoff Prompt".

---

## P1 Kickoff Prompt (for next Claude Code session)

> I want to start P1 from `Docs/futures-backtester-design.md` — the daily spine
> of the futures research harness.
>
> Per the design doc:
>
> - Scope: NEW files only, all under `research/` + a `make research` target +
>   `research/runs/` gitignore. No forbidden path is touched; this PR is normal
>   review (no `risk-review-approved` needed).
> - Build: `research/config/{schema.py,examples/p1_buy_and_hold.yaml}`,
>   `research/data/daily_loader.py` (read-only over a COPY of bar_sync's on-disk
>   bars — never the live volume), `research/data/contract_specs.py` (the §5.3
>   table), `research/strategy/{contract.py,buy_and_hold.py}`,
>   `research/eval/report.py` (skeleton), and optionally
>   `research/screen/vbt_screen.py`.
> - House rules: structlog (no print), the §14 Q-C float exemption is bounded to
>   research analytics with Decimal/string at governance boundaries.
>
> Read in this order:
> 1. `Docs/futures-backtester-design.md` (this design — §2 decisions, §4
>    architecture, §5.3 specs, §8 acceptance are the spine)
> 2. `CLAUDE.md` + `Docs/recent-architecture-changes.md`
> 3. `Docs/claude-dev-guide.md` §1.5 + §3 (coding standards) + §6.6 (parity)
> 4. `services/data/bar_sync.py` (on-disk format helpers: `equity_daily_zip_path`,
>    `futures_trade_zip_path`, `futures_oi_zip_path`, the universe + map_file paths)
> 5. `strategies/v1_trend_following/parameters.py` (universe + specs)
>
> Acceptance for this session:
> - `make research RUN=research/config/examples/p1_buy_and_hold.yaml` produces
>   `research/runs/<ts>/report.html`.
> - The daily loader + buy-and-hold reference reproduce a hand-computable
>   buy-and-hold return EXACTLY (no fees) for at least one futures contract and
>   one bond ETF — the §8 P1 proof that the data plumbing is correct.
> - `contract_specs.py` unit-tested against the multipliers in `parameters.py`.
> - `make test` green before opening the PR.

### P2–P7 kickoff stubs

Each later phase starts the same way ("start P{N} from
`Docs/futures-backtester-design.md`"), scoped to that phase's files in §9 +
acceptance in §9/§10. P2 makes the LEAN driver + parity rail real and reproduces
V1; P3 adds leverage/ruin; P4 adds walk-forward/sweep; P5 (gated on §14 Q-B) adds
intraday ingest + sessions; P6 adds tick; P7 adds isolated live paper-forward.
