# The Strategy, In Plain English

> **Provenance:** This is a derived, plain-English companion document. The canonical
> sources of truth are `Docs/crypto-perps-strategy.md` (including Amendments A and B in
> its header), `research/crypto_perps/REPORT.md` (backtest validation), and
> `Docs/decisions-log.md` (deviations and venue truth). **On any conflict, the spec and
> decisions log win and this document gets corrected.** Every number in this document
> comes from those sources; nothing here is new.
>
> **Audiences:** (1) the operator — a memory-jogger for what the strategy does and every
> rule it follows; (2) prospective investors — people with capital but not necessarily a
> trading or crypto background. Nothing in this document is investment advice or an offer
> of any kind. The system currently trades only the operator's own risk capital.
>
> **Last updated: 2026-07-09**

---

## The 30-second version

We run a fully automated program that trades two US-regulated crypto futures contracts —
small-denomination ("nano") Bitcoin and Ether futures on Coinbase's derivatives exchange.
Once a day it asks one question per asset: *is the price in an established trend, up or
down?* If yes, it holds a position in that direction, sized so that a bad day hurts a
predictable, capped amount; if no, it holds nothing. It never bets on news, tips, or
predictions — only on measured price behavior. The single most important risk rule: the
size of every position is set so that the built-in stop-loss on any one trade can cost at
most **5% of the account**, and if the whole account loses **8% in a single day, the
machine sells everything and halts itself** until the operator manually intervenes. A
human cannot forget to enforce these rules, because a human doesn't enforce them — the
machine does, on a 30-second clock.

---

## What we trade

**The venue.** Coinbase Financial Markets (CFM), the US-regulated futures arm of
Coinbase. The contracts are listed on Coinbase Derivatives Exchange (CDE), which is a
CFTC-designated contract market — the same regulatory category as the major US futures
exchanges — and CFM is a registered futures commission merchant (FCM) and NFA member.
This is deliberately *not* an offshore crypto exchange: the account is USD-denominated,
US-regulated, and using offshore venues is a locked "never" decision.

**The products.** Two contracts (live product IDs as of 2026-07-09; the system discovers
them from the exchange at runtime rather than hardcoding them):

| Product | Contract size | Rough value of 1 contract |
|---|---|---|
| nano Bitcoin perpetual-style future (`BIP-20DEC30-CDE`) | 0.01 BTC | ~$1,000 when Bitcoin is at $100,000 |
| nano Ether perpetual-style future (`ETP-20DEC30-CDE`) | 0.10 ETH | ~$350 when Ether is at $3,500 |

**What a "perpetual-style future" is, in plain words.** A *future* is a contract whose
price tracks an asset — here Bitcoin or Ether — without you owning the asset itself. You
post a cash deposit (called *margin*), and as the asset's price moves, money is added to
or subtracted from your account daily. Futures let you profit from a price *fall* as
easily as a rise (see "short" in the glossary), and they let you control more value than
the cash you post (*leverage* — which we cap tightly; see the sizing section).

Ordinary futures expire on a set date. "Perpetual" futures, popular offshore, never
expire. The US products we trade are a hybrid: legally they are futures with a far-off
expiry (December 2030), but they behave like perpetuals because of a mechanism called
**funding**. Every hour, the exchange measures whether the futures price is running above
or below the actual (spot) Bitcoin/Ether price. If the future is trading rich — usually
because lots of people are betting on a rise — holders of long positions pay a small
hourly fee to holders of short positions, which nudges the prices back together. If the
future trades cheap, the payment flows the other way. Coinbase's own worked examples use
0.010% per hour as a representative rate — small per hour, but it compounds, so the
strategy treats funding as a real cost: it's charged in our accounting, and extreme
funding readings actually veto trades (see "the signal").

One honest structural note: because this is a real futures account at an FCM — not an
offshore perp engine with an insurance fund — a violent enough price gap can, in theory,
push the account below zero (a debit). The risk framework's leverage caps exist
specifically to keep that tail tiny; it is documented as an accepted residual risk rather
than hidden. The exchange also closes every Friday 5:00–6:00 PM ET and for one ~3-hour
maintenance window per quarter; the rules below handle both.

---

## Why we believe this makes money

The strategy is **trend following** (the academic term is *time-series momentum*): assets
that have been going up for weeks-to-months tend, on average, to keep going up a while
longer, and likewise for down. This is one of the most replicated findings in finance —
documented across stocks, bonds, currencies, and commodities over decades
(Moskowitz–Ooi–Pedersen 2012), and specifically in Bitcoin and Ether in peer-reviewed
work through the 2021–2025 period. The behavioral driver: investors under-react to new
information at first and over-react late, and in crypto that cycle is amplified by
retail herding and leverage.

**"If it works, why hasn't everyone arbitraged it away?"** Because harvesting it is
genuinely unpleasant. Trend following loses small amounts repeatedly during choppy,
directionless markets — sometimes for a year or more — and its wins come in occasional
big streaks. Most capital cannot or will not sit through long whipsaw stretches and deep
drawdowns relative to buy-and-hold. What's left is best understood as a *risk-bearing
premium* — payment for tolerating discomfort — not a free lunch. The spec is explicit
about the honest caveat: trend in BTC/ETH has been visibly weaker and choppier in
2024–2026 than in 2013–2021, the expected edge is modest, and the strategy is built to
**survive being mediocre, not to assume it is great**.

