"""services/signal/crypto_trend.py — the crypto-perps trend signal engine.

Crypto-pivot C0-B3 (delta spec §3.3). Ports S1-S4 + the Amendment B
semantics from ``research/crypto_perps/backtest.py`` — the **reference
implementation** and locked backtest authority (delta spec §4/§6). The
C0 exit gate for this module is the golden parity test
(``tests/golden/test_crypto_trend_parity.py``): running this engine's
:func:`simulate` over the full recorded 2016-2026 bar history must
produce a trade list IDENTICAL to the reference's — same dates, same
assets, same integer contract deltas, same prices, same costs, bit-for-
bit equity. The same trust-bridge discipline the LEAN stack had.

Layout — three layers sharing one code path:

* :func:`compute_indicators` — S1 trend ensemble, S2 EWMA vol, S3
  vol-ratio, ATR(14) Wilder. **Verbatim pandas port** of the reference
  (same library, same calls) so indicator arithmetic can never diverge.
* :func:`compute_targets` + :func:`plan_delta` — the per-day decision:
  S3 vol-regime state machine, S1 hysteresis (Amendment B hold
  semantics), S4 funding gates, ETH min-price rule, stop lockout, §6
  vol-target sizing with the §7 per-trade risk cap taking precedence
  (REPORT F-4), per-asset/gross caps, integer rounding, dead-band +
  Amendment B band-edge rebalancing. This is the surface the live
  00:05 UTC daily decision (strategy worker, C0-B4/C1) calls.
* :func:`simulate` — the full event-loop replay (stops, MTM, funding,
  cash yield, halt, daily/weekly loss limits) used by the parity gate
  and the quarterly frozen-parameter re-validation (delta spec §4). It
  calls the SAME decision functions live trading uses.

**Float arithmetic, deliberately.** The parity mandate ("identical
integer-contract targets") requires this module to reproduce the
reference's float64 arithmetic exactly, so signal/sizing math runs on
floats (same as the numpy-based Higham repair in ``services/risk/
sizing.py``). [A05] still governs every boundary that touches money in
the platform: order quantities leave this module as integers, and
prices/equity enter the execution + DB layers as ``Decimal``. The float
domain is contained to this module + its replay harness.

**Funding input is parametric** (``params.funding_ann``), mirroring the
reference. Live S4 will substitute the trailing realized CDE funding
from the ``funding_rates`` table once gate B3 validates the capture —
that substitution is a C1 change to the *input*, not to this logic.

A02 BINDS — ``services/signal/**`` is on the forbidden whitelist;
``risk-review-approved`` required (delta spec §3.3: "correct and
intended").
A25 — structlog only. Signal parameters are FROZEN (locked decision:
signal knobs move only via falsify-and-replace; risk-preference knobs
only via amendment + decisions-log).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import structlog

if TYPE_CHECKING:
    import pandas as pd

log = structlog.get_logger()

#: The two-asset universe (strategy §3; locked). BTC gets 2/3 of gross
#: risk. Position sizing reads contract multipliers from runtime product
#: discovery in live trading; these constants mirror the reference
#: backtest's nano contract sizes for the simulation/parity path.
ASSETS: Final[tuple[str, ...]] = ("BTC", "ETH")
CONTRACT_MULT: Final[dict[str, float]] = {"BTC": 0.01, "ETH": 0.10}
DAYS_YEAR: Final[float] = 365.0


@dataclass(frozen=True)
class CryptoTrendParams:
    """Field-for-field mirror of the reference ``backtest.Params``.

    Defaults are the strategy-doc BASE profile (§4-§7 as written); the
    production profile is :data:`AMENDMENT_B_PARAMS`. Values are floats
    by design (see module docstring). The §3.4 ``parameter_sets`` seed
    is the operational source of truth for the risk-preference knobs;
    this dataclass is the computational one — the two are locked
    together by the parity + seed tests.
    """

    # S1 trend ensemble
    sma_fast: int = 100
    sma_slow: int = 200
    mom_lb: int = 20
    hysteresis_days: int = 2
    hysteresis_hold: bool = False  # Amendment B: True (hold through unconfirmed flip)
    # S2 vol estimate
    ewma_lambda: float = 0.94
    vol_floor: float = 0.20
    vol_cap: float = 1.50
    # S3 vol-regime filter
    volratio_slow_window: int = 90
    volratio_block: float = 2.0
    volratio_resume: float = 1.5
    # S4 funding gate (annualized thresholds)
    funding_long_veto: float = 0.30
    funding_short_gate: float = -0.10
    # sizing (§6)
    v_target: float = 0.40
    w_btc: float = 2.0 / 3.0
    per_asset_cap: float = 1.4
    gross_cap: float = 2.0
    deadband_abs: float = 200.0
    deadband_frac: float = 0.05
    band_edge_rebalance: bool = False  # Amendment B: True
    cash_yield_ann: float = 0.0
    margin_frac: float = 0.25
    subscription_usd_month: float = 0.0
    # stops (§5)
    atr_window: int = 14
    client_stop_atr: float = 2.0
    lockout_days: int = 2
    # risk framework (§7)
    per_trade_risk_frac: float = 0.025
    daily_loss_limit: float = -0.04
    weekly_loss_limit: float = -0.08
    weekly_penalty_days: int = 7
    halt_frac: float = 0.50
    dd_tiers: tuple[tuple[float, float], ...] = (
        (0.10, 1.0),
        (0.20, 0.6),
        (0.35, 0.35),
        (9.9, 0.2),
    )
    eth_min_price: float = 2000.0
    # costs (§1.5) — per SIDE
    fee_bps: float = 5.0
    slip_bps: float = 4.0
    min_fee_per_contract: float = 0.20
    cost_mult: float = 1.0
    # parametric funding model: annualized, longs pay / shorts receive
    funding_ann: float = 0.1095
    initial_equity: float = 6000.0


#: The PRODUCTION profile (Amendment B, operator-locked 2026-07-08):
#: Amendment A risk knobs (2x base; halt 25%) + hysteresis-hold +
#: dd-tiers removed + band-edge rebalancing + cash-yield economics
#: (Coinbase One Basic 3.5% minus $4.99/mo). Mirrors the reference's
#: ``production`` scenario EXACTLY — the parity gate runs both base and
#: production profiles.
AMENDMENT_B_PARAMS: Final[CryptoTrendParams] = replace(
    CryptoTrendParams(),
    v_target=0.80,
    per_trade_risk_frac=0.05,
    daily_loss_limit=-0.08,
    weekly_loss_limit=-0.16,
    gross_cap=3.0,
    halt_frac=0.25,
    dd_tiers=((9.9, 1.0),),
    hysteresis_hold=True,
    band_edge_rebalance=True,
    cash_yield_ann=0.035,
    subscription_usd_month=4.99,
)


def compute_indicators(df: pd.DataFrame, p: CryptoTrendParams) -> pd.DataFrame:
    """S1/S2/S3 indicator columns from a daily OHLC frame.

    VERBATIM port of the reference's ``compute_indicators`` — same
    pandas calls in the same order, so the arithmetic is identical by
    construction. Input frame: index = session date, columns
    ``open/high/low/close`` (floats).
    """
    import pandas as pd  # runtime dep; local import keeps module import light

    out = df.copy()
    c = out["close"]
    out["ret"] = np.log(c / c.shift(1))
    out["sma_fast"] = c.rolling(p.sma_fast).mean()
    out["sma_slow"] = c.rolling(p.sma_slow).mean()
    out["mom"] = out["ret"].rolling(p.mom_lb).sum()

    # EWMA variance of daily returns (RiskMetrics), seeded on first 30 obs
    r = out["ret"].fillna(0.0).to_numpy()
    var = np.full(len(r), np.nan)
    seed_n = 30
    if len(r) > seed_n:
        v = float(np.nanvar(r[1 : seed_n + 1]))
        var[seed_n] = v
        lam = p.ewma_lambda
        for i in range(seed_n + 1, len(r)):
            v = lam * v + (1 - lam) * r[i] ** 2
            var[i] = v
    out["sigma_ann_raw"] = np.sqrt(var) * math.sqrt(DAYS_YEAR)
    out["sigma_ann"] = out["sigma_ann_raw"].clip(p.vol_floor, p.vol_cap)

    out["vol_slow"] = out["ret"].rolling(p.volratio_slow_window).std() * math.sqrt(DAYS_YEAR)
    out["vol_ratio"] = out["sigma_ann_raw"] / out["vol_slow"]

    # ATR(14) Wilder
    hl = out["high"] - out["low"]
    hc = (out["high"] - c.shift(1)).abs()
    lc = (out["low"] - c.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1.0 / p.atr_window, adjust=False).mean()
    out["atrp"] = out["atr"] / c

    # S1 trend score in {-1, -1/3, +1/3, +1}
    s_a = np.where(c > out["sma_fast"], 1.0, -1.0)
    s_b = np.where(c > out["sma_slow"], 1.0, -1.0)
    s_c = np.where(out["mom"] > 0, 1.0, -1.0)
    out["trend"] = (s_a + s_b + s_c) / 3.0
    out.loc[
        out[["sma_slow", "mom", "sigma_ann", "vol_slow", "atr"]].isna().any(axis=1), "trend"
    ] = np.nan
    return out


@dataclass
class AssetState:
    """Per-asset decision state — mirror of the reference ``AssetState``.

    Live trading persists/reconstructs this per §7 restart rules; the
    replay evolves it in-memory exactly as the reference does.
    """

    contracts: float = 0.0  # signed integer count (stored as float)
    entry_vwap: float = 0.0
    stop_level: float = math.nan  # client 2xATR stop
    applied_dir: int = 0
    pending_dir: int = 0
    pending_count: int = 0
    lockout_until: int = -1  # bar index; same-direction re-entry block
    lockout_dir: int = 0
    vol_blocked: bool = False  # S3 state
    stopped_today: bool = False


@dataclass(frozen=True)
class AssetDecisionRow:
    """One asset's indicator row at decision time (floats; NaN allowed)."""

    trend: float
    vol_ratio: float
    sigma_ann: float
    atrp: float
    close: float


