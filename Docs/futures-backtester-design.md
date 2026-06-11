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
  `research/lean/projects/donchian_reference.py` + `research/strategy/donchian.py` +
  `research/eval/{parity,reproduce_v1}.py` + make
  `tests/integration/test_vbt_lean_parity.py` real. **Code + parser + parity-logic +
  V1 cross-check land CI-green WITHOUT LEAN** (committed LEAN-output fixtures); the
  REAL engine run is the operator's acceptance gate (see "P2 landed" below + the
  ⚠️ POST hazard). **Acceptance:** programmatic LEAN daily backtest runs end-to-end;
  §6.6 parity passes; reproduces production V1 on a fixed window.
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

## P2 Kickoff Prompt (for next Claude Code session)

P1 (daily spine) merged in PR #319. Copy-paste this to start P2:

> I want to start P2 from `Docs/futures-backtester-design.md` — the LEAN driver +
> the vbt↔LEAN parity rail + reproducing production V1. This is the phase that
> makes LEAN the authority and proves the backtest→live trust bridge.
>
> Context: P1 shipped `research/{config,data,strategy,screen,eval}` + `run.py` +
> `make research`. P1's evaluator (`research/screen/daily_eval.py`) is
> research-only / NON-authoritative; do NOT extend it into an engine — drive LEAN.
>
> Per the design doc (D1, §4.3, §6.6, §8, §9):
>
> - Scope: NEW files under `research/lean/` + make `tests/integration/
>   test_vbt_lean_parity.py` real. No forbidden path → normal review.
> - Build:
>   - `research/lean/config_render.py` — render a THROWAWAY per-run `lean.json`
>     (env `backtesting`, resolution, the `parameters` block, `set_cash`,
>     start/end) into a temp project dir. NEVER edit production `lean/lean.json`.
>   - `research/lean/images.py` — the invocation backend: LEAN CLI (`lean
>     backtest`) default + raw `docker run` against the Launcher as fallback.
>   - `research/lean/driver.py` — launch a daily LEAN backtest for V1 or a
>     reference strategy against the on-disk bar COPY (§4.2; never the live
>     volume); collect the output dir.
>   - `research/lean/results.py` — PARSE LEAN's result JSON (orders/trades/
>     statistics) and NORMALIZE into the SHARED
>     `research.eval.results.BacktestResult` (+ a trades list). A parser, not a
>     second result type.
>   - Promote `tests/integration/test_vbt_lean_parity.py` (dev-guide §6.6) from
>     sketch to real: run a daily strategy in BOTH the research evaluator and
>     LEAN; assert per-trade slippage ≤ 5 bps, aggregate P&L ≤ 0.5% of starting
>     equity, trade count within 5%. Use Decimal at the comparison boundary.
>
> Hard realities — handle them, don't pretend them away:
>   - LEAN/Docker may be ABSENT locally and in CI. Make the driver + parser
>     UNIT-testable WITHOUT running LEAN by committing a CAPTURED LEAN-output
>     fixture under `tests/`, and gate the REAL LEAN run behind an availability
>     check that SKIPS when LEAN/Docker is missing — mirror the optional-vectorbt
>     seam in `research/screen/vbt_screen.py`. A missing LEAN must never become a
>     silent pass.
>   - Reproduce production V1: drive LEAN to run `V1TrendFollowingAlgorithm`
>     (`lean/v1_strategy.py`) on a fixed daily window with the live params, and
>     show the harness-captured `BacktestResult` matches a RECORDED production V1
>     backtest committed as a golden fixture (dev-guide §6.5 pattern).
>   - Decimal-at-boundary (design D8): parse LEAN trade prices/P&L as Decimal
>     where they feed the §6.6 comparison.
>
> Read in this order:
>   1. `Docs/futures-backtester-design.md` (§4.3 driver, §6.6, §8 trust bridge, §9 P2)
>   2. `Docs/claude-dev-guide.md` §6.6 (parity criteria) + §6.5 (golden pattern)
>   3. `lean/lean.json` (environments `backtesting` vs `paper-internal`;
>      algorithm-type-name / location / parameters). NOTE `lean/README.md`'s
>      `paper-internal` clientId=10 / data-queue text is STALE post-Option-C —
>      trust `lean.json` + `CLAUDE.md` + `Docs/recent-architecture-changes.md`.
>   4. `lean/v1_strategy.py` (the algorithm to reproduce: `add_future` RAW +
>      `OPEN_INTEREST` + `contract_depth_offset=0` + `set_filter(-365, 90)`; IBKR
>      MARGIN brokerage model; warmup; 17:30 ET cycle)
>   5. `research/screen/daily_eval.py` + `research/eval/results.py` (the evaluator
>      + the shared result type to parity-check against)
>   6. `research/data/daily_loader.py` (the on-disk format the LEAN data copy uses)
>
> Acceptance (design §8/§9):
>   - A programmatic LEAN daily backtest runs end-to-end (real LEAN) → `BacktestResult`.
>   - `test_vbt_lean_parity.py` passes the §6.6 tolerances on a daily strategy.
>   - Production V1 reproduced: harness-driven LEAN matches the recorded V1 golden
>     within tolerance.
>   - Driver + parser UNIT-tested against a committed LEAN-output fixture (CI green
>     with no Docker/LEAN). `make test` green; ruff + mypy `--strict` clean
>     (`research` is in the typecheck target).

### P2 landed — implementation notes + the ⚠️ POST hazard (versioned)

This subsection records how P2 was built so it is versioned with the design.

