"""Locks the Amendment B canonical parameter set (crypto-pivot C0-B4).

Two jobs: (1) pin the operator-approved values so an accidental edit
fails a named test (editing them is an amendment + decisions-log
entry); (2) lock the seed dict to the parity-locked signal engine's
float profile so the operational and computational values can never
drift apart silently.
"""

from __future__ import annotations

import pytest

from services.risk.crypto_parameters import (
    AMENDMENT_B_CANONICAL_PARAMETERS,
    default_crypto_parameters,
)
from services.version.composite_hash import (
    OPERATOR_ONLY_FLAG_KEYS,
    compute_parameter_set_hash,
)

P = AMENDMENT_B_CANONICAL_PARAMETERS


class TestAmendmentBValues:
    def test_delta_spec_3_4_seed_values(self) -> None:
        assert P["V_TARGET"] == "0.80"
        assert P["PER_TRADE_RISK_FRAC"] == "0.05"
        assert P["DAILY_LOSS_LIMIT"] == "-0.08"
        assert P["WEEKLY_LOSS_LIMIT"] == "-0.16"
        assert P["GROSS_CAP"] == "3.0"
        assert P["HALT_EQUITY_USD"] == "1500"
        assert P["HYSTERESIS_HOLD"] == "True"
        assert P["BAND_EDGE"] == "True"
        assert P["ETH_MIN_PRICE"] == "2000"

    def test_operator_flags_present_and_safe(self) -> None:
        for flag in OPERATOR_ONLY_FLAG_KEYS:
            assert P[flag] == "False"

    def test_all_values_are_strings(self) -> None:
        assert all(isinstance(v, str) for v in P.values())  # [A05] Decimal-as-string

    def test_hash_is_stable_and_flag_independent(self) -> None:
        h = compute_parameter_set_hash(P)
        assert len(h) == 64 and h == h.lower()
        flipped = dict(P, STRATEGY_DECOMMISSIONED="True")
        assert compute_parameter_set_hash(flipped) == h  # flags excluded

    def test_default_returns_defensive_copy(self) -> None:
        d = default_crypto_parameters()
        d["V_TARGET"] = "9.99"
        assert P["V_TARGET"] == "0.80"


class TestEngineConsistency:
    """The seed dict and the parity-locked engine profile agree.

    Skips until the C0-B3 signal-engine PR merges (the two PRs are
    parked independently); once both are on main this lock is active
    permanently.
    """

    def test_float_twins_match(self) -> None:
        crypto_trend = pytest.importorskip(
            "services.signal.crypto_trend",
            reason="C0-B3 signal engine not merged yet",
        )
        e = crypto_trend.AMENDMENT_B_PARAMS
        assert float(P["V_TARGET"]) == e.v_target
        assert float(P["PER_TRADE_RISK_FRAC"]) == e.per_trade_risk_frac
        assert float(P["DAILY_LOSS_LIMIT"]) == e.daily_loss_limit
        assert float(P["WEEKLY_LOSS_LIMIT"]) == e.weekly_loss_limit
        assert float(P["GROSS_CAP"]) == e.gross_cap
        assert float(P["PER_ASSET_CAP"]) == e.per_asset_cap
        assert float(P["DEADBAND_ABS_USD"]) == e.deadband_abs
        assert float(P["DEADBAND_FRAC"]) == e.deadband_frac
        assert float(P["W_BTC"]) == e.w_btc
        assert float(P["EWMA_LAMBDA"]) == e.ewma_lambda
        assert float(P["VOL_FLOOR"]) == e.vol_floor
        assert float(P["VOL_CAP"]) == e.vol_cap
        assert float(P["VOLRATIO_BLOCK"]) == e.volratio_block
        assert float(P["VOLRATIO_RESUME"]) == e.volratio_resume
        assert float(P["FUNDING_LONG_VETO"]) == e.funding_long_veto
        assert float(P["FUNDING_SHORT_GATE"]) == e.funding_short_gate
        assert float(P["ETH_MIN_PRICE"]) == e.eth_min_price
        assert float(P["CLIENT_STOP_ATR"]) == e.client_stop_atr
        assert float(P["CASH_YIELD_ANN"]) == e.cash_yield_ann
        assert float(P["SUBSCRIPTION_USD_MONTH"]) == e.subscription_usd_month
        assert float(P["MARGIN_BUFFER_FRAC"]) == e.margin_frac
        assert int(P["SMA_FAST_DAYS"]) == e.sma_fast
        assert int(P["SMA_SLOW_DAYS"]) == e.sma_slow
        assert int(P["MOM_LOOKBACK_DAYS"]) == e.mom_lb
        assert int(P["HYSTERESIS_DAYS"]) == e.hysteresis_days
        assert int(P["VOLRATIO_SLOW_WINDOW"]) == e.volratio_slow_window
        assert int(P["ATR_WINDOW"]) == e.atr_window
        assert int(P["LOCKOUT_DAYS"]) == e.lockout_days
        assert int(P["WEEKLY_PENALTY_DAYS"]) == e.weekly_penalty_days
        assert (P["HYSTERESIS_HOLD"] == "True") == e.hysteresis_hold
        assert (P["BAND_EDGE"] == "True") == e.band_edge_rebalance
        assert (P["DD_TIERS_REMOVED"] == "True") == (e.dd_tiers == ((9.9, 1.0),))
        # HALT_EQUITY_USD 1500 = halt_frac 0.25 x initial 6000
        assert float(P["HALT_EQUITY_USD"]) == e.halt_frac * e.initial_equity
        # native stop 3xATR matches the execution layer's locked constant
        assert P["NATIVE_STOP_ATR"] == "3.0"
