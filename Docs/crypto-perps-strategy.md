# Leveraged Crypto Derivatives Strategy — Coinbase CFM US Perpetual-Style Futures

> **Provenance:** Produced by a deep-research session on 2026-07-08 from the operator's strategy-research prompt; delivered verbatim to this repo for validation. Backtest validation lives in `research/crypto_perps/` (see `REPORT.md` there). Status: **VALIDATED (all §9 falsification criteria pass) — approved to proceed to delta spec + build, with Amendment A below.**
>
> **⚠️ AMENDMENT A (2026-07-08, operator-directed, post-backtest) — production risk profile is 2× this document's base profile.** The operator (full-risk-capital mandate, explicitly accepting total loss) directed doubling the risk profile after reviewing base-profile backtest results. Every §6/§7 risk knob scales coherently; the body text below states the BASE values — production uses:
> - `V_target_base`: 40% → **80%** annualized
> - Per-trade risk cap: 2.5% → **5% of E**
> - Daily loss limit: −4% → **−8%** · Weekly loss limit: −8% → **−16%**
> - Drawdown tiers: **0–20% → 1.0; 20–40% → 0.6; 40–70% → 0.35; >70% → 0.2**
> - Portfolio gross cap: 2.0x → **3.0x** (the §7 30%-liquidation-buffer rule still applies and effectively binds near ~2.8x; verify against live overnight margin, §11-1)
> - **Hard halt: $3,000 (50%) → $1,500 (25% of initial equity).** The operator asked to remove the halt entirely; it is retained at −75% strictly as a **malfunction circuit-breaker** (broken feed/venue change/code fault) and debit-risk backstop, NOT a risk-tolerance bound — backtest shows it never fires in 9.5 years at the 2× profile, so it costs nothing. Restart still requires manual flag removal.
> - §10 small-live gates unchanged (Phase A still sizes off `E_effective = min(equity, $1,500)`).
>
> Validated profile (2017→2026H1, see `research/crypto_perps/REPORT.md`): **+31.1% CAGR, Sharpe 1.02, max DD 51.3%**, worst year −7.1% (2021), no halt/bust; 2×-cost stress +23.2% CAGR / Sharpe 0.82. Expect ~40% average gross leverage (peaks ~1.4x), roughly double the base profile's cost and funding drag.
>
> **⚠️ AMENDMENT B (2026-07-08, operator-directed) — final production profile = Amendment A plus four structural changes** (validated as "combo2x + cash + band-edge", see REPORT.md exploration sections):
> 1. **Hysteresis-hold:** during an unconfirmed direction flip (§4 hysteresis), HOLD the current position instead of going flat; act only on confirmation. (Removes a full exit+re-entry cost per one-day trend wiggle.)
> 2. **Drawdown tiers REMOVED** (`drawdown_mult ≡ 1.0`). Daily/weekly loss limits, the §7 per-trade risk cap, and the halt remain. Accepted trade-off: dead-regime bleeds run deeper in exchange for full size when trends resume.
> 3. **Band-edge rebalancing:** same-direction rebalances trade only to the nearest edge of the §5 dead-band, not to the exact target. (CAGR-neutral at modeled costs; lifts 2×-cost Sharpe 0.90→1.00 — cost insurance for a thin venue.)
> 4. **Cash-yield layer (build requirement):** equity not required as futures margin is swept to yield (~4% assumed; verify the actually-accessible instrument and rate at implementation — CFM margin is USD; keep swept cash reclaimable same-day for margin calls). Modeled on cash beyond a 25%-of-gross margin buffer.
>
> Validated (2017→2026H1): **+41.9% CAGR, Sharpe 1.16, max DD 58.5%** (at 2.5% yield: +40.1%/1.12); 2×-trading-cost stress: +34.4% / Sharpe 1.00. The 3× alternative (+~48% CAGR, Sharpe ~1.05, DD ~66%) was evaluated and REJECTED: lower Sharpe, and only ~9 pts of drawdown headroom against the operator's 75% budget vs ~16 pts here. Scale-up beyond this profile is a **post-live decision** (≥6 months of live data), not a launch parameter.

**Prepared:** July 8, 2026 · **Capital:** $6,000 USD · **Venue:** Coinbase Financial Markets (CFM) via Coinbase Derivatives Exchange (CDE), traded through the Coinbase Advanced Trade API
**Status:** Implementation spec for a coding agent. Every rule is stated exactly; anything unverifiable from public sources is logged in §11.
**Note:** This is a research/engineering specification, not investment advice from a licensed advisor. The design assumes full loss of the $6,000 is tolerable, per the operator's constraints.

---

## 1. Venue verification findings