def trade_cost(delta_contracts: float, price: float, mult: float, p: CryptoTrendParams) -> float:
    """One-side cost for changing position by delta_contracts at price.

    Verbatim reference port (§1.5 cost model).
    """
    n = abs(delta_contracts)
    if n == 0:
        return 0.0
    notional = n * mult * price
    fee = max(p.fee_bps / 1e4 * notional, p.min_fee_per_contract * n)
    slip = p.slip_bps / 1e4 * notional
    return (fee + slip) * p.cost_mult


def compute_targets(
    *,
    bar_index: int,
    rows: dict[str, AssetDecisionRow],
    states: dict[str, AssetState],
    equity: float,
    v_target: float,
    dd_mult: float,
    p: CryptoTrendParams,
    trace: dict[str, Any] | None = None,
) -> dict[str, float]:
    """The S1-S4 + §6/§7 per-day target computation (integer contracts).

    Verbatim port of the reference decision step (its "step 6" per-asset
    block + the portfolio gross cap). MUTATES ``states`` exactly as the
    reference does (S3 block latch, hysteresis pending counters,
    applied direction). ``v_target`` already carries the weekly-loss
    halving; ``dd_mult`` carries the drawdown tier (≡ 1.0 under
    Amendment B). Returns signed integer-valued contract targets.

    ``trace`` (optional, 2026-07-20 agentic-refinement capture): when a
    dict is supplied, each asset's WHICH-RULE-DECIDED label is recorded
    into it (``trace[asset] = {"gate": ..., ...}``; a portfolio-level
    gross-cap rescale records ``trace["gross_cap_scale"]``). PURE
    telemetry: the numeric path is byte-identical with or without it
    (the parity gate runs with ``trace=None``), and every recorded
    value is a JSON-serializable primitive so the strategy worker can
    embed it in the ``strategy_decisions.outcome`` payload verbatim.
    """
    targets: dict[str, float] = {}
    for s in ASSETS:
        st = states[s]
        row = rows[s]
        mult = CONTRACT_MULT[s]
        if math.isnan(row.trend):
            targets[s] = st.contracts  # no signal yet: hold
            if trace is not None:
                trace[s] = {"gate": "no_signal_yet"}
            continue

        # S3 vol-regime state machine
        vr = row.vol_ratio
        if not math.isnan(vr):
            if st.vol_blocked and vr < p.volratio_resume:
                st.vol_blocked = False
            elif not st.vol_blocked and vr > p.volratio_block:
                st.vol_blocked = True

        # hysteresis on direction
        raw_dir = 1 if row.trend > 0 else -1
        if st.stopped_today:
            targets[s] = 0.0
            if trace is not None:
                trace[s] = {"gate": "stopped_today_flat"}
            continue
        if raw_dir != st.applied_dir:
            if raw_dir == st.pending_dir:
                st.pending_count += 1
            else:
                st.pending_dir = raw_dir
                st.pending_count = 1
            if st.pending_count >= p.hysteresis_days:
                st.applied_dir = raw_dir
                st.pending_dir = 0
                st.pending_count = 0
            else:
                targets[s] = st.contracts if p.hysteresis_hold else 0.0
                if trace is not None:
                    trace[s] = {
                        "gate": "hysteresis_pending",
                        "pending_dir": st.pending_dir,
                        "pending_count": st.pending_count,
                        "held_position": bool(p.hysteresis_hold),
                    }
                continue
        else:
            st.pending_dir = 0
            st.pending_count = 0

        d = st.applied_dir
        # S4 short gate: shorts only at full-strength bearish score
        if d < 0 and (row.trend > -0.99 or p.funding_ann < p.funding_short_gate):
            targets[s] = 0.0
            if trace is not None:
                trace[s] = {"gate": "short_gate_veto"}
            continue
        # funding long veto (parametric funding: fires only in high-funding scenarios)
        funding_mult = 0.5 if (d > 0 and p.funding_ann > p.funding_long_veto) else 1.0
        # ETH minimum-price rule
        if s == "ETH" and row.close < p.eth_min_price:
            targets[s] = 0.0
            if trace is not None:
                trace[s] = {"gate": "eth_min_price_veto"}
            continue
        # lockout after stop
        if bar_index < st.lockout_until and d == st.lockout_dir:
            targets[s] = st.contracts if (st.contracts != 0) else 0.0
            if trace is not None:
                trace[s] = {"gate": "lockout_hold"}
            continue

        strength = abs(row.trend)
        w = p.w_btc if s == "BTC" else 1.0 - p.w_btc
        notional = equity * v_target * w * strength / row.sigma_ann * dd_mult * funding_mult
        # per-trade risk cap (§7 precedence over §6 vol-target, REPORT F-4)
        max_notional = p.per_trade_risk_frac * equity / (p.client_stop_atr * row.atrp)
        notional = min(notional, max_notional, p.per_asset_cap * equity)
        n: float = math.floor(notional / (mult * row.close) + 0.5) * d
        # S3: blocked = no increase in |position|; existing halved
        if st.vol_blocked:
            cur = st.contracts
            half = math.floor(abs(cur) / 2.0) * (1 if cur > 0 else -1)
            n = half if cur != 0 else 0.0
            if trace is not None:
                trace[s] = {"gate": "vol_blocked", "halved_from": cur}
        elif trace is not None:
            # Which of the three §6/§7 constraints set the notional
            # (recomputed read-only from in-scope floats — no effect on
            # the verbatim sizing lines above; ties label the stricter).
            vol_target_notional = (
                equity * v_target * w * strength / row.sigma_ann * dd_mult * funding_mult
            )
            asset_cap_notional = p.per_asset_cap * equity
            if max_notional <= min(vol_target_notional, asset_cap_notional):
                binding = "per_trade_risk_cap"
            elif asset_cap_notional < min(vol_target_notional, max_notional):
                binding = "per_asset_cap"
            else:
                binding = "vol_target"
            trace[s] = {
                "gate": "sized",
                "strength": strength,
                "funding_mult": funding_mult,
                "binding_constraint": binding,
            }
        targets[s] = float(n)

    # portfolio gross cap
    gross = sum(
        abs(targets[s]) * CONTRACT_MULT[s] * rows[s].close
        for s in ASSETS
        if not math.isnan(rows[s].close)
    )
    if gross > p.gross_cap * equity and gross > 0:
        scale = p.gross_cap * equity / gross
        for s in ASSETS:
            t = targets[s] * scale
            targets[s] = float(math.floor(abs(t)) * (1 if t > 0 else -1))
        if trace is not None:
            trace["gross_cap_scale"] = scale
    return targets


