# Futures Backtester — Kickoff Prompt

**Purpose:** a self-contained prompt to seed a fresh Claude Code session that will *design and build*
a futures strategy backtesting + research system for this repo. Paste the fenced block below as the
first message of a new session (in this repo). It will read the right docs, think through the
considerations, and produce a design doc at `Docs/futures-backtester-design.md` for operator review
before any code is written.

**How we got here (decisions already settled in conversation — the prompt encodes these so the next
session won't re-litigate them):**

- **Futures-only.** "Any IBKR security" was considered and rejected — the comprehensive *engine*
  already exists (LEAN), the real cost of breadth is data + validation + ops, and futures alone is a
  deep, profitable space with no survivorship-bias or corporate-action minefield. Equities / options /
  FX are explicitly out of scope.
- **All resolutions + live, no artificial ceiling.** Not daily-only — daily → minute → tick, plus
  live / paper-forward on real-time data.
- **LEAN = engine of record** (backtest *and* live, every resolution, authoritative). It natively runs
  the same strategy code in backtest and live and supports tick→daily, so it *is* "no resolution
  ceiling + seamless backtest→live." **vectorbt is demoted to an optional fast daily screen** for idea
  triage. This keeps the locked "LEAN authoritative / vectorbt research-only" rule intact and avoids a
  second engine that could disagree with the live system.
- **Leverage is first-class.** The operator wants leverage for outsized returns (and accepts the
  symmetric downside). The system must model intrabar margin / liquidation and surface risk-of-ruin —
  show the full distribution of outcomes including blow-up, not flatter a strategy.
- **Governed.** New code lives under a top-level `research/` dir, touches no forbidden path, and a
  winning idea graduates via a `strategies/` PR with LEAN backtest delta + `risk-review-approved`.

---

````text
# GOAL
Design and build a fast, trustworthy FUTURES strategy backtesting + research SYSTEM with no ARTIFICIAL
ceiling: ALL resolutions (daily -> minute -> tick), realistic LEVERAGE / margin / liquidation, and a
unified path from historical backtest to LIVE / real-time data using the SAME strategy code. It must
let a solo, non-coding operator test many futures strategies quickly, then graduate winners into the
live system through existing governance.

# KEY ARCHITECTURAL DECISION (this is the consequence of "minute + tick + live + no ceiling")
- LEAN is the ENGINE OF RECORD for backtest AND live, across ALL resolutions. LEAN natively supports
  tick/second/minute/hour/daily and runs the IDENTICAL QCAlgorithm in backtest and live mode — that IS
  "no resolution ceiling + same code backtest->live." It also has the margin/leverage/fill models that
  make leveraged intraday realistic. (Production v1_strategy.py already runs in LEAN backtest + live.)
- vectorbt is an OPTIONAL FAST SCREEN for daily / low-frequency idea triage only (vectorized = seconds
  for big sweeps, but NOT the tool for intraday / live / path-dependent leverage). This keeps the
  locked "LEAN authoritative, vectorbt research-only" rule intact.
- Net: build a research / iteration / leverage-modeling / reporting LAYER that DRIVES LEAN (and
  optionally vectorbt for daily screening). Do NOT build a from-scratch engine.

# HONEST LIMITS — design WITHIN these; there is no "zero-limit" system
- SPEED vs RESOLUTION: event-driven minute/tick backtests are inherently far slower than daily
  vectorized sweeps. Mitigate by TIERING: fast daily screen (vbt) -> minute/tick confirm (LEAN). You
  cannot sweep 10k combos at tick resolution in seconds.
- INTRADAY DATA DEPTH & COST: IBKR historical intraday is pacing-limited and shallow — NOT a research
  archive. Deep minute/tick CME history needs a vendor (e.g. Databento for CME futures) = recurring
  cost + ingestion pipeline + storage (minute ~= ~1000x daily volume). NOTE: the existing "no
  DataBento" rule governed the LIVE data path; an intraday RESEARCH archive is a SEPARATE sourcing
  decision — surface it for operator sign-off.
- INTRABAR FILL REALISM: a minute bar doesn't reveal the path within the bar; leveraged stop /
  liquidation outcomes depend on fill assumptions. Tick data reduces but never eliminates this. Model
  it explicitly (LEAN fill models) and REPORT residual uncertainty.
- OVERFITTING: higher frequency = more data points, more params, more microstructure noise = more ways
  to fool yourself. Anti-overfitting discipline matters MORE intraday, not less.
- LIVE-DATA INTERFERENCE: candidate strategies run on live data must NOT touch production. Use an
  ISOLATED LEAN instance + separate paper account + clientId 80-99 (never 1/3/10).

# LOCKED CONTEXT — read to confirm, then DO NOT re-litigate
- CLAUDE.md, Docs/recent-architecture-changes.md
- Docs/claude-dev-guide.md §1.5 (LEAN authoritative; vectorbt research-only) and §6.6 (vbt-vs-LEAN
  parity: <=5bps/trade slippage, <=0.5% cumulative P&L, trade count within 5%)
- Data + continuous-contract handling: services/data/bar_sync.py, services/data/map_file_synthesis.py,
  lean/v1_strategy.py (add_future block + resolution config), lean/README.md, lean/lean.json (envs)
- Strategy package: strategies/v1_trend_following/ and .../parameters.py (universe + specs)

Settled decisions (encode; don't reopen):
1. SCOPE = CME micro futures only (active V1 universe per parameters.py::V1_CANDIDATE_UNIVERSE; /MCL +
   sidelined names excluded). ALL resolutions in scope (daily, minute, tick where data exists). Single
   + multi-contract portfolios. Historical backtest AND live / paper-forward on real-time data. No
   equities / options / FX.
2. ENGINES: LEAN = engine of record (backtest + live, all resolutions, authoritative). vectorbt =
   optional fast daily / low-freq screen (research-only). New code under a top-level research/ dir.
3. DATA: daily reuses the api's existing on-disk LEAN-format bars. Minute/tick needs an ingestion path
   that writes LEAN's intraday on-disk format from a research vendor (propose options; flag the
   DataBento note). Offline-by-default; live-mode data via the existing IBKR path but in an ISOLATED
   instance (clientId 80-99, separate paper account) so production is untouched.
4. GOVERNANCE: touch NO forbidden path (services/risk|signal|audit|execution|reconciliation|
   calibration, services/agent/*, alembic). A winning idea graduates via a strategies/ PR with LEAN
   backtest delta + `risk-review-approved` label. The harness FEEDS governance; never bypasses it.
5. HOUSE RULES: structlog (no print / stdlib logging). Propose an EXPLICIT, documented exemption from
   the production Decimal-for-money rule for the research analytics engine (numpy/vbt are float) and
   flag it for sign-off — never silently violate it.

# PART 1 — WHAT TO CONSIDER (considerations & landmines)

Futures data correctness (the #1 source of silent, fatal bugs):
- Continuous-contract construction & rolls. Document price treatment (RAW per-expiry vs back-/ratio-
  adjusted for signals); MATCH LEAN (OPEN_INTEREST mapping, RAW normalization, contract_depth_offset=0)
  so parity holds. Respect roll cycles + the existing roll-collapse logic in map_file_synthesis.
- INTRADAY-specific: trading session handling (RTH vs ETH / overnight), timezone correctness (CME is
  CT; bars must be unambiguous UTC), session breaks, half-days, and how daily settlement relates to the
  minute stream. Mismatches here silently fabricate returns.
- Contract reference data per symbol: multiplier/point value (/MES $5, /MNQ $2, /M2K $5, /MGC $10/oz,
  /MBT 0.1 BTC), tick size, currency, initial & maintenance margin, calendar, expiry schedule.
- Look-ahead avoidance: signal on bar t fills at close[t] or open[t+1]; enforce the shift, default to
  next bar's open. At minute resolution be especially strict about not using bar t's close to act in bar t.

Leverage & margin (operator WANTS leverage for outsized returns — model it honestly):
- Size in integer CONTRACTS; notional = contracts x multiplier x price. Track LEVERAGE = gross notional
  / equity as a first-class, tunable dial the operator can sweep.
- Model initial & maintenance margin, available margin, and MARGIN CALLS / FORCED LIQUIDATION when
  equity < maintenance margin — INTRABAR at minute/tick resolution. A backtest without liquidation
  modeling lets a strategy "ride through" drawdowns that would have wiped the account out; that is the
  most dangerous way to be wrong about leverage. Liquidation MUST be simulated (LEAN models this).
- Sizing schemes: fixed contracts, fixed-fractional, vol-targeting, ATR-based (as V1), risk-parity
  across contracts — all under a hard LEVERAGE CAP.

Risk realism under leverage (surface ruin, don't hide it):
- Report CAGR, ann vol, Sharpe, Sortino, MAX DRAWDOWN, Calmar/MAR, VOLATILITY DRAG (geometric vs
  arithmetic), RISK OF RUIN / P(drawdown > X%), time-to-recovery, worst day/week, downside deviation,
  CVaR/tail, MARGIN-CALL FREQUENCY, peak leverage used. Show Kelly / fractional-Kelly context.
- The deliverable must let the operator SEE the leverage level at which the strategy would have been
  liquidated, and how often. Model the live risk engine's position limits so findings stay actionable.

Anti-overfitting / validity (a backtester that lies is worse than none):
- In-sample/out-of-sample split; WALK-FORWARD; report sweeps on OOS not IS; parameter-sensitivity;
  multiple-testing awareness (deflated-Sharpe / reality-check thinking — critical at high frequency);
  regime analysis. Benchmarks: buy-and-hold per contract + the live V1.

Trust bridge to live:
- vbt<->LEAN parity (§6.6) for daily; reproduce V1 (which LEAN already backtests) as the first
  end-to-end validation. Backtest->live uses the SAME LEAN code path, so "trust the backtest" reduces
  to "trust LEAN + the data" — keep data validation front and center.

# PART 2 — WHAT TO BUILD (architecture & phases)

Proposed structure (new research/ package; adjust with rationale if better):
- research/data/     — daily loader (reuse on-disk LEAN bars) + INTRADAY ingestion (vendor -> LEAN
                       intraday on-disk format) + contract-spec reference data + parquet caching +
                       session/timezone handling. Offline-by-default; IBKR only on clientId 80-99.
- research/strategy/ — a RESOLUTION-AGNOSTIC strategy contract; reference strategies: buy-and-hold,
                       time-series momentum, Donchian breakout, V1 adapter.
- research/lean/     — a LEAN DRIVER: programmatically launch/parametrize LEAN backtests (any
                       resolution) + live/paper-forward runs, collect results, normalize for comparison.
- research/screen/   — optional vectorbt fast daily screen for idea triage.
- research/risk/     — sizing schemes, intrabar margin/liquidation model, risk-metrics module.
- research/eval/     — stats, walk-forward, sweep, comparison, and an OPERATOR-LEGIBLE report (equity
                       curve, drawdown, LEVERAGE-over-time, MARGIN usage, ruin metrics, vs benchmark)
                       written to research/runs/<ts>/.
- tests/integration/test_vbt_lean_parity.py — the parity rail.
- OPERATOR INTERFACE: config-driven run (universe, dates, RESOLUTION, strategy, params, sizing/leverage,
  costs, backtest-vs-live) -> report artifact. The operator never edits engine code.

Phased delivery (each phase independently useful; one PR each):
- P1 Daily spine:     data loader + contract specs + strategy contract + buy-and-hold + report skeleton
                      + optional vbt fast screen. Sanity: reproduces buy-and-hold exactly.
- P2 LEAN driver:     programmatic LEAN backtests at daily + results capture + §6.6 parity + reproduce V1.
- P3 Leverage:        intrabar margin/liquidation model + risk-of-ruin metrics + sizing schemes.
- P4 Validity:        walk-forward + sweep + anti-overfitting + comparison reports (ranked on OOS).
- P5 INTRADAY:        minute-bar vendor ingestion -> LEAN format + minute backtests + intrabar fill
                      realism + session/timezone correctness.
- P6 TICK (if data):  tick-resolution backtests where vendor data supports it.
- P7 LIVE:            paper-forward — run a candidate in LEAN live-mode on real-time data in an ISOLATED
                      instance (separate paper account, clientId 80-99), no production interference.

# OUTPUT / PROCESS
- First produce a design doc at Docs/futures-backtester-design.md in the house style of existing
  Docs/*-design.md (motivation -> proposed decisions -> scope -> architecture -> futures-specific +
  intraday hard parts -> leverage/margin/liquidation/risk model -> anti-overfitting -> phase/PR
  breakdown -> test plan -> risks+mitigations -> out-of-scope -> files-touched -> sign-off + per-PR
  kickoff prompts).
- Make sensible DEFAULT decisions, presented as PROPOSED (ratify in sign-off); the operator prefers
  reviewing a doc over answering many live questions. Escalate ONLY genuinely consequential or
  strategy/risk-ambiguous choices (the intraday data vendor + cost is one such; the LEAN-as-engine
  shift is another — confirm both).
- Be brutally honest about leverage AND about the resolution/speed/data tradeoffs: the system's job is
  to show the FULL distribution of outcomes — including ruin — not to flatter a strategy.
- Wait for sign-off before building; then implement P1->P7 as separate PRs, tests passing before each.
````
