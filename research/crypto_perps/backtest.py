"""Standalone backtest for the Coinbase CFM crypto perps strategy.

Implements Docs/crypto-perps-strategy.md sections 4-7 with frozen parameters,
on daily spot OHLCV (FMP BTCUSD/ETHUSD) as the perp proxy, per the section 9
protocol. Funding is parametric (no free CDE/Binance funding history was
reachable from this environment) — see REPORT.md "Data provenance & limitations".

Run:  python3 research/crypto_perps/backtest.py            # full scenario suite
      python3 research/crypto_perps/backtest.py --base     # base scenario only
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "results.json")

ASSETS = ("BTC", "ETH")
CONTRACT_MULT = {"BTC": 0.01, "ETH": 0.10}  # nano contract size in coins
DAYS_YEAR = 365.0


@dataclass(frozen=True)
class Params:
    # S1 trend ensemble
    sma_fast: int = 100
    sma_slow: int = 200
    mom_lb: int = 20
    hysteresis_days: int = 2
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
    # sizing (section 6)
    v_target: float = 0.40
    w_btc: float = 2.0 / 3.0
    per_asset_cap: float = 1.4
    gross_cap: float = 2.0
    deadband_abs: float = 200.0
    deadband_frac: float = 0.05
    # stops (section 5)
    atr_window: int = 14
    client_stop_atr: float = 2.0
    lockout_days: int = 2
    # risk framework (section 7)
    per_trade_risk_frac: float = 0.025
    daily_loss_limit: float = -0.04
    weekly_loss_limit: float = -0.08
    weekly_penalty_days: int = 7
    halt_frac: float = 0.50  # <= 0 disables the drawdown halt (bust at E<=0 still stops)
    dd_tiers: tuple = ((0.10, 1.0), (0.20, 0.6), (0.35, 0.35), (9.9, 0.2))
    eth_min_price: float = 2000.0
    # costs (section 1.5) — per SIDE
    fee_bps: float = 5.0
    slip_bps: float = 4.0
    min_fee_per_contract: float = 0.20
    cost_mult: float = 1.0
    # parametric funding model: annualized rate, longs pay / shorts receive
    funding_ann: float = 0.1095  # 0.01% per 8h equivalent, long-run perp mode
    initial_equity: float = 6000.0


def load_asset(symbol: str) -> pd.DataFrame:
    """Combine the agent-fetched part CSVs for one asset; sort, dedupe."""
    parts = []
    for i in (1, 2, 3):
        path = os.path.join(DATA_DIR, f"{symbol.lower()}usd_part{i}.csv")
        if os.path.exists(path):
            parts.append(pd.read_csv(path))
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date").sort_values("date").set_index("date")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="raise")
    return df[["open", "high", "low", "close"]]


def compute_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
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
    out.loc[out[["sma_slow", "mom", "sigma_ann", "vol_slow", "atr"]].isna().any(axis=1), "trend"] = np.nan
    return out


@dataclass
class AssetState:
    contracts: float = 0.0          # signed integer count (stored as float)
    entry_vwap: float = 0.0
    stop_level: float = np.nan      # client 2xATR stop
    applied_dir: int = 0            # direction after hysteresis
    pending_dir: int = 0            # unconfirmed new direction
    pending_count: int = 0
    lockout_until: int = -1         # bar index; same-direction re-entry block
    lockout_dir: int = 0
    vol_blocked: bool = False       # S3 state
    stopped_today: bool = False


def trade_cost(delta_contracts: float, price: float, mult: float, p: Params) -> float:
    """One-side cost for changing position by delta_contracts at price."""
    n = abs(delta_contracts)
    if n == 0:
        return 0.0
    notional = n * mult * price
    fee = max(p.fee_bps / 1e4 * notional, p.min_fee_per_contract * n)
    slip = p.slip_bps / 1e4 * notional
    return (fee + slip) * p.cost_mult


def run_backtest(data: dict[str, pd.DataFrame], p: Params, start: str, end: str) -> dict:
    """Event loop on the union calendar of both assets. Returns metrics + curves."""
    idx = data["BTC"].index
    for sym in ASSETS[1:]:
        idx = idx.union(data[sym].index)
    idx = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]

    frames = {s: data[s].reindex(idx) for s in ASSETS}
    E = p.initial_equity
    hwm = E
    halted = False
    pause_until = -1        # daily-loss 24h pause (skip decisions through this bar)
    weekly_mult_until = -1  # bar index until which v_target is halved
    states = {s: AssetState() for s in ASSETS}

    equity_curve = []
    trades = []             # (date, asset, delta, price, cost, reason)
    total_costs = 0.0
    total_funding = 0.0
    gross_expo = []

    equity_prev_close = E
    week_equity = []        # trailing closes for 7d loss rule

    for i, ts in enumerate(idx):
        if halted:
            equity_curve.append((ts, E))
            continue
        day_realized = 0.0

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
                day_realized += pnl - cost
                total_costs += cost
                trades.append((str(ts.date()), s, -st.contracts, hit, cost, "stop"))
                st.lockout_until = i + p.lockout_days
                st.lockout_dir = 1 if st.contracts > 0 else -1
                st.contracts = 0.0
                st.stop_level = np.nan
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

        # ---- 3) hard halt (or bust: equity wiped even with halt disabled) ----
        if (p.halt_frac > 0 and E <= p.halt_frac * p.initial_equity) or E <= 0:
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if st.contracts != 0 and not np.isnan(row["close"]):
                    cost = trade_cost(st.contracts, row["close"], CONTRACT_MULT[s], p)
                    E -= cost
                    total_costs += cost
                    trades.append((str(ts.date()), s, -st.contracts, row["close"], cost, "halt"))
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
                    trades.append((str(ts.date()), s, -st.contracts, row["close"], cost, "daily_loss"))
                    st.contracts = 0.0
                    st.stop_level = np.nan
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
            # compute integer targets per asset
            targets = {}
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                mult = CONTRACT_MULT[s]
                if np.isnan(row.get("trend", np.nan)):
                    targets[s] = st.contracts  # no signal yet: hold
                    continue

                # S3 vol-regime state machine
                vr = row["vol_ratio"]
                if not np.isnan(vr):
                    if st.vol_blocked and vr < p.volratio_resume:
                        st.vol_blocked = False
                    elif not st.vol_blocked and vr > p.volratio_block:
                        st.vol_blocked = True

                # hysteresis on direction
                raw_dir = 1 if row["trend"] > 0 else -1
                if st.stopped_today:
                    targets[s] = 0.0
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
                        targets[s] = 0.0  # unconfirmed flip: go flat
                        continue
                else:
                    st.pending_dir = 0
                    st.pending_count = 0

                d = st.applied_dir
                # S4 short gate: shorts only at full-strength bearish score
                if d < 0 and (row["trend"] > -0.99 or p.funding_ann < p.funding_short_gate):
                    targets[s] = 0.0
                    continue
                # funding long veto (parametric funding: fires only in high-funding scenarios)
                funding_mult = 0.5 if (d > 0 and p.funding_ann > p.funding_long_veto) else 1.0
                # ETH minimum-price rule
                if s == "ETH" and row["close"] < p.eth_min_price:
                    targets[s] = 0.0
                    continue
                # lockout after stop
                if i < states[s].lockout_until and d == st.lockout_dir:
                    targets[s] = st.contracts if (st.contracts != 0) else 0.0
                    continue

                strength = abs(row["trend"])
                w = p.w_btc if s == "BTC" else 1.0 - p.w_btc
                notional = E * v_target * w * strength / row["sigma_ann"] * dd_mult * funding_mult
                # per-trade risk cap: notional * 2*ATRp <= 2.5% E
                max_notional = p.per_trade_risk_frac * E / (p.client_stop_atr * row["atrp"])
                notional = min(notional, max_notional, p.per_asset_cap * E)
                n = math.floor(notional / (mult * row["close"]) + 0.5) * d
                # S3: blocked = no increase in |position|; existing halved
                if st.vol_blocked:
                    cur = st.contracts
                    half = math.floor(abs(cur) / 2.0) * (1 if cur > 0 else -1)
                    n = half if cur != 0 else 0.0
                targets[s] = float(n)

            # portfolio gross cap
            gross = sum(
                abs(targets[s]) * CONTRACT_MULT[s] * frames[s]["close"].loc[ts]
                for s in ASSETS
                if not np.isnan(frames[s]["close"].loc[ts])
            )
            if gross > p.gross_cap * E and gross > 0:
                scale = p.gross_cap * E / gross
                for s in ASSETS:
                    t = targets[s] * scale
                    targets[s] = float(math.floor(abs(t)) * (1 if t > 0 else -1))

            # execute deltas with dead-band
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if np.isnan(row["close"]):
                    continue
                mult = CONTRACT_MULT[s]
                delta = targets[s] - st.contracts
                if delta == 0:
                    continue
                # dead-band applies to rebalances, not to full exits/entries from flat
                is_exit = targets[s] == 0 or st.contracts == 0 or (targets[s] * st.contracts < 0)
                if not is_exit and abs(delta) * mult * row["close"] < max(p.deadband_abs, p.deadband_frac * E):
                    continue
                cost = trade_cost(delta, row["close"], mult, p)
                E -= cost
                total_costs += cost
                trades.append((str(ts.date()), s, delta, row["close"], cost, "signal"))
                new_pos = st.contracts + delta
                if new_pos != 0 and (st.contracts == 0 or st.contracts * new_pos < 0 or abs(new_pos) > abs(st.contracts)):
                    # position opened/flipped/expanded: reset entry vwap + stops
                    if st.contracts * new_pos <= 0:
                        st.entry_vwap = row["close"]
                    else:
                        add = abs(delta)
                        st.entry_vwap = (st.entry_vwap * abs(st.contracts) + row["close"] * add) / abs(new_pos)
                    sign = 1 if new_pos > 0 else -1
                    st.stop_level = st.entry_vwap * (1 - sign * p.client_stop_atr * row["atrp"])
                st.contracts = new_pos
                if new_pos == 0:
                    st.stop_level = np.nan
                    if targets[s] == 0 and st.applied_dir != 0 and (delta * st.applied_dir < 0):
                        pass  # applied_dir persists; flat via gates keeps direction state

        gross_now = sum(
            abs(states[s].contracts) * CONTRACT_MULT[s] * frames[s]["close"].loc[ts]
            for s in ASSETS
            if not np.isnan(frames[s]["close"].loc[ts])
        )
        gross_expo.append(gross_now / E if E > 0 else 0.0)
        assert gross_now <= p.gross_cap * E * 1.10 + 1e-6, f"leverage cap breach at {ts}"
        equity_prev_close = E
        equity_curve.append((ts, E))

    curve = pd.Series({ts: e for ts, e in equity_curve}).sort_index()
    return summarize(curve, trades, total_costs, total_funding, gross_expo, halted, p)


def summarize(curve, trades, total_costs, total_funding, gross_expo, halted, p) -> dict:
    rets = curve.pct_change().dropna()
    def sharpe(x):
        return float(x.mean() / x.std() * math.sqrt(DAYS_YEAR)) if len(x) > 1 and x.std() > 0 else 0.0
    def maxdd(c):
        return float((1 - c / c.cummax()).max())
    years = sorted(set(curve.index.year))
    per_year = {}
    for y in years:
        c = curve[curve.index.year == y]
        r = c.pct_change().dropna()
        if len(r) < 30:
            continue
        per_year[str(y)] = {
            "return": float(c.iloc[-1] / c.iloc[0] - 1),
            "sharpe": round(sharpe(r), 3),
            "maxdd": round(maxdd(c), 4),
        }
    sub = curve[curve.index >= "2023-01-01"]
    n_trades = len(trades)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t[5]] = reasons.get(t[5], 0) + 1
    yrs_span = (curve.index[-1] - curve.index[0]).days / DAYS_YEAR
    return {
        "final_equity": round(float(curve.iloc[-1]), 2),
        "total_return": round(float(curve.iloc[-1] / curve.iloc[0] - 1), 4),
        "cagr": round(float((curve.iloc[-1] / curve.iloc[0]) ** (1 / yrs_span) - 1), 4),
        "sharpe": round(sharpe(rets), 3),
        "maxdd": round(maxdd(curve), 4),
        "sharpe_2023on": round(sharpe(sub.pct_change().dropna()), 3),
        "return_2023on": round(float(sub.iloc[-1] / sub.iloc[0] - 1), 4) if len(sub) else None,
        "worst_year_dd": round(max(v["maxdd"] for v in per_year.values()), 4),
        "per_year": per_year,
        "n_position_changes": n_trades,
        "trade_reasons": reasons,
        "trades_per_year": round(n_trades / yrs_span, 1),
        "total_costs": round(total_costs, 2),
        "total_funding_paid": round(total_funding, 2),
        "avg_gross_leverage": round(float(np.mean(gross_expo)), 3),
        "max_gross_leverage": round(float(np.max(gross_expo)), 3),
        "pct_days_in_market": round(float(np.mean(np.array(gross_expo) > 0)), 3),
        "halted": halted,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", action="store_true", help="run base scenario only")
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-06-30")
    args = ap.parse_args()

    base = Params()
    data = {s: compute_indicators(load_asset(s), base) for s in ASSETS}

    # Amendment A (2026-07-08, operator-directed): "2x profile" — every risk
    # knob scaled coherently, drawdown halt moved 50% -> 75% (malfunction
    # circuit-breaker only). aggr_nohalt quantifies removing the halt entirely.
    aggr = replace(
        base, v_target=0.80, per_trade_risk_frac=0.05,
        daily_loss_limit=-0.08, weekly_loss_limit=-0.16, gross_cap=3.0,
        dd_tiers=((0.20, 1.0), (0.40, 0.6), (0.70, 0.35), (9.9, 0.2)),
        halt_frac=0.25,
    )

    scenarios = {"base": base}
    if not args.base:
        scenarios["aggr_halt25"] = aggr
        scenarios["aggr_nohalt"] = replace(aggr, halt_frac=0.0)
        scenarios["aggr_2xcost"] = replace(aggr, cost_mult=2.0)
        scenarios["aggr_fund2x"] = replace(aggr, funding_ann=2 * base.funding_ann)
        scenarios["costs_2x"] = replace(base, cost_mult=2.0)
        scenarios["funding_0x"] = replace(base, funding_ann=0.0)
        scenarios["funding_2x"] = replace(base, funding_ann=2 * base.funding_ann)
        scenarios["gross_no_costs"] = replace(base, cost_mult=0.0, funding_ann=0.0)
        # falsification #4: +-20% lookback perturbations (joint and single)
        perts = {
            "lb_all_-20": dict(sma_fast=80, sma_slow=160, mom_lb=16),
            "lb_all_+20": dict(sma_fast=120, sma_slow=240, mom_lb=24),
            "lb_fast_-20": dict(sma_fast=80), "lb_fast_+20": dict(sma_fast=120),
            "lb_slow_-20": dict(sma_slow=160), "lb_slow_+20": dict(sma_slow=240),
            "lb_mom_-20": dict(mom_lb=16), "lb_mom_+20": dict(mom_lb=24),
        }
        for name, kw in perts.items():
            scenarios[name] = replace(base, **kw)

    results = {}
    for name, params in scenarios.items():
        d = data if params.sma_fast == base.sma_fast and params.sma_slow == base.sma_slow and params.mom_lb == base.mom_lb \
            else {s: compute_indicators(load_asset(s), params) for s in ASSETS}
        results[name] = run_backtest(d, params, args.start, args.end)
        r = results[name]
        print(f"{name:16s} ret={r['total_return']:+8.1%} cagr={r['cagr']:+7.1%} "
              f"sharpe={r['sharpe']:+5.2f} dd={r['maxdd']:6.1%} "
              f"s23={r['sharpe_2023on']:+5.2f} trades/yr={r['trades_per_year']:5.1f} "
              f"costs=${r['total_costs']:8.0f} halted={r['halted']}")

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nresults -> {RESULTS_PATH}")

    if not args.base:
        b = results["base"]
        f1 = b["sharpe"] < 0.30 or results["costs_2x"]["sharpe"] < 0
        f2 = b["sharpe_2023on"] < 0
        f3 = b["worst_year_dd"] > 0.40
        f4 = any(results[k]["total_return"] < 0 for k in results if k.startswith("lb_")) and b["total_return"] > 0
        print("\nFalsification criteria (section 9): any True => do not deploy")
        print(f"  F1 full-sample Sharpe<0.30 or 2x-cost Sharpe<0 : {f1}")
        print(f"  F2 2023+ subsample Sharpe<0                    : {f2}")
        print(f"  F3 any-year max DD > 40%                       : {f3}")
        print(f"  F4 sign flip under +-20% lookback perturbation : {f4}")


if __name__ == "__main__":
    main()