Two supporting ideas complete the design:

- **Volatility targeting** (the sizing engine): crypto's riskiness swings wildly between
  calm and violent regimes. We size positions *inversely* to current volatility — smaller
  when markets are wild, larger when calm — so the account's dollar risk stays roughly
  constant. This is the best-replicated risk-control result in the futures literature,
  and at this account size it is also what keeps fee drag and liquidation risk small.
- **Funding as a veto, not a signal**: when funding gets extreme it means a trade is
  crowded and expensive to hold. We use it to shrink or block trades — never as a
  standalone reason to trade.

Everything else that was evaluated — social sentiment, on-chain flows, liquidation
cascades, basis trades — was **rejected** on evidence or cost grounds and lives only as
telemetry. Two robust signals beat seven fitted ones.

---

## The signal: TrendScore

### The dinner-table explanation

Each day, for each asset, three simple judges each cast a vote: "uptrend" (+1) or
"downtrend" (−1):

1. **The slow judge** — is today's price above its average over the last 100 days?
2. **The very slow judge** — is today's price above its average over the last 200 days?
   (The 100- and 200-day averages are the most-watched trend lines in finance; think of
   them as the asset's "cruising altitude" over roughly a quarter and roughly a year.)
3. **The recent-memory judge** — has the price gone up, net, over the last 20 days?

The **TrendScore** is the average of the three votes, so it can only be **−1, −⅓, +⅓, or
+1**. All three judges agree upward: +1 (strong uptrend). Two of three: +⅓ (weak
uptrend). And so on down to −1 (strong downtrend).

Two refinements stop the score from causing hyperactive trading:

- **Hysteresis (the two-day confirmation rule).** The system doesn't reverse direction
  the first day the score flips — one day can be noise. A change of direction requires
  the score to point the new way (at magnitude ≥ ⅓) for **two consecutive daily
  closes**. Under the production profile (Amendment B), while a flip is pending
  confirmation the system **holds its current position** rather than going flat —
  backtesting showed that exiting on every one-day wiggle paid a round trip of costs for
  nothing.
- **The crowding veto (funding gate).** Before acting, the system checks the trailing
  7-day average funding rate, annualized. If we'd be going long while longs are paying
  more than **+30% annualized** in funding, the position is capped at **half** its normal
  size (crowded trade, expensive to hold, elevated reversal risk). Shorts are only
  allowed at all when the trend is strongly down **and** shorts aren't themselves paying
  meaningful funding (see exact rule below).

### The exact rules (reference)

Computed once daily on bars sampled at 00:00 UTC (signal input is Coinbase spot
`BTC-USD`/`ETH-USD` prices; executions reference live futures quotes):

- `s_a = +1 if close > SMA(100) else −1`
- `s_b = +1 if close > SMA(200) else −1`
- `s_c = +1 if the 20-day total return > 0 else −1`
- `TrendScore = (s_a + s_b + s_c) / 3` ∈ {−1, −⅓, +⅓, +1}
- **Direction** = sign of TrendScore, but a *change* of direction requires
  |TrendScore| ≥ ⅓ in the new direction for 2 consecutive daily closes (hysteresis).
  While unconfirmed: hold the current position (Amendment B hysteresis-hold).
- **Strength** = |TrendScore| (⅓ or 1) — a weak trend gets one-third the size of a
  strong one.
- **Volatility estimate (for sizing):** EWMA of squared daily returns, λ = 0.94,
  annualized ×√365, floored at 20% and capped at 150%.
- **Volatility-regime filter:** if fast volatility exceeds 2.0× the slow (90-day)
  volatility — a vol explosion — no *new* positions, and existing targets are halved
  until the ratio falls back below 1.5.
- **Funding gate:** `F` = trailing 7-day mean of hourly funding, annualized.
  Long veto: if TrendScore > 0 and F > +30%/yr → cap position at 50% of target.
  Short gate: shorts permitted only if TrendScore ≤ −⅔ **and** F > −10%/yr.

The three lookbacks (100/200/20) are literature-standard values, deliberately **not**
optimized on our data, and frozen: changing them requires a falsify-and-replace research
cycle, not a tweak. The backtest confirmed no "parameter cliff" — perturbing all three by
±20% barely moved the results.

---

## Entry and exit rules — the checklist

**Cadence: one decision per asset per day at 00:05 UTC** (five minutes after the daily
bar closes). No intraday signal trading, ever. A separate risk loop runs every 30 seconds
but only *protects* (stops, limits, halts); it never initiates trades.

**We ENTER or resize a position when, at the daily decision:**

1. The TrendScore direction (after the two-day confirmation rule) says long or short, and
2. the funding gate doesn't veto it, and the vol-explosion filter isn't blocking new
   positions, and
3. the target size from the sizing engine (next section) differs from the current
   position by **more than max($200, 5% of equity)** in notional value — the
   *rebalance dead-band*. Smaller differences are ignored: churning tiny adjustments
   just pays fees.
4. **Band-edge rebalancing (Amendment B):** when a same-direction resize does trade, it
   trades only to the nearest *edge* of that dead-band, not all the way to the exact
   target — the minimum trade that gets us back inside the "close enough" zone. In
   backtests this was neutral at expected costs but is meaningful insurance if real
   trading costs run higher than modeled.
5. Orders execute via a patience ladder: join the best bid/ask passively → if unfilled
   after 10 minutes, cross the spread with a limit at mid ± 5 bps → if still unfilled,
   market order. Every order carries a deterministic ID so a crash-and-restart can never
   accidentally double an order.

**We EXIT (or flip) a position when any of these happens:**

1. **Confirmed trend flip** — TrendScore points the other way for 2 consecutive closes.
2. **Stop-loss hit** — two independent layers, always on:
   - *Client stop (the intended stop):* the 30-second risk loop flattens the position at
     market if price crosses 2.0 × ATR(14) against the entry price (ATR — Average True
     Range — is a standard measure of typical daily movement, so the stop adapts to
     current volatility rather than being a fixed percent).
   - *Venue-native backstop:* within seconds of any fill that opens or grows a position,
     a stop-limit order is placed on the exchange itself at 3.0 × ATR against entry (with
     a wide limit so it behaves like a stop-market). This one lives at the exchange, so
     it protects even if our software dies.
3. **A risk-framework action** (daily loss limit, hard halt, forced de-risking — next
   sections).

**After a client-stop exit,** that asset is locked out of re-entering in the same
direction for **2 daily closes** — no immediate revenge re-entry.

**There is no take-profit.** Trend strategies die by cutting winners early; positions are
held until the trend ends, a stop fires, or a risk rule intervenes.

**We do NOTHING when:** the trend is unconfirmed either way (hold what we have), the
target change is inside the dead-band, the vol-explosion filter is on (for new
positions), the funding gate blocks a short, or Ether is below $2,000 — the spec's own
minimum-price rule, because below that the exchange's per-contract minimum fee makes ETH
contracts structurally too expensive to trade. (ETH has been below $2,000 recently, so
**at launch this is effectively a Bitcoin-only system** until ETH recovers.)

**Calendar handling:** no new entries in the 60 minutes before the Friday 5–6 PM ET
exchange close (positions may be held through the 1-hour gap); before the known quarterly
~3-hour maintenance window, gross exposure is reduced to at most 1.0× equity.

---

## Position sizing and leverage

### The idea

Size each position so the *portfolio's* volatility lands near a target, then apply hard
caps on top, then — most binding in practice — shrink until the stop-loss on the trade
can only cost a capped fraction of the account. The production (Amendment B) knobs:

| Knob | Value |
|---|---|
| Volatility target | 80% annualized, portfolio level |
| Risk split | ⅔ Bitcoin, ⅓ Ether |
| Per-trade risk cap | expected stop-loss ≤ **5% of equity** per position (this rule **overrides** the volatility-target output when they conflict) |
| Per-asset notional cap | 1.4 × equity |
| Portfolio gross cap | 3.0 × equity (the 30% liquidation-buffer rule effectively binds near ~2.8×) |
| Max concurrent positions | 2 (BTC + ETH) |

An 80% volatility target sounds enormous. In practice the strategy's own dampeners stack
multiplicatively — weak-trend days run at ⅓ strength, crypto's actual volatility usually
sits well above the formula's assumption, ETH is often excluded — so the backtest at this
profile averaged just **0.50× gross leverage with a peak of 1.72×**. The 3.0× cap exists
as a hard ceiling, not a place the system lives. For reference: the venue would permit up
to 10× intraday; we never opt into that (locked decision), and our policy ceiling is less
than a third of it.

### A worked example (hypothetical numbers)

Say equity is **$6,000** (the actual starting capital), Bitcoin is at $100,000 (so one
nano contract ≈ $1,000 of exposure), the TrendScore is +1 (strong uptrend), and Bitcoin's
measured volatility is 60% annualized.

1. **Volatility-target size:** $6,000 × (80% × ⅔) ÷ 60% ≈ **$5,333** of Bitcoin exposure
   → about 5 nano contracts. That's 0.89× leverage on the BTC sleeve.
2. **Per-trade risk check:** suppose ATR says a typical day moves Bitcoin 3%, so the
   client stop sits 2 × 3% = 6% away. A $5,333 position stopped out at −6% loses ~$320 —
   but 5% of $6,000 is **$300**. The cap binds: shrink to $5,000 → **5 contracts** at
   exactly the $300 risk cap (integer contracts; the engine floors, never rounds up).
3. **Dead-band:** if we already held 4 contracts, the $1,000 difference exceeds
   max($200, 5% × $6,000 = $300), so the rebalance trades — but only to the nearest
   dead-band edge, not necessarily the full contract.

If the same day Bitcoin's volatility were 120% (a wild regime), step 1 alone would give
~$2,667 → 2 contracts: same dollar risk, half the exposure. That is volatility targeting
doing its job.

### What "cash" means here (Amendment B cash-yield layer)

Most of the account isn't in the market most of the time (average gross leverage ~0.5×
means roughly half the equity backs positions as margin, often far less). Idle cash is
not left dead:

