"""LEAN Local algorithm wrapper for `v1_trend_following` (post-pivot 2026-05-12).

Runs inside the `lean_local` Docker container on the operator's Hetzner VPS
(`quantconnect/lean:latest` image). The pure strategy logic lives in
`strategies/v1_trend_following/` (broker-agnostic; no QC or LEAN imports;
pytest-friendly). This wrapper:

1. Subscribes to the Phase 1 sub-universe (CME micro futures + NYSE bond ETFs).
2. Emits a daily 17:30 ET signal cycle (US/Eastern, DST-aware).
3. For each cycle: assembles ``BarSeries`` from LEAN's ``History`` API, snapshots
   broker positions, calls ``V1TrendFollowing.generate_signals``, and **POSTs
   each emitted signal to the backend at ``POST /api/internal/lean/signals``
   with shared-bearer auth.**
4. The backend's ``services/api/routes/internal/lean.py`` endpoint validates
   the bearer, writes a ``signal_emitted`` audit event via
   ``services.audit.writer.append_audit_event``, and INSERTs the row into
   the ``signals`` table per backend-spec §2.10.

**Architectural pivot 2026-05-12 (DP-025 → Option 4):** This file was previously
``lean/v1_qc_algorithm.py`` and wrote to QC ObjectStore via ``self.object_store.save()``;
QC's ``/object/get`` REST endpoint is gated behind the Institutional subscription
tier on the operator's account, so the polling-from-backend architecture is
infeasible. Pivoted to LEAN Local + HTTP POST to backend; QC Cloud is no longer
in the production path. See ``Docs/decisions-log.md`` 2026-05-12 entry
"Phase-1 architecture pivot — QC ObjectStore → LEAN Local + direct IBKR"
and ``Docs/backend-spec.md`` §1.2 (post-pivot architecture).

**Where this runs:** ``quantconnect/lean:latest`` Docker container on the
operator's VPS. The container mounts ``./lean/`` as the algorithm directory
+ ``./strategies/`` for the broker-agnostic strategy package. Networking is
inside the Docker ``internal`` network; the api endpoint resolves to
``http://api:8000/api/internal/lean/signals`` via Docker DNS.

**Authentication:** ``LEAN_LOCAL_BEARER_TOKEN`` env var (sops-encrypted in
``secrets/<env>.enc.yaml::lean.api_bearer_token``; injected into the container
via the ``lean_local`` entrypoint). The backend's ``LeanAuthMiddleware`` runs
outermost in the request stack and constant-time-compares against
``services.api.config.APISettings.lean_local_bearer_token``.

**Brokerage configuration (POST-CEREMONY 2026-05-12):** LEAN binds to its
built-in ``PaperBrokerage`` simulator (``live-mode-brokerage = PaperBrokerage``
in ``lean.json``'s ``paper-internal`` env). The strategy never calls
broker-mutating APIs (``self.market_order`` / ``self.limit_order`` / etc.) —
it only emits signals via HTTP POST to the api. **The api owns the real
IBKR broker contract via ``services/execution/ibkr_adapter.py``;** LEAN's
brokerage object is read-only from the strategy's perspective (used only
to satisfy ``self.portfolio[symbol]`` queries, which under PaperBrokerage
return flat positions — correct post-pivot because LEAN doesn't own
positions, the api does). The pre-ceremony plan bound LEAN to
``InteractiveBrokersBrokerage`` directly, but the bare ``quantconnect/lean:latest``
image doesn't ship ``QuantConnect.Brokerages.InteractiveBrokers.dll`` so
live-mode boot crash-looped on LEAN's Composer broker-factory lookup
(``Sequence contains no matching element``). See ``Docs/decisions-log.md``
2026-05-12 entry "Post-ceremony session — LEAN container's IBKR DLL gap".

API convention: snake_case (QC migrated the Python API from PascalCase to
snake_case ~2024; the local LEAN runtime accepts both but snake_case is the
documented preferred form). Method names, enum values, and keyword arguments
are all snake_case / SCREAMING_SNAKE_CASE. Class names (QCAlgorithm, Slice,
Resolution, BrokerageName, etc.) remain PascalCase per Python convention.

**Pivot-PR-D (2026-05-12) — signal_emitted emission live.** The cycle now
runs `V1TrendFollowing.generate_signals(...)` against the active universe
and POSTs each emitted signal to the backend with the full payload required
by `services/api/routes/internal/lean.py::post_lean_signal` (market /
direction / target_contracts / decision_price / sizing_trace /
strategy_version). Sizing for Phase 1 is the conservative single-lot
allocation (target_contracts=1 per signal) — the full Stage 0-5 pipeline
runs server-side as approved signals are dispatched (Worker-PR-1's
order_placement_worker). The single-lot allocation gives the operator
explicit per-signal approval control during the 30-CME-session paper
clock; once the Stage 0-5 server-side path is wired, this scales to
multi-contract per spec §2.4.1.

This file is intentionally NOT in the project's mypy/ruff target set (see
``pyproject.toml`` ``exclude`` lists). LEAN's ``AlgorithmImports`` injects
symbols that mypy can't resolve. Lint discipline for this file is "matches
LEAN's published examples"; we don't enforce our backend conventions here.
"""

# LEAN injects these symbols from `AlgorithmImports`; type: ignore because the
# import only resolves inside LEAN's runtime / Docker container.
# ruff: noqa: F401, N802, N803, N806  -- LEAN API symbols
from AlgorithmImports import *  # type: ignore[import-not-found,import-untyped]  # noqa: F403

