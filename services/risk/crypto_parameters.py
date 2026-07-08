"""services/risk/crypto_parameters.py — the Amendment B canonical parameter set.

Crypto-pivot C0-B4 (delta spec §3.4): the operational source of truth
for the crypto-perps risk-preference knobs, seeded into the
``parameter_sets`` table (via ``scripts/operator_tools/
bootstrap_live_account.py --mint-from-defaults``) and hashed with the
existing ``services.version.composite_hash`` convention (SHA-256 over
JCS of the canonical dict, operator-only flags excluded so the
decommission flip stays hash-stable).

All values are Decimal-as-string per [A05]. The FLOAT twins that the
parity-locked signal engine computes with live in
``services.signal.crypto_trend.AMENDMENT_B_PARAMS``; the consistency
test (``tests/unit/test_crypto_parameters.py``) locks the two together
so the seeded operational values and the engine's computational values
can never drift silently.

Governance (locked, decisions-log 2026-07-08): signal parameters
(100/200/20 lookbacks, thresholds) move ONLY via falsify-and-replace
against the reference backtest; risk-preference knobs move only via
amendment + decisions-log entry. Editing this dict IS such an amendment.

A02 BINDS — ``services/risk/**`` forbidden path.
"""

from __future__ import annotations

from typing import Final

#: Amendment B canonical parameter set (delta spec §3.4; strategy doc
#: Amendments A+B, operator-locked 2026-07-08). UPPER_CASE keys +
#: string values mirror the V1-era ``to_canonical_dict()`` shape the
#: hash/bootstrap machinery expects. Booleans render "True"/"False"
#: (round-tripping via ``str(v).strip().lower() == "true"`` readers).
AMENDMENT_B_CANONICAL_PARAMETERS: Final[dict[str, str]] = {
    # --- §6 sizing (Amendment A 2x profile) -------------------------------
    "V_TARGET": "0.80",
    "W_BTC": "0.6666666666666666",  # 2/3 gross risk to BTC (strategy §3)
    "PER_ASSET_CAP": "1.4",
    "GROSS_CAP": "3.0",
    "DEADBAND_ABS_USD": "200",
    "DEADBAND_FRAC": "0.05",
    # --- §7 risk framework (Amendment A) -----------------------------------
    "PER_TRADE_RISK_FRAC": "0.05",
    "DAILY_LOSS_LIMIT": "-0.08",
    "WEEKLY_LOSS_LIMIT": "-0.16",
    "WEEKLY_PENALTY_DAYS": "7",
    "HALT_EQUITY_USD": "1500",
    # --- Amendment B structural switches -----------------------------------
    "HYSTERESIS_HOLD": "True",
    "BAND_EDGE": "True",
    "DD_TIERS_REMOVED": "True",
    # --- §4 signal constants (FROZEN; falsify-and-replace only) ------------
    "SMA_FAST_DAYS": "100",
    "SMA_SLOW_DAYS": "200",
    "MOM_LOOKBACK_DAYS": "20",
    "HYSTERESIS_DAYS": "2",
    "EWMA_LAMBDA": "0.94",
    "VOL_FLOOR": "0.20",
    "VOL_CAP": "1.50",
    "VOLRATIO_SLOW_WINDOW": "90",
    "VOLRATIO_BLOCK": "2.0",
    "VOLRATIO_RESUME": "1.5",
    "FUNDING_LONG_VETO": "0.30",
    "FUNDING_SHORT_GATE": "-0.10",
    "ETH_MIN_PRICE": "2000",
    # --- §5 stops / lockout -------------------------------------------------
    "ATR_WINDOW": "14",
    "CLIENT_STOP_ATR": "2.0",
    "NATIVE_STOP_ATR": "3.0",
    "LOCKOUT_DAYS": "2",
    # --- §3.6 cash-yield economics (Coinbase One Basic, locked 2026-07-08) --
    "CASH_YIELD_ANN": "0.035",
    "SUBSCRIPTION_USD_MONTH": "4.99",
    "MARGIN_BUFFER_FRAC": "0.25",
    # --- operator-only flags (EXCLUDED from the hash; safe defaults) --------
    "STRATEGY_DECOMMISSIONED": "False",
    "EXIT_AUTO_APPROVE": "False",
}


def default_crypto_parameters() -> dict[str, str]:
    """The canonical dict for hashing/seeding (defensive copy).

    Drop-in successor of the retired ``default_v1_parameters().
    to_canonical_dict()`` in the bootstrap seed path.
    """
    return dict(AMENDMENT_B_CANONICAL_PARAMETERS)


__all__ = [
    "AMENDMENT_B_CANONICAL_PARAMETERS",
    "default_crypto_parameters",
]