def plan_delta(
    *,
    target: float,
    current_contracts: float,
    close: float,
    mult: float,
    equity: float,
    p: CryptoTrendParams,
) -> float:
    """Dead-band + Amendment B band-edge delta planning for ONE asset.

    Verbatim port of the reference's per-asset delta block (§5 rule 1
    dead-band; Amendment B band-edge rebalancing). Returns the signed
    contract delta to execute — ``0.0`` means no trade. Pure (no state
    mutation): the caller applies fills/costs.
    """
    delta = target - current_contracts
    if delta == 0:
        return 0.0
    # dead-band applies to rebalances, not to full exits/entries from flat
    is_exit = target == 0 or current_contracts == 0 or (target * current_contracts < 0)
    band_notional = max(p.deadband_abs, p.deadband_frac * equity)
    if not is_exit and abs(delta) * mult * close < band_notional:
        return 0.0
    if not is_exit and p.band_edge_rebalance:
        # rebalance only to the nearest edge of the no-trade band
        band_n = math.floor(band_notional / (mult * close))
        if band_n >= 1:
            edge = target - band_n if delta > 0 else target + band_n
            if (delta > 0 and edge > current_contracts) or (delta < 0 and edge < current_contracts):
                delta = edge - current_contracts
    return delta


@dataclass(frozen=True)
class SimulatedTrade:
    """One position change in the replay (mirror of the reference tuple)."""

    date: str
    asset: str
    delta: float
    price: float
    cost: float
    reason: str  # "signal" / "stop" / "halt" / "daily_loss"

    def as_tuple(self) -> tuple[str, str, float, float, float, str]:
        return (self.date, self.asset, self.delta, self.price, self.cost, self.reason)