- The futures account at CFM keeps USD margin plus a buffer of **25% of gross position
  value**.
- Equity beyond that is held as **USDC** (a fully-reserved digital dollar) at Coinbase,
  earning rewards — **3.50%/yr** as of 2026-07 via a Coinbase One Basic subscription
  ($4.99/mo, netted in all performance modeling).
- The non-negotiable constraint: swept cash must be reclaimable **same-day** for margin
  calls (USDC converts to USD 1:1 instantly). Higher-yield alternatives (DeFi lending
  vaults at ~4.8%, staking) were evaluated and **rejected** for system cash because their
  liquidity dries up in exactly the crypto stress events in which the margin account
  needs cash immediately. A margin reserve must be most liquid when markets are worst.

On ~$4,500 of average idle cash this is only ~$150/yr gross, but it compounds and it
carries no price risk. (The automated sweep worker turns on in Phase C2; during the
current small-live phase the operator has parked the split manually: $1,500 at the
futures side, the rest in USDC.)

---

## The risk framework — every rule

This is the section that matters most. Rules are listed from "routine trade protection"
to "break glass." All of them are enforced by code — the daily decision at 00:05 UTC and
a risk loop that re-checks marks, margin, and limits **every 30 seconds**.

**Per-position protection (every trade, always):**