All claims below were verified against sources current as of July 2026. Anything marked ⚠️ must be re-confirmed against live API responses before go-live (see §11).

**1.1 Product structure.** US "Perpetual-Style Futures" are NOT true perps. They are long-dated futures (5-year expiries, e.g., Dec 2030) listed on Coinbase Derivatives Exchange (a CFTC-designated contract market), carried by CFM (a registered FCM/NFA member), with an hourly funding-rate mechanism that anchors price to spot. Margin and settlement are in **USD**, not USDC. Spot balances sit at Coinbase Inc. (CBI); futures margin sits at CFM — two separate legal entities, with automatic USD sweeps from CBI→CFM to meet margin. (Sources: coinbase.com/blog/perpetual-futures-have-arrived-in-the-us; help.coinbase.com US perpetual-style futures overview; docs.cdp.coinbase.com Advanced Trade US Derivatives guide.)

Separately, in May 2026 the CFTC issued a no-action letter permitting CFM to route customers to certain "true" perpetuals via Coinbase's Bermuda affiliate (CoinDesk, May 29 2026; Dechert client note, June 2026). ⚠️ As of this writing it is not verified that this routing is live for retail via the Advanced Trade API. **This spec targets only the CDE perpetual-style contracts, which are confirmed live.** If the Bermuda-routed product becomes API-tradable, re-evaluate (fees/leverage may differ materially).

**1.2 Contract lineup available to US retail (as of mid-2026).**
- nano Bitcoin Perpetual-Style (product IDs of the form `BIP-20DEC30-CDE`): **0.01 BTC/contract**
- nano Ether Perpetual-Style: **0.10 ETH/contract**
- nano SOL: **5 SOL/contract**; nano XRP: **500 XRP/contract** (launched Aug 18, 2025)
- ~11 altcoin perp-style contracts added Dec 2025: AVAX, BCH, ADA, LINK, DOGE, HBAR, LTC, DOT, SHIB, XLM, SUI (CoinDesk Nov 2025; CCN Dec 2025). Liquidity in these is thin relative to BTC/ETH.
- Coinbase Derivatives also lists dated futures and a Mag7+Crypto equity index future; out of scope.

⚠️ The exact live product IDs, tick sizes, and current minimums must be pulled at runtime via `GET /api/v3/brokerage/products?product_type=FUTURE` and filtered for perpetual-style CDE symbols. Do not hardcode symbols; contract IDs encode the expiry (e.g., `BIP-20DEC30-CDE`) and new series will appear.

**Minimum order = 1 contract.** At a $100,000 BTC price, 1 nano BTC contract = ~$1,000 notional (~17% of capital); at $3,500 ETH, 1 nano ETH = ~$350 notional (~6% of capital). Granularity is adequate for a $6k account: position sizes of 2–10 BTC contracts and 5–30 ETH contracts are achievable.

**1.3 Leverage and margin.** Up to **10x intraday** leverage on crypto perp-style contracts (Coinbase launch blog, Jul 2025). Critically, CFM distinguishes **intraday** (8am–4pm ET window, opt-in) from **overnight** margin; overnight requirements are higher (the API exposes `get_intraday_margin_setting` / `get_current_margin_window`, and the Get Futures Balance Summary endpoint returns both intraday and overnight margin health — docs.cdp.coinbase.com US Derivatives guide). ⚠️ Exact overnight initial/maintenance margin percentages per contract must be read from the live API/product metadata; assume overnight max effective leverage is materially below 10x (plan for ~4–5x available; this strategy needs at most 2x, so the constraint is not binding).

**1.4 Funding mechanism (verified, from Coinbase Help "US perpetual-style futures — Overview").**
- Funding rate computed **hourly**: 20 samples (every 3 min) of futures-mark minus spot-mark premium are averaged and **scaled by 1/24**; smoothing = 75% current hour + 25% prior hour.
- `funding_payment = funding_rate × contracts × contract_multiplier × futures_mark_price × (−1)` (positive rate: longs pay shorts). Accrues hourly; settles to the derivatives cash account ~12 AM ET on weekdays.
- Typical levels: Coinbase's own worked examples use +0.010%/hr as representative. Hourly funding of ±0.01% ≈ ±0.24%/day ≈ ±87%/yr annualized at the extreme — but sustained typical levels on BTC in calm regimes run far lower (annualized single digits). ⚠️ CDE funding history is short and venue-specific; the implementer must log realized hourly funding from day one (see §9). Coinbase publishes CDE funding rates at coinbase.com/market-data/derivatives.