**⚠️ POST hazard (the #1 safety fact).** `lean/v1_strategy.py` POSTs `signal_emitted`
+ `lean_cycle_heartbeat` to `LEAN_LOCAL_API_BASE_URL` (default `http://api:8000`) on
EVERY daily cycle, in backtest AND live mode, and `initialize()` fail-closes when
`LEAN_LOCAL_BEARER_TOKEN` is empty. A research V1 backtest MUST NOT reach the prod
api. The driver (`research/lean/driver.py`) neutralizes this THREE ways, applied
uniformly to every run (defense in depth; the reference strategies don't POST):

1. **`--network none`** — the run is never joined to the prod `internal` docker
   network, so `http://api:8000` cannot resolve at all.
2. **Unreachable `LEAN_LOCAL_API_BASE_URL` stub** (`http://127.0.0.1:9`) — every POST
   fails fast/harmlessly (`v1_strategy._post_event` is best-effort; a URLError is
   logged, not fatal).
3. **Dummy non-empty `LEAN_LOCAL_BEARER_TOKEN`** — satisfies the fail-closed check
   without authenticating anything (no api is ever reached).

For the parser's captured fixture + the parity rail, a NON-posting reference
(`donchian_reference.py`) is preferred so no POST path is exercised at all. Data
isolation (R1) is enforced too: the data mount is ALWAYS a read-only host COPY
(`research/data/cache/lean_bars`); `driver._guard_data_root` refuses the live
`trading_lean_data` / `lean_data` volume by name. Unit tests assert these invariants
on the assembled `docker run` argv (no prod api string, no live-volume mount,
`--network none`, the stub URL + dummy bearer).

**Backend split.** Two invocation backends (design §4.3), chosen by availability
(`research/lean/images.py`, which probes the `lean` EXECUTABLE + the Docker DAEMON —
never `importlib.find_spec`, which an empty `lean` namespace-package artifact fools):
the **raw-docker backend against the `lean_local` image** is the production-faithful,
env-controllable path and the ONLY one used for V1 (its entrypoint merges the
throwaway config + honours the isolation env; the CLI cannot inject V1's `os.environ`
POST config). The **LEAN CLI backend** serves the POST-free reference strategies.

**Parser tolerance + fixture-capture handoff.** LEAN's result JSON drifts across
releases, so `research/lean/results.py` finds the result file by CONTENT (not
filename), reads the equity curve from both the line-series `{x,y}` and candlestick
`{x,…,close}` shapes, and accepts PascalCase/camelCase keys + int/string enums. It is
unit-tested against committed fixtures covering all three shapes (line, candlestick,
array-form). The V1 reproduction golden uses a REAL captured V1 LEAN log
(`tests/fixtures/v1_repro_log/`, from an isolated harness run on the production
algorithm) + a REAL prod `signals` oracle snapshot (`tests/fixtures/v1_oracle/`); see
the real-engine results below.

**What is CI-green vs operator-acceptance.** Green with NO Docker/LEAN: the parser,
config render, availability checks, command-assembly safety invariants, parity
comparison logic, and the reproduce-V1 parse + oracle cross-check (all on committed
fixtures). Gated to SKIP visibly until the operator runs the real engine: the
end-to-end LEAN backtest, the empirical §6.6 tolerance pass, and the real V1↔oracle
match. If the parity tolerances need a fill-model tweak on first real run, that is a
one-line change in `donchian_reference.py` — expected iteration against a real engine.

### P2 real-engine acceptance — RESULTS (2026-06-04, `quantconnect/lean:latest` 42.5GB)

Both trust-bridge proofs ran against actual LEAN (image built locally; bars snapshotted
from prod; oracle captured from the prod `signals` table):

- **§6.6 parity rail — PASS.** Donchian on TLT (2025-07→2026-05): trade count 3 = 3,
  aggregate P&L Δ ≈ 0.0005% of equity, per-trade slippage **0.0 bps**. The real LEAN
  result JSON for this build is **array-form equity points** (`[ts, o, h, l, c]`, not
  dict points) + all-camelCase — the parser was extended to handle array-form (a third
  shape beside line `{x,y}` and candlestick `{x,…,close}`). Slippage is 0 because the
  numpy fills are priced at the NEXT session's open (the realistic, LEAN-matching
  convention) rather than the decision close; a close-vs-open comparison was 14–68 bps.
- **Reproduce-V1 — PARTIAL (structural), harness PROVEN.** The harness drives the
  production `V1TrendFollowingAlgorithm` end-to-end in real LEAN, FULLY ISOLATED (every
  per-cycle POST → "Connection refused"; zero prod contact), warmup completes, and the
  real ER-gate decision logic executes. But a clean decision match against the live
  oracle is **structurally limited** (4/9 strict (date,market); 3/4 markets), for three
  real reasons that are now documented + handled:
  1. **V1 emits via POST, not LEAN orders** — it places no LEAN orders, so its decisions
     live in the LEAN LOG (`v1_signals_generated` / `v1_signal_rejected`), not the result
     JSON. `research/eval/reproduce_v1.py` parses the log (emitted = universe − rejected
     per cycle), keeping the strongest `--network none` isolation (no POST-capture
     sidecar). [The original order-fill extraction yielded 0 for V1 — fixed.]
  2. **Backtest V1 has no position feedback** (PaperBrokerage; the `/positions` GET is
     live-mode only), so it RE-EMITS the same breakout every cycle, unlike the
     position-aware live system (anti-pyramiding, PR #250). `first_entry_per_market`
     reduces both sides to neutralize this for a fair market-level comparison.
  3. **Live used a distinct param-hash per signal** (params calibrated mid-window + the
     ER gate landed 2026-06-02) vs the uniform-param backtest → date/market mismatches.
  Net: V1 reproduction is a directional proof (harness runs prod V1 + reproduces live
  decisions where params align), not a byte-for-byte match. Exact short-side capture
  would need POST-body capture (a future enhancement; today V1 is long-only and the
  oracle confirms all-long).

### P3 landed — leverage / margin / liquidation / ruin (2026-06-04)

Shipped `research/risk/{sizing_schemes,leverage,liquidation,metrics}.py` + the report /
config / run wiring. Implementation notes (versioned with the design):

- **Sizing schemes** (`sizing_schemes.py`): fixed / fixed-fractional / vol-target /
  ATR / risk-parity, each a pure `size_for_bar` under a HARD `leverage.cap`
  (`cap_contracts_to_leverage` can only shrink a position). `simulate_sized_path` is the
  P3 analog of `evaluate_daily` — per-bar sizing, one-bar lag, RIDES THROUGH a wipeout
  (it never auto-de-risks, so ruin is surfaced, not hidden — §6.2).
- **Parity pin** (`tests/unit/test_research_sizing_parity_pin.py`): `vol_target_notional`
  is pinned against the LIVE `services/risk/sizing.py` Stage-1 `unconstrained_notional`
  (imported public result; forbidden path NOT modified), compared as `Decimal` at the
  boundary (D8/R7). Holds across 3 (vol, m_combined) cases on V1's locked 0.15 target.
- **Liquidation** (`liquidation.py`): the daily intrabar ESTIMATOR overlays each bar's
  high/low on the held position vs maintenance margin → a WARNING with the mandatory
  residual-uncertainty caveat (re-run at minute = P5). Maintenance margin is `Decimal`
  (futures: fixed $/contract reference, or LEAN's `margins/<SYM>.csv` when the snapshot
  is present; ETFs: Reg-T 25% of notional).
- **Metrics** (`metrics.py`, moved+extended from `eval/metrics.py`): the full §6.5 suite,
  liquidation-aware (absolute-$ drawdown + `is_wiped` replace the P1 post-wipeout pct-DD
  limitation; `risk_of_ruin = 1.0` when liquidated/wiped, else the parametric Brownian
  P(dd>50%)).
- **LEAN-native liquidation** (`lean/results.py`): `parse_margin_events` surfaces
  margin-call / liquidation orders by tag; `ParsedLeanResult.liquidated` + the report's
  RED banner fire on the estimator OR a LEAN margin call.
- **Acceptance MET:** `make research RUN=research/config/examples/p3_leverage_sweep.yaml`
  → a ruin report (leverage-over-time + the §6.5 suite + a RED liquidation banner). The
  TLT sweep cleanly shows the ETF 25%-maintenance cliff: survives ≤3x, LIQUIDATED ≥5x;
  vol drag scales ~quadratically (0.5% → 31% from 1x → 8x). All green: full unit suite,
  `mypy --strict`, `ruff`.

### P3–P7 kickoff stubs

Each later phase starts the same way ("start P{N} from
`Docs/futures-backtester-design.md`"), scoped to that phase's files in §9 +
acceptance in §9/§10. P3 adds leverage / margin / liquidation + ruin metrics +
sizing schemes (and replaces the P1 drawdown's post-wipeout limitation with
liquidation-aware ruin metrics); P4 adds walk-forward / sweep / anti-overfitting
ranked on OOS; P5–P7 are DEFERRED (intraday ingest + sessions; tick; isolated
live paper-forward) pending the operator taking up the intraday-data decision.

---

## Trust-with-Money Charter (post-#333, 2026-06-08)

> **Status: the design's daily phases P1–P4 are MERGED + the V1 backtest-window
> param (#333) is merged — the daily backtester is FEATURE-COMPLETE.** This charter
> is the next-phase kickoff: the gaps between feature-complete and "trustworthy
> enough to base real-money decisions on its verdicts." It is ~4 PRs in order, each:
> build → real-engine validate → subagent review → address nits → update-branch → CI
> green → merge. Commit early (never work only in `/tmp`). Use a git worktree off
> latest `main`; do NOT disturb the main checkout. PAUSE at ⚠️ESCALATE points.

**DONE (context, do not redo):** P1 #319, P2 #320, P3 #322, v1_adapter #325,
authoritative `engine=lean` #330, P4 walk-forward/sweep #332, V1 backtest-window param
#333 (`a4c4aee`). **Verified:** `engine=lean` + `ref=v1_adapter` runs the REAL V1
(donchian + MA-200 trend + Kaufman ER gate) over any window — but V1 is **POST-ONLY
(places no LEAN orders)**, so the LEAN equity is FLAT: it captures DECISIONS, not P&L.
Reference strategies (donchian/buy_and_hold) via `research_runner` DO place orders +
produce P&L.

**Read first:** the memory note on this initiative (the arc + the POST-only finding +
the G1/G2/G3 continuous-futures order gotchas solved in
`research/lean/projects/research_runner.py`); §1 + §8 above (trust bridge — "the thing
you backtest must BE the thing that trades"); `CLAUDE.md` + dev-guide §1.5/§2.2. The
operator's box has the `trading-lean-local` image + a data snapshot at
`research/data/cache/lean_bars` → validate against the REAL engine (~30s–2min/run).

### PR A — Authoritative V1 P&L (HIGHEST VALUE: the real "how did V1 do over years") — ✅ DONE (#335, 2026-06-09)
Problem: #333 gave multi-year V1 DECISIONS but a flat equity curve (V1 POSTs, places
no LEAN orders). We need a real V1 equity curve / Sharpe / max-DD / ruin.
**APPROACH: DECIDED — Approach A (operator, 2026-06-08).** In `lean/v1_strategy.py`, in
BACKTEST mode ONLY (`not self.live_mode`), ALSO place LEAN market orders mirroring the
signals V1 already computes — entries sized by V1's vol-target → integer contracts on
the MAPPED front contract; exits per the existing exit pipeline — so LEAN computes
fills + margin + P&L. Reuse the G1/G2/G3 continuous-futures order/roll mechanics from
`research/lean/projects/research_runner.py`. This keeps the backtest IDENTICAL to the
real V1 (no fork, no parity drift) — the trust-bridge principle. (Approach B — porting
V1's logic into the runner — was REJECTED: it duplicates V1 and adds a parity-
maintenance burden.)
⚠️ **LIVE-FILE / GOVERNANCE:** this edits the live strategy. It MUST be provably
backtest-only (live mode places NO orders, behavior byte-for-byte unchanged), with a
live-safety unit test (mirror #333's `_parse_backtest_date` test + the
`test_lean_live_positions.py` `AlgorithmImports` stub) + subagent review + operator
sign-off before merge. `lean/` is lint-excluded — `py_compile` it + validate on the
real engine. Do NOT auto-merge a live-strategy change.
Acceptance: a real V1 backtest over 2023-09→2026-06 (existing micro data) produces a
NON-flat equity curve with trades, Sharpe, max-DD, and LEAN-native ruin surfacing;
prod live behavior provably unchanged (live places no orders) + a live-safety test.

**✅ DONE — PR #335 (`a10013c`), merged 2026-06-09.** Backtest-only LEAN orders
(`lean/v1_strategy.py::_place_backtest_orders`, master-gated `not self.live_mode`) sized
by the real Stage 0-5 sizer (`services/risk/sizing.py`, loaded by file-path in-container;
`services/` mounted read-only by `research/lean/driver.py`). Live path byte-for-byte
unchanged (POST-only, zero LEAN orders); live-safety unit test in
`tests/unit/test_v1_backtest_orders.py`. Real-engine acceptance 2023-09-01→2026-06-08
(isolated container — `--network none`, POST stub `http://127.0.0.1:9`, dummy bearer,
read-only data COPY `research/data/cache/lean_bars`): **1013 bars · 45 fills · 18 closed
trades · +4.10% total return · Sharpe −1.00 · max-DD 6.30% · 0 margin events · non-flat
equity $100k→$104,104 · realized vol 4.4%.**

#### PR A follow-up (post-#335, 2026-06-09): roll nits + vol diagnostic
Two roll nits and the vol-deployment question were chartered as PR-A follow-ups. After
measurement, **no live-file change was warranted** — both nits resolve to "inert" or "real
but unfixable in the roll handler," and the vol shortfall is not a sizing-cap problem.

- **Roll nit (a) — `invested_since` reset on roll: STRUCTURALLY INERT, no fix.** The roll
  re-opens the carried leg via `market_order`, restamping LEAN's `invested_since`. But the
  MIN_HOLDING_DAYS gate never reads it in backtest: `_snapshot_position` sources
  `opened_at_session_date` solely from `holding.invested_since`, absent in this LEAN build →
  always `None` → the gate (`strategies/v1_trend_following/strategy.py`) is SKIPPED. Verified:
  0 MIN_HOLDING rejections in the acceptance log; every trend exit held ≫14 days. Re-stamping
  a value the gate ignores is a no-op; editing the live file for it adds risk for zero effect.

- **Roll nit (b) — chain-edge exposure gap: REAL, but UNFIXABLE in the roll handler;
  root-caused to a price-source artifact.** #335's handler closes the expiring leg and
  re-opens the new front *only if it is priced* (`v1_roll_carried`, 4 of 13 rolls); else
  close-only (`v1_roll_close_only`, 9 of 13). A prototyped "park the carry + restore when the
  new front prices" fix produced a **byte-identical** curve (same 45 fills, same +4.10%) and
  was reverted. Reason: on the index micros the *mapped front contract's* `.price` reads **0
  for months** after a roll (`v1_backtest_order_skipped_zero_price`: /MNQ 631, /MYM 631, /MES
  540, /MGC 363 of 1013 bars), during which the continuous re-rolls — so there is no priced
  contract to carry onto and the parked key goes stale. The multi-month flats this causes
  (e.g. /MGC flat 2024-10-30→2025-02-10 while gold ran 2781→2934) are a **price-source
  artifact** (reading the mapped contract's `.price` instead of the continuous's), not a
  handler bug — no handler logic can order a contract that has no price. **Recommended
  follow-up (separate, properly-scoped PR):** source the backtest order path's price from the
  continuous future (or add explicit front-month subscriptions / revisit the roll config),
  then re-run acceptance — a material change to the live order path (moves the curve by
  percentage points; changes sizing inputs + turnover) that needs its OWN acceptance +
  risk-review, NOT a bundle with docs. **✅ DONE — see "PR A.2 — order-routing +
  position-state fix" below (explicit front subscriptions, market-level position state,
  record-then-consolidate rolls).**

- **Vol-deployment diagnostic — realized 4.4% vs `VOL_TARGET_PCT_ANNUAL` 15% (READ-ONLY;
  `services/risk/sizing.py` untouched).** The BINDING constraint is NOT a portfolio cap.
  From LEAN's own `Exposure` chart over the acceptance run:
  - **Time-in-market dominates:** 66% of the 1013 bars are completely FLAT (gross < 0.01).
    Concurrent open positions: median 1, mean 1.22, max 5 (0–1 positions two-thirds of the time).
  - **When deployed, gross is modest:** mean 0.45×, median 0.28× — vs the **3.0× gross cap
    (mean usage 0.15× = 5% of cap)** and **1.5× net cap** (grazed once, late, via an anomalous
    bond-ETF short reaching ~1.8× notional → flag for the PR B cost/fill review). The
    portfolio gross/net caps essentially **never bind**.
  - **The 25% per-position cap × micro granularity is the real per-name throttle:** all 19
    futures entries are ≥21% of $100k for a SINGLE contract (MNQ 49%, MGC 37–43%, MES 34–38%,
    MYM 26%), so the vol-target-implied size (~3–4 contracts for 15% vol) is clipped to 1 —
    integer-contract granularity even forces some single positions *over* 25%. At $100k the
    account is too small to hold the micro universe diversified under 25%/name, and the
    breakout + MA-200 + ER(0.20) gating (plus the price=0 skip artifact) rarely puts ≥3 names
    in-trend-and-tradeable at once → time-averaged gross stays ~0.15×.
  - **Arithmetic:** 15% × √(0.34 time-in-market) ≈ 8.7%, × (0.45× deployed gross / ~1.0×
    needed) ≈ **4.4%** — matches the realized figure. **Lever order (before ANY parameter
    tuning):** (1) fix the price=0 skip artifact (raises time-in-market — same root cause as
    nit b); (2) more simultaneous diversifying trends, or a larger account so micros fit under
    25%/name. Tuning vol-target or the caps UPWARD does nothing for the 66% of days that are flat.

#### PR A.2 — Order-routing + position-state fix (the nit (b) / price-source follow-up) — ✅ DONE (2026-06-10)

The complete fix for the price-source artifact, probe-driven against the real engine
(`lean/v1_strategy.py`, BACKTEST-ONLY, master-gated; live POST path byte-for-byte
unchanged — live-safety tests + source tripwires lock it):

- **Step-0 findings (probes against `trading-lean-local:latest` + the real bar copy):**
  (1) trading the canonical continuous is NOT supported in this LEAN build — the Engine
  DLL's order validation rejects it ("…continuous Futures contracts are not tradable"),
  empirically confirmed (`Invalid` ticket) → fallback (B), explicit front subscriptions.
  (2) The artifact reproduced A/B: control arms logged 295 + 184 mapped-price-zero cycles
  across two windows (/M2K 175 consecutive sessions, /MYM 9+ months); arms with an
  every-cycle `add_future_contract(mapped)` cure logged ZERO. (3) Orders placed at the
  SymbolChangedEvent moment are synchronously rejected Invalid (new front has no bar yet,
  NO order event emitted) — the old close+reopen-at-event roll handler therefore silently
  EVAPORATED carried positions on unpriced-front rolls; a recorded roll consolidated at
  the NEXT cycle (deferring until the front prices) fills cleanly. (4) Canonical and
  mapped prices are byte-equal under RAW when both present.
- **The fix (5 coupled changes):** explicit front subscription each cycle (fillability);
  MARKET-level position state — legs summed across contract months (kills the re-emit
  bug + revives the exit pipeline post-roll; mirrors live's `SUM(qty) GROUP BY market`);
  rolls recorded at the event + consolidated next cycle (no more evaporation); a
  pending-order-aware reconcile (calendar-day cycles no longer stack weekend duplicates
  of an unfilled delta — entries could triple, exits could overshoot through flat); a
  backtest entry-date tracker restoring the MIN_HOLDING_DAYS gate (LEAN holdings carry
  no `invested_since` in this build, so the 14-day live gate never engaged in backtest).
  Plus: the per-cycle decommission-flag GET is now live-gated (was logging a spurious
  `lean_parameters_fetch_failed` + flagging the heartbeat on every backtest cycle).
- **Acceptance (real engine, isolated, 2023-09-01 → 2026-06-08, 1013 bars):** **85 fills
  · 40 closed trades · +3.45% total ($100k→$103,455) · Sharpe 0.14 · realized vol 9.1% ·
  max-DD 11.59% · 0 margin events · no liquidation.** Mechanics: zero-price skips
  **2,165 → 56 market-days (−97%)**; rolls **18 recorded → 18 carried (0 lost)**;
  time-in-market **34% → 65%** of bars; ZERO same-market entry re-emits (the /MGC
  Sept-2024 cluster now: enter 09-12 → hold through the 10-30 roll → exit 11-11 on
  signal); weekend-stack suppression engaged 4×; MIN_HOLDING now rejects (1 occurrence).
- **Reading the delta vs #335's +4.10%:** the baseline's higher headline was an artifact
  of NON-execution — the futures sleeve was untradeable on ~60% of market-days, so the
  book sat in a few low-vol positions (4.4% vol, Sharpe −1.00). With routing fixed, the
  SAME signals actually deploy and the curve shows what V1's rules produce: ~1.2%/yr at
  9.1% vol with an 11.6% drawdown (Sharpe 0.14). The drop is not value destroyed by the
  fix; it is the difference between a curve that couldn't execute and one that does.
- **Residual vol gap (9.1% vs 15% target):** (a) the 25%/name cap × integer micro
  granularity still clips most positions to 1 contract; (b) ~35% of bars remain flat
  (breakout + MA-200 + ER gating); (c) the first ~9 months of the window have
  structurally degraded futures data — **all six markets' map_files carry NO roll rows
  before mid-2024** (first rows 2024-06-23 index micros / 2024-06-30 MBT / 2024-08-29
  MGC; /MYM worst: pinned to its June-2024 contract from 2023-09), so early deployment
  is thin. That horizon is a `services/data` map_file-synthesis backfill follow-up (data
  fix, NOT a strategy fix — same family as the #326 live-edge work).
- **Known divergences now documented in-file (operator charter questions, NOT bundled):**
  the backtest places NO stop orders while live brackets every entry at 3-ATR (max-DD /
  ruin exclude the primary live loss-cutter — chartered as a follow-up decision); the
  curve models Stage 0-5 sizing while live dispatch places `target_contracts=1` per
  approved signal (docstrings corrected; wiring Stage 0-5 into live dispatch is a
  separate charter question); `single_contract_overrides` / `m_combined` defaults are
  assumed (a live-scale $15–25k run would Stage-0-drop every micro — documented).

### PR B — Cost / fill fidelity (every P&L number — incl. V1's new one — depends on it)
Confirm LEAN's IB-margin commission + slippage match real IBKR micro-futures costs
(≈ $0.25–0.85/contract incl. fees; slippage ≈ ~1 tick) for `research_runner` AND V1.
If off, set an explicit documented fee/slippage model; surface the assumed cost/
contract + slippage in the report header; unit-test the per-contract commission.
Acceptance: a backtest's reported commission/round-trip matches IBKR micro reality
within a stated tolerance; slippage assumption reported, not hidden.

#### PR B — Cost / fill fidelity — ✅ DONE (2026-06-11)

Probe-driven like PR A.2 (engine over docs), then the smallest correct change:

- **Probe findings (real engine, `trading-lean-local:latest`, per-order fees read from
  the order-events JSON of the 85-fill acceptance run + targeted runner probes):**
  (1) **LEAN's bundled `InteractiveBrokersFeeModel` is stale**: it charges
  **$0.57/contract/side for ALL CME/COMEX micros** (incl. MGC) and **$4.77 for MBT**.
  Reality (IBKR fixed, non-member, as of 2026-06: $0.25 commission + exchange + $0.02
  NFA; crypto micros carry a $2.25 IBKR commission): index micros **$0.62**, MGC
  **$1.37** (exchange $1.10 — LEAN ~58% under), MBT **$3.42** (exchange $1.15 — LEAN
  ~39% OVER; its $2.50 exchange fee is the 2021 schedule). ETFs: LEAN charges exactly
  IBKR fixed ($0.005/share, $1.00 min — IEF 280 sh = $1.40 ✓) — kept as-is.
  (2) **Fill conventions pinned empirically:** a 17:30 ET scheduled FUTURES market
  order fills SAME-instant at that session's close — the close of the very bar the
  signal was computed from (MGC order 2024-04-01 17:30 → fill 2298.1 == that bar's
  close), zero inherent slippage. An ETF order pends overnight and fills at the NEXT
  session's official open (TLT 83.70 / IEF 93.62 / SHY 82.04 — each exactly the next
  day's open). The §6.6 parity story's next-open assumption holds for equities.
  (3) A model set on the CANONICAL future never touches a fill (fills land on mapped
  per-expiry contract securities) — the runner's old `costs: zero` path had exactly
  that gap on futures (fee model applied to a security that never fills).
- **Build:** explicit cost tables in `research/data/contract_specs.py` (the canonical:
  `commission_per_side`, `slippage_ticks`, ETF per-share constants, `FILL_CONVENTION`,
  as-of + ±$0.10/side stated tolerance) mirrored inline into BOTH LEAN-side algorithms
  (they cannot import `research/` in-container; an AST unit test pins all three tables
  in sync). `research_runner` (`COSTS_MODEL=ibkr`, the default) and the V1 backtest
  order path (`_apply_backtest_cost_models`, master-gated, applied once per traded
  contract at the two gated subscription sites) now set a per-contract fee model +
  **1-tick adverse slippage** on every traded futures contract. ETFs keep the bundled
  (accurate) model with zero slippage — fills at next-open are an achievable auction
  print; the choice is explicit, not hidden. `costs: zero` now zeroes mapped-contract
  fills too. Custom-model interface (snake_case `get_order_fee` /
  `get_slippage_approximation`) probe-proven before touching the live file.
- **Live safety:** live mode is byte-for-byte unchanged — the model classes are built
  lazily inside the gate (never at import), both application sites are master-gated,
  and the source tripwires now also pin `set_fee_model`/`set_slippage_model` (both
  casings) to the single gated helper. Live-safety suite green.
- **Surfacing:** every report (md/html/result.json) now carries a cost-model header —
  per-market commission, slippage, fill conventions, as-of + tolerance — rendered from
  `contract_specs.py` so it cannot drift; `result.json` gets a machine-readable
  `cost_model` block. Unit tests cover the commission math (futures per-contract, ETF
  min-per-order), the table sync, and the header presence.
- **New authoritative acceptance (real engine, isolated, 2023-09-01 → 2026-06-08,
  1013 bars):** **85 fills · 40 closed trades · +3.42% total ($100k→$103,420.76) ·
  Sharpe 0.14 · realized vol 9.1% · max-DD 11.61% · 0 margin events · total fees
  $223.03.** Delta vs PR A.2's +3.45%/$103,454.68: **−$33.92 (−0.03pp), entirely
  cost-model mechanics** — commissions DOWN $42.01 ($265.04→$223.03; removing MBT's
  39% overcharge outweighs MGC's correction up) while 1-tick slippage costs ≈$76
  across ~118 futures contract-sides (the ≈ also absorbs the 1-share TLT resize). Decisions are untouched: 84 of 85 fills are
  byte-identical (same symbol/timestamp/side/quantity); the single difference is the
  TLT short sizing 314→313 shares (Stage 0-5 reads equity, which now carries the
  slippage drag). Per-market census: index micros exactly $0.62/contract·side, MGC
  $1.37, MBT $3.42, ETFs $0.005/share — reported commission matches the stated IBKR
  reference within the stated tolerance by construction, verified per-fill.

### PR C — Deep multi-year PARENTS history (extends V1 P&L + powers walk-forward)
Walk-forward + a multi-decade V1 backtest need years across regimes (2008/2020/2022);
the snapshot is ~3yr micros / ~1yr ETFs. Decision made: PARENTS (ES/NQ/RTY/YM/GC/BTC,
decades) for the long lookback, micros for the recent live-aligned period.
Build: parent↔micro map in `research/data/contract_specs.py`; source + ingest deep
DAILY parent bars into the on-disk cache (reuse `services/data/map_file_synthesis.py`
for rolls/map_files; offline, idempotent, cached); validate coverage + probe a price.
⚠️ESCALATE the data SOURCE + cost (candidates: `lean data download` from the QC
dataset; a small Databento daily backfill; or LEAN free sample if deep enough) and
WAIT for operator go before purchasing.
Acceptance: a walk-forward (or V1 backtest) on a parent spans ≥ 10yr with several OOS
folds crossing 2020 + 2022; the multiple-testing null is over a real sample.

#### PR C — ⚠️ ESCALATED 2026-06-11 — data source needs an operator decision (no $0 path covers it)

**$0-source investigation (charter step 1) — all verified empty for the full-fidelity
need:**

- **LEAN's bundled sample data is a demo stub:** the image's `future/cme/daily/
  es_trade.zip` holds 8 contract files / 75 TOTAL daily bar-lines (a few days each);
  COMEX `gc` = 54 lines. The bundled `es.csv` map_file spans decades, but the BARS
  behind it do not exist. The VPS snapshot carries the same stubs.
- **`lean data download` free tier:** QC Cloud gives free US-futures access only
  INSIDE their cloud (tick→minute, for cloud backtests). Local LEAN-format downloads
  are paid (QCC) — pricing below. Running on QC Cloud would re-enter the architecture
  the 2026-05-12 pivot retired.
- **Free continuous series (probed):** Stooq's futures endpoint is gone (404).
  Yahoo's public chart API serves full daily history for `ES=F`-style continuous
  symbols (probed: ES first-trade 2000-09-18; 2008 slice complete, 505/505 bars) —
  ES/NQ/YM/GC reach 2000-2002; RTY only 2017+, BTC 2018+ (contracts younger).
  **Fidelity limits:** UNADJUSTED front-month chains (roll gaps sit INSIDE the
  series → false breakouts for exactly our strategy class; severe for GC contango
  ~0.5-1.5%/roll and BTC 5-15%/quarter), no per-expiry contracts → LEAN cannot run
  them as real futures (no rolls/margin/OI), numpy-screen walk-forward only;
  unofficial API, ToS-gray.
- **IBKR (the operator's own account):** historical data for EXPIRED futures is
  ~2 years per IBKR docs; a read-only CONTFUT depth probe via the existing gateway
  (clientId 98) timed out in the nightly-reset window 2026-06-11 ~01:20 ET — depth
  UNVERIFIED; a 10-minute retry ceremony is available on request, but even a deep
  CONTFUT series would be a continuous chain with the same fidelity limits as Yahoo.

**The decision menu (operator picks; NO purchase/signup made):**

| Option | Cost | Coverage | Fidelity | Notes |
|---|---|---|---|---|
| **A. QC AlgoSeek US Futures daily files** | **~$60–90 one-time** (500 QCC ≈ $5 per daily file; 6 roots × trade+OI ≈ 12–18 files; QCC sold in $10 units) | May 2009 → present (16+ yr; covers 2020+2022, NOT 2008) | **Full**: native LEAN per-expiry zips + OI — drops straight into the research cache; map_files via the existing synthesis helpers | Operator already has a QC account. Cleanest path; LEAN-as-futures works (margin/rolls/ruin) |
| **B. Databento GLBX daily backfill** | ~$0 net (new-account $125 free credit ≫ a daily-OHLCV pull) but **requires a new vendor account** | 2010-06 → present (covers 2020+2022, NOT 2008) | Full per-instrument OHLCV+definitions, but needs a research-side converter (CSV → LEAN zips + maps; ~a day of build) | The design already names Databento for the (deferred) intraday phase — this would front-load that vendor decision |
| **C. Yahoo continuous (free, no account)** | $0 | 2000 → present for ES/NQ/YM/GC (incl. 2008); RTY 2017+, BTC 2018+ | **Degraded**: unadjusted continuous, numpy-screen walk-forward ONLY, never LEAN-authoritative; ToS-gray | Could power an indicative P4 walk-forward with loud caveats; cannot support trust-with-money verdicts |
| **D. IBKR CONTFUT probe first** | $0 | unknown (probe pending) | continuous-chain limits (as C) even if deep | 10-min read-only ceremony; answers "is there a free deep chain in-house" before any spend |
| **E. Defer PR C** | $0 | — | — | Walk-forward stays on ~3yr micros (single regime); revisit when intraday (P5) forces the vendor question anyway |

**Recommendation (for the operator to accept or override):** Option A — bounded
one-time ~$60–90, native format, full LEAN fidelity, no new vendor relationship,
16 years crossing 2020 + 2022 (2008 is unreachable at reasonable cost in ANY
per-expiry source surveyed; AlgoSeek starts 2009-05, Databento 2010-06). Optionally
precede with D (free, 10 minutes) to close the "was there a free path" question.

**Build plan once unblocked (pre-scoped):** parent↔micro map + parent ContractSpecs
in `research/data/contract_specs.py` (⚠️ PR B's runner now FAILS LOUDLY on futures
tickers missing from the cost tables — parents must extend them; verify per-product
all-in commissions, e.g. ES ≈ $0.25+$1.40+$0.02 class); ingest into
`research/data/cache/lean_bars` (idempotent, offline); validate coverage + probe
prices; ≥10yr walk-forward with OOS folds crossing 2020 + 2022; the multiple-testing
null over the real sample.

### PR D — Quantify + tighten the V1↔live trust bridge (now that V1 runs multi-year)
Re-run `reproduce_v1` over a single-param-hash aligned window; MEASURE the decision
match rate + DOCUMENT the residual & why (POST-not-orders, no position feedback in
backtest, per-signal param hash). OPTIONALLY improve via POST-body capture (capture
V1's POSTed decisions incl. short side) — only if clean; else document the bound.
Acceptance: the V1 reproduction match rate has a stated error bar.

#### PR D — Trust bridge MEASURED — ✅ DONE (2026-06-11)

Real-engine run (isolated, post-#337 routing + post-#339 costs) over
2026-05-01→2026-06-08, cross-checked against a fresh prod oracle capture
(`signals` table 2026-06-11: 19 entry rows → 10 unique decisions, all long,
2026-05-13→2026-06-04). The committed fixtures are now these REAL artifacts and the
golden test (`tests/golden/test_research_v1_repro.py`) pins every number below.

- **Headline (stated error bars are exact binomial 95% CIs; DATE+MARKET and
  SIDE-VERIFIED stated separately — the LEAN log carries no direction, so the
  parser labels every backtest entry `long`; side evidence comes from the signed
  `v1_backtest_order_placed target=±N` lines, golden-pinned):**
  - **Market-level — did each market live flagged get flagged by the backtest?
    Date+market 4/5 = 80%, CI [0.28, 0.99]** — of those 4: /MES + /MYM
    side-VERIFIED long, **TLT side-FLIPPED** (backtest shorted −299 where live was
    long), /MNQ side-unverifiable (never sized). The single date+market miss
    (/M2K) is the ER-gate regime flip, not drift (below).
  - Strict decision-level, full span: date+market **4/10 = 40%, CI [0.12, 0.74]**;
    **side-verified 2/10 = 20%, CI [0.03, 0.56]** (the two TLT matches are
    opposite-side). Stabilization window (05-26→06-08): 2/3, both side-verified.
    **ER-aligned regime window (06-02→06-08, gate live BOTH sides): 1/1
    side-verified, CI [0.02, 1.00]** (one backtest extra in-window: /MNQ 06-02).
  - The side-verified matches are the decision that became a real paper position
    (/MES 05-28, target=+1) and the only post-ER-regime entry (/MYM 06-04,
    target=+1).
- **Residual: ZERO unexplained decisions after attribution.** Four causes — the
  backtest-side rejection/sizing evidence is verified in the committed fixture
  log; the data-revision explanation in (3) is the inference those lines plus the
  TLT side flip support:
  1. live dormant anti-pyramiding re-emission pre-#312 (9/19 oracle rows are dups,
     incl. multiple same-day cycle invocations — 3×TLT on 05-17; backtest is
     position-aware → `position_already_same_direction`);
  2. ER-gate regime flip at 2026-06-02 (backtest applies the current gate to
     pre-boundary dates: /M2K 05-26 `efficiency_below_threshold` — live emitted it
     gateless and the real paper account opened /M2K 05-27);
  3. bar data revised since live decided (daily bar_sync overwrite + #326 map
     re-synthesis): /MES 05-18/19 + /MNQ 05-13/18 read `no_breakout` today; the
     same revision explains the IEF/SHY/TLT-05-15 extras and **the TLT side flip
     itself** (the bars live read on 05-16 produced a LONG signal; today's bars
     produce a SHORT one);
  4. sizing-to-zero re-emission (`v1_backtest_sizing_empty` on EVERY /MNQ emit
     date — both the 05-06→05-14 and the 05-26→06-02 extras: 25%/name cap clips a
     ≈$43k /MNQ contract to 0 at $100k → flat → re-emits; live /MNQ never filled
     either).
- **Key discovery — prod stamps a DISTINCT `parameter_set_hash` on EVERY signal**
  (19 rows, 19 hashes, golden-pinned), so the charter's "single-param-hash window"
  is not a usable filter. Regime windows are cut by DATE; the live boundary is the
  ER gate landing 2026-06-02. `reproduce_v1` + the integration test + README §3
  now say so.
- **POST-body capture (the optional short-side improvement) was NOT taken:** it
  requires either live-file changes or api-side capture — out of research-side
  scope. The bound is stated instead: direction is structurally unobserved by the
  log parser; the oracle is all-long as of capture (golden-pinned) so the ORACLE
  side is unambiguous, but the BACKTEST side is only verifiable where the order
  path sized the market (signed order lines) — POST-body capture is the complete
  fix and the first live short entry will force it.
- **Why the error bars are wide:** clean paper history is ~3 weeks of entries
  (n=5 markets / 10 unique decisions). The ceremony is repeatable (README §3);
  each additional paper entry tightens the bound. The structural causes (1)+(2)
  age out on their own — both ended by 2026-06-02 — so the ER-aligned window is
  the one that grows.

### PR E — Exercise the graduation pipeline once, end-to-end (proves governance, D6)
The "research feeds governance" bridge (D6) has never run. Prove it with a
DELIBERATELY SAFE candidate (re-affirm current params or a tiny justified tweak):
research report + LEAN backtest-delta artifact → `strategies/**` PR → full cycle.
⚠️ GOVERNANCE: `strategies/**` REQUIRES the `risk-review-approved` label (operator-
applied, never self-applied) + ultrareview; live strategy code → operator sign-off
mandatory. Write the repeatable graduation runbook as you go.
Acceptance: one change has traversed research → PR (w/ backtest delta) →
risk-review-approved → merge; the graduation runbook is documented.

**When A–E land:** the backtester can show an authoritative MULTI-YEAR, MULTI-REGIME
P&L for the real strategy with honest costs + ruin, a quantified trust bridge, and a
proven path to production — trustworthy enough to risk money on its verdicts. (Intraday
P5–P7 stays DEFERRED; the live-money cutover itself follows
`Docs/live-money-cutover-plan.md` and is a separate operator decision.)
