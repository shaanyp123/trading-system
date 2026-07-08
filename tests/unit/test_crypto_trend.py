"""Unit tests for services/signal/crypto_trend (crypto-pivot C0-B3).

Focused decision-logic tests with hand-built rows/states — the heavy
verification is the golden parity gate
(tests/golden/test_crypto_trend_parity.py: identical trades vs the
reference over the full recorded 2016-2026 history). These tests pin
the individual S1-S4 / Amendment B behaviors so a future edit that
breaks one rule fails HERE with a named test, not just as an opaque
parity diff.
"""

from __future__ import annotations

import math

from services.signal.crypto_trend import (
    AMENDMENT_B_PARAMS,
    ASSETS,
    AssetDecisionRow,
    AssetState,
    CryptoTrendParams,
    compute_targets,
    plan_delta,
    trade_cost,
)

P = CryptoTrendParams()  # base profile


def _row(
    *,
    trend: float = 1.0,
    vol_ratio: float = 1.0,
    sigma_ann: float = 0.45,
    atrp: float = 0.03,
    close: float = 100_000.0,
) -> AssetDecisionRow:
    return AssetDecisionRow(
        trend=trend, vol_ratio=vol_ratio, sigma_ann=sigma_ann, atrp=atrp, close=close
    )


def _rows(
    btc: AssetDecisionRow | None = None, eth: AssetDecisionRow | None = None
) -> dict[str, AssetDecisionRow]:
    return {
        "BTC": btc if btc is not None else _row(),
        "ETH": eth if eth is not None else _row(sigma_ann=0.6, close=3_500.0, atrp=0.035),
    }


def _states(**over: AssetState) -> dict[str, AssetState]:
    states = {s: AssetState() for s in ASSETS}
    states.update(over)
    return states


def _targets(
    rows: dict[str, AssetDecisionRow],
    states: dict[str, AssetState],
    *,
    bar_index: int = 100,
    equity: float = 6_000.0,
    v_target: float | None = None,
    dd_mult: float = 1.0,
    p: CryptoTrendParams = P,
) -> dict[str, float]:
    return compute_targets(
        bar_index=bar_index,
        rows=rows,
        states=states,
        equity=equity,
        v_target=v_target if v_target is not None else p.v_target,
        dd_mult=dd_mult,
        p=p,
    )


class TestHysteresis:
    def test_fresh_direction_needs_two_confirmations(self) -> None:
        states = _states()
        # day 1: raw dir +1 vs applied 0 → pending, target flat (base: no hold)
        t1 = _targets(_rows(), states)
        assert t1["BTC"] == 0.0
        assert states["BTC"].pending_count == 1
        # day 2: confirmation → applied, sized target
        t2 = _targets(_rows(), states)
        assert t2["BTC"] > 0
        assert states["BTC"].applied_dir == 1
        assert states["BTC"].pending_count == 0

    def test_amendment_b_holds_position_through_unconfirmed_flip(self) -> None:
        # long 3 contracts, trend flips bearish for ONE day
        long_state = AssetState(contracts=3.0, applied_dir=1)
        states = _states(BTC=long_state)
        rows = _rows(btc=_row(trend=-1.0))
        held = _targets(rows, states, p=AMENDMENT_B_PARAMS)
        assert held["BTC"] == 3.0  # hysteresis-hold: keep the position

        # base profile goes flat during the unconfirmed flip
        states_base = _states(BTC=AssetState(contracts=3.0, applied_dir=1))
        flat = _targets(rows, states_base, p=P)
        assert flat["BTC"] == 0.0

    def test_stop_today_forces_flat(self) -> None:
        st = AssetState(applied_dir=1, stopped_today=True)
        states = _states(BTC=st)
        t = _targets(_rows(), states)
        assert t["BTC"] == 0.0


class TestGates:
    def test_short_needs_full_strength_score(self) -> None:
        st = AssetState(applied_dir=-1)
        states = _states(BTC=st)
        # -1/3 bearish: short gate blocks
        t = _targets(_rows(btc=_row(trend=-1.0 / 3.0)), states)
        assert t["BTC"] == 0.0
        # full -1 bearish with acceptable funding: short allowed
        states2 = _states(BTC=AssetState(applied_dir=-1))
        t2 = _targets(_rows(btc=_row(trend=-1.0)), states2)
        assert t2["BTC"] < 0

    def test_funding_long_veto_halves_target(self) -> None:
        import dataclasses

        hot_funding = dataclasses.replace(P, funding_ann=0.50)  # > +30% veto
        states_a = _states(BTC=AssetState(applied_dir=1))
        states_b = _states(BTC=AssetState(applied_dir=1))
        normal = _targets(_rows(), states_a, p=P)
        vetoed = _targets(_rows(), states_b, p=hot_funding)
        assert 0 < vetoed["BTC"] < normal["BTC"]

    def test_eth_min_price_rule(self) -> None:
        states = _states(ETH=AssetState(applied_dir=1))
        rows = _rows(eth=_row(sigma_ann=0.6, close=1_500.0, atrp=0.035))  # < $2,000
        t = _targets(rows, states)
        assert t["ETH"] == 0.0

    def test_lockout_blocks_same_direction_reentry(self) -> None:
        st = AssetState(applied_dir=1, lockout_until=200, lockout_dir=1)
        states = _states(BTC=st)
        t = _targets(_rows(), states, bar_index=150)
        assert t["BTC"] == 0.0
        # past the lockout window: sized again
        states2 = _states(BTC=AssetState(applied_dir=1, lockout_until=200, lockout_dir=1))
        t2 = _targets(_rows(), states2, bar_index=250)
        assert t2["BTC"] > 0

    def test_s3_block_halves_existing_position(self) -> None:
        st = AssetState(contracts=5.0, applied_dir=1)
        states = _states(BTC=st)
        rows = _rows(btc=_row(vol_ratio=2.5))  # > 2.0 block threshold
        t = _targets(rows, states)
        assert t["BTC"] == 2.0  # floor(5/2)
        assert states["BTC"].vol_blocked is True
        # resume below 1.5
        rows2 = _rows(btc=_row(vol_ratio=1.2))
        t2 = _targets(rows2, states)
        assert states["BTC"].vol_blocked is False
        assert t2["BTC"] > 2.0

    def test_nan_trend_holds_current(self) -> None:
        st = AssetState(contracts=4.0, applied_dir=1)
        states = _states(BTC=st)
        t = _targets(_rows(btc=_row(trend=math.nan)), states)
        assert t["BTC"] == 4.0