**1.5 Fees and slippage.**
- Retail introductory rate: **0.05% of notional per contract per side**, inclusive of exchange/clearing/NFA fees, with a **minimum of $0.15–$0.20 per contract** (Coinbase launch blog states $0.15 min; the earlier Advanced fee blog states $0.20 min and "0.05% introductory" — ⚠️ confirm the live number via the Get Transaction Summary endpoint; this spec budgets $0.20). Marketing's "fees as low as 0.02%" is a high-volume tier a $6k account will never reach.
- Minimum-fee trap: at 0.05%, the percentage fee exceeds the $0.20 minimum only when contract notional > $400. Nano BTC (~$1,000) is fine. Nano ETH is fine while ETH > $4,000; below that the $0.20 minimum binds and the effective fee on a $300 ETH contract is ~6.7 bps/side. This slightly favors concentrating in BTC contracts (see §3).
- Slippage: nano-contract order books on CDE are thinner than offshore perps. For marketable orders of 1–10 contracts in BTC/ETH, budget **3–5 bps** half-spread+impact per side in normal conditions, 10+ bps in stress. ⚠️ No public depth study exists; this must be measured live (it is an explicit acceptance gate in §10).
- Total round-trip cost budget used throughout: **taker 5 bps + slippage 4 bps = 9 bps/side ⇒ ~18 bps (0.18%) of traded notional per round trip**, plus funding while held.

**1.6 Liquidation mechanics.** This is an FCM-carried, USD-margined futures account, not an offshore perp engine with an insurance fund and socialized ADL. If margin health degrades, CFM issues margin calls and can **auto-liquidate**; Coinbase's own risk language confirms **losses can exceed the deposited amount** (a negative/debit balance is possible on gap-through moves). There is also a hard market closure every **Friday 5:00–6:00 PM ET** plus a **3-hour quarterly maintenance window** during which no orders (including stops) can execute. Both facts drive the leverage cap and the pre-close de-risking rules in §7.

**1.7 Advanced Trade API support (for CFM futures).**
- REST `/api/v3/brokerage` supports order create/edit/cancel on futures products (`product_type=FUTURE`); WebSocket channels include `ticker`, `level2`, `market_trades`, `user` (order/fill updates), `heartbeats`, and `futures_balance_summary`. Official Python SDK: `coinbase-advanced-py`.
- Order types: market (IOC), limit (GTC/GTD/IOC/FOK), **native stop-limit**, and **bracket / TP-SL attached orders** (the Coinbase order-types help page documents TP/SL behavior specifically for perpetuals and US futures, including the trigger→limit repricing buffer, ~5% of the stop trigger). There is **no native stop-market**; a stop-limit with a wide limit offset is the closest venue-native protective order. ⚠️ Confirm bracket/TP-SL availability on CDE perp-style symbols specifically (it was in beta on spot first).
- Futures-specific endpoints: futures balance summary, list/close positions, schedule futures sweep, intraday margin setting/window.
- Note (Sept 2026): Coinbase is migrating **international** derivatives to a new Deribit-powered gateway with new order types. This does not obviously affect CFM/CDE products, but ⚠️ watch the changelog — US futures endpoints have had breaking changes before.
- **Sandbox:** CDP documents an Advanced Trade sandbox for integration testing, but there is no evidence of a full CFM futures paper-trading environment that simulates fills, funding, and margin. ⚠️ Treat the sandbox as an API-plumbing test only. This is consistent with the operator's plan: no extended paper phase; go live at minimum size with hard gates (§10).

---

## 2. Strategy thesis

**Commit: volatility-targeted, slow time-series momentum (trend following) on BTC and ETH perp-style futures, long-biased with a funding-conditioned short side, daily cadence, ≤2x gross leverage.** The edge: crypto exhibits the same medium-horizon return persistence documented across asset classes (Moskowitz–Ooi–Pedersen 2012) and specifically in BTC/ETH (Liu & Tsyvinski, *Risks and Returns of Cryptocurrency*, RFS 2021 — "strong time-series momentum effect"; Kim & Suh 2023 find 1–8 week persistence; a 2025 Journal of Futures Markets study finds BTC momentum lookbacks of ~125–230 days remain the profitable region in the 2021–2025 sample). The behavioral driver — under-reaction to flows and delayed over-reaction, amplified in crypto by retail herding and leverage cycles — has not been arbitraged away because harvesting it requires sitting through long whipsaw stretches and deep relative drawdowns, which is a risk-bearing premium, not a free lunch. Volatility targeting converts crypto's regime-switching variance into roughly constant dollar risk, which both the TSMOM literature and crypto-specific studies (e.g., Grayscale Research 2023 on MA strategies: higher Sharpe and much smaller drawdowns than buy-and-hold 2012–2023) show improves risk-adjusted returns and, at this account size, is what keeps fee drag and liquidation risk small. Funding is used as a cost gate and crowding veto, not as a standalone alpha. Honest caveat the design accepts: trend on BTC/ETH has been visibly weaker/choppier in 2024–2026 than 2013–2021; expected Sharpe is modest (§8) and the strategy is built to survive being mediocre, not to assume it is great.