1. **Per-trade risk cap:** position size is shrunk until the expected stop-loss is at
   most **5% of equity**. This rule has precedence over the volatility-target size.
2. **Two-layer stop-loss:** client stop at 2×ATR (the intended exit, fired by the
   30-second loop) plus an exchange-resident stop-limit backstop at 3×ATR (survives our
   software dying). On a $6,000 account, the most a single normal stop-out should cost is
   ~$300; the backstop bounds the abnormal case.
3. **Post-stop lockout:** after a stop-out, no same-direction re-entry in that asset for
   2 daily closes.

**Account-level limits:**

4. **Daily loss limit −8% of equity:** if P&L since 00:00 UTC (realized + unrealized)
   hits −8% — $480 on a $6,000 account — the machine **flattens every position, cancels
   every order, and halts new entries**. As implemented live (operator-approved
   2026-07-09, deliberately *stricter* than the spec's automatic 24-hour pause), resuming
   requires **manual operator action**, and the first **3 clean UTC days** back run at
   **half size** (the "convalescent" state). The resume day itself counts toward the 3 —
   but the day of the breach never does, and a day only counts if no new trigger fired
   (amended from 5 days, operator directive 2026-07-09 late session). One exception
   (added 2026-07-11): if a halt turns out to have been caused by a **system defect**
   rather than a genuine risk event — e.g. the phantom reconciliation break on the first
   overnight position — the operator can formally mark it a **false positive** from the
   web dashboard (passkey re-verification required) and return to full size immediately.
   The adjudication is always a human decision, must cite the fix for the defect, and is
   permanently recorded in the audit chain.
5. **Weekly loss limit −16%:** if rolling 7-day P&L is worse than −16% of equity, the
   volatility target is **halved for 7 days** — the system automatically de-risks after a
   bad week.
6. **Drawdown tiers: removed.** The base spec shrank positions progressively as the
   account fell from its high-water mark. Amendment B deliberately removed this
   (`drawdown_mult ≡ 1.0`) — an operator-directed, backtested trade-off accepting deeper
   losing-streak bleeds in exchange for full size when trends resume. Stated here
   honestly because an investor should know it: *between* the daily/weekly limits and
   the hard halt, there is no additional automatic size reduction for drawdown.

**Structural / solvency protections:**

7. **Leverage caps:** per-asset 1.4× equity, portfolio gross 3.0× equity, integer
   contracts only, never opted into the venue's 10× intraday margin regime (one
   conservative overnight margin regime, 24/7, by construction — the trading code
   physically lacks the API call to opt in).
8. **Liquidation buffer ≥ 30%:** at all times the price must be at least 30% away from
   the estimated forced-liquidation level. The 30-second loop computes this from the
   broker's own margin numbers and **force-reduces positions** if it's ever violated
   (belt-and-suspenders — the leverage caps make this structurally true already).
9. **Funding-bleed check:** if cumulative funding paid on an open position exceeds
   **1.5% of equity**, the position must re-justify itself with the funding veto at full
   strength — a slow-bleeding "winner" gets re-examined instead of held on autopilot.

**Break-glass:**