# Standard-library imports for HTTP POST + JSON serialization. These are
# available inside the `quantconnect/lean:latest` Python runtime; no
# `pip install` needed.
import json
import os
import urllib.error
import urllib.request
from datetime import date as _date
from decimal import Decimal

# Local strategy imports — `./strategies/` is mounted into the container
# by docker-compose at `/Lean/strategies` (lowercase, read-only), and
# the lean_local entrypoint adds `/Lean` to PYTHONPATH so the
# `strategies` package namespace resolves. Imports use the
# `strategies.v1_trend_following.X` convention to match the package's
# internal absolute imports (strategy.py does `from
# strategies.v1_trend_following.indicators import (...)` etc.).
# The strategy module is broker-agnostic pure Python — no QC / LEAN
# imports — so this import is safe to run inside LEAN's Python runtime.
from strategies.v1_trend_following.lean_history_adapter import (  # type: ignore[import-not-found]
    parse_history_to_bars,
)
from strategies.v1_trend_following.parameters import (  # type: ignore[import-not-found]
    V1Parameters,
)
from strategies.v1_trend_following.signals import (  # type: ignore[import-not-found]
    Bar,
    BarSeries,
    Direction,
    Position,
)
from strategies.v1_trend_following.strategy import (  # type: ignore[import-not-found]
    STRATEGY_NAME,
    V1TrendFollowing,
)


# Phase 1 sub-universe — keep aligned with `strategies/v1_trend_following/parameters.py`
# `V1_CANDIDATE_UNIVERSE`. The Week 2 sub-universe verification step gates this
# list; do not add markets here without updating the canonical source.
# /MCL sidelined 2026-05-23 (PR #228). See
# ``strategies/v1_trend_following/parameters.py``
# ``V1_SIDELINED_MARKETS`` for the canonical sideline registry + the
# re-enable runbook. The 6 remaining micros all resolve cleanly under
# the PR-#225 + PR-#226 SID-hash + bare-ticker + /MYM-cbot fix chain.
PHASE1_FUTURES = ("MES", "MNQ", "MYM", "M2K", "MGC", "MBT")
PHASE1_ETFS = ("TLT", "IEF", "SHY", "TIP")


# Parameter keys + V1_DEFAULTS fallbacks (kept in sync with
# strategies/v1_trend_following/parameters.py V1_DEFAULTS).
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
    "STARTING_CASH_USD": "100000",
}


# Backend POST configuration — read from env vars at algorithm initialize.
# The container entrypoint sets these from sops-decrypted secrets.
_API_BASE_URL_DEFAULT = "http://api:8000"
_API_TIMEOUT_SECONDS = 10.0


# Strategy version identifier sent in `signal_emitted` payloads. Format mirrors
# the QC adapter convention `qc_algorithm_version` (backend-spec §3.3 / §4.5.1)
# so the audit-side `strategy_hash` derivation in
# `services.qc_adapter.signal_ingestion._derive_strategy_hash` continues to
# work without modification. Phase 1 carries a static string — once a build
# system stamps the git SHA into a constant at deploy time, this becomes
# `f"{STRATEGY_NAME}@{git_sha}"`. The static string is acceptable for the
# Phase 1 ceremony because `_derive_strategy_hash` sha1's any non-40-hex
# suffix into a deterministic hash, preserving audit-chain integrity.
_STRATEGY_VERSION_DEFAULT = f"{STRATEGY_NAME}@phase1-pivot-d"


