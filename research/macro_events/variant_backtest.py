"""Tier-1 macro-event entry-filter variant study on the LOCKED reference backtest.

The reference backtest (`research/crypto_perps/backtest.py`) is the locked
authority — this module never modifies it. `run_backtest_filtered` below is a
verbatim copy of its `run_backtest` event loop with ONE addition: an
event-window entry filter applied to the per-asset targets in the decision
block (section 6 of the loop). Everything upstream (indicators, stops,
daily/weekly loss, halt, costs, funding) is byte-identical, and a parity gate
in `main()` proves it: with no events the filtered engine must reproduce the
locked engine's summary EXACTLY (dict equality) before any variant runs.

Filter semantics (risk-reducing actions are NEVER filtered):
- A decision is "in window" when any calendar event lies within [0, N hours]
  AFTER the live decision instant. Bar `ts` closes at 00:00 UTC on ts+1 and
  the live decision fires 00:05 UTC on ts+1, so decision_dt = ts + 1d + 5min.
- suppress mode: fresh entries from flat -> 0; the entering leg of a flip -> 0
  (the exit leg still executes); same-direction adds -> capped at current.
- halfsize mode: the risk-INCREASING component is halved (floor toward zero in
  whole contracts — a 1-contract entry halves to 0 at nano scale).
- Exits, reductions, stops, daily-loss flattens, halts: untouched by design.

Float arithmetic is retained deliberately: parity with the locked float engine
is the correctness gate here, and no live money flows through this study.

Run:  python3 variant_backtest.py            # parity gate + full variant suite
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

import numpy as np
import pandas as pd
import structlog

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "crypto_perps"))
import backtest as ref  # the locked reference engine

log = structlog.get_logger(module="macro_events.variant_backtest")

CAL_PATH = os.path.join(os.path.dirname(__file__), "data", "tier1_calendar.csv")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "results_variants.json")

CORE_TYPES = ("CPI", "NFP", "FOMC_DECISION")
EXT_TYPES = ("CPI", "NFP", "FOMC_DECISION", "PCE", "FOMC_MINUTES")

DECISION_LAG = pd.Timedelta(days=1, minutes=5)  # bar close -> live 00:05 UTC decision


def load_events(types: tuple[str, ...]) -> list[pd.Timestamp]:
    cal = pd.read_csv(CAL_PATH)
    cal = cal[cal["event_type"].isin(types)]
    return sorted(pd.to_datetime(cal["scheduled_at_utc"]))


def in_window(ts: pd.Timestamp, events: list[pd.Timestamp], window_hours: float) -> bool:
    """True when a calendar event lies within [0, window_hours] after the decision."""
    if not events or window_hours <= 0:
        return False
    decision_dt = ts + DECISION_LAG
    horizon = decision_dt + pd.Timedelta(hours=window_hours)
    i = int(np.searchsorted(events, decision_dt))
    return i < len(events) and bool(events[i] <= horizon)


def filter_target(cur: float, tgt: float, mode: str) -> float:
    """Clamp the risk-increasing component of a target; never touch reductions."""
    if tgt == cur:
        return tgt
    entering = 0.0
    base = cur
    if cur != 0 and tgt * cur < 0:  # flip: exit leg always allowed
        entering, base = tgt, 0.0
    elif cur == 0 and tgt != 0:  # fresh entry
        entering, base = tgt, 0.0
    elif tgt * cur > 0 and abs(tgt) > abs(cur):  # same-direction add
        entering, base = tgt - cur, cur
    else:  # reduction or exit
        return tgt
    if mode == "suppress":
        return base
    if mode == "halfsize":
        half = math.floor(abs(entering) / 2.0) * (1 if entering > 0 else -1)
        return base + half
    raise ValueError(f"unknown mode {mode!r}")


def run_backtest_filtered(
    data: dict[str, pd.DataFrame],
    p: ref.Params,
    start: str,
    end: str,
    events: list[pd.Timestamp] | None = None,
    window_hours: float = 0.0,
    mode: str = "suppress",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verbatim copy of ref.run_backtest + the entry-window filter (marked)."""
    events = events or []
    affected: list[dict[str, Any]] = []  # decisions the filter changed (variant path)

    ASSETS, CONTRACT_MULT = ref.ASSETS, ref.CONTRACT_MULT
    trade_cost = ref.trade_cost

    idx = data["BTC"].index
    for sym in ASSETS[1:]:
        idx = idx.union(data[sym].index)
    idx = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]

    frames = {s: data[s].reindex(idx) for s in ASSETS}
    E = p.initial_equity
    hwm = E
    halted = False
    pause_until = -1
    weekly_mult_until = -1
    states = {s: ref.AssetState() for s in ASSETS}

    equity_curve = []
    trades = []
    total_costs = 0.0
    total_funding = 0.0
    gross_expo = []

    equity_prev_close = E
    week_equity = []

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
            f = st.contracts * mult * row["close"] * (p.funding_ann / ref.DAYS_YEAR)
            E -= f
            total_funding += f

        # ---- 2b) yield on cash not tied up as futures margin ----
        if p.cash_yield_ann > 0:
            gross_held = sum(
                abs(states[s].contracts) * CONTRACT_MULT[s] * frames[s]["close"].loc[ts]
                for s in ASSETS
                if not np.isnan(frames[s]["close"].loc[ts])
            )
            E += (p.cash_yield_ann / ref.DAYS_YEAR) * max(0.0, E - p.margin_frac * gross_held)
            E -= p.subscription_usd_month * 12.0 / ref.DAYS_YEAR

        # ---- 3) hard halt (or bust) ----
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

        # ---- 4) daily loss limit ----
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
                        (str(ts.date()), s, -st.contracts, row["close"], cost, "daily_loss")
                    )
                    st.contracts = 0.0
                    st.stop_level = np.nan
                    st.applied_dir = 0
            pause_until = i + 1
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

        # ---- 6) decision at close ----
        if not skip_decision:
            targets = {}
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                mult = CONTRACT_MULT[s]
                if np.isnan(row.get("trend", np.nan)):
                    targets[s] = st.contracts
                    continue

                vr = row["vol_ratio"]
                if not np.isnan(vr):
                    if st.vol_blocked and vr < p.volratio_resume:
                        st.vol_blocked = False
                    elif not st.vol_blocked and vr > p.volratio_block:
                        st.vol_blocked = True

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
                        targets[s] = st.contracts if p.hysteresis_hold else 0.0
                        continue
                else:
                    st.pending_dir = 0
                    st.pending_count = 0

                d = st.applied_dir
                if d < 0 and (row["trend"] > -0.99 or p.funding_ann < p.funding_short_gate):
                    targets[s] = 0.0
                    continue
                funding_mult = 0.5 if (d > 0 and p.funding_ann > p.funding_long_veto) else 1.0
                if s == "ETH" and row["close"] < p.eth_min_price:
                    targets[s] = 0.0
                    continue
                if i < states[s].lockout_until and d == st.lockout_dir:
                    targets[s] = st.contracts if (st.contracts != 0) else 0.0
                    continue

                strength = abs(row["trend"])
                w = p.w_btc if s == "BTC" else 1.0 - p.w_btc
                notional = E * v_target * w * strength / row["sigma_ann"] * dd_mult * funding_mult
                max_notional = p.per_trade_risk_frac * E / (p.client_stop_atr * row["atrp"])
                notional = min(notional, max_notional, p.per_asset_cap * E)
                n = math.floor(notional / (mult * row["close"]) + 0.5) * d
                if st.vol_blocked:
                    cur = st.contracts
                    half = math.floor(abs(cur) / 2.0) * (1 if cur > 0 else -1)
                    n = half if cur != 0 else 0.0
                targets[s] = float(n)

            # ==== MACRO-EVENT ENTRY FILTER (the ONLY addition to the loop) ====
            if in_window(ts, events, window_hours):
                for s in ASSETS:
                    cur = states[s].contracts
                    tgt = targets[s]
                    new_tgt = filter_target(cur, tgt, mode)
                    if new_tgt != tgt:
                        affected.append(
                            {
                                "date": str(ts.date()),
                                "asset": s,
                                "current": cur,
                                "target_unfiltered": tgt,
                                "target_filtered": new_tgt,
                            }
                        )
                        targets[s] = new_tgt
            # ==== end filter ====

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

            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if np.isnan(row["close"]):
                    continue
                mult = CONTRACT_MULT[s]
                delta = targets[s] - st.contracts
                if delta == 0:
                    continue
                is_exit = targets[s] == 0 or st.contracts == 0 or (targets[s] * st.contracts < 0)
                band_notional = max(p.deadband_abs, p.deadband_frac * E)
                if not is_exit and abs(delta) * mult * row["close"] < band_notional:
                    continue
                if not is_exit and p.band_edge_rebalance:
                    band_n = math.floor(band_notional / (mult * row["close"]))
                    if band_n >= 1:
                        edge = targets[s] - band_n if delta > 0 else targets[s] + band_n
                        if (delta > 0 and edge > st.contracts) or (
                            delta < 0 and edge < st.contracts
                        ):
                            delta = edge - st.contracts
                cost = trade_cost(delta, row["close"], mult, p)
                E -= cost
                total_costs += cost
                trades.append((str(ts.date()), s, delta, row["close"], cost, "signal"))
                new_pos = st.contracts + delta
                if new_pos != 0 and (
                    st.contracts == 0
                    or st.contracts * new_pos < 0
                    or abs(new_pos) > abs(st.contracts)
                ):
                    if st.contracts * new_pos <= 0:
                        st.entry_vwap = row["close"]
                    else:
                        add = abs(delta)
                        st.entry_vwap = (
                            st.entry_vwap * abs(st.contracts) + row["close"] * add
                        ) / abs(new_pos)
                    sign = 1 if new_pos > 0 else -1
                    st.stop_level = st.entry_vwap * (1 - sign * p.client_stop_atr * row["atrp"])
                st.contracts = new_pos
                if new_pos == 0:
                    st.stop_level = np.nan
                    if targets[s] == 0 and st.applied_dir != 0 and (delta * st.applied_dir < 0):
                        pass

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
    summary = ref.summarize(curve, trades, total_costs, total_funding, gross_expo, halted, p)
    return summary, {"affected": affected, "trades": trades}


