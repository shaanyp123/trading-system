"""QuantConnect LEAN algorithm wrapper for `v1_trend_following`.

Runs in the QC cloud or LEAN Local container. The pure strategy logic lives in
`strategies/v1_trend_following/` (which has no QC imports and can be unit-tested
with pytest). This wrapper:

1. Subscribes to the Phase 1 sub-universe (CME micro futures + NYSE bond ETFs).
2. Emits a daily 17:30 ET signal cycle (US/Eastern, DST-aware).
3. For each cycle: assembles `BarSeries` from QC's `History` API, snapshots
   broker positions, calls `V1TrendFollowing.generate_signals`, persists the
   result to QC's ObjectStore as `signals/<session_date>.jsonl`.
4. The backend's `services/qc_adapter/` polls ObjectStore and ingests the file
   into the canonical `signals` table per backend-spec §2.10 (Phase 1 is
   ObjectStore-only — NO direct IBKR connection per CLAUDE.md anti-pattern A13).

Files referenced by this module are NOT installed via QC's standard pip layer;
QC's pip support is limited to a curated whitelist. Strategy logic is therefore
copied into the QC project workspace by `scripts/qc_sync.py` (Week 4 deliverable)
or, for now, manually by the operator on each strategy version bump.

Day 4 status (paper-broker ready):
- Brokerage model: InteractiveBrokers / Margin (matches Phase 2 production).
  Backtest fees/slippage simulate IBKR; live broker is selected at deploy time
  in the QC dashboard (recommended: 'Quant Connect Paper Trading' until IBKR
  Pro is approved). For LEAN Local, the broker is read from `lean.json`'s
  `environments.live-paper-qc.live-mode-brokerage = PaperBrokerage`.
- Parameters: read from QC's parameter map (`self.GetParameter`) with
  `strategies.v1_trend_following.parameters.V1_DEFAULTS` as fallback. The
  QC Live Trading deploy form exposes these as editable knobs.
- Warmup: 200 trading days (longest indicator lookback = MA_SLOW_DAYS).
- OnDailySignalCycle: heartbeat + ObjectStore write only. Strategy wiring
  lands Week 4 once the QC adapter golden test (claude-dev-guide §10.1
  Week 4 gate) is green.

Week 4 will add: BarSeries assembly via self.History; position snapshot via
self.Portfolio; call into self._strategy.generate_signals; JSONL emit to
ObjectStore at key f"signals/{self.Time.date()}.jsonl".

This file is intentionally NOT in the project's mypy/ruff target set
(see pyproject.toml `exclude` lists). QC's `AlgorithmImports` injects symbols
that mypy can't resolve, and QC code uses CamelCase per LEAN conventions which
ruff would flag. Lint discipline for this file is "matches QC's published LEAN
examples"; we don't enforce our backend conventions here.
"""

# QC injects these from `AlgorithmImports`; type: ignore because the import
# only resolves inside QC's runtime / LEAN Local container.
# ruff: noqa: F401, N802, N803, N806  -- QC's PascalCase API
from AlgorithmImports import *  # type: ignore[import-not-found,import-untyped]  # noqa: F403

# Local strategy imports — copied into the QC project workspace alongside this
# file by scripts/qc_sync.py.
# Day 4: leave commented out so this file lints/parses without the strategy
# module being present in the QC workspace. Wired in Week 4.
#
# from v1_trend_following.parameters import default_v1_parameters
# from v1_trend_following.strategy import V1TrendFollowing


# Phase 1 sub-universe — keep aligned with `strategies/v1_trend_following/parameters.py`
# `V1_CANDIDATE_UNIVERSE`. The Week 2 sub-universe verification step gates this
# list; do not add markets here without updating the canonical source.
PHASE1_FUTURES = ("MES", "MNQ", "MYM", "M2K", "MGC", "MCL", "MBT")
PHASE1_ETFS = ("TLT", "IEF", "SHY", "TIP")


# Parameter keys + V1_DEFAULTS fallbacks (kept in sync with
# strategies/v1_trend_following/parameters.py V1_DEFAULTS). QC's `GetParameter`
# returns `None` if a key is missing in the deploy config; we fall back to
# these values. The keys match the agent `tighten_parameter` enum (backend-spec
# §12.3) — never rename without a parameter_sets migration.
V1_PARAMETER_DEFAULTS = {
    "LOOKBACK_DAYS_DONCHIAN": "60",
    "MA_FAST_DAYS": "50",
    "MA_SLOW_DAYS": "200",
    "HURST_THRESHOLD": "0.55",
    "STOP_DISTANCE_ATR_MULT": "3.0",
    "ATR_LOOKBACK_DAYS": "20",
    "MIN_HOLDING_DAYS": "14",
    "VOL_TARGET_PCT_ANNUAL": "0.15",
    "INSTRUMENT_VOL_LOOKBACK_DAYS": "60",
    "ROLL_DAYS_BEFORE_EXPIRY": "5",
    "STARTING_CASH_USD": "15000",
}