10. **Hard halt at $1,500 of total capital (−75% from the $6,000 start):** checked on
    every 30-second tick. Flatten everything at market, cancel all orders, set a
    persistent HALTED flag, exit the process. **Restart requires a human to manually
    remove the flag.** Important honesty note: this is a *malfunction circuit-breaker*,
    not a risk-tolerance bound — in 9.5 years of backtest at this profile it never fired
    (including a 48.5% intra-year drawdown), and a system down 75% is far more likely
    broken than unlucky. The operator's actual risk tolerance is full loss of the
    $6,000; the halt exists to catch a broken feed, a venue change, or a code fault
    before it can dig into a negative balance.
    *Measurement basis (Amendment C, 2026-07-18):* the floor compares against **total
    capital** — the futures-side USD equity *plus* the cash-yield USDC parked at
    Coinbase (latest daily capture, no older than 48 hours). This fixed the 2026-07-17
    false alarm where a routine ~$52 subscription charge tripped the breaker: the USD
    side sat near $1,500 by design while ~$4k of USDC was invisible to the check. If
    the USDC reading is missing or stale, the check falls back to USD-only — which can
    only make the breaker *more* likely to fire, never less. The trading size
    calculation deliberately still ignores USDC.
    *Operator note:* after converting USDC → USD, the daily USDC reading is stale-high
    until the next 00:20 UTC capture — for that window the breaker double-counts the
    converted amount (it is less sensitive, never more). **Close the window immediately
    after any conversion by re-capturing the reading: `/cash-recapture` in Discord (or
    `POST /api/system/cash-capture` from the web session).** The re-capture re-reads
    the venue balances and replaces today's reading; the scheduled 00:20 capture still
    never overwrites an existing one.

**Operational fail-safes:**

11. **Data outage / API failure:** if market data goes stale for more than 3 minutes or
    order endpoints fail — if every open position has its exchange-resident backstop
    stop confirmed resting, **hold** (the venue is protecting us); any position without
    one is **flattened** as soon as any order path works. On a process crash, restart
    reconciles state from the broker's own records (positions, orders, fills) and
    re-arms missing stops before doing anything else.
12. **Exchange calendar:** no entries 60 minutes before the Friday close; ≤1.0× gross
    before quarterly maintenance windows.
13. **Vol-explosion filter:** (from the signal section) fast volatility > 2× slow → no
    new positions, existing targets halved until it calms.
14. **Accepted residual, stated plainly:** an FCM futures account can gap through stops
    into a negative balance in an extreme event (e.g., a >30% instantaneous move during
    the weekly close). The leverage caps and liquidation buffer make this tail small; it
    cannot be made zero while holding leveraged overnight crypto, and the spec documents
    it as accepted rather than pretending it away.

**Current extra clamps (small-live phase, on top of everything above):** sizing uses
`min(equity, $1,500)` as effective equity, positions capped at 2 BTC / 4 ETH contracts.
All risk rules run at full strictness.

**What the machine can and cannot do without the operator:**

