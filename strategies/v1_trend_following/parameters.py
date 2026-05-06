"""V1 parameters — locked + agent-mutable surface.

Locked vs agent-mutable per `Docs/backend-spec.md §12.3` (`tighten_parameter` enum):

| Parameter                      | Class           | Tighten direction |
| ------------------------------ | --------------- | ----------------- |
| `LOOKBACK_DAYS_DONCHIAN`       | agent-mutable   | up   (longer lookback = fewer signals = tighter) |
| `MA_FAST_DAYS`                 | agent-mutable   | up   (slower = fewer crossovers) |
| `MA_SLOW_DAYS`                 | agent-mutable   | up   (slower = stricter trend filter) |
| `HURST_THRESHOLD`              | agent-mutable   | up   (more selective on persistence) |
| `STOP_DISTANCE_ATR_MULT`       | agent-mutable   | down (tighter stop) |
| `VOL_TARGET_PCT_ANNUAL`        | agent-mutable   | down (lower target = smaller positions) |
| `ROLL_DAYS_BEFORE_EXPIRY`      | agent-mutable   | up   (roll earlier = more conservative) |
| `INSTRUMENT_VOL_LOOKBACK_DAYS` | agent-mutable   | n/a  (no tighten direction) |
| `MIN_HOLDING_DAYS`             | **LOCKED**      | PR-only           |
| `ATR_LOOKBACK_DAYS`            | **LOCKED**      | PR-only           |

`V1_DEFAULTS` below is the source-of-truth for skeleton tests and the initial
parameter set persisted to the `parameter_sets` table on first deploy. In
production the active values come from the DB head pointer; this module's
defaults are loaded only when no DB row exists yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Final

# UPPER_CASE keys mirror the agent `tighten_parameter` enum (backend-spec §12.3) and
# the param-name strings persisted to `parameter_sets.parameters` JSONB. These
# string keys are CANONICAL — do not rename without a migration of every existing
# parameter_sets row + an audit `parameter_change_applied` event for each.
V1_DEFAULTS: Final[dict[str, int | str]] = {
    # Entry signal.
    "LOOKBACK_DAYS_DONCHIAN": 60,
    "MA_FAST_DAYS": 50,
    "MA_SLOW_DAYS": 200,
    # HURST_THRESHOLD = 0.55, not 0.50, to compensate for the R/S estimator's
    # known small-sample upward bias on a 60-bar window (typically inflates H
    # by ~0.05). 0.55 is what 0.50 buys you on a 60-bar lookback after
    # de-biasing — i.e. "moderate persistence" rather than "any positive
    # autocorrelation." Agent can tighten further (up direction = stricter)
    # via tighten_parameter; PR required to loosen below 0.50.
    "HURST_THRESHOLD": "0.55",
    # Stop / exit. ATR_LOOKBACK_DAYS and MIN_HOLDING_DAYS are LOCKED at the values
    # in backend-spec §2.3 — agent cannot tighten; PR required to change.
    "STOP_DISTANCE_ATR_MULT": "3.0",
    "ATR_LOOKBACK_DAYS": 20,
    "MIN_HOLDING_DAYS": 14,
    # Sizing inputs (consumed by services/risk/sizing.py Stage 1).
    "VOL_TARGET_PCT_ANNUAL": "0.15",
    "INSTRUMENT_VOL_LOOKBACK_DAYS": 60,
    # Contract roll (consumed by services/scheduler/calendar.py).
    "ROLL_DAYS_BEFORE_EXPIRY": 5,
}

# Phase 1 candidate sub-universe — LOCKED on 2026-05-05 per Docs/decisions-log.md.
#
# Each line: market symbol + one-line rationale. This is the CANDIDATE POOL that
# Stage 0 of position sizing (services/risk/sizing.py per backend-spec §2.4.1)
# filters at runtime via the 1-contract-notional <= 50% x equity rule. The
# active set at $15k-$25k starting equity will be a subset; at higher equity
# tiers more candidates qualify. Week 2 verification (implementation-guide §3
# Week 2 Tue) is a data-executability check (QC bundled data availability
# per market) — it may flag specific markets as data-unavailable but the
# locked candidate pool below is the universe the strategy code targets.
#
# Source for inclusion: backend-spec §2.3 ("micro futures + bond ETFs") +
# implementation-guide §3 Week 2 Mon. Coverage rationale: 4 micro families
# (equity index, rates-via-ETF, commodity, crypto) at the smallest available
# CME contract sizes plus the standard 4-point bond-ETF curve.
V1_CANDIDATE_UNIVERSE: Final[tuple[str, ...]] = (
    # CME micro futures.
    "/MES",  # E-mini S&P 500 Micro      ($5 x index;     ~$26k notional @ 5235)
    "/MNQ",  # E-mini Nasdaq-100 Micro   ($2 x index;     ~$36k notional @ 18000)
    "/MYM",  # E-mini Dow Micro          ($0.50 x index;  ~$20k notional @ 40000)
    "/M2K",  # E-mini Russell 2000 Micro ($5 x index;     ~$10k notional @ 2000)
    "/MGC",  # Gold Micro                (10 oz x $;      ~$24k notional @ $2400)
    "/MCL",  # WTI Crude Micro           (100 bbl x $;    ~$8k notional  @ $80)
    "/MBT",  # Bitcoin Micro             (0.1 BTC x $;    ~$10k notional @ $100k)
    # NYSE bond ETFs (cash equity, no contract roll).
    "TLT",  # 20+ Year Treasury  — long-duration anchor
    "IEF",  # 7-10 Year Treasury — intermediate
    "SHY",  # 1-3 Year Treasury  — short
    "TIP",  # TIPS               — inflation-linked
)


@dataclass(frozen=True, slots=True)
class V1Parameters:
    """Frozen parameter set for V1 trend-following.

    Identity (`parameter_set_hash` per backend-spec §3.11) is computed from the
    canonical JSON serialization of these fields by `services/version/composite_hash.py`.
    Constructing a `V1Parameters` instance does NOT compute the hash; the version
    service does that and persists to the `parameter_sets` table.
    """

    # --- Entry signal -------------------------------------------------------
    lookback_days_donchian: int
    ma_fast_days: int
    ma_slow_days: int
    hurst_threshold: Decimal

    # --- Stop / exit --------------------------------------------------------
    stop_distance_atr_mult: Decimal
    atr_lookback_days: int  # locked at 20 per backend-spec §2.3
    min_holding_days: int  # locked at 14 per backend-spec §2.3

    # --- Sizing inputs (consumed by services/risk/sizing.py Stage 1) -------
    vol_target_pct_annual: Decimal  # 0.15 = 15%/yr Clenow-style
    instrument_vol_lookback_days: int

    # --- Contract roll ------------------------------------------------------
    roll_days_before_expiry: int

    def __post_init__(self) -> None:
        # Range validation. The agent's `tighten_parameter` tool also enforces
        # these at the API surface; duplicating the floor here catches direct
        # construction in tests / replay paths.
        if not 20 <= self.lookback_days_donchian <= 252:
            raise ValueError(
                f"lookback_days_donchian={self.lookback_days_donchian} outside [20, 252]"
            )
        if not 5 <= self.ma_fast_days < self.ma_slow_days <= 400:
            raise ValueError(f"MA pair invalid: fast={self.ma_fast_days}, slow={self.ma_slow_days}")
        if not Decimal("0.40") <= self.hurst_threshold <= Decimal("0.80"):
            raise ValueError(f"hurst_threshold={self.hurst_threshold} outside [0.40, 0.80]")
        if not Decimal("1.0") <= self.stop_distance_atr_mult <= Decimal("6.0"):
            raise ValueError(
                f"stop_distance_atr_mult={self.stop_distance_atr_mult} outside [1.0, 6.0]"
            )
        if not 5 <= self.atr_lookback_days <= 60:
            raise ValueError(f"atr_lookback_days={self.atr_lookback_days} outside [5, 60]")
        if not 1 <= self.min_holding_days <= 60:
            raise ValueError(f"min_holding_days={self.min_holding_days} outside [1, 60]")
        if not Decimal("0.05") <= self.vol_target_pct_annual <= Decimal("0.40"):
            raise ValueError(
                f"vol_target_pct_annual={self.vol_target_pct_annual} outside [0.05, 0.40]"
            )
        if not 20 <= self.instrument_vol_lookback_days <= 252:
            raise ValueError(
                f"instrument_vol_lookback_days={self.instrument_vol_lookback_days} "
                f"outside [20, 252]"
            )
        if not 1 <= self.roll_days_before_expiry <= 21:
            raise ValueError(
                f"roll_days_before_expiry={self.roll_days_before_expiry} outside [1, 21]"
            )

    def to_canonical_dict(self) -> dict[str, str]:
        """Return UPPER_CASE-keyed canonical dict for JCS hashing.

        Decimals serialized as strings (preserves precision); ints as decimal-string
        for shape consistency. Output is the input to `parameter_set_hash` computation
        in `services/version/composite_hash.py`.
        """
        snake = asdict(self)
        canonical_keys = {
            "lookback_days_donchian": "LOOKBACK_DAYS_DONCHIAN",
            "ma_fast_days": "MA_FAST_DAYS",
            "ma_slow_days": "MA_SLOW_DAYS",
            "hurst_threshold": "HURST_THRESHOLD",
            "stop_distance_atr_mult": "STOP_DISTANCE_ATR_MULT",
            "atr_lookback_days": "ATR_LOOKBACK_DAYS",
            "min_holding_days": "MIN_HOLDING_DAYS",
            "vol_target_pct_annual": "VOL_TARGET_PCT_ANNUAL",
            "instrument_vol_lookback_days": "INSTRUMENT_VOL_LOOKBACK_DAYS",
            "roll_days_before_expiry": "ROLL_DAYS_BEFORE_EXPIRY",
        }
        return {canonical_keys[k]: str(v) for k, v in snake.items()}


def default_v1_parameters() -> V1Parameters:
    """Construct a `V1Parameters` from `V1_DEFAULTS`. Used for first deploy + tests."""
    raw = V1_DEFAULTS
    return V1Parameters(
        lookback_days_donchian=int(raw["LOOKBACK_DAYS_DONCHIAN"]),
        ma_fast_days=int(raw["MA_FAST_DAYS"]),
        ma_slow_days=int(raw["MA_SLOW_DAYS"]),
        hurst_threshold=Decimal(str(raw["HURST_THRESHOLD"])),
        stop_distance_atr_mult=Decimal(str(raw["STOP_DISTANCE_ATR_MULT"])),
        atr_lookback_days=int(raw["ATR_LOOKBACK_DAYS"]),
        min_holding_days=int(raw["MIN_HOLDING_DAYS"]),
        vol_target_pct_annual=Decimal(str(raw["VOL_TARGET_PCT_ANNUAL"])),
        instrument_vol_lookback_days=int(raw["INSTRUMENT_VOL_LOOKBACK_DAYS"]),
        roll_days_before_expiry=int(raw["ROLL_DAYS_BEFORE_EXPIRY"]),
    )