class V1TrendFollowingAlgorithm(QCAlgorithm):  # type: ignore[misc,name-defined]  # noqa: F405
    """QC entry-point. Daily resolution; 17:30 ET signal cycle."""

    def Initialize(self):
        # Read parameters from QC's parameter map (set in lean.json's
        # `parameters` block + overridable in the QC Live deploy form).
        # Fall back to V1_DEFAULTS when a key is missing.
        params = {
            key: self.GetParameter(key) or default
            for key, default in V1_PARAMETER_DEFAULTS.items()
        }

        # Backtest window — operator can override in QC's UI; the defaults below
        # match the locked Phase 1 paper-trading window.
        self.SetStartDate(2026, 5, 1)
        self.SetEndDate(2026, 12, 31)
        self.SetCash(int(params["STARTING_CASH_USD"]))
        self.SetTimeZone("America/New_York")
        self.SetBenchmark("SPY")

        # Brokerage MODEL — controls backtest fees/slippage simulation. Match
        # IBKR Margin since Phase 2 will run live against IBKR. The LIVE
        # broker is independent: QC Cloud uses the broker chosen in the
        # dashboard deploy form; LEAN Local uses lean.json's
        # `environments.<env>.live-mode-brokerage`. See the Day 4 operator
        # runbook (lean/README.md) for dashboard step-by-step.
        self.SetBrokerageModel(
            BrokerageName.InteractiveBrokersBrokerage,  # noqa: F405
            AccountType.Margin,  # noqa: F405
        )

        # Subscribe to micro futures (continuous contract via QC's `Future` API).
        for ticker in PHASE1_FUTURES:
            future = self.AddFuture(
                f"/{ticker}",
                resolution=Resolution.Daily,  # noqa: F405
                extendedMarketHours=False,
                dataMappingMode=DataMappingMode.OpenInterest,  # noqa: F405
                contractDepthOffset=0,
            )
            future.SetFilter(0, 90)  # rolling 0-90 day window for contract selection

        # Subscribe to bond ETFs.
        for ticker in PHASE1_ETFS:
            self.AddEquity(
                ticker,
                resolution=Resolution.Daily,  # noqa: F405
                extendedMarketHours=False,
            )

        # Warmup — longest indicator lookback is MA_SLOW_DAYS (default 200).
        # QC fills indicators by replaying historical bars before the first
        # OnData fires; without this, the first ~200 sessions emit no signals.
        self.SetWarmUp(int(params["MA_SLOW_DAYS"]), Resolution.Daily)  # noqa: F405

        # Daily 17:30 ET scheduled action — fires after CME settlement.
        self.Schedule.On(
            self.DateRules.EveryDay(),
            self.TimeRules.At(17, 30, "America/New_York"),
            self.OnDailySignalCycle,
        )

        # Strategy parameters — Day 4 still skeleton: instantiated with the
        # parameter map but signals NOT yet routed to ObjectStore. Wired Week 4.
        # self._strategy = V1TrendFollowing(default_v1_parameters().override(params))
        self._params = params
        self.Log(
            f"v1_trend_following algorithm initialized "
            f"(skeleton; live_mode={self.LiveMode}; "
            f"params_keys={sorted(params.keys())})"
        )

    def OnData(self, data: Slice):  # noqa: F405
        """No per-tick logic in V1. All signal work happens in OnDailySignalCycle."""
        return

    def OnDailySignalCycle(self):
        """17:30 ET daily — assemble bars, run strategy, write signals/<date>.jsonl
        to ObjectStore for backend ingestion.

        Day 4 skeleton: emit a heartbeat log + ObjectStore heartbeat key. The
        backend's qc_adapter (Week 4) will start consuming `signals/*.jsonl` once
        strategy wiring lands. Until then, the heartbeat key gives the operator
        a way to confirm via QC's ObjectStore tab that the daily cycle is firing.
        """
        if self.IsWarmingUp:
            return

        session_date = self.Time.date().isoformat()
        equity = self.Portfolio.TotalPortfolioValue
        msg = (
            f"signal_cycle_tick utc={self.UtcTime} et={self.Time} "
            f"session_date={session_date} equity={equity}"
        )
        self.Log(msg)

        # Heartbeat-only ObjectStore write. Week 4 replaces this with a real
        # signals/<date>.jsonl emit per backend-spec §2.10. Key namespace
        # `heartbeat/` is reserved for liveness pings and will not be consumed
        # by qc_adapter (which polls `signals/`, `state/`, `events/`).
        heartbeat_payload = (
            f'{{"session_date":"{session_date}",'
            f'"utc":"{self.UtcTime}",'
            f'"equity_usd":"{equity}",'
            f'"live_mode":{str(self.LiveMode).lower()}}}'
        )
        self.ObjectStore.Save(f"heartbeat/{session_date}.json", heartbeat_payload)
        return