**Evidence review verdicts by signal family:**

| Family | Verdict | Reasoning |
|---|---|---|
| Time-series momentum, BTC/ETH | **KEEP (core)** | Persistent across academic samples incl. 2021–2025 (RFS 2021; JFM 2025; Grayscale 2023). Medium lookbacks (~1–8 months) remain the robust region; short lookbacks (<1 month) have degraded. Low turnover fits the fee structure. |
| Volatility-regime filter + vol-targeted sizing | **KEEP (core)** | Best-replicated result in the futures literature (Harvey et al. on vol scaling; TSMOM implementations). In crypto it directly controls the two account-killers here: liquidation distance and fee drag. |
| Funding-rate signals | **KEEP as gate/veto only** | Funding *carry* (short perp/long spot) is real but structurally unavailable here at acceptable cost: legs sit at two legal entities (CBI spot, CFM futures) with no cross-margin, doubling capital needs and fees on $6k. Extreme-funding *contrarian* evidence is real but concentrated in tails and regime-dependent (funding predictability is time-varying — Inan, SSRN 2025); too fragile as a standalone signal, useful as a crowding veto and as an explicit holding cost in the P&L model. |
| Open interest / liquidation cascades | **REJECT** | Edge exists intraday but requires low-latency infra, deep books, and per-venue liquidation feeds; CDE has no public liquidation feed and thin nano books. At $6k, fee+slippage per attempt consumes the expected edge. |
| Perp-spot basis / term structure | **REJECT as a trade; keep as monitor** | Two-legged basis trades are capital- and fee-inefficient across CBI/CFM (see funding row). CDE-vs-offshore basis is informative context; log it, don't trade it. |
| Social sentiment (LunarCrush, Fear & Greed) | **REJECT as signal** | Published out-of-sample evidence that social metrics add alpha *after costs* on BTC/ETH at daily horizons is weak and unstable; most positive results are in-sample or on illiquid alts. Optional: log LunarCrush Galaxy Score/AltRank daily for post-hoc research only. Nothing in the live decision path. |
| On-chain flows (exchange netflows, stablecoin supply) | **REJECT as signal** | Netflow signals degraded badly post-2024 as ETF/custodial flows broke the "coins to exchange = sell pressure" mapping. Stablecoin supply growth is a slow macro regime proxy at best. Not in the decision path. |

Two robust signals beat seven fitted ones: the live system trades **trend × vol-targeting**, with **funding as a gate**. Everything else is telemetry.

---

## 3. Universe

**Trade: nano BTC perpetual-style (BIP-…-CDE) and nano ETH perpetual-style.** Rationale: (a) only these two have both academic evidence of TSMOM persistence through recent regimes and adequate CDE liquidity; (b) BTC contract notional (~$1,000) sits comfortably above the $400 minimum-fee crossover, ETH usually near it; (c) SOL/XRP/altcoin perps on CDE are recent, thin, and their nano notionals (~$500–$1,500 for XRP/SOL, tiny for SHIB/DOGE) put many trades into the minimum-fee regime, structurally raising costs. Capital split target: **2/3 of gross risk to BTC, 1/3 to ETH** (BTC gets more because its contract economics are better and its trend evidence is strongest). If ETH price < $2,000 (nano notional < $200, minimum fee > 10 bps/side), **drop ETH and run BTC-only** until it recovers.

---

## 4. Signals — exact definitions

All computations run on **daily bars sampled at 00:00 UTC** built from CDE perp-style trade prices (fallback: Coinbase spot `BTC-USD`/`ETH-USD` candles via the public Advanced Trade candles endpoint — the funding mechanism keeps the perp within bps of spot, so spot bars are an acceptable signal input; executions always reference live perp quotes).

Let `P_t` = daily close, `r_t = ln(P_t / P_{t-1})`.

**S1. Trend ensemble (per asset).** Three binary sub-signals, equally weighted:
- `s_a = +1 if P_t > SMA(P, 100) else −1`  (100-day simple moving average)
- `s_b = +1 if P_t > SMA(P, 200) else −1`
- `s_c = +1 if r_{t−20..t} sum > 0 else −1`  (20-day total return sign; the short leg of the ensemble)
- `TrendScore_t = (s_a + s_b + s_c) / 3 ∈ {−1, −1/3, +1/3, +1}`

**Hysteresis (fee control):** a *change* of target direction requires `|TrendScore|` ≥ 1/3 in the new direction for **2 consecutive daily closes**. Flat is entered when TrendScore crosses 0 without confirmation.