| The machine can, autonomously | The machine can NEVER, by construction |
|---|---|
| Enter/exit/resize positions within every rule above | Move money out of the account (the API key has trade permission only — no transfer permission — and is IP-locked to the server) |
| Fire stops, flatten on limits, force-reduce on margin | Resume trading after a halt (manual flag removal / operator action only) |
| Halt itself | Change its own risk parameters or strategy logic (parameter and code changes require a reviewed pull request the operator must approve with a special label; the CI system physically blocks merges without it) |
| Announce every trade and decision to Discord | Opt into 10× intraday margin (the API call doesn't exist in the code) |
| | Trade any venue other than Coinbase CFM, or any product beyond the discovered CDE perp-style contracts |

There is deliberately **no per-trade human approval** (operator mandate): the machine
trades within its rules and *announces*; the human supervises, audits, and holds the only
keys to resume, approve changes, and move money. Every event — every order, fill, risk
action, parameter change — is written to an append-only, hash-chained audit log that
even the database administrator cannot silently edit.

---

## What the machine does on its own — a day in the life

All times UTC (crypto trades 24/7; there is no "market open").

- **00:00 — the daily bar closes.** The day's official prices are in.
- **00:05 — the daily decision.** For each asset: compute TrendScore, volatility,
  funding gate; produce a target position; compare to the current position; trade the
  difference if it's outside the dead-band (via the patience ladder). This is the *only*
  time of day the system initiates trades.
- **00:10 — the `/cycle` digest.** A plain-English summary posts to the operator's
  Discord: per-asset score → target → action taken → costs. Every fill also posts as it
  happens, with its rationale (trend score, stop level, funding estimate).
- **00:15 — reconciliation.** An independent job pulls the broker's own record of
  positions, balances, and fills and compares it line-by-line against what the system
  *thinks* is true. Any mismatch raises an alert. Trust, but verify — against the
  broker's books, daily.
- **Every 30 seconds, around the clock — the risk loop.** Fresh mark prices in; check
  client stops, the daily loss limit, the liquidation buffer, the $1,500 halt, data
  staleness. This loop protects; it never initiates.
- **Hourly** — funding rates are logged for every product (telemetry powering the
  funding gate and the live-vs-model validation).
- **Continuously** — an independent watchdog pings the system from outside and alerts
  the operator if it goes unreachable.

**Where the human sits:** the operator reads the daily digest and alerts on Discord, can
halt the system from a phone at any time (`/halt`), is the *only* actor who can resume
from a halt, reviews and approves every strategy/risk code change through a labeled
pull-request process, and receives monthly and quarterly governance reports. Live
telemetry gates (slippage, fees, funding vs. model) run permanently, and a quarterly
re-validation re-runs the full backtest falsification suite on extended data.

---

## Expected performance — the honest version

**Read this section as ranges and caveats, not promises. Backtested performance does not
guarantee — and systematically overstates — future results.**

### What the backtest says

The strategy was validated on 9.5 years of data (January 2017 → June 2026) with
parameters frozen in advance — no optimization, no tuning to make history look good —
and had to pass four pre-registered falsification criteria (minimum full-sample Sharpe,
profitability in the recent 2023–2026 regime, drawdown bounds, and insensitivity to
parameter perturbation). All four passed. The final production profile (Amendment B), on
a $6,000 start:

| Metric (backtest, 2017 → mid-2026) | Value |
|---|---|
| Compound annual growth rate (CAGR) | **+40.7%/yr** |
| Sharpe ratio (risk-adjusted return) | 1.14 |
| Worst peak-to-trough drawdown | **−58.6%** |
| Worst single year | −4% (2024) |
| Same strategy if trading costs were double | +33.2%/yr, Sharpe 0.98 |
| Recent-regime check (2023 onward) | Sharpe +0.73 |
| Average gross leverage | 0.50× (peak 1.72×) |
| Hard halt ever triggered | never |

### Why you should mentally mark those numbers down

The validation report itself instructs this, so we repeat it:

- **Selection bias.** The production profile was chosen as the best of roughly eight
  pre-registered structural variants. Picking the best backtest inflates expected live
  performance. The report's own guidance: treat the headline as **the optimistic edge of
  the range**, and expect a central case of roughly **~30%/yr with ~50% drawdowns** —
  and even that assumes the historical edge persists.
- **Red years are normal, not exceptional.** In the backtest, 2021 and 2025 were losing
  years (−7.1% and −6.8% at the Amendment A profile) and 2024 finished at +0.6% *after a
  48.5% intra-year drawdown*. Anyone allocating should expect flat-to-negative years as
  a routine part of the deal; the edge shows up in trending years.
- **The drawdown number deserves respect.** A −58.6% backtest drawdown means the
  realistic plan includes watching the account fall by half or more and continuing to
  follow the rules. History is one draw; a live path can be worse.
- **Funding was modeled, not measured.** The venue is young (contracts launched
  mid-2025), so long-run funding history doesn't exist; the backtest used a constant
  funding cost, stress-tested at 0× and 2× (Sharpe 0.99–1.07 across that band). Real
  funding is regime-correlated; measuring it live is an explicit acceptance gate before
  the system is allowed to scale up.
- **Costs are real and budgeted.** Fees are ~0.05% of traded value per side (minimum
  $0.20/contract) plus slippage — budgeted at ~0.18% per round trip — plus funding while
  positions are held. At the base profile the spec's estimated all-in cost drag was
  **8–15% of equity per year**; the production profile trades roughly double the
  notional, and total modeled costs over the 9.5-year backtest were ~$23,900 against a
  $6,000 start. The strategy's low trading frequency (roughly 30–60 round trips per year
  across both assets) exists precisely because every faster variant examined lost its
  edge to this cost stack. If live slippage measures worse than ~8 bps per side, the
  system's own gates force it to stay at reduced size and redesign execution.

### The base-profile floor

The same strategy at the conservative base profile (half the size of production) did
+16.7%/yr, Sharpe 1.04, max drawdown 27.8% over the same period, with only one losing
year in ten. Production is the identical edge run at double size — the returns roughly
double, and so do the pain numbers. Size, not signal, is the aggression dial, and
scaling it further (a 2.5× profile was evaluated) is deliberately deferred until at
least 6 months of live data exist.

---

## How this loses money

The spec's own self-critique, translated faithfully. These are the three most likely
failure modes, in order:

1. **Extended chop.** The market goes nowhere violently — trends keep starting and
   aborting. Each confirmed false start costs a round trip (~0.18% of traded value) and
   occasionally a stop-out. This is *the* known cost of trend following, and 2024-style
   years are exactly what it looks like: the backtest's 2024 finished +0.6% after a
   48.5% drawdown. Mitigations: the two-day confirmation rule, the dead-band, the weekly
   loss limit halving size. Honest residual — a multi-year no-trend regime bleeds
   roughly 8–15%/yr in costs against no edge, and with Amendment B's drawdown tiers
   removed, that bleed runs at full size until the daily/weekly limits or the halt
   intervene.
2. **A price gap through the stops.** Stops are not guarantees: the exchange closes for
   an hour every Friday and ~3 hours quarterly, and a thin order book or flash event can
   jump past stop prices. In a futures account, a violent enough gap can exceed the
   account balance. Mitigations: low actual leverage (typically well under 1×, peak
   ~1.7× in backtest, hard-capped ~2.8–3.0×), the 30% liquidation buffer, forced
   de-risking before known closures, exchange-resident backstop stops. Honest residual —
   a >30% instantaneous gap at maximum leverage would produce a near-total loss and
   possibly a small negative balance. Accepted and documented; designing it away would
   mean not holding leveraged overnight crypto at all.