def episodes_from_trades(
    trades: list[tuple[str, str, float, float, float, str]],
) -> list[dict[str, Any]]:
    """Reconstruct flat-to-flat position episodes per asset with cash-flow PnL.

    Flip trades are split into their closing and opening components (cost
    apportioned pro-rata by |contracts|). Funding is excluded (small at the
    parametric rate; noted in REPORT.md). Episodes still open at sample end
    are marked closed=False and excluded from win-rate computations.
    """
    eps: list[dict[str, Any]] = []
    pos: dict[str, float] = {}
    cur_ep: dict[str, dict[str, Any] | None] = {}
    for date, s, delta, price, cost, reason in trades:
        mult = ref.CONTRACT_MULT[s]
        p0 = pos.get(s, 0.0)
        p1 = p0 + delta
        legs = []
        if p0 != 0 and p1 != 0 and p0 * p1 < 0:  # flip: split close + open
            legs.append((-p0, True))
            legs.append((p1, False))
        else:
            legs.append((delta, p1 == 0 or abs(p1) < abs(p0)))
        for leg_delta, _is_reducing in legs:
            frac = abs(leg_delta) / abs(delta) if delta else 0.0
            leg_cost = cost * frac
            ep = cur_ep.get(s)
            if ep is None:
                ep = {
                    "asset": s,
                    "open_date": date,
                    "direction": 1 if leg_delta > 0 else -1,
                    "cash": 0.0,
                    "closed": False,
                    "close_date": None,
                    "close_reason": None,
                }
                cur_ep[s] = ep
                eps.append(ep)
            ep["cash"] += -leg_delta * mult * price - leg_cost
            p0 += leg_delta
            if p0 == 0 and cur_ep.get(s) is not None:
                ep["closed"] = True
                ep["close_date"] = date
                ep["close_reason"] = reason
                cur_ep[s] = None
        pos[s] = p1
    return eps