**S2. Volatility estimate.** `σ_t` = EWMA of squared daily returns, λ = 0.94 (RiskMetrics), annualized: `σ_ann = σ_t × √365`. Floor at 20% annualized, cap at 150% (guards the sizing formula).

**S3. Volatility-regime filter.** `VolRatio_t = σ_ann(fast, λ=0.94, ~20d half-life proxy) / σ_ann(slow: 90-day simple realized vol)`. If `VolRatio_t > 2.0` (vol explosion), no NEW positions; existing positions have target size halved until VolRatio < 1.5.

**S4. Funding gate.** `F_t` = trailing 7-day mean of CDE hourly funding rate for the asset, annualized (`mean_hourly × 24 × 365`). ⚠️ Pull from Coinbase market-data/derivatives or the products endpoint; if CDE funding is unavailable programmatically, proxy with Binance BTCUSDT/ETHUSDT funding (free API) and log the discrepancy — but flag this as degraded mode.
- **Long veto:** if TrendScore > 0 and `F_t > +30%` annualized → cap position at 50% of target (crowded long: you pay heavy carry and tail-reversal risk is elevated).
- **Short gate:** shorts are only permitted when TrendScore ≤ −2/3 **and** `F_t > −10%` annualized (don't pay meaningful funding to be short alongside a crowd).
- Funding is also charged explicitly in the backtest and in live P&L attribution.

**Signal-to-target mapping:**
```
direction_t = sign(TrendScore_t) after hysteresis, subject to S4 short gate; else 0
strength_t  = |TrendScore_t|            # 1/3 or 1 (2/3 states resolve to 1/3 weight… see note)
# Note: with three ±1 components, |TrendScore| ∈ {1/3, 1}. Use it directly as a scalar.
```

---

## 5. Entry and exit rules

**Cadence:** one decision per asset per day at **00:05 UTC** (5-min buffer after bar close), plus a continuous risk loop (below). No intraday signal trading.

**Entry (or target change):**
1. Compute target contracts `N*` from §6. If `|N* − N_current| × contract_notional < max($200, 5% of equity)`, do nothing (rebalance dead-band — kills fee churn).
2. Execute the delta as a **limit order at the touch (join best bid/ask), post-only if supported on futures; if unfilled after 10 minutes, cancel and cross the spread with a limit at mid ± 5 bps IOC; if still unfilled, market order.** All orders carry `client_order_id` = deterministic hash of (date, asset, decision-seq) for idempotent recovery.

**Protective stop (always on, two layers):**
- **Layer 1 — venue-native backstop:** immediately after any fill establishing/expanding a position, place/replace a **native stop-limit** on CDE at `entry_ref × (1 ∓ 3.0 × ATRp)` where `ATRp = ATR(14, daily)/P_t` (stop trigger), with limit price a further 1.0% through the trigger (wide limit ≈ stop-market behavior). This order rests on the venue and protects against bot death.
- **Layer 2 — client-side soft stop:** the risk loop (every 30 s off WebSocket marks) flattens the position with a market order if price crosses `entry_ref × (1 ∓ 2.0 × ATRp)`. The client stop is the *intended* stop; the native stop is disaster insurance ~1×ATR further out.
- `entry_ref` = volume-weighted average entry price of the current position (from fills; the API exposes entry VWAP on positions).

**Take-profit:** none. Trend strategies die by cutting winners; exits are signal flips, stops, or risk-rule flattenings.

**Exit:** (a) hysteresis-confirmed TrendScore flip or 0-cross → close via the same execution ladder; (b) stop hit; (c) risk framework action (§7). After a Layer-2 stop-out, that asset is locked out of same-direction re-entry for **2 daily closes**.

**Weekly close handling:** CDE halts Fridays 5–6 PM ET. No new entries within 60 min before the halt. Positions may be held through it (1-hour gap ≈ tolerable at ≤2x), but before the **quarterly 3-hour maintenance window** (calendar known in advance), reduce gross exposure to ≤ 1.0x equity.

---

## 6. Position sizing and leverage policy

Volatility targeting with hard caps. Let `E` = current account equity (futures balance summary, marked), `V_target` = strategy vol target.

```
V_target_base = 40% annualized (portfolio level)
per-asset budget: BTC 2/3, ETH 1/3 of V_target (≈ risk-weighted, not notional-weighted)

For asset i:
  notional_i* = E × (V_target × w_i) / σ_ann,i × strength_i × direction_i × drawdown_mult × funding_mult
  N*_i = round(notional_i* / (contract_size_i × mark_price_i))   # integer contracts

Caps (applied after rounding, binding in order):
  |notional_i| ≤ 1.4 × E                       (per-asset cap)
  Σ|notional_i| ≤ 2.0 × E                      (portfolio gross cap = max effective leverage 2.0x)
  N*_i ≥ 1 contract or 0 (no fractional intent below 1 contract → 0)
```

- `drawdown_mult` from §7; `funding_mult` from §4 (1.0 or 0.5).
- **Effective vs venue leverage:** the venue permits 10x intraday; this system's *policy* maximum is **2.0x gross overnight**, i.e., at most ~$12k notional on $6k. With ≥10% maintenance-margin-equivalent at 2x, the mark must move ~40%+ against the book before liquidation mechanics are in sight — the liquidation buffer in §7 is therefore satisfied by construction. Worked example: E=$6,000, BTC σ_ann=45%, TrendScore=+1, no vetoes → notional* = 6000×(0.40×0.667)/0.45 ≈ $3,556 → at $100k BTC, `N*=4` contracts ($4,000 actual, 0.67x). Portfolio typically runs **0.5x–1.3x gross**; 2.0x is reached only in the lowest-vol regimes with full-strength signals.
- **Max concurrent positions: 2** (BTC, ETH). Same-direction concentration is expected (correlated assets); the portfolio cap governs it.

---

## 7. Risk framework

| Rule | Specification | Action |
|---|---|---|
| Per-trade risk cap | Client stop at 2.0×ATR ⇒ per-position risk ≈ notional × 2×ATRp. Sizing must satisfy: expected stop-loss ≤ **2.5% of E** per asset. If not, shrink N*. | Size reduction at entry |
| Daily loss limit | Realized+unrealized P&L from 00:00 UTC < **−4% of E** | Flatten everything, cancel all orders, no entries until next 00:05 UTC decision after a full 24h pause |
| Weekly loss limit | Rolling 7-day P&L < −8% of E | Halve `V_target` for 7 days |
| Drawdown tiers (`drawdown_mult`) | DD from equity high-water mark: 0–10% → 1.0; 10–20% → 0.6; 20–35% → 0.35; >35% → 0.2 | Applied continuously in sizing |
| **Hard halt** | Equity ≤ **$3,000** (50% of initial), checked on every risk-loop tick against marked equity | Flatten via market orders, cancel all orders, set persistent `HALTED` flag in state store, exit process. Restart requires manual flag removal. |
| Liquidation buffer | At all times, distance from mark to estimated liquidation price ≥ **30%** of mark. With the 2.0x cap this holds structurally; the risk loop still computes it from the balance-summary margin fields and force-reduces if violated (belt and suspenders — e.g., after a margin-requirement change by CFM). | Force-reduce to compliance |
| Funding-cost accounting | Accrue expected hourly funding into open-position P&L attribution; if cumulative funding paid on an open position exceeds **1.5% of E**, re-run the entry decision with funding veto at 100% (position must re-justify itself). | Possible flatten |
| Intraday margin window | **Do not opt in** to intraday 10x margin. Run overnight margin rules 24/7 — one regime, no 4 PM ET cliff. | Config |
| Data outage / API failure | Market data stale > 3 min OR order endpoints failing: if a native Layer-1 stop is confirmed resting for every open position → **hold** (venue protects); if any position lacks a resting native stop → **flatten that position** as soon as any order path works. If the process crashes: on restart, reconcile state from `list_positions` + `list_orders` + fills (client_order_id idempotency), re-arm missing native stops before doing anything else. | Fail-safe = protected-hold, else flatten |
| Weekly close / maintenance | No entries 60 min pre-halt; ≤1.0x gross before quarterly maintenance; accept the Friday 1-hour gap. | Scheduler |
| Negative-balance tail | Because FCM futures can gap through margin into a debit, the 2.0x cap + 30% liquidation buffer is the primary control; residual tail (e.g., a >30% instantaneous gap during the weekly close) is **accepted and documented** — it is the irreducible cost of holding leveraged overnight crypto. | Accepted risk |

---

## 8. Expected performance — honest estimates

**Cost model per round trip (BTC nano, $1,000 contract):** taker fee 5 bps + slippage ~4 bps per side ⇒ **~18 bps of traded notional per round trip** (≥ $0.40 fee minimum per contract RT is non-binding for BTC, occasionally binding for ETH).

**Turnover:** daily-cadence trend with hysteresis + dead-band produces ~15–30 position-changing round trips per asset per year (flips, stops, rebalances past the dead-band). Each round trip costs ~0.18% of *its own* traded notional. Worst case (churny year, 90 RTs total at ~$5,400 average notional): 90 × 0.0018 × $5,400 ≈ **$875/yr ≈ 14.5% of starting equity**. Realistic case (positions held weeks, 35–50 RTs total): **$340–$490/yr ≈ 6–8% of E**. Funding: at typical +5–15% annualized funding paid on the long side, average 0.8x exposure, ~60% time in market ⇒ **2–7% of E per year in net funding paid**. **Total expected cost drag: ~8–15% of E annually.**

**Gross edge context:** vol-targeted medium-term trend on BTC/ETH at 40% vol target produced gross Sharpe ~0.8–1.2 in 2015–2021 samples and roughly 0.2–0.6 in the choppier 2022–2025 window (consistent with the JFM 2025 finding that only medium lookbacks stayed positive). Planning assumptions, stated as ranges:

| Metric | Expected range |
|---|---|
| Trades (round trips)/year | 30–60 total across BTC+ETH |
| Win rate | 35–45% (trend profile: small losses, occasional large wins) |
| Avg win / avg loss | 2.0–3.5× |
| Net annual return on $6k | **−20% to +45%**; central estimate **+5% to +15%** (net Sharpe ~0.2–0.5 at 40% vol target) |
| Max drawdown (1-yr horizon) | **15–35%**; the 50% halt is the enforced worst case |
| Probability the halt fires within 12 months | est. 5–15% |

If these numbers look unexciting: they are, deliberately. At $6k on a 5 bps-taker venue with thin nano books, **a lower-frequency, ≤2x approach dominates** — every faster/hotter variant examined loses its expected edge to the §1.5 cost stack. This is the strategy the evidence supports.

---

## 9. Backtest plan

**Data (all free):**
- **Coinbase spot OHLCV** `BTC-USD`, `ETH-USD`: public Advanced Trade candles endpoint (`GET /api/v3/brokerage/market/products/{id}/candles`), 1d/1h granularity, history to 2015 (BTC). Primary signal series.
- **CDE perp-style prices + funding:** Coinbase market-data/derivatives pages and the products/candles API for `*-CDE` symbols — history only since **July 21, 2025** (≈1 year). Use for cost calibration and funding-behavior study, not for long-horizon signal testing.
- **Proxy for pre-2025 perp behavior:** Binance USDT-perp klines + full funding-rate history, free bulk downloads at `data.binance.vision` and `GET /fapi/v1/fundingRate`; Bybit public API equivalently. Coinalyze offers a free API for aggregated funding/OI as a cross-check. ⚠️ Verify current rate limits/terms of each at implementation time.
- **Proxy limitations (state them in the report):** Binance funding is 8-hourly with ±0.75% clamps vs CDE's hourly smoothed mechanism; Binance books are 100x deeper (slippage must come from live CDE measurement, not the proxy); CDE has the weekly close (proxy has none — inject synthetic 1-hour Friday gaps); USD vs USDT margin basis differs by a few bps.

**Protocol:** walk-forward, 2017-01→2026-06 on the proxy, parameters **frozen as specified in §4–§6** (no optimization; the parameters are literature-standard on purpose). Costs: 18 bps/RT + realized proxy funding. Report per-year net Sharpe, max DD, turnover, and the same at 2× assumed costs.

**Falsification criteria (any one ⇒ do not deploy / halt research):**
1. Full-sample net Sharpe < 0.30 at stated costs, or < 0 at 2× costs.
2. All net profit concentrated pre-2023 (2023–2026 sub-sample net Sharpe < 0).
3. Max DD in any walk-forward year > 40% (incompatible with the 50% halt surviving a fair sample).
4. Results flip sign under ±20% perturbation of the three lookbacks (100/200/20) — fragility test.

---

## 10. Small-live acceptance criteria

**Phase A — live at reduced size, immediately.** Config: `E_effective = min(equity, $1,500)` for sizing; max 2 BTC contracts / 4 ETH contracts; all risk rules active at full strictness.

**Software-correctness gates (ALL required to scale):**
- **A1:** ≥ **15 consecutive filled orders** where recorded fill price, fee, and contract count match API-reported fills exactly, and modeled P&L matches CFM statement P&L within **±$1 or ±2%** per trade (whichever is larger), including funding accruals over ≥ 10 funding settlements.
- **A2:** Layer-1 native stop verified to exist on the venue within 10 s of every position-opening fill (checked via list_orders), across ≥ 10 positions; at least **one** native or client stop observed to actually trigger and flatten correctly (if no natural stop occurs within 30 days, force one with a tiny 1-contract test position).
- **A3:** ≥ **2 clean restarts mid-position**: kill the process with an open position and resting orders; on restart it must reconcile positions/orders/fills, re-arm stops, and take no duplicate action (client_order_id idempotency proven).
- **A4:** 30 days with zero unhandled exceptions in the risk loop and zero missed daily decisions.

**Strategy-behavior gates:**
- **B1:** Realized slippage per side over ≥ 20 fills: median ≤ 5 bps, 90th percentile ≤ 12 bps (vs the 4 bps model — if median lands 5–8 bps, recompute §8; if > 8 bps, the strategy fails its cost model → stay at reduced size and redesign execution).
- **B2:** Realized fee rate per side ≤ 6 bps of notional on BTC (confirms the fee schedule as modeled).
- **B3:** Realized CDE funding on held positions within 2× of the Binance-proxy funding over the same window (validates the proxy).

**Scale-up:** all gates green **and** ≥ 45 calendar days ⇒ raise `E_effective` to 50% of equity for 30 days, then 100%. **Demotion back to Phase A (automatic) if any of:** slippage median > 8 bps over any trailing 20 fills; any reconciliation mismatch > $5; any unhandled crash that required manual intervention; weekly loss limit triggered twice in 30 days; any venue rule/fee change detected (transaction-summary fee rate change, margin change) pending review.

---

## 11. Open questions for the implementer (verify against live API before go-live)

1. Exact live product IDs, tick sizes, position limits, and **overnight initial/maintenance margin %** per CDE perp-style contract (`GET /products`, product metadata).
2. Current retail fee rate and per-contract minimum ($0.15 vs $0.20 vs changed) via Get Transaction Summary and a 1-contract test fill.
3. Whether **bracket/TP-SL and stop-limit orders are accepted on `*-CDE` perp-style symbols** via API (documented for perpetuals in help pages, but confirm the futures order-configuration schema).
4. Whether CDE **hourly funding rates are retrievable programmatically** (products endpoint field vs market-data page scrape) and their exact timestamp conventions.
5. Whether the CDP **sandbox** covers futures order flow at all; if yes, use it for A-gate rehearsal, but do not substitute it for Phase A.
6. Status of the May-2026 CFTC no-action **Bermuda-routed true perps** for US retail via API — if live, produce a comparison memo (fees, funding, leverage, margin currency) before considering migration.
7. WebSocket `user` channel behavior for futures fills (event schema parity with spot) and `futures_balance_summary` update latency.
8. Confirmation that **not opting in** to intraday margin means a single overnight margin regime 24/7 (assumed in §7).
9. Quarterly maintenance-window calendar publication location (needed by the scheduler).
10. Tax/reporting note (not a design input, but log it): CDE futures are §1256 60/40 instruments per Coinbase's product page — confirm with a tax professional.

---

## Self-critique — three most likely ways this loses money

1. **Extended chop (the 2024-style regime persists).** Trend flips repeatedly; each confirmed flip costs ~0.18% notional + a 2×ATR stop occasionally. *Handled:* hysteresis (2-day confirmation), rebalance dead-band, drawdown multiplier shrinking size as losses accumulate, and the falsification gate that already required 2023–2026 profitability before deployment. *Residual accepted:* a multi-year Sharpe-0 regime slowly bleeds 8–15%/yr in costs against no edge; the drawdown tiers turn that into a shallow bleed rather than ruin, and the 50% halt bounds it. This is the known price of trend following.
2. **Gap through stops during the weekly close, maintenance, or a flash event on a thin book.** Client stops can't fill in a halt; native stop-limits can be jumped; an FCM account can go debit. *Handled:* ≤2x gross (usually <1.3x), 30% liquidation buffer, ≤1x before known maintenance, no entries pre-halt, wide-limit native backstops. *Residual accepted:* a >30% instantaneous gap at 2x produces a near-total loss and possibly a small debit. Judged acceptable given historical BTC 1-hour gap distribution and the risk-capital mandate; documented rather than designed away, because designing it away means not holding overnight crypto at all.
3. **The venue itself invalidates the cost/microstructure model** — nano books thinner than budgeted, fee schedule ends its "introductory" period, funding on CDE runs structurally hotter than the Binance proxy (small venue, one-sided retail flow), or a breaking API change mid-position. *Handled:* this is exactly what §10 exists for — slippage/fee/funding gates measured live at $1.5k before real size, automatic demotion on any fee/margin change, restart-reconciliation drills. *Residual accepted:* between measurements, a sudden liquidity withdrawal can cost a few extra bps per exit; bounded by position sizes that are ≤10 nano contracts.

**Bottom line:** boring and robust won. Daily-cadence, vol-targeted trend at ≤2x on BTC/ETH is the only examined configuration whose evidence-supported gross edge clearly exceeds this venue's cost stack at $6,000 of capital. Faster, hotter, or more clever variants were each rejected on the arithmetic in §1.5 and §8, not on taste.