3. **The venue invalidates the cost model.** This is a young, thin market: order books
   could be thinner than budgeted, the "introductory" fee schedule could end, funding
   could run structurally hotter than the model, or an API change could break execution
   mid-position. Mitigations: this is exactly what the current small-live phase measures
   at $1,500 effective size before real money scales — slippage, fees, and funding each
   have explicit pass/fail gates, and any detected fee or margin change automatically
   demotes the system back to small size. Honest residual — between measurements, a
   sudden liquidity withdrawal costs extra basis points on exits, bounded by the small
   position sizes.

And the one an investor should weigh most: **the edge itself may have decayed.** Trend
in BTC/ETH has been weaker since 2024 than in the decade before. The strategy passed a
falsification test requiring profitability in the 2023–2026 subsample, but that is
evidence, not proof. The system's answer is structural: costs kept minimal, size kept
survivable, permanent live-vs-model telemetry, and a quarterly re-run of the
falsification suite — if the edge is gone, the plan is to find out cheaply and stop.

---

## Where we are right now

> **📅 Status as of 2026-07-09. This section goes stale fastest — check the date, and
> for anything load-bearing confirm against `Docs/decisions-log.md` (latest entries).**

The build plan runs in three gated phases:

- **C0 — build + offline validation (COMPLETE, 2026-07-08 → 07-09).** The Coinbase
  execution layer, market data + funding telemetry, signal engine, risk deltas,
  reconciliation, and Discord digest were built and merged. Exit
  gates passed: the live signal engine reproduces the validated backtest **trade-for-
  trade with bit-exact final equity** over the full 2016–2026 history (the "parity
  gate"), and live 1-contract canary drills against the real venue passed all seven
  tests — including a real nano-BTC round trip (maker fills both ways, ~$1.47 total
  fees), an exchange-accepted-and-verified backstop stop, and a kill-and-restart
  recovery with no duplicated orders. (One C0 gate — the daily digest firing 3
  consecutive days — structurally requires the live worker and rides the first days of
  C1.)
- **C1 — small-live (CURRENT — began 2026-07-09 ~18:05 UTC).** The strategy worker is
  live on a fresh server with real money, hard-clamped small: sizing treats equity as
  **min(actual, $1,500)**, max 2 BTC / 4 ETH contracts, every risk rule at full
  strictness. The first autonomous daily decision is 2026-07-10 00:05 UTC. To exit C1
  the system must run **at least 45 days** and pass seven pre-registered gates: modeled
  P&L matching the broker's statements within tolerance across ≥15 fills, stops verified
  resting on-venue for every position, clean crash-restart drills, 30 days without an
  unhandled error or missed decision, and measured slippage / fees / funding each within
  their modeled bounds.
- **C2 — full size.** Only after all C1 gates are green: effective equity steps to 50%
  for 30 days, then 100%. The automated cash-yield sweep turns on. Automatic demotion
  back to small size stays armed permanently (triggered by slippage drift, any
  reconciliation mismatch over $5, an unhandled crash, repeated weekly loss limits, or
  any detected venue fee/margin change).
- **Later:** a possible 2.5× scale-up decision no earlier than the 6-month live review.

Current facts worth knowing: capital is $6,000 of the operator's own funds ($1,500 at
the futures side per the C1 clamp, the rest in USDC earning rewards); the system is
effectively **BTC-only** until ETH trades above $2,000 (its own minimum-price rule); no
strategy losses or halts have occurred because the first autonomous trading decision
hasn't happened yet as of this writing.

---

## Glossary

Plain-English definitions of every term of art used above.