def main() -> None:
    base = ref.Params()
    aggr = ref.Params(
        v_target=0.80,
        per_trade_risk_frac=0.05,
        daily_loss_limit=-0.08,
        weekly_loss_limit=-0.16,
        gross_cap=3.0,
        dd_tiers=((0.20, 1.0), (0.40, 0.6), (0.70, 0.35), (9.9, 0.2)),
        halt_frac=0.25,
    )
    production = ref.Params(
        v_target=0.80,
        per_trade_risk_frac=0.05,
        daily_loss_limit=-0.08,
        weekly_loss_limit=-0.16,
        gross_cap=3.0,
        dd_tiers=((9.9, 1.0),),
        halt_frac=0.25,
        hysteresis_hold=True,
        band_edge_rebalance=True,
        cash_yield_ann=0.035,
        subscription_usd_month=4.99,
    )
    del aggr  # documented derivation; production is the studied profile

    start, end = "2017-01-01", "2026-06-30"
    data = {s: ref.compute_indicators(ref.load_asset(s), base) for s in ref.ASSETS}

    # ---- PARITY GATE: no-event filtered engine must equal the locked engine ----
    results: dict[str, Any] = {}
    for pname, params in (("production", production), ("base", base)):
        locked = ref.run_backtest(data, params, start, end)
        mirrored, _extra = run_backtest_filtered(data, params, start, end)
        if locked != mirrored:
            log.error("parity_gate_FAILED", profile=pname)
            raise SystemExit(1)
        log.info("parity_gate_green", profile=pname, sharpe=locked["sharpe"])
        results[f"{pname}_baseline"] = locked

    core = load_events(CORE_TYPES)
    ext = load_events(EXT_TYPES)
    log.info("events_loaded", core=len(core), extended=len(ext))

    # ---- counterfactual: baseline entries that each window set would have hit ----
    _, prod_extra = run_backtest_filtered(data, production, start, end)
    eps = episodes_from_trades(prod_extra["trades"])
    counterfactual: dict[str, Any] = {}
    for set_name, events in (("core", core), ("ext", ext)):
        for n in (12, 24, 48):
            hits = [e for e in eps if in_window(pd.Timestamp(e["open_date"]), events, n)]
            closed = [e for e in hits if e["closed"]]
            wins = [e for e in closed if e["cash"] > 0]
            counterfactual[f"{set_name}_{n}h"] = {
                "episodes_opened_in_window": len(hits),
                "closed": len(closed),
                "win_rate": round(len(wins) / len(closed), 3) if closed else None,
                "sum_pnl": round(sum(e["cash"] for e in closed), 2),
                "mean_pnl": round(np.mean([e["cash"] for e in closed]), 2) if closed else None,
                "median_pnl": round(float(np.median([e["cash"] for e in closed])), 2)
                if closed
                else None,
            }
    all_closed = [e for e in eps if e["closed"]]
    counterfactual["all_episodes"] = {
        "episodes": len(eps),
        "closed": len(all_closed),
        "win_rate": round(sum(1 for e in all_closed if e["cash"] > 0) / len(all_closed), 3),
        "mean_pnl": round(np.mean([e["cash"] for e in all_closed]), 2),
        "median_pnl": round(float(np.median([e["cash"] for e in all_closed])), 2),
    }
    results["counterfactual_baseline_episodes"] = counterfactual

    # ---- variant matrix ----
    for set_name, events in (("core", core), ("ext", ext)):
        for mode in ("suppress", "halfsize"):
            for n in (12, 24, 48):
                name = f"production_{set_name}_{mode}_{n}h"
                summary, extra = run_backtest_filtered(
                    data, production, start, end, events=events, window_hours=n, mode=mode
                )
                summary["n_affected_decisions"] = len(extra["affected"])
                results[name] = summary
                log.info(
                    "variant_done",
                    name=name,
                    cagr=summary["cagr"],
                    sharpe=summary["sharpe"],
                    maxdd=summary["maxdd"],
                    affected=len(extra["affected"]),
                )
    # base-profile sensitivity (core set, 24h)
    for mode in ("suppress", "halfsize"):
        name = f"base_core_{mode}_24h"
        summary, extra = run_backtest_filtered(
            data, base, start, end, events=core, window_hours=24, mode=mode
        )
        summary["n_affected_decisions"] = len(extra["affected"])
        results[name] = summary
        log.info("variant_done", name=name, cagr=summary["cagr"], sharpe=summary["sharpe"])

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    log.info("results_written", path=RESULTS_PATH)


if __name__ == "__main__":
    main()