class TestSizing:
    def test_per_trade_risk_cap_overrides_vol_target(self) -> None:
        """REPORT F-4 / §3.3: the §7 risk cap binds over the §6 output."""
        st = AssetState(applied_dir=1)
        states = _states(BTC=st)
        # huge atrp → risk cap tiny; vol target alone would size larger
        rows = _rows(btc=_row(atrp=0.20, sigma_ann=0.20))
        t = _targets(rows, states, equity=6_000.0)
        # max_notional = 0.025*6000/(2*0.20) = 375 → 0 contracts at $100k
        assert t["BTC"] == 0.0

    def test_targets_are_integer_contracts(self) -> None:
        states = _states(BTC=AssetState(applied_dir=1), ETH=AssetState(applied_dir=1))
        t = _targets(_rows(), states, equity=50_000.0)
        for asset, target in t.items():
            assert target == int(target), (asset, target)

    def test_gross_cap_scales_down(self) -> None:
        import dataclasses

        # absurdly high vol target so the gross cap binds
        p = dataclasses.replace(P, v_target=50.0, per_trade_risk_frac=50.0, per_asset_cap=50.0)
        states = _states(BTC=AssetState(applied_dir=1), ETH=AssetState(applied_dir=1))
        equity = 6_000.0
        t = _targets(_rows(), states, equity=equity, v_target=p.v_target, p=p)
        gross = abs(t["BTC"]) * 0.01 * 100_000.0 + abs(t["ETH"]) * 0.10 * 3_500.0
        assert gross <= p.gross_cap * equity * 1.10  # reference's own tolerance


class TestPlanDelta:
    def test_deadband_suppresses_small_rebalance(self) -> None:
        # long 10, target 11 → delta notional 1*0.01*100000 = $1000 > band?
        # band = max(200, 0.05*6000) = 300 → trades. Use smaller equity move:
        delta = plan_delta(
            target=11.0, current_contracts=10.0, close=20_000.0, mult=0.01, equity=6_000.0, p=P
        )
        # 1 contract * 0.01 * 20000 = $200 < band 300 → suppressed
        assert delta == 0.0

    def test_exits_bypass_deadband(self) -> None:
        delta = plan_delta(
            target=0.0, current_contracts=1.0, close=20_000.0, mult=0.01, equity=6_000.0, p=P
        )
        assert delta == -1.0

    def test_band_edge_rebalance_trades_to_edge(self) -> None:
        # Amendment B: rebalance only to the nearest band edge.
        # band = max(200, 0.05*6000)=300; band_n = floor(300/(0.01*30000)) = 1
        delta = plan_delta(
            target=15.0,
            current_contracts=10.0,
            close=30_000.0,
            mult=0.01,
            equity=6_000.0,
            p=AMENDMENT_B_PARAMS,
        )
        assert delta == 4.0  # to edge (target - band_n), not the full 5

    def test_base_profile_trades_to_exact_target(self) -> None:
        delta = plan_delta(
            target=15.0, current_contracts=10.0, close=30_000.0, mult=0.01, equity=6_000.0, p=P
        )
        assert delta == 5.0


class TestParamsAndCosts:
    def test_amendment_b_locked_values(self) -> None:
        b = AMENDMENT_B_PARAMS
        assert b.v_target == 0.80
        assert b.per_trade_risk_frac == 0.05
        assert b.daily_loss_limit == -0.08
        assert b.weekly_loss_limit == -0.16
        assert b.gross_cap == 3.0
        assert b.halt_frac == 0.25
        assert b.dd_tiers == ((9.9, 1.0),)  # tiers removed
        assert b.hysteresis_hold is True
        assert b.band_edge_rebalance is True
        assert b.cash_yield_ann == 0.035
        assert b.subscription_usd_month == 4.99
        # signal knobs FROZEN at base values
        assert (b.sma_fast, b.sma_slow, b.mom_lb) == (100, 200, 20)

    def test_trade_cost_minimum_fee_binds_small_notional(self) -> None:
        # 1 ETH nano contract at $1500 → $150 notional; 5bps = $0.075 < $0.20 min
        cost = trade_cost(1.0, 1_500.0, 0.10, P)
        fee = 0.20
        slip = 4.0 / 1e4 * 150.0
        assert cost == fee + slip

    def test_trade_cost_zero_for_no_change(self) -> None:
        assert trade_cost(0.0, 100.0, 0.01, P) == 0.0


class TestStateShape:
    def test_asset_state_defaults(self) -> None:
        st = AssetState()
        assert st.contracts == 0.0
        assert math.isnan(st.stop_level)
        assert st.applied_dir == 0
        assert st.vol_blocked is False