@dataclass
class SimulationResult:
    """Replay output consumed by the parity gate + quarterly re-validation."""

    trades: list[SimulatedTrade]
    equity_curve: list[tuple[Any, float]]  # (timestamp, equity)
    final_equity: float
    total_costs: float
    total_funding: float
    halted: bool
    daily_targets: list[tuple[Any, dict[str, float]]] = field(default_factory=list)


def simulate(
    data: dict[str, pd.DataFrame],
    p: CryptoTrendParams,
    start: str,
    end: str,
) -> SimulationResult:
    """Full event-loop replay on the union calendar of both assets.

    Verbatim port of the reference ``run_backtest`` with the decision
    step delegated to :func:`compute_targets` / :func:`plan_delta` —
    the exact functions live trading calls. Used by the golden parity
    gate and the quarterly frozen-parameter re-validation; NOT part of
    the live decision path itself.

    ``data`` maps asset → indicator frame from :func:`compute_indicators`.
    """
    import pandas as pd

    idx = data["BTC"].index
    for sym in ASSETS[1:]:
        idx = idx.union(data[sym].index)
    idx = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]

    frames = {s: data[s].reindex(idx) for s in ASSETS}
    E = p.initial_equity
    hwm = E
    halted = False
    pause_until = -1  # daily-loss 24h pause (skip decisions through this bar)
    weekly_mult_until = -1  # bar index until which v_target is halved
    states = {s: AssetState() for s in ASSETS}

    equity_curve: list[tuple[Any, float]] = []
    trades: list[SimulatedTrade] = []
    daily_targets: list[tuple[Any, dict[str, float]]] = []
    total_costs = 0.0
    total_funding = 0.0

    equity_prev_close = E
    week_equity: list[float] = []  # trailing closes for 7d loss rule

    for i, ts in enumerate(idx):
        if halted:
            equity_curve.append((ts, E))
            continue

        # ---- 1) intraday stop checks (positions carried into day i) ----
        for s in ASSETS:
            st = states[s]
            st.stopped_today = False
            row = frames[s].loc[ts]
            if st.contracts == 0 or np.isnan(row["low"]) or np.isnan(st.stop_level):
                continue
            mult = CONTRACT_MULT[s]
            prev_close = frames[s]["close"].iloc[i - 1] if i > 0 else row["open"]
            hit = None
            if st.contracts > 0 and row["low"] <= st.stop_level:
                hit = min(row["open"], st.stop_level)
            elif st.contracts < 0 and row["high"] >= st.stop_level:
                hit = max(row["open"], st.stop_level)
            if hit is not None:
                pnl = st.contracts * mult * (hit - prev_close)
                cost = trade_cost(st.contracts, hit, mult, p)
                E += pnl - cost
                total_costs += cost
                trades.append(SimulatedTrade(str(ts.date()), s, -st.contracts, hit, cost, "stop"))
                st.lockout_until = i + p.lockout_days
                st.lockout_dir = 1 if st.contracts > 0 else -1
                st.contracts = 0.0
                st.stop_level = math.nan
                st.applied_dir = 0
                st.pending_dir = 0
                st.pending_count = 0
                st.stopped_today = True

        # ---- 2) mark-to-market at close + funding ----
        for s in ASSETS:
            st = states[s]
            row = frames[s].loc[ts]
            if st.contracts == 0 or np.isnan(row["close"]):
                continue
            mult = CONTRACT_MULT[s]
            prev_close = frames[s]["close"].iloc[i - 1] if i > 0 else row["open"]
            E += st.contracts * mult * (row["close"] - prev_close)
            # funding: longs pay positive rate, shorts receive
            f = st.contracts * mult * row["close"] * (p.funding_ann / DAYS_YEAR)
            E -= f
            total_funding += f

        # ---- 2b) yield on cash not tied up as futures margin ----
        if p.cash_yield_ann > 0:
            gross_held = sum(
                abs(states[s].contracts) * CONTRACT_MULT[s] * frames[s]["close"].loc[ts]
                for s in ASSETS
                if not np.isnan(frames[s]["close"].loc[ts])
            )
            E += (p.cash_yield_ann / DAYS_YEAR) * max(0.0, E - p.margin_frac * gross_held)
            E -= p.subscription_usd_month * 12.0 / DAYS_YEAR

        # ---- 3) hard halt (or bust: equity wiped even with halt disabled) ----
        if (p.halt_frac > 0 and E <= p.halt_frac * p.initial_equity) or E <= 0:
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if st.contracts != 0 and not np.isnan(row["close"]):
                    cost = trade_cost(st.contracts, row["close"], CONTRACT_MULT[s], p)
                    E -= cost
                    total_costs += cost
                    trades.append(
                        SimulatedTrade(str(ts.date()), s, -st.contracts, row["close"], cost, "halt")
                    )
                    st.contracts = 0.0
            halted = True
            equity_curve.append((ts, E))
            continue

        # ---- 4) daily loss limit (close-to-close approximation) ----
        skip_decision = i <= pause_until
        if (E / equity_prev_close - 1.0) < p.daily_loss_limit:
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if st.contracts != 0 and not np.isnan(row["close"]):
                    cost = trade_cost(st.contracts, row["close"], CONTRACT_MULT[s], p)
                    E -= cost
                    total_costs += cost
                    trades.append(
                        SimulatedTrade(
                            str(ts.date()), s, -st.contracts, row["close"], cost, "daily_loss"
                        )
                    )
                    st.contracts = 0.0
                    st.stop_level = math.nan
                    st.applied_dir = 0
            pause_until = i + 1  # skip next decision (24h pause)
            skip_decision = True

        # ---- 5) weekly loss limit / drawdown tier / HWM ----
        week_equity.append(E)
        if len(week_equity) > 8:
            week_equity.pop(0)
        if len(week_equity) == 8 and (E / week_equity[0] - 1.0) < p.weekly_loss_limit:
            weekly_mult_until = i + p.weekly_penalty_days
        hwm = max(hwm, E)
        dd = 1.0 - E / hwm
        dd_mult = next(m for lvl, m in p.dd_tiers if dd <= lvl)
        v_target = p.v_target * (0.5 if i <= weekly_mult_until else 1.0)

        # ---- 6) decision at close (00:05 UTC next day, approximated at close) ----
        if not skip_decision:
            rows = {
                s: AssetDecisionRow(
                    trend=float(frames[s]["trend"].loc[ts]),
                    vol_ratio=float(frames[s]["vol_ratio"].loc[ts]),
                    sigma_ann=float(frames[s]["sigma_ann"].loc[ts]),
                    atrp=float(frames[s]["atrp"].loc[ts]),
                    close=float(frames[s]["close"].loc[ts]),
                )
                for s in ASSETS
            }
            targets = compute_targets(
                bar_index=i,
                rows=rows,
                states=states,
                equity=E,
                v_target=v_target,
                dd_mult=dd_mult,
                p=p,
            )
            daily_targets.append((ts, dict(targets)))

            # execute deltas with dead-band (+ Amendment B band-edge)
            for s in ASSETS:
                st = states[s]
                row = rows[s]
                if math.isnan(row.close):
                    continue
                mult = CONTRACT_MULT[s]
                delta = plan_delta(
                    target=targets[s],
                    current_contracts=st.contracts,
                    close=row.close,
                    mult=mult,
                    equity=E,
                    p=p,
                )
                if delta == 0:
                    continue
                cost = trade_cost(delta, row.close, mult, p)
                E -= cost
                total_costs += cost
                trades.append(SimulatedTrade(str(ts.date()), s, delta, row.close, cost, "signal"))
                new_pos = st.contracts + delta
                if new_pos != 0 and (
                    st.contracts == 0
                    or st.contracts * new_pos < 0
                    or abs(new_pos) > abs(st.contracts)
                ):
                    # position opened/flipped/expanded: reset entry vwap + stops
                    if st.contracts * new_pos <= 0:
                        st.entry_vwap = row.close
                    else:
                        add = abs(delta)
                        st.entry_vwap = (st.entry_vwap * abs(st.contracts) + row.close * add) / abs(
                            new_pos
                        )
                    sign = 1 if new_pos > 0 else -1
                    st.stop_level = st.entry_vwap * (1 - sign * p.client_stop_atr * row.atrp)
                st.contracts = new_pos
                if new_pos == 0:
                    st.stop_level = math.nan

        equity_prev_close = E
        equity_curve.append((ts, E))

    return SimulationResult(
        trades=trades,
        equity_curve=equity_curve,
        final_equity=E,
        total_costs=total_costs,
        total_funding=total_funding,
        halted=halted,
        daily_targets=daily_targets,
    )


__all__ = [
    "AMENDMENT_B_PARAMS",
    "ASSETS",
    "CONTRACT_MULT",
    "DAYS_YEAR",
    "AssetDecisionRow",
    "AssetState",
    "CryptoTrendParams",
    "SimulatedTrade",
    "SimulationResult",
    "compute_indicators",
    "compute_targets",
    "plan_delta",
    "simulate",
    "trade_cost",
]
