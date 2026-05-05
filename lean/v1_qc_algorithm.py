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

Day 2 status: skeleton — algorithm structure + universe subscription + a stub
`OnData` that just logs. Wiring to actual `V1TrendFollowing` happens in Week 4
when QC ObjectStore parity is verified (implementation-guide §3 Week 4 + claude-
dev-guide §10.1 Week 4 gate).

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
# Day 2: leave commented out so this file lints/parses without the strategy
# module being present in the QC workspace.
#
# from v1_trend_following.parameters import default_v1_parameters
# from v1_trend_following.strategy import V1TrendFollowing


# Phase 1 sub-universe — keep aligned with `strategies/v1_trend_following/parameters.py`
# `V1_CANDIDATE_UNIVERSE`. The Week 2 sub-universe verification step gates this
# list; do not add markets here without updating the canonical source.
PHASE1_FUTURES = ("MES", "MNQ", "MYM", "M2K", "MGC", "MCL", "MBT")
PHASE1_ETFS = ("TLT", "IEF", "SHY", "TIP")


class V1TrendFollowingAlgorithm(QCAlgorithm):  # type: ignore[misc,name-defined]  # noqa: F405
    """QC entry-point. Daily resolution; 17:30 ET signal cycle."""

    def Initialize(self):
        # Backtest window — operator can override in QC's UI; the defaults below
        # match the locked Phase 1 paper-trading window.
        self.SetStartDate(2026, 5, 1)
        self.SetEndDate(2026, 12, 31)
        self.SetCash(15000)
        self.SetTimeZone("America/New_York")
        self.SetBenchmark("SPY")

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

        # Daily 17:30 ET scheduled action — fires after CME settlement.
        self.Schedule.On(
            self.DateRules.EveryDay(),
            self.TimeRules.At(17, 30, "America/New_York"),
            self.OnDailySignalCycle,
        )

        # Strategy parameters — Day 2 skeleton: instantiate but don't yet route
        # signals to ObjectStore. Wired in Week 4.
        # self._strategy = V1TrendFollowing(default_v1_parameters())
        self.Log("v1_trend_following algorithm initialized (skeleton)")

    def OnData(self, data: Slice):  # noqa: F405
        """No per-tick logic in V1. All signal work happens in OnDailySignalCycle."""
        return

    def OnDailySignalCycle(self):
        """17:30 ET daily — assemble bars, run strategy, write signals/<date>.jsonl
        to ObjectStore for backend ingestion.

        Day 2 skeleton: emit a heartbeat log only. Implementation lands Week 4
        once QC adapter golden test (claude-dev-guide §10.1 Week 4 gate) is
        green.
        """
        self.Log(
            f"signal_cycle_tick utc={self.UtcTime} et={self.Time} "
            f"equity={self.Portfolio.TotalPortfolioValue}"
        )
        # TODO(week-4): assemble BarSeries from self.History(...); snapshot
        # positions from self.Portfolio; call self._strategy.generate_signals;
        # write JSONL to ObjectStore at key f"signals/{self.Time.date()}.jsonl".
        return