| Term | Meaning |
|---|---|
| **Long** | A position that profits if the price goes up. "Going long Bitcoin" = betting on a rise. |
| **Short** | A position that profits if the price goes down. Futures make this as easy as going long. |
| **Flat** | Holding no position at all. |
| **Future** | A contract whose value tracks an asset without owning it; you post a cash deposit (margin) and gains/losses settle against it. |
| **Perpetual-style future** | Our US-regulated hybrid: legally a future expiring Dec 2030, but tethered to the live crypto price by hourly funding payments, so it behaves like a never-expiring contract. |
| **Funding (funding rate)** | The small hourly payment between longs and shorts that keeps the futures price tied to the spot price. When the future trades rich, longs pay shorts; when cheap, the reverse. |
| **Spot price** | The price of the actual asset itself (e.g., one Bitcoin), as opposed to a derivative's price. |
| **Mark price** | The official reference price the exchange uses to value positions and margin moment to moment. |
| **Basis** | The gap between a future's price and the spot price. Funding exists to keep it small. |
| **Margin** | The cash deposit backing a futures position. If losses eat too far into it, the broker issues a margin call or force-closes positions. |
| **Liquidation** | The broker force-closing positions because margin ran too low. Our rules keep price at least 30% away from where that would happen. |
| **Leverage** | Controlling more market value than the cash posted. 2× leverage = $2 of exposure per $1 of equity. Our backtest average was 0.5×. |
| **Gross exposure** | The sum of all position sizes regardless of direction, as a multiple of equity. |
| **Notional (value)** | The full market value a position controls (contracts × contract size × price). |
| **Contract (nano)** | The unit traded: 0.01 Bitcoin or 0.10 Ether per contract — small denominations suited to a small account. |
| **Equity (E)** | Current account value, marked to market. |
| **SMA (simple moving average)** | The plain average of the closing price over the last N days — a smoothed trend line. |
| **EWMA (exponentially weighted moving average)** | An average that weights recent days more heavily; we use it to estimate current volatility. (An "EMA" of prices is the same idea.) |
| **Volatility (vol)** | How much the price typically swings, usually annualized. 60% annualized vol roughly means ±3% is an ordinary day. |
| **Volatility targeting** | Sizing positions inversely to volatility so the account's dollar risk stays roughly constant across calm and wild regimes. |
| **ATR (Average True Range)** | The typical size of one day's trading range; we set stop distances in ATR multiples so they adapt to conditions. |
| **Stop-loss (stop)** | A pre-committed exit if price moves against the position by a set amount. |
| **Stop-limit** | A stop that triggers a limit order; with a wide limit it behaves like a market-order stop. Ours rests at the exchange itself. |
| **Trend following / time-series momentum** | Betting that an asset's own recent multi-week/month direction continues. |
| **TrendScore** | Our three-vote trend measure per asset: −1, −⅓, +⅓, or +1. |
| **Hysteresis** | The two-consecutive-day confirmation required before the system reverses direction — noise insurance. |
| **Dead-band** | The "close enough" zone around the target position inside which the system refuses to trade — fee-churn insurance. |
| **Slippage** | The difference between the expected price and the actual fill price. Thin markets slip more. |
| **Basis point (bp)** | One hundredth of a percent. 18 bps = 0.18%. |
| **Round trip** | One full enter-and-exit of a position; our all-in cost budget is ~0.18% of traded value per round trip. |
| **Drawdown** | The decline from the account's peak value to a subsequent trough. A 50% drawdown halves the account. |
| **High-water mark** | The account's highest value to date; drawdowns are measured from it. |
| **CAGR** | Compound annual growth rate — the smoothed per-year return implied by start and end values. |
| **Sharpe ratio** | Return earned per unit of volatility taken; the standard risk-adjusted score. Roughly: below 0.5 weak, ~1 solid, 2+ exceptional (and rare, live). |
| **Backtest** | Running the strategy's exact rules against historical data to see how it would have done. Evidence, never a guarantee. |
| **Walk-forward** | A backtest run chronologically with parameters fixed in advance — no peeking at the future. |
| **Falsification criteria** | Pre-registered "do not deploy" conditions the backtest had to clear (all four passed). |
| **Parity gate** | The requirement that the live trading code reproduce the validated backtest trade-for-trade before being allowed to run. |
| **Kill switch / halt** | The machinery that stops trading — from "no new entries" up to "flatten everything and require a human to restart." |
| **Reconciliation** | The daily line-by-line comparison of the system's records against the broker's official records. |
| **Audit log (hash-chained)** | The append-only record of every system event, cryptographically linked so tampering is detectable. |
| **FCM** | Futures Commission Merchant — a regulated US futures broker; CFM's role for our account. |
| **CFM / CDE** | Coinbase Financial Markets (the regulated broker arm) / Coinbase Derivatives Exchange (the CFTC-designated exchange listing the contracts). |
| **USDC** | A fully-reserved digital dollar (1 USDC ≈ $1) usable at Coinbase; where the system's idle cash earns rewards. |
| **Maker / taker** | A *maker* order rests on the book waiting to be matched; a *taker* order executes immediately against a resting one. Makers typically pay less. |
| **IOC (immediate-or-cancel)** | An order that fills whatever it can instantly and cancels the rest. |

---

## Change log

| Date | Change | Reason |
|---|---|---|
| 2026-07-09 | Initial version. | Documents the Amendment B production profile as validated and built (spec + Amendments, REPORT.md, delta spec), status as of C1 small-live start. |
| 2026-07-09 (later) | Convalescent probation shortened: 3 clean UTC days (was 5), resume day counts, breach day never counts. | Operator amendment at C1 night one; decisions-log "C1 night one, CONVALESCENT amendment". |
| 2026-07-11 | False-positive halt adjudication added: from the convalescent state, the operator can graduate back to NORMAL immediately when the halt was caused by a system defect (web-only, re-auth gated, must cite the defect fix). | Operator directive after the 2026-07-11 phantom recon-break auto-halt; decisions-log "C1 night two". |
| 2026-07-18 | Hard-halt floor now measures **total capital** (futures USD equity + latest CBI-USDC capture ≤48h old), fail-safe fallback to USD-only when the USDC reading is missing/stale. Floor value unchanged at $1,500; sizing still reads USD only. | Amendment C after the 2026-07-17 decommission-floor halt (annual subscription charge against a zero-headroom USD-visible floor); decisions-log 2026-07-18 queued decision, option (b). |
| 2026-07-19 | Amendment C stale-reading window closed by an operator re-capture: `/cash-recapture` (Discord) or `POST /api/system/cash-capture` refreshes today's USDC reading immediately after a conversion. Gate B3's Binance-proxy funding comparison series now logs daily. | C1→C2 follow-up build (proxy funding logger + re-capture hook); decisions-log 2026-07-19 PR 1. |