class V1TrendFollowingAlgorithm(QCAlgorithm):  # type: ignore[misc,name-defined]  # noqa: F405
    """LEAN Local entry-point. Daily resolution; 17:30 ET signal cycle.

    Post-pivot 2026-05-12: emits via HTTP POST to backend, not via ObjectStore.
    """

    def initialize(self):
        # Read parameters from LEAN's parameter map (set in lean.json's
        # `parameters` block + overridable at deploy time). Fall back to
        # V1_DEFAULTS when a key is missing.
        params = {
            key: self.get_parameter(key) or default
            for key, default in V1_PARAMETER_DEFAULTS.items()
        }

        # Backend POST configuration — sourced from container env vars.
        # Hard fail-close at initialize if the bearer is missing: an algorithm
        # that emits unauthenticated POSTs to the backend silently fails (the
        # backend returns 401 + canonical envelope) and the signals never land
        # in audit_log. Better to crash the algorithm visibly than silently
        # emit signals into a black hole.
        self._api_base_url = os.environ.get("LEAN_LOCAL_API_BASE_URL", _API_BASE_URL_DEFAULT)
        self._api_bearer_token = os.environ.get("LEAN_LOCAL_BEARER_TOKEN", "")
        self._strategy_version_str = os.environ.get(
            "LEAN_STRATEGY_VERSION", _STRATEGY_VERSION_DEFAULT
        )
        if not self._api_bearer_token:
            raise RuntimeError(
                "LEAN_LOCAL_BEARER_TOKEN env var missing or empty. "
                "Verify sops decryption + container env-var mapping per "
                "deploy/lean_local/README.md Step 2."
            )

        # Backtest window (only relevant in backtest mode; live mode ignores).
        self.set_start_date(2026, 5, 1)
        self.set_end_date(2026, 12, 31)
        self.set_cash(int(params["STARTING_CASH_USD"]))
        self.set_time_zone("America/New_York")
        self.set_benchmark("SPY")

        # Brokerage MODEL — controls fee/slippage simulation for any
        # orders LEAN's simulator fills. Under PaperBrokerage (post-ceremony
        # 2026-05-12), LEAN never actually places orders — the strategy only
        # POSTs signals to the api, and the api dispatches via ib-async.
        # The MODEL is still useful for the api side: real IBKR fills land
        # with IBKR's fee schedule, and pinning the LEAN-side model to the
        # IBKR profile keeps backtest cost simulations comparable to the
        # api-side live fills. `BrokerageName.INTERACTIVE_BROKERS_BROKERAGE`
        # is a pure-MODEL enum (it loads `InteractiveBrokersBrokerageModel`
        # from `QuantConnect.Brokerages`, which ships in the base image);
        # it does NOT depend on `QuantConnect.Brokerages.InteractiveBrokers.dll`
        # (which is missing from `quantconnect/lean:latest` and was the
        # cause of the post-ceremony 2026-05-12 boot crash). If a future
        # LEAN release drops the IBKR model from its core assemblies the
        # same way the brokerage was dropped, swap to `BrokerageName.DEFAULT`.
        self.set_brokerage_model(
            BrokerageName.INTERACTIVE_BROKERS_BROKERAGE,  # noqa: F405
            AccountType.MARGIN,  # noqa: F405
        )

        # Track symbol references per market key so `on_daily_signal_cycle`
        # can iterate the universe + assemble BarSeries from the history
        # provider. Futures keys are `/MES` form (matching V1_CANDIDATE_UNIVERSE);
        # ETF keys are bare tickers like `TLT`. The dict value is a LEAN Symbol
        # object — opaque to us but accepted by `self.history()` /
        # `self.portfolio[...]` / `self.securities[...]`.
        self._market_subscriptions: dict[str, object] = {}

        # Subscribe to micro futures (continuous contract via QC's `Future` API).
        for ticker in PHASE1_FUTURES:
            # ``add_future`` requires the BARE ticker — LEAN's
            # ``QCAlgorithm.AddFuture`` prepends ``/`` itself when constructing
            # the canonical alias (``var alias = "/" + ticker``) and passes the
            # caller's ``ticker`` unchanged to
            # ``SecurityIdentifier.GenerateFuture(...)``. Passing ``"/MES"``
            # results in ``sid.Symbol = "/MES"`` (with slash) + ``alias = "//MES"``
            # (double slash) + ``symbol.Value = "//MES"``. ``LiveMappingEventProvider``
            # then logs the canonical as ``//MES`` and tries to look up
            # ``_bySymbol["/MES"]`` in the MapFileResolver — but the on-disk
            # map_file's permtick is ``MES`` (filename ``mes.csv``) and its
            # MappedSymbol column starts with ``mes`` (bare permtick on the
            # inception sentinel; ``mes <sid_hash>`` on per-roll rows after
            # the 2026-05-22 SID-hash extension), so the lookup misses and
            # ``MapFile.Count`` stays at 0 for every futures symbol.
            #
            # The 2026-05-22 21:30 UTC + 23:02 UTC probes captured exactly
            # this failure mode (``hist_type=DataFrame hist_len=0 hist_cols=[]``
            # for /MES /MNQ /MYM /M2K /MGC /MCL /MBT vs ``hist_len=205`` for
            # the 4 ETFs from the same code path). The bar_sync map_file
            # synthesizer (PR #222) landed populated rolls but the
            # ``_bySymbol["/MES"]`` lookup was still missing them. LEAN's
            # reference algorithm
            # ``Algorithm.CSharp/ContinuousFuturesDailyRegressionAlgorithm.cs``
            # uses ``AddFuture(Futures.Indices.SP500EMini, ...)`` where
            # ``SP500EMini = "ES"`` — the bare ticker. We mirror that here.
            #
            # This is the bare-ticker change FIRST landed in PR #223 (which
            # got reverted via PR #224 because PR #222's bare-permtick
            # ``MappedSymbol`` on per-roll rows crashed LEAN's
            # ``SecurityIdentifier.Parse`` with "The string must be splittable
            # on space into two parts in SecurityIdentifier.cs:line 818").
            # The 2026-05-22 follow-up landed the LEAN-canonical
            # ``<perm> <SID-hash>`` ``MappedSymbol`` rendering in
            # ``services/data/map_file_synthesis.py`` (validated against 55/55
            # historical ES contracts in LEAN's bundled
            # ``Data/future/cme/map_files/es.csv``); this PR re-applies the
            # bare-ticker change atomically with that synthesizer update so
            # the crash mode can't recur.
            #
            # ``_market_subscriptions`` keeps the ``/<ticker>`` form as its
            # dict key so the existing backend signal-payload contract
            # (``market="/MES"``) and the strategy's logging stay unchanged.
            # See ``Docs/decisions-log.md`` 2026-05-22 entries "bar_sync
            # map_file synthesis lands" + "SID-hash MappedSymbol + bare-ticker
            # add_future to unblock futures self.history()" for the chain.
            #
            # ``data_normalization_mode=Raw`` — explicitly Raw for the V1
            # strategy under any data source.  ``add_future()``'s default
            # normalization mode resolves to BackwardsRatio per QC docs forum
            # 17093 staff response, which requires factor_files we can't
            # synthesize under the Option C data-layer pivot. Raw mode operates
            # on un-adjusted per-expiry contract prices — LEAN's continuous-
            # contract resolver picks the active contract per session date via
            # ``DataMappingMode.OPEN_INTEREST`` + the per-day universe file
            # produced by bar_sync. See ``Docs/decisions-log.md`` 2026-05-20
            # data-layer pivot entry + 2026-05-12 Path 4 entry for the
            # original explicit-Raw rationale.
            future = self.add_future(
                ticker,
                resolution=Resolution.DAILY,  # noqa: F405
                extended_market_hours=False,
                data_mapping_mode=DataMappingMode.OPEN_INTEREST,  # noqa: F405
                data_normalization_mode=DataNormalizationMode.RAW,  # noqa: F405
                contract_depth_offset=0,
            )
            # ``set_filter(-365, 90)`` — extend the continuous-future chain
            # window to include contracts that expired up to 365 days ago
            # (rather than ``(0, 90)`` which only includes not-yet-expired
            # contracts in the next 90 days). The 2026-05-24 21:30 UTC LEAN
            # cycle's probe captured ``hist_len=176 hist_cols=['close', ...]``
            # for all 6 active futures vs ``hist_len=205`` for the 4 ETFs,
            # even AFTER PR #229's historical-contract backfill placed
            # ~7 historical contracts per ticker into the daily zip. Root
            # cause: ``set_filter(0, 90)`` limits LEAN's continuous-future
            # universe to currently-trading contracts; LEAN's resolver
            # therefore loads bars only from the current front-month
            # (e.g. /MES 202606 starting from its listing date ~2025-09-17),
            # and ignores the 6 historical contracts the backfill wrote
            # to disk. The fix is to relax the filter's lower bound so
            # historical contracts re-enter the chain — LEAN then stitches
            # them per the synthesized map_file's roll boundaries +
            # ``self.history()`` can reach back further than the current
            # contract's listing window. ``MA_SLOW_DAYS=200`` requires
            # ≥ 200 daily bars to compute, so this is the gate on
            # actual signal emission. See ``Docs/decisions-log.md``
            # 2026-05-24 entry "set_filter extended to -365 days for
            # historical contract stitching".
            future.set_filter(-365, 90)
            self._market_subscriptions[f"/{ticker}"] = future.symbol

        # Subscribe to bond ETFs.
        for ticker in PHASE1_ETFS:
            equity = self.add_equity(
                ticker,
                resolution=Resolution.DAILY,  # noqa: F405
                extended_market_hours=False,
            )
            self._market_subscriptions[ticker] = equity.symbol

        # Warmup — longest indicator lookback is MA_SLOW_DAYS (default 200).
        # Pad slightly so the first signal cycle has enough history for ATR
        # (which needs lookback + 1 prior bar) without an off-by-one trim.
        self._strategy_min_bars: int | None = None
        warmup_days = int(params["MA_SLOW_DAYS"]) + int(params["ATR_LOOKBACK_DAYS"]) + 5
        self.set_warm_up(warmup_days, Resolution.DAILY)  # noqa: F405

        # Daily 17:30 ET scheduled action — fires after CME settlement.
        self.schedule.on(
            self.date_rules.every_day(),
            self.time_rules.at(17, 30),
            self.on_daily_signal_cycle,
        )

        self._params = params
        self._v1_parameters: V1Parameters | None = None  # built lazily on first cycle
        self.log(
            f"v1_strategy initialized (post-pivot 2026-05-12, Pivot-PR-D) "
            f"live_mode={self.live_mode} api_base={self._api_base_url} "
            f"strategy_version={self._strategy_version_str} "
            f"params_keys={sorted(params.keys())}"
        )

        # DATA-LAYER PIVOT v2 RESTORATION (Option C; 2026-05-20 evening):
        # the v1 attempt (PR #195) removed the `_log_universe_freshness`
        # invocation because LEAN was supposed to read market data directly
        # from IBKR via the `InteractiveBrokersBrokerage` data-queue-handler
        # — no on-disk universe files to walk. The v1 path was blocked at
        # deploy (IBAutomater gateway-launch conflict); Option C revives
        # the on-disk path with api-managed freshness via
        # `services/data/bar_sync.py` (BarSyncWorker on clientId=2; daily
        # 17:00 ET). The freshness check resumes its original purpose:
        # catching api-side bar-sync failures (if BarSyncWorker fails for
        # 5+ calendar days the operator sees `v1_universe_data_stale` in
        # the LEAN log + the AsyncTaskMonitor's pending P2 alert hook
        # fires per the 2026-05-17 evening incident's follow-up #3).
        # See Docs/decisions-log.md 2026-05-20 evening + the v2 landing
        # entry for the full chain.
        self._log_universe_freshness()

        # Emit a startup heartbeat so the operator can confirm the algorithm
        # successfully reached this point + can authenticate against the
        # backend before the first 17:30 ET signal cycle. Best-effort:
        # network errors don't crash initialize (LEAN may not yet have
        # warmed up enough for HTTP to be reliable). Errors are logged.
        self._post_event("lean_strategy_initialized")

    def on_data(self, data: Slice):  # noqa: F405
        """No per-tick logic in V1. All signal work happens in on_daily_signal_cycle."""
        return

    def on_daily_signal_cycle(self):
        """17:30 ET daily — run V1 strategy + POST each signal to the backend.

        Order of operations (Pivot-PR-D scope):

        1. Skip if still warming up.
        2. Build / refresh `V1Parameters` (lazy; depends on `self._params`
           which is set in `initialize()`).
        3. For each subscribed market, assemble `BarSeries` via `self.history`.
        4. Snapshot positions via `self.portfolio`.
        5. Call `V1TrendFollowing.generate_signals(...)`.
        6. POST a `lean_cycle_heartbeat` event with the per-cycle summary
           (signal count, rejection count, equity, live_mode flag) so the
           backend's `liveness_probes` table sees the algorithm alive even
           on no-signal days.
        7. For each emitted signal, POST a `signal_emitted` event with the
           full payload required by
           `services/api/routes/internal/lean.py::post_lean_signal`.

        Sizing for Phase 1 is conservative single-lot allocation
        (`target_contracts=1`). The full Stage 0-5 sizing pipeline runs
        server-side when approved signals are dispatched to the broker
        (Worker-PR-1 `services/risk/order_placement_worker.py`); the strategy
        contributes the indicator snapshot (Stage 0 `strategy_inputs`).
        """
        if self.is_warming_up:
            return

        session_date = self.time.date()
        equity = self.portfolio.total_portfolio_value

        # Step 2: ensure V1Parameters exist + cached.
        try:
            v1_params = self._get_v1_parameters()
        except Exception as exc:  # noqa: BLE001 -- log + heartbeat-only fallback
            self.log(f"v1_params_build_failed session_date={session_date} exc={exc!r}")
            self._post_event(
                "lean_cycle_heartbeat",
                extra={
                    "session_date_et": session_date.isoformat(),
                    "equity_usd": str(equity),
                    "live_mode": bool(self.live_mode),
                    "signals_emitted_count": 0,
                    "rejections_count": 0,
                    "error": "v1_params_build_failed",
                },
            )
            return

        strategy = V1TrendFollowing(parameters=v1_params)
        self._strategy_min_bars = strategy.min_required_bars

        # Step 3-4: build active universe + positions.
        active_universe: dict[str, BarSeries] = {}
        current_positions: dict[str, Position] = {}
        history_failures: list[str] = []
        for market_key, symbol in self._market_subscriptions.items():
            series = self._build_bar_series(
                market_key=market_key,
                symbol=symbol,
                count=strategy.min_required_bars + 5,
            )
            if series is None:
                history_failures.append(market_key)
                continue
            active_universe[market_key] = series
            current_positions[market_key] = self._snapshot_position(
                market_key=market_key, symbol=symbol
            )

        if history_failures:
            self.log(
                f"v1_history_unavailable session_date={session_date} "
                f"failed_markets={history_failures}"
            )

        # Step 5: run the strategy.
        try:
            result = strategy.generate_signals(
                active_universe=active_universe,
                current_positions=current_positions,
                as_of_session_date=session_date,
            )
        except Exception as exc:  # noqa: BLE001 -- log + heartbeat-only fallback
            self.log(
                f"v1_generate_signals_failed session_date={session_date} exc={exc!r}"
            )
            self._post_event(
                "lean_cycle_heartbeat",
                extra={
                    "session_date_et": session_date.isoformat(),
                    "equity_usd": str(equity),
                    "live_mode": bool(self.live_mode),
                    "signals_emitted_count": 0,
                    "rejections_count": 0,
                    "error": "generate_signals_failed",
                },
            )
            return

        # Per-market rejection visibility — added 2026-05-25 after the saga's
        # 21:30 UTC validation cycle showed ``signals_emitted_count=0
        # rejections_count=10`` with no way to tell which filter (no_breakout
        # vs hurst_below_threshold vs trend_filter_failed vs ...) rejected
        # each of the 10 markets. ``result.rejections`` is
        # ``tuple[tuple[str, RejectionReason], ...]`` (see
        # ``strategies/v1_trend_following/signals.py::SignalGenerationResult``);
        # the reasons were always computed, just never surfaced at the LEAN
        # runtime layer. Each rejection becomes a structured log line for
        # operator at-a-glance debugging; aggregate counts by reason are
        # appended to the existing summary for trend visibility across cycles.
        # Hot-fix scope (lean/**); pure observability addition.
        rejection_reason_counts: dict[str, int] = {}
        for rejected_market, reason in result.rejections:
            reason_str = reason.value
            rejection_reason_counts[reason_str] = (
                rejection_reason_counts.get(reason_str, 0) + 1
            )
            self.log(
                f"v1_signal_rejected session_date={session_date} "
                f"market={rejected_market} reason={reason_str}"
            )

        signals_count = len(result.signals)
        rejections_count = len(result.rejections)
        self.log(
            f"v1_signals_generated session_date={session_date} "
            f"signals_emitted_count={signals_count} rejections_count={rejections_count} "
            f"reasons={rejection_reason_counts}"
        )

        # Step 6: heartbeat with per-cycle summary (always emitted so
        # liveness_probes sees the algorithm alive even when no signals).
        self._post_event(
            "lean_cycle_heartbeat",
            extra={
                "session_date_et": session_date.isoformat(),
                "equity_usd": str(equity),
                "live_mode": bool(self.live_mode),
                "signals_emitted_count": signals_count,
                "rejections_count": rejections_count,
            },
        )

        # Step 7: emit each signal.
        for signal in result.signals:
            target_contracts = self._naive_target_contracts(equity=equity)
            if target_contracts <= 0:
                self.log(
                    f"v1_signal_skipped_zero_size session_date={session_date} "
                    f"market={signal.market} equity={equity}"
                )
                continue
            sizing_trace = self._build_minimal_sizing_trace(
                signal=signal,
                target_contracts=target_contracts,
                equity=equity,
            )
            self._post_event(
                "signal_emitted",
                extra={
                    "session_date_et": session_date.isoformat(),
                    "equity_usd": str(equity),
                    "live_mode": bool(self.live_mode),
                    "market": signal.market,
                    "direction": signal.direction.value,
                    "target_contracts": target_contracts,
                    "decision_price": str(signal.decision_price),
                    "sizing_trace": sizing_trace,
                    "strategy_version": self._strategy_version_str,
                },
            )
        return

    # ------------------------------------------------------------------
    # Strategy plumbing helpers (Pivot-PR-D)
    # ------------------------------------------------------------------

    def _get_v1_parameters(self) -> V1Parameters:
        """Build (and cache) the `V1Parameters` dataclass from the LEAN parameter map.

        Lazy — built on the first `on_daily_signal_cycle` rather than in
        `initialize()` so a malformed parameter is surfaced as a cycle log
        line (after warmup) instead of crashing the algorithm at boot.
        """
        if self._v1_parameters is not None:
            return self._v1_parameters
        raw = self._params
        # TODO PR-B: read STRATEGY_DECOMMISSIONED + EXIT_AUTO_APPROVE from raw
        # so an operator UPDATE to parameter_sets.parameters propagates into
        # the daily LEAN cycle. Until then both fall back to V1Parameters
        # dataclass defaults (False/False) and the kill-switch ceremony
        # documented in Docs/exit-pipeline-design.md §11 R6 is silently inert
        # via this entrypoint. Exit-emission wiring (generate_exit_candidates
        # call) also lands with PR-B; reading the flag here without that
        # would be a half-fix.
        self._v1_parameters = V1Parameters(
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
        return self._v1_parameters

    def _build_bar_series(
        self, *, market_key: str, symbol: object, count: int
    ) -> BarSeries | None:
        """Call `self.history(symbol, count, Resolution.DAILY)` → `BarSeries`.

        Returns None on any failure (insufficient history, history API
        error, malformed bars). The strategy pipeline interprets a missing
        market as "no bars for this session" and skips it; no rejection
        event is emitted.

        Parse logic lives in
        ``strategies.v1_trend_following.lean_history_adapter.parse_history_to_bars``
        — pure-Python, unit-tested, handles both the modern QC Python API
        (DataFrame with MultiIndex ``(symbol, time)`` + lowercase OHLCV
        columns) AND the legacy iterable-of-``TradeBar`` form. The wrapper
        here keeps the LEAN-runtime-specific concerns: the
        ``self.history(...)`` call, structured ``self.log(...)`` lines on
        each failure mode, and the sort + dedup + ``BarSeries``
        construction at the end.

        The 2026-05-12 ceremony surfaced the DataFrame parsing gap when
        the first cycle with seeded data emitted ``v1_history_unavailable``
        for every market — the legacy iteration treated a DataFrame as an
        iterable of column-name strings, which all skipped the
        ``end_time is None: continue`` branch and left ``bars`` empty. PR
        #129 fixed at the surface; the adapter + tests in this PR lock
        the regression contract.
        """
        try:
            history = self.history(symbol, count, Resolution.DAILY)  # noqa: F405
        except Exception as exc:  # noqa: BLE001 -- log + skip; alternative is crash
            self.log(f"v1_history_call_failed market={market_key} exc={exc!r}")
            return None

        if history is None:
            return None

        try:
            bars = parse_history_to_bars(history)
        except Exception as exc:  # noqa: BLE001 -- log + skip
            self.log(f"v1_history_parse_failed market={market_key} exc={exc!r}")
            return None

        if not bars:
            # Fires when LEAN returned a non-empty history that the parser
            # dropped to ``[]`` (e.g. DataFrame whose every row had a NaN
            # OHLC cell). Distinct from the empty-DataFrame case where
            # ``len(history) == 0`` from the start. Useful operator signal
            # for diagnosing parser-side bugs vs LEAN-side data-layer
            # empties even after the 2026-05-22 PR #220 probe was retired.
            try:
                hist_len: object = len(history)
            except (TypeError, AttributeError):
                try:
                    hist_len = history.shape[0]  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 -- diagnostic only
                    hist_len = "?"
            self.log(
                f"v1_history_parsed_empty market={market_key} "
                f"hist_type={type(history).__name__} hist_len={hist_len}"
            )
            return None
        # BarSeries `__post_init__` enforces strictly-increasing session_date.
        # LEAN returns bars in chronological order but defend against accidental
        # reverse-iteration or duplicate dates.
        bars.sort(key=lambda b: b.session_date)
        deduped: list[Bar] = []
        prev: _date | None = None
        for b in bars:
            if prev is None or b.session_date > prev:
                deduped.append(b)
                prev = b.session_date
        try:
            return BarSeries(market=market_key, bars=tuple(deduped))
        except ValueError as exc:
            self.log(f"v1_barseries_invalid market={market_key} exc={exc!r}")
            return None

    def _snapshot_position(self, *, market_key: str, symbol: object) -> Position:
        """Snapshot `self.portfolio[symbol]` to the strategy's `Position` dataclass.

        Returns a FLAT position when no holdings or holdings of zero quantity.
        The strategy's `MIN_HOLDING_DAYS` check needs `opened_at_session_date`;
        LEAN exposes this via `holding.invested_since` (modern) or `.InvestedSince`
        (older). When neither is available, falls back to None which the
        strategy treats as "unknown — apply MIN_HOLDING_DAYS check using
        the current session date" (conservative; produces TREND_FILTER_FAILED
        or MIN_HOLDING_DAYS_NOT_SATISFIED rather than crashing).
        """
        holding = None
        try:
            holding = self.portfolio[symbol]
        except Exception:  # noqa: BLE001 -- portfolio dict may not contain symbol
            holding = None
        if holding is None:
            return Position(
                market=market_key,
                direction=Direction.FLAT,
                quantity=0,
                avg_cost=Decimal("0"),
                opened_at_session_date=None,
            )

        quantity_raw = getattr(holding, "quantity", None)
        if quantity_raw is None:
            quantity_raw = getattr(holding, "Quantity", 0)
        try:
            quantity = int(quantity_raw)
        except (TypeError, ValueError):
            quantity = 0

        if quantity > 0:
            direction = Direction.LONG
        elif quantity < 0:
            direction = Direction.SHORT
        else:
            direction = Direction.FLAT

        avg_price = getattr(holding, "average_price", None)
        if avg_price is None:
            avg_price = getattr(holding, "AveragePrice", 0)
        try:
            avg_cost = Decimal(str(avg_price))
        except Exception:  # noqa: BLE001
            avg_cost = Decimal("0")

        opened_at: _date | None = None
        if direction is not Direction.FLAT:
            invested_since = getattr(holding, "invested_since", None) or getattr(
                holding, "InvestedSince", None
            )
            if invested_since is not None and hasattr(invested_since, "date"):
                try:
                    opened_at = invested_since.date()
                except Exception:  # noqa: BLE001
                    opened_at = None

        return Position(
            market=market_key,
            direction=direction,
            quantity=quantity,
            avg_cost=avg_cost,
            opened_at_session_date=opened_at,
        )

    def _naive_target_contracts(self, *, equity) -> int:
        """Conservative single-lot allocation for Phase 1.

        Returns `1` when equity > 0, else 0. The full Stage 0-5 sizing
        pipeline (services/risk/sizing.py) runs server-side when the
        operator approves a signal and the order_placement_worker picks
        it up; LEAN emits single-lot candidates because:

          (a) Phase 1 starting equity is $15k-$25k; most micro-futures
              contracts have notional ~$10k-$30k so 1 lot is the natural
              single-position allocation.
          (b) Operator approves each signal individually via the /signals
              page — they retain sizing control.
          (c) Server-side Stage 0-5 sizing depends on contract metadata
              + correlation matrices that LEAN doesn't carry. Pushing
              sizing into LEAN duplicates the risk engine and creates a
              second source of truth.
        """
        try:
            if Decimal(str(equity)) > 0:
                return 1
        except Exception:  # noqa: BLE001
            return 0
        return 0

    def _build_minimal_sizing_trace(
        self,
        *,
        signal,
        target_contracts: int,
        equity,
    ) -> dict:
        """Stage 0-shaped trace populated with strategy_inputs only.

        Matches the `Stage0Trace` TypedDict from
        `strategies/v1_trend_following/sizing_trace.py` (TypedDict, total=False).
        Stages 1-5 are left absent (consumers tolerate `None` per the
        TypedDict's `total=False` declaration). The full Stage 0-5 trace
        is written by the server-side risk engine when the operator-
        approved signal is dispatched (Worker-PR-1).

        The trace is the payload's `sizing_trace` field; it lands in the
        `signals.sizing_trace` JSONB column on backend insert.
        """
        snapshot = signal.indicators_snapshot
        strategy_inputs = {
            signal.market: {
                "donchian_high": str(snapshot["donchian_high"]),
                "donchian_low": str(snapshot["donchian_low"]),
                "ma_fast": str(snapshot["ma_fast"]),
                "ma_slow": str(snapshot["ma_slow"]),
                "hurst": str(snapshot["hurst"]),
                "atr": str(snapshot["atr"]),
                "stop_price": str(signal.stop_price),
                "lookback_days_donchian": int(snapshot["lookback_days_donchian"]),
            }
        }
        return {
            "schema_version": 1,
            "stage_0_universe": {
                "active_markets": [signal.market],
                "excluded": [],
                "strategy_inputs": strategy_inputs,
            },
            # Phase 1 sentinel: the conservative single-lot sizing decision
            # taken on the LEAN side. Stage 1-5 will be appended server-side
            # by the risk engine when the operator-approved signal is
            # dispatched. Including the LEAN sizing decision under a stable
            # key lets downstream review surfaces (PR review, audit
            # explorer) attribute the contract count to the LEAN-side
            # decision rather than guessing.
            "lean_naive_sizing": {
                "target_contracts": target_contracts,
                "equity_usd": str(equity),
                "rationale": (
                    "Phase 1 conservative single-lot allocation pending "
                    "server-side Stage 0-5 sizing (Worker-PR-1)."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Universe freshness defensive check (2026-05-17 evening followup)
    # ------------------------------------------------------------------

    def _log_universe_freshness(self) -> None:
        """Emit structured log lines about futures universe staleness.

        Thin LEAN-runtime wrapper around the pure-Python evaluator at
        ``strategies.v1_trend_following.universe_freshness``. The
        wrapper wires ``self.time.date()`` + ``self.log(...)`` to the
        evaluator's filesystem scan + classification logic; the
        evaluator owns the data shape and the threshold calibration
        + has full unit-test coverage in
        ``tests/unit/test_universe_freshness.py`` (27 tests).

        Three structured log lines per category found:

        * ``v1_universe_data_fresh markets_checked=7 threshold_days=5
          fresh_count=7`` — happy path; emitted always (even when no
          warning fires) so operators can confirm the check ran.
        * ``v1_universe_data_stale threshold_days=5 stale_count=<N>
          stale_markets=[<symbol>(<days>d/<file>),...]`` — canonical
          fingerprint for the staleness pattern. The Phase 1+
          AsyncTaskMonitor extension (per ``Docs/decisions-log.md``
          2026-05-17 evening entry follow-up #3) will alert on this.
        * ``v1_universe_data_missing missing_markets=[...]`` —
          directory not found, no ``YYYYMMDD.csv`` entries, or
          unparseable filename. Distinct severity (volume-mount issue
          or seed-never-ran).

        Defensive: catches every exception from the import + the
        evaluator. The algorithm boot should NEVER fail because this
        check raised — it's pure observability.

        **Context.** The 2026-05-17 evening incident found the 7 micro
        futures sub-universe failing ``v1_history_unavailable`` for 5+
        days because the seed data had aged out. The per-cycle
        heartbeat stayed clean and the audit chain stayed clean
        throughout, so the staleness was invisible until the operator
        inspected the LEAN cycle log directly. This check is the
        defensive layer that catches the next occurrence at boot.

        See ``Docs/decisions-log.md`` 2026-05-17 evening entry
        "7-micro v1_history_unavailable root cause" for the full
        investigation. PR #172 shipped the inline implementation
        because a pre-existing pytest time-bomb blocked the
        extraction; PR #173 fixed the time-bomb; this PR repays the
        technical debt with the extraction + full test coverage.
        """
        try:
            from strategies.v1_trend_following.universe_freshness import (  # type: ignore[import-not-found]
                DEFAULT_STALENESS_THRESHOLD_DAYS,
                V1_FUTURES_MARKET_PATHS,
                evaluate_universe_freshness,
            )
        except Exception as exc:  # noqa: BLE001 -- log + skip; check is observability-only
            self.log(f"v1_universe_freshness_import_failed exc={exc!r}")
            return

        try:
            result = evaluate_universe_freshness(
                today=self.time.date(),
                data_root="/Lean/Data/future",
                markets=V1_FUTURES_MARKET_PATHS,
            )
        except Exception as exc:  # noqa: BLE001 -- log + skip
            self.log(f"v1_universe_freshness_check_failed exc={exc!r}")
            return

        # Emit structured log lines per category found. Each category is
        # an independent line so log analyzers can grep for one without
        # parsing the others.
        if result.missing_markets:
            self.log(
                f"v1_universe_data_missing missing_markets={list(result.missing_markets)}"
            )
        if result.stale_markets:
            summary = ",".join(
                f"{s.market}({s.days_stale}d/{s.last_file})" for s in result.stale_markets
            )
            self.log(
                f"v1_universe_data_stale "
                f"threshold_days={DEFAULT_STALENESS_THRESHOLD_DAYS} "
                f"stale_count={len(result.stale_markets)} stale_markets=[{summary}]"
            )
        if not result.missing_markets and not result.stale_markets:
            self.log(
                f"v1_universe_data_fresh "
                f"markets_checked={len(V1_FUTURES_MARKET_PATHS)} "
                f"threshold_days={DEFAULT_STALENESS_THRESHOLD_DAYS} "
                f"fresh_count={len(result.fresh_markets)}"
            )

    # ------------------------------------------------------------------
    # HTTP POST plumbing (Pivot-PR-A)
    # ------------------------------------------------------------------

    def _post_event(self, event_type: str, extra: dict | None = None) -> None:
        """POST a heartbeat or signal event to the backend.

        Best-effort. Network errors are logged but do NOT crash the algorithm:
        a transient backend outage shouldn't take down LEAN's signal cycle.
        Persistent errors are surfaced via the structlog stream on the
        backend side (LEAN's `self.log` writes to the LEAN container's log).
        """
        body = {
            "event_type": event_type,
            "ts_utc": self.utc_time.isoformat(),
            "algorithm_id": STRATEGY_NAME,
        }
        if extra:
            body.update(extra)

        url = f"{self._api_base_url.rstrip('/')}/api/internal/lean/signals"
        try:
            req = urllib.request.Request(
                url=url,
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._api_bearer_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "trading-lean-local/0.1",
                },
            )
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECONDS) as response:
                status = response.status
                if status >= 400:
                    self.log(f"lean_signal_post_failed status={status} event_type={event_type}")
                else:
                    self.log(f"lean_signal_post_succeeded status={status} event_type={event_type}")
        except urllib.error.HTTPError as exc:
            self.log(
                f"lean_signal_post_http_error status={exc.code} "
                f"event_type={event_type} reason={exc.reason}"
            )
        except urllib.error.URLError as exc:
            self.log(f"lean_signal_post_url_error event_type={event_type} reason={exc.reason}")
        except Exception as exc:  # noqa: BLE001  -- best-effort net I/O
            self.log(f"lean_signal_post_unexpected_error event_type={event_type} exc={exc!r}")
