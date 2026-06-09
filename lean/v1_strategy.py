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

# ``from __future__ import annotations`` (PEP 563) makes all annotations lazy
# strings so QC type names used in method signatures (e.g. ``data: Slice``) are
# NOT evaluated at class-definition time. This keeps the module importable
# outside LEAN's runtime with only ``QCAlgorithm`` stubbed, which lets the pure
# module-level helpers (``_position_from_api_row``, ``_coerce_bool_param``) be
# unit-tested directly (tests/unit/test_lean_live_positions.py). LEAN never
# introspects strategy annotations at runtime, so this is behavior-preserving.
from __future__ import annotations

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
from dataclasses import replace
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
    CandidateSignal,
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
#
# PR-B (2026-05-26): STRATEGY_DECOMMISSIONED + EXIT_AUTO_APPROVE added so an
# operator UPDATE to parameter_sets.parameters propagates into the daily
# LEAN cycle. Both default to "False" (the SAFE value) at launch per
# exit-pipeline-design.md §L4/L6. The strings round-trip cleanly through
# LEAN's parameter map: "True"/"False" → bool(...) via the "True" literal
# check in the lazy V1Parameters builder.
V1_PARAMETER_DEFAULTS = {
    "LOOKBACK_DAYS_DONCHIAN": "60",
    "MA_FAST_DAYS": "50",
    "MA_SLOW_DAYS": "200",
    "EFFICIENCY_RATIO_THRESHOLD": "0.20",
    "STOP_DISTANCE_ATR_MULT": "3.0",
    "ATR_LOOKBACK_DAYS": "20",
    "MIN_HOLDING_DAYS": "14",
    "VOL_TARGET_PCT_ANNUAL": "0.15",
    "INSTRUMENT_VOL_LOOKBACK_DAYS": "60",
    "ROLL_DAYS_BEFORE_EXPIRY": "5",
    "STARTING_CASH_USD": "100000",
    "STRATEGY_DECOMMISSIONED": "False",
    "EXIT_AUTO_APPROVE": "False",
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


def _coerce_bool_param(value) -> bool:  # noqa: ANN001 — LEAN dict[str, str] surface, untyped on purpose
    """LEAN parameter-map -> bool for STRATEGY_DECOMMISSIONED / EXIT_AUTO_APPROVE.

    LEAN's ``get_parameter`` returns strings. PR-B (2026-05-26) added two
    bool flags whose SAFE default is False (per exit-pipeline-design.md
    §L4 + §L6). We treat ANY value other than the literal ``"True"``
    (case-insensitive) as False - defensive: typos / blank-strings / None
    all collapse to the safer default rather than silently enabling the
    kill-switch or auto-approve. A real bool input is preserved as-is.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _parse_backtest_date(token, default_y, default_m, default_d):  # noqa: ANN001 — LEAN str surface
    """Parse a ``YYYYMMDD`` backtest-window parameter, else the default (y, m, d).

    BACKTEST-ONLY: LEAN ignores ``set_start_date`` / ``set_end_date`` in live mode,
    so this knob is inert in production. A missing / malformed token falls back to
    the default, so a bad param can never silently shift the window to an
    unparseable date — it reproduces the original hard-coded paper window. Lets the
    research harness backtest any window without editing this file again.
    """
    token = (token or "").strip()
    if len(token) == 8 and token.isdigit():
        return int(token[:4]), int(token[4:6]), int(token[6:8])
    return default_y, default_m, default_d


def _position_from_api_row(row) -> Position:  # noqa: ANN001 — JSON dict from the api; untyped on purpose
    """Map one ``GET /api/internal/lean/positions`` item → strategy ``Position`` (PR-A2).

    Pure + module-level so it is unit-testable without the LEAN runtime. Mirrors
    ``scripts/operator_tools/trigger_v1_cycle.py::build_position_from_row``:
    quantity sign drives direction (positive=LONG, negative=SHORT, zero=FLAT).
    ``opened_at_session_date`` is the api-computed ET calendar date (already
    DST-aware), so no tzdata is needed here — a missing/blank value (no open
    trade recorded) leaves it None and the strategy conservatively skips the
    MIN_HOLDING_DAYS gate.
    """
    quantity = int(row["quantity"])
    if quantity > 0:
        direction = Direction.LONG
    elif quantity < 0:
        direction = Direction.SHORT
    else:
        direction = Direction.FLAT

    opened_at_session_date: _date | None = None
    raw_opened = row.get("opened_at_session_date")
    if direction is not Direction.FLAT and raw_opened:
        try:
            opened_at_session_date = _date.fromisoformat(str(raw_opened))
        except ValueError:
            opened_at_session_date = None

    return Position(
        market=str(row["market"]),
        direction=direction,
        quantity=quantity,
        avg_cost=Decimal(str(row["avg_cost"])),
        opened_at_session_date=opened_at_session_date,
    )


# ---------------------------------------------------------------------------
# Backtest-only Stage 0-5 sizing (Trust-with-Money charter PR A).
#
# In BACKTEST mode V1 places REAL LEAN orders so the run yields an authoritative
# equity curve. It sizes them with the SAME canonical five-stage sizer the live
# risk engine uses — ``services/risk/sizing.py`` — loaded BY FILE PATH inside the
# LEAN container (``V1TrendFollowingAlgorithm._load_sizing_pipeline``; the package
# ``__init__`` drags sqlalchemy + ``services.audit``, absent in the LEAN image, so
# a normal import can't be used). This is NOT a re-implementation/fork: the sizer
# is the production module. LIVE mode never reaches any of this — it POSTs
# ``target_contracts=1`` and the server sizes.
#
# These pure module-level helpers build the sizer's NUMERIC inputs from the
# cycle's ``BarSeries`` closes: an annualized return covariance (Stage 1
# inverse-vol weighting + Stage 3 PSD repair / cluster shrink) and a per-market
# momentum proxy (the Stage 3 non-convergence drop tiebreak). Matrix math is
# numpy ``float64`` — the documented Decimals+numpy split (sizing.py module
# docstring / anti-pattern A05): money stays ``Decimal``; only the covariance is
# float64, exactly as ``services/risk/sizing.py`` itself does. Dependency-light
# and pure → unit-tested outside the LEAN runtime (test_v1_backtest_orders.py).
# ---------------------------------------------------------------------------
_TRADING_DAYS_PER_YEAR = 252

# Stage 3 cluster map — macro grouping per V1 market (mirrors the universe's
# cluster assignment used by the sizer's Stage 3 cluster cap). The eventual live
# wiring of Stage 0-5 will source this from the ``contracts`` table; here it is
# pinned to the V1 universe. Markets absent here fall back to their own name as a
# singleton cluster (no cluster sharing — the conservative default).
_V1_CLUSTER_MAP = {
    "/MES": "equity_index",
    "/MNQ": "equity_index",
    "/MYM": "equity_index",
    "/M2K": "equity_index",
    "/MGC": "commodity",
    "/MCL": "commodity",
    "/MBT": "crypto",
    "TLT": "rates",
    "IEF": "rates",
    "SHY": "rates",
    "TIP": "rates",
}


def _daily_returns_by_date(bars, lookback):  # noqa: ANN001 -- list[Bar]; LEAN runtime surface
    """Simple daily returns over the last ``lookback`` window, keyed by date.

    ``r_t = (c_t - c_{t-1}) / c_{t-1}`` as ``float`` — these feed the numpy
    covariance matrix (the documented money-stays-Decimal / matrix-is-float64
    split). Keyed by ``bar.session_date`` so :func:`_annualized_covariance` can
    align markets on a listwise date intersection. A non-positive prior close
    (bad data) skips that step. Returns ``{}`` when < 2 returns are available.
    """
    if not bars or lookback < 2:
        return {}
    # ``lookback`` returns need ``lookback + 1`` closes; take the tail window.
    window = bars[-(lookback + 1):]
    out: dict = {}
    for i in range(1, len(window)):
        prev = window[i - 1].close
        if prev <= 0:
            continue
        out[window[i].session_date] = float((window[i].close - prev) / prev)
    return out


def _annualized_covariance(returns_by_market):  # noqa: ANN001 -- dict[str, dict[date, float]]
    """Annualized return covariance over the listwise-common dates of all markets.

    Returns ``(sigma_index, sigma_annual)`` — exactly the pair ``SizingInputs``
    wants — where ``sigma_index`` is the tuple of SURVIVING markets (>= 2 aligned
    returns AND positive sample variance over the common window) and
    ``sigma_annual`` is the matching ``n x n`` numpy ``float64`` covariance scaled
    by 252. Markets with too little overlap or a degenerate (zero-variance) window
    are dropped — the sizer rejects non-positive diagonal variance, and the caller
    sizes a dropped market 0 this cycle (reconciling next). Returns ``None`` when
    fewer than two common dates remain or no market survives (caller sizes none).
    """
    import numpy as np

    markets = [m for m, r in returns_by_market.items() if len(r) >= 2]
    if not markets:
        return None
    common = set.intersection(*(set(returns_by_market[m]) for m in markets))
    if len(common) < 2:
        return None
    dates = sorted(common)
    rows: list = []
    kept: list = []
    for m in markets:
        row = [returns_by_market[m][d] for d in dates]
        if float(np.var(row, ddof=1)) <= 0.0:
            continue  # constant stretch → the sizer rejects non-positive variance
        rows.append(row)
        kept.append(m)
    if not kept:
        return None
    data = np.array(rows, dtype=np.float64)
    if len(kept) == 1:
        var = float(np.var(data[0], ddof=1)) * _TRADING_DAYS_PER_YEAR
        return tuple(kept), np.array([[var]], dtype=np.float64)
    cov = np.asarray(np.cov(data, ddof=1), dtype=np.float64) * _TRADING_DAYS_PER_YEAR
    return tuple(kept), cov


def _total_return(bars, lookback):  # noqa: ANN001 -- list[Bar]; LEAN runtime surface
    """Total return over the last ``lookback`` closes, as ``Decimal`` (momentum).

    A monotonic proxy for V1's momentum z-score — only the RELATIVE ordering
    across this cycle's candidates matters (Stage 3 drops the lowest-momentum
    signal in the binding cluster on non-convergence). Returns ``Decimal('0')``
    on insufficient data or a non-positive base close.
    """
    if not bars or lookback < 1 or len(bars) < lookback + 1:
        return Decimal("0")
    base = bars[-(lookback + 1)].close
    if base <= 0:
        return Decimal("0")
    return (bars[-1].close - base) / base


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
        # Overridable via BACKTEST_START_DATE / BACKTEST_END_DATE (YYYYMMDD) so the
        # research harness can backtest any window; DEFAULTS reproduce the original
        # hard-coded paper window, so production (which sets neither param) is
        # byte-for-byte unchanged.
        start_y, start_m, start_d = _parse_backtest_date(
            self.get_parameter("BACKTEST_START_DATE"), 2026, 5, 1
        )
        end_y, end_m, end_d = _parse_backtest_date(
            self.get_parameter("BACKTEST_END_DATE"), 2026, 12, 31
        )
        self.set_start_date(start_y, start_m, start_d)
        self.set_end_date(end_y, end_m, end_d)
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
        # PR-C (design §12): set True for the current cycle when the DB
        # parameter fetch fails (fail-OPEN); surfaced on the cycle heartbeat's
        # ``error`` field so the api observes the kill-switch read failure.
        self._parameters_fetch_failed: bool = False

        # Trust-with-Money charter PR A: the SINGLE master switch for the
        # backtest-only order-placement path. Fixed ONCE here from ``live_mode``.
        # When False (LIVE), every order-placement code path early-returns, so the
        # production strategy stays POST-only — zero LEAN orders, byte-for-byte
        # unchanged. When True (BACKTEST), V1 ALSO places real LEAN orders that
        # mirror the signals it already computes, producing an authoritative
        # equity curve. Grep ``market_order`` to verify: every call sits behind a
        # ``self._backtest_orders_enabled`` guard (``_place_backtest_orders`` +
        # ``on_symbol_changed_events``).
        self._backtest_orders_enabled: bool = not bool(self.live_mode)
        # Lazy cache for the canonical Stage 0-5 sizer (loaded by file path on
        # first backtest order cycle; see ``_load_sizing_pipeline``). Stays None
        # in LIVE mode — the sizer is never loaded there.
        self._sizing_pipeline_module = None

        self.log(
            f"v1_strategy initialized (post-pivot 2026-05-12, Pivot-PR-D) "
            f"live_mode={self.live_mode} api_base={self._api_base_url} "
            f"strategy_version={self._strategy_version_str} "
            f"backtest_orders_enabled={self._backtest_orders_enabled} "
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

    def on_symbol_changed_events(self, events):  # noqa: ANN001 -- SymbolChangedEvents; LEAN runtime
        """Carry the economic position across a futures roll — BACKTEST ONLY.

        Trust-with-Money charter PR A / G3: when LEAN's continuous-future mapping
        rolls to a new front contract, CLOSE the expiring leg and OPEN the same
        signed quantity in the new front so the position carries through the roll.
        We use paired ``market_order`` legs and NEVER ``self.liquidate()`` — the
        liquidate path tags fills "Liquidated", which false-trips the ruin parser
        (``research/run.py::lean_ruin_alert``).

        Provably unreachable in live mode: the live strategy is POST-only (the api
        owns real roll handling via ``services/execution``); this early-returns when
        ``self._backtest_orders_enabled`` is False. Roll resets ``invested_since``
        on the new leg (a MIN_HOLDING_DAYS artifact) — tolerable since quarterly
        rolls (~63 sessions) vastly exceed the 14-day min-holding gate.
        """
        if not self._backtest_orders_enabled:
            return
        for changed in events.values():
            old_symbol = changed.old_symbol
            new_symbol = changed.new_symbol
            try:
                old_qty = int(self.portfolio[old_symbol].quantity)
            except Exception as exc:  # noqa: BLE001 -- defensive; a bad holding read must not crash
                self.log(f"v1_roll_snapshot_failed old={old_symbol} exc={exc!r}")
                continue
            if old_qty == 0:
                continue
            # Close the expiring leg unconditionally (always priced — it is held).
            self.market_order(old_symbol, -old_qty, tag="roll")
            # Re-open the carried position in the new front; guard the chain-edge
            # zero price (G2) — if unavailable, the daily reconcile re-opens once
            # the new front prices.
            new_price = self.securities[new_symbol].price if new_symbol in self.securities else 0
            if new_price and new_price > 0:
                self.market_order(new_symbol, old_qty, tag="roll")
                self.log(f"v1_roll_carried old={old_symbol} new={new_symbol} qty={old_qty}")
            else:
                self.log(
                    f"v1_roll_close_only old={old_symbol} new={new_symbol} "
                    f"qty={old_qty} reason=new_front_price_unavailable"
                )

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

        # PR-C (parameter-sets-bootstrap-design §12): override
        # STRATEGY_DECOMMISSIONED from the DB head row so the nightly cycle
        # honors the operator's runtime kill-switch flip (the flag lives in the
        # DB, not lean.json). The lean.json value built above is the per-cycle
        # base; the DB flag wins when reachable. Fetched EVERY cycle (not just at
        # initialize) so a flip is honored on the next nightly cycle without a
        # restart.
        #
        # FAIL-OPEN (§12.4): on ANY fetch failure keep the lean.json default and
        # flag it on the heartbeat — a transient api blip must NOT crash the
        # cycle or silently flatten the book. SAFETY DEPENDENCY (§12.4
        # forward-guard): fail-open is safe ONLY while EXIT_AUTO_APPROVE
        # auto-approval is unbuilt + False — every emitted signal (entries AND
        # decommission exits) is operator-gated today, so a wrong fail-open
        # ``False`` cannot auto-execute. Re-evaluate (fail-closed for exits) if
        # exit auto-approval ever ships.
        fetched_decommission = self._fetch_active_decommission_flag()
        if fetched_decommission is None:
            self._parameters_fetch_failed = True
            self.log(
                f"lean_parameters_fetch_failed session_date={session_date} "
                f"fail_open=True lean_json_default={v1_params.strategy_decommissioned}"
            )
        else:
            self._parameters_fetch_failed = False
            if fetched_decommission != v1_params.strategy_decommissioned:
                self.log(
                    f"lean_decommission_override session_date={session_date} "
                    f"lean_json={v1_params.strategy_decommissioned} "
                    f"db={fetched_decommission}"
                )
                v1_params = replace(v1_params, strategy_decommissioned=fetched_decommission)

        strategy = V1TrendFollowing(parameters=v1_params)
        self._strategy_min_bars = strategy.min_required_bars

        # Step 3-4: build active universe + positions.
        #
        # PR-A2: the position source is LIVE-MODE-AWARE. Under PaperBrokerage
        # live-mode ``self.portfolio`` is empty (the api owns positions), which
        # left both the exit pipeline AND the entry anti-pyramiding guard
        # dormant (Docs/decisions-log.md 2026-05-31). In live mode we GET the
        # api's REAL open positions + the in-flight-exit set; these feed BOTH
        # generate_signals (POSITION_ALREADY_SAME_DIRECTION) and
        # generate_exit_candidates (trend_flip / reversal / decommission). In
        # BACKTEST we keep ``_snapshot_position`` so backtest behavior is
        # byte-for-byte unchanged. FAIL SAFE: a live fetch failure falls back to
        # the empty snapshot (today's behavior — legit new entries still fire,
        # no spurious exits) and flags the heartbeat.
        live_positions: dict[str, Position] | None = None
        exits_in_flight: frozenset[str] = frozenset()
        positions_fetch_failed = False
        if self.live_mode:
            fetched = self._fetch_live_positions()
            if fetched is None:
                positions_fetch_failed = True
            else:
                live_positions, exits_in_flight = fetched

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
            if live_positions is None:
                # Backtest, or live fetch failed (fail-safe) → LEAN snapshot
                # (empty under live PaperBrokerage; populated in backtest).
                #
                # PR A: in BACKTEST the holding lives in the MAPPED front contract,
                # not the continuous canonical, so snapshot THAT — otherwise the
                # snapshot reads FLAT and the strategy would re-emit entries every
                # cycle (pyramiding) and never see a position to exit. Gated on
                # ``_backtest_orders_enabled`` so the LIVE fetch-failure fallback is
                # byte-for-byte unchanged (snapshots the continuous symbol, which is
                # empty under PaperBrokerage anyway).
                snapshot_symbol = symbol
                if self._backtest_orders_enabled:
                    tradeable = self._tradeable_symbol(market_key, symbol)
                    if tradeable is not None:
                        snapshot_symbol = tradeable
                current_positions[market_key] = self._snapshot_position(
                    market_key=market_key, symbol=snapshot_symbol
                )

        if live_positions is not None:
            # Live: the api's real positions are authoritative and cover ALL held
            # markets — including any whose bars failed to load this cycle, so a
            # decommission close can still fire (trend_flip self-skips on
            # INSUFFICIENT_BAR_HISTORY inside generate_exit_candidates).
            current_positions = dict(live_positions)

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
            self.log(f"v1_generate_signals_failed session_date={session_date} exc={exc!r}")
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
        # vs efficiency_below_threshold vs trend_filter_failed vs ...) rejected
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
            rejection_reason_counts[reason_str] = rejection_reason_counts.get(reason_str, 0) + 1
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

        # Generate exit candidates BEFORE the heartbeat so the per-position
        # exit-proximity records (PR-A of exit-proximity-design.md) ride the
        # same lean_cycle_heartbeat as the entry proximity. Generation is pure
        # (no POSTs); the exit signals are emitted after the heartbeat. A None
        # result (generation raised) leaves exit proximity empty but never
        # blocks the heartbeat or the entry emissions.
        exit_result = self._generate_exits(
            strategy=strategy,
            active_universe=active_universe,
            current_positions=current_positions,
            entry_candidates=result.signals,
            entry_evaluations=result.market_evaluations,
            session_date=session_date,
        )

        # Step 6: heartbeat with per-cycle summary (always emitted so
        # liveness_probes sees the algorithm alive even when no signals).
        # PR-A of signal-proximity-design.md attaches the per-market
        # proximity records to the heartbeat extra. The api accepts them as
        # an optional field; PR-B will persist them. Observation-only —
        # this does NOT influence which signals fire.
        proximity_payload = self._build_market_evaluations_payload(result)
        exit_proximity_payload = self._build_position_exit_evaluations_payload(exit_result)
        heartbeat_extra: dict = {
            "session_date_et": session_date.isoformat(),
            "equity_usd": str(equity),
            "live_mode": bool(self.live_mode),
            "signals_emitted_count": signals_count,
            "rejections_count": rejections_count,
            "market_evaluations": proximity_payload,
            "position_exit_evaluations": exit_proximity_payload,
        }
        # PR-C (§12.4): surface a kill-switch parameter-fetch failure so the api
        # observes it (and a thin follow-up can escalate to a P1 Discord alert).
        if self._parameters_fetch_failed:
            heartbeat_extra["error"] = "parameters_fetch_failed"
        elif positions_fetch_failed:
            # PR-A2: surface the live-positions fetch failure on the heartbeat.
            # Lower priority than parameters_fetch_failed (which has its own P1
            # alert keyed on the exact string) so the param alert is never
            # masked; this cycle already fell back to the empty snapshot above.
            heartbeat_extra["error"] = "positions_fetch_failed"
        self._post_event("lean_cycle_heartbeat", extra=heartbeat_extra)

        # Step 7: emit each entry signal.
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
                    # PR-B (2026-05-26): explicit entry discriminator. The
                    # api defaults to 'entry' for backwards compat with
                    # pre-PR-B emitters, but we send it explicitly so the
                    # value round-trips cleanly into the audit payload +
                    # the signals row + downstream SSE events.
                    "signal_type": "entry",
                },
            )

        # Step 8: emit each exit candidate computed above (the exit pipeline
        # ran before the heartbeat so its proximity rode along). Emission is
        # independent of the entry path — a failure here does not roll back
        # already-emitted entry signals. Reversal candidates carry
        # ``paired_entry_market`` so the dispatcher serializes the paired
        # EXIT-then-ENTRY sequence per §11 R2.
        if exit_result is not None:
            self._emit_exits(
                exit_result=exit_result,
                session_date=session_date,
                equity=equity,
                exits_in_flight=exits_in_flight,
            )

        # Step 9 (BACKTEST ONLY): place real LEAN orders mirroring the decisions
        # POSTed above, so the backtest produces an AUTHORITATIVE equity curve
        # (Trust-with-Money charter PR A). Both this call-site guard AND the
        # method's own early-return key off ``self._backtest_orders_enabled``
        # (== ``not self.live_mode``, fixed at initialize) — defense in depth so
        # the live strategy stays POST-only: zero LEAN orders, byte-for-byte.
        if self._backtest_orders_enabled:
            self._place_backtest_orders(
                active_universe=active_universe,
                entry_signals=result.signals,
                exit_result=exit_result,
                equity=equity,
                vol_target_pct_annual=v1_params.vol_target_pct_annual,
                instrument_vol_lookback_days=v1_params.instrument_vol_lookback_days,
                session_date=session_date,
            )
        return

    def _generate_exits(
        self,
        *,
        strategy: V1TrendFollowing,
        active_universe: dict,  # type: ignore[type-arg]  -- LEAN runtime; mypy excluded
        current_positions: dict,  # type: ignore[type-arg]
        entry_candidates: tuple,  # type: ignore[type-arg]  -- tuple[CandidateSignal,...]
        entry_evaluations: tuple,  # type: ignore[type-arg]  -- tuple[MarketProximity,...]
        session_date,  # noqa: ANN001  -- date; LEAN runtime
    ):  # noqa: ANN201  -- ExitGenerationResult | None; LEAN runtime
        """Run ``generate_exit_candidates`` + log the per-cycle summary.

        PURE — no POSTs. Split from emission (``_emit_exits``) so the result's
        ``position_exit_evaluations`` can be folded into the heartbeat before
        any exit signal is emitted (PR-A of exit-proximity-design.md). Returns
        the ``ExitGenerationResult`` or ``None`` if generation raised (the
        heartbeat still ships; entry POSTs are unaffected).

        ``entry_evaluations`` is the entry result's ``market_evaluations`` —
        observation-only, passed through so ``generate_exit_candidates`` can
        derive the reversal exit-proximity dimension; it does NOT change which
        exits fire.
        """
        try:
            exit_result = strategy.generate_exit_candidates(
                active_universe=active_universe,
                current_positions=current_positions,
                as_of_session_date=session_date,
                entry_candidates=entry_candidates,
                entry_evaluations=entry_evaluations,
            )
        except Exception as exc:  # noqa: BLE001 -- log + skip; don't crash the algorithm
            self.log(f"v1_generate_exit_candidates_failed session_date={session_date} exc={exc!r}")
            return None

        # Per-rejection structured log lines for forensic visibility
        # (mirror the entry-side pattern earlier in this cycle). Each
        # exit-side rejection counts the markets where (b)/(c) condition
        # didn't trip — TREND_HOLDS / MIN_HOLDING_NOT_REACHED /
        # INSUFFICIENT_BAR_HISTORY. Useful for diagnosing "why didn't we
        # exit /M2K today" without inspecting the strategy code.
        exit_rejection_reason_counts: dict[str, int] = {}
        for rejected_market, reason in exit_result.rejections:
            reason_str = reason.value
            exit_rejection_reason_counts[reason_str] = (
                exit_rejection_reason_counts.get(reason_str, 0) + 1
            )
            self.log(
                f"v1_exit_rejected session_date={session_date} "
                f"market={rejected_market} reason={reason_str}"
            )

        exit_count = len(exit_result.signals)
        self.log(
            f"v1_exits_generated session_date={session_date} "
            f"exits_emitted_count={exit_count} "
            f"exit_rejections_count={len(exit_result.rejections)} "
            f"exit_reasons={exit_rejection_reason_counts}"
        )
        return exit_result

    def _emit_exits(
        self,
        *,
        exit_result,  # noqa: ANN001  -- ExitGenerationResult; LEAN runtime
        session_date,  # noqa: ANN001  -- date; LEAN runtime
        equity,  # noqa: ANN001  -- Decimal; LEAN runtime
        exits_in_flight=frozenset(),  # noqa: ANN001  -- frozenset[str]; LEAN runtime
    ) -> None:
        """POST each exit candidate computed by ``_generate_exits``.

        Best-effort per-signal via ``_post_event`` so a single market's exit
        can't suppress the others. Called after the heartbeat (and after the
        entry POSTs) per the cycle ordering — generation already happened so
        the proximity could ride the heartbeat.

        PR-A2 idempotency: skip a market that already has a non-terminal exit
        in flight (``exits_in_flight``, from the positions endpoint). Without
        this, a trend_flip — which persists every cycle until the close fills —
        would re-POST the same exit each night. The server-side ingest guard
        (``DuplicateInFlightExitError``) backstops this for any that slip
        through; suppressing here avoids the wasted POST + benign-200 noise.
        ``exits_in_flight`` is always empty in backtest (it is live-only).
        """
        for exit_signal in exit_result.signals:
            if exit_signal.market in exits_in_flight:
                self.log(
                    f"v1_exit_suppressed_in_flight session_date={session_date} "
                    f"market={exit_signal.market} exit_reason={exit_signal.exit_reason}"
                )
                continue
            payload = self._build_exit_signal_payload(
                exit_signal=exit_signal,
                session_date=session_date,
                equity=equity,
            )
            self._post_event("signal_emitted", extra=payload)

    def _build_market_evaluations_payload(self, result) -> list:  # noqa: ANN001 -- SignalGenerationResult; LEAN runtime
        """Serialize ``result.market_evaluations`` into the heartbeat payload.

        Per PR-A of ``Docs/signal-proximity-design.md`` §5: each market in
        the active universe contributes one entry; gate states are emitted
        as strings, Decimals as strings (Decimal-as-string per A05), and
        None survives unchanged so the api can deserialize cleanly.
        Best-effort: if ``market_evaluations`` is empty (pre-PR-A strategy
        builds) or any per-market serialization raises, return whatever
        was built so far + log. Heartbeat must NEVER be blocked by
        proximity emission.
        """
        evaluations = getattr(result, "market_evaluations", ()) or ()
        out: list = []
        for evaluation in evaluations:
            try:
                out.append(self._serialize_market_proximity(evaluation))
            except Exception as exc:  # noqa: BLE001 -- log + skip; per-market isolation
                self.log(
                    f"v1_market_proximity_serialize_failed "
                    f"market={getattr(evaluation, 'market', '?')} exc={exc!r}"
                )
        return out

    def _serialize_market_proximity(self, evaluation) -> dict:  # noqa: ANN001 -- MarketProximity; LEAN runtime
        """Convert one MarketProximity dataclass to a wire-shape dict."""

        def _gate(gate) -> dict:  # noqa: ANN001 -- GateProximity; LEAN runtime
            return {
                "state": gate.state.value,
                "headroom": str(gate.headroom) if gate.headroom is not None else None,
                "detail": gate.detail,
            }

        last_close = evaluation.last_close
        eff_value = evaluation.efficiency_value
        eff_threshold = evaluation.efficiency_threshold
        return {
            "market": evaluation.market,
            "long_donchian": _gate(evaluation.long_donchian),
            "short_donchian": _gate(evaluation.short_donchian),
            "long_trend": _gate(evaluation.long_trend),
            "short_trend": _gate(evaluation.short_trend),
            "efficiency": _gate(evaluation.efficiency),
            "last_close": str(last_close) if last_close is not None else None,
            # Raw ER + active threshold carried alongside the gate so the live
            # ER distribution is mineable from signal_proximity (calibration of
            # the 0.20 launch threshold — no backtester yet).
            "efficiency_ratio_value": str(eff_value) if eff_value is not None else None,
            "efficiency_ratio_threshold": str(eff_threshold) if eff_threshold is not None else None,
            "overall_state": evaluation.overall_state.value,
            "closest_gate": evaluation.closest_gate,
            "gate_status": evaluation.gate_status,
        }

    def _build_position_exit_evaluations_payload(self, exit_result) -> list:  # noqa: ANN001 -- ExitGenerationResult | None; LEAN runtime
        """Serialize ``exit_result.position_exit_evaluations`` for the heartbeat.

        Per PR-A of ``Docs/exit-proximity-design.md`` §5: one entry per held
        position; trigger states as strings, Decimals as strings (A05), None
        survives unchanged. Best-effort + per-position isolation, mirroring
        ``_build_market_evaluations_payload``. A ``None`` exit_result (exit
        generation raised) yields an empty list so the heartbeat still ships.
        """
        if exit_result is None:
            return []
        evaluations = getattr(exit_result, "position_exit_evaluations", ()) or ()
        out: list = []
        for evaluation in evaluations:
            try:
                out.append(self._serialize_position_exit_proximity(evaluation))
            except Exception as exc:  # noqa: BLE001 -- log + skip; per-position isolation
                self.log(
                    f"v1_position_exit_proximity_serialize_failed "
                    f"market={getattr(evaluation, 'market', '?')} exc={exc!r}"
                )
        return out

    def _serialize_position_exit_proximity(self, evaluation) -> dict:  # noqa: ANN001 -- PositionExitProximity; LEAN runtime
        """Convert one PositionExitProximity dataclass to a wire-shape dict."""

        def _trigger(trigger) -> dict:  # noqa: ANN001 -- ExitTriggerProximity; LEAN runtime
            return {
                "state": trigger.state.value,
                "headroom": str(trigger.headroom) if trigger.headroom is not None else None,
                "detail": trigger.detail,
            }

        last_close = evaluation.last_close
        stop_price = evaluation.stop_price
        return {
            "market": evaluation.market,
            "direction": evaluation.direction,
            "held_days": evaluation.held_days,
            "trend_flip": _trigger(evaluation.trend_flip),
            "stop": _trigger(evaluation.stop),
            "reversal": _trigger(evaluation.reversal),
            "decommission": _trigger(evaluation.decommission),
            "last_close": str(last_close) if last_close is not None else None,
            "stop_price": str(stop_price) if stop_price is not None else None,
            "overall_state": evaluation.overall_state.value,
            "closest_exit": evaluation.closest_exit,
            "gate_status": evaluation.gate_status,
        }

    def _build_exit_signal_payload(
        self,
        *,
        exit_signal: CandidateSignal,
        session_date,  # noqa: ANN001  -- date
        equity,  # noqa: ANN001  -- Decimal
    ) -> dict:
        """Assemble the LeanEventRequest body for an exit candidate.

        The api's route-side validation requires market / direction /
        decision_price / sizing_trace / strategy_version + the exit fields
        (exit_reason / prior_position_direction / prior_position_quantity).
        target_contracts is OPTIONAL for exits — the dispatcher computes
        the close qty at place-order time per design §Q3 — but we still
        send 0 explicitly to keep the signals row's target_contracts column
        well-defined.
        """
        prior_dir = exit_signal.prior_position_direction
        prior_qty = exit_signal.prior_position_quantity
        payload: dict = {
            "session_date_et": session_date.isoformat(),
            "equity_usd": str(equity),
            "live_mode": bool(self.live_mode),
            "market": exit_signal.market,
            # Exit-direction sentinel is FLAT — see design §4 + §Q3. The
            # dispatcher resolves the actual buy/sell side from the prior
            # position at place-order time.
            "direction": exit_signal.direction.value,
            "target_contracts": 0,
            "decision_price": str(exit_signal.decision_price),
            "sizing_trace": self._build_exit_sizing_trace(
                exit_signal=exit_signal,
                equity=equity,
            ),
            "strategy_version": self._strategy_version_str,
            "signal_type": "exit",
            "exit_reason": exit_signal.exit_reason,
            "prior_position_direction": prior_dir.value if prior_dir is not None else None,
            "prior_position_quantity": prior_qty,
        }
        if exit_signal.paired_entry_market is not None:
            payload["paired_entry_market"] = exit_signal.paired_entry_market
        return payload

    def _build_exit_sizing_trace(
        self,
        *,
        exit_signal: CandidateSignal,
        equity,  # noqa: ANN001  -- Decimal
    ) -> dict:
        """Stage 0-shaped trace for exits (Decimal-as-string per A05).

        Distinct from the entry-side ``_build_minimal_sizing_trace``
        because exit candidates DO NOT carry the Donchian / Efficiency-Ratio keys
        (per CandidateSignal docstring: indicators_snapshot is populated
        only when a snapshot was computed — trend_flip exits — and is
        empty for decommission / reversal exits). We surface the
        exit_reason + prior_position state in lean_naive_sizing so the
        operator-facing /signals page + the audit_log payload have the
        forensic context next to the size decision.
        """
        snapshot = exit_signal.indicators_snapshot or {}
        # Coerce snapshot values to strings (Decimal → str per A05) so the
        # JSONB column doesn't carry mixed types. Empty snapshot stays as
        # an empty dict on the wire.
        strategy_inputs: dict = {exit_signal.market: {k: str(v) for k, v in snapshot.items()}}
        prior_dir = exit_signal.prior_position_direction
        return {
            "schema_version": 1,
            "stage_0_universe": {
                "active_markets": [exit_signal.market],
                "excluded": [],
                "strategy_inputs": strategy_inputs,
            },
            "lean_naive_sizing": {
                # Exits never carry a target_contracts the strategy can
                # justify — the dispatcher closes the full prior position.
                # 0 is the sentinel; ``lean_naive_sizing.rationale`` makes
                # the intent explicit so the field can't be silently
                # misread as "size 0 contracts" by a future reader.
                "target_contracts": 0,
                "equity_usd": str(equity),
                "rationale": (
                    "Exit signal — full close of prior position. Dispatcher "
                    "computes actual close qty from current positions at "
                    "place-order time per exit-pipeline-design §Q3."
                ),
                "exit_reason": exit_signal.exit_reason,
                "prior_position_direction": (prior_dir.value if prior_dir is not None else None),
                "prior_position_quantity": exit_signal.prior_position_quantity,
                "paired_entry_market": exit_signal.paired_entry_market,
            },
        }

    # ------------------------------------------------------------------
    # Strategy plumbing helpers (Pivot-PR-D)
    # ------------------------------------------------------------------

    def _get_v1_parameters(self) -> V1Parameters:
        """Build (and cache) the `V1Parameters` dataclass from the LEAN parameter map.

        Lazy — built on the first `on_daily_signal_cycle` rather than in
        `initialize()` so a malformed parameter is surfaced as a cycle log
        line (after warmup) instead of crashing the algorithm at boot.

        PR-B (2026-05-26): STRATEGY_DECOMMISSIONED + EXIT_AUTO_APPROVE are
        now read from the parameter map (default "False"). String→bool
        coercion uses the literal "True" check (case-insensitive) — LEAN's
        parameter map serializes every value as a string. Any value other
        than "True"/"true" is treated as False (defensive: the SAFE default
        for both flags is False per design §L4 + §L6).
        """
        if self._v1_parameters is not None:
            return self._v1_parameters
        raw = self._params
        self._v1_parameters = V1Parameters(
            lookback_days_donchian=int(raw["LOOKBACK_DAYS_DONCHIAN"]),
            ma_fast_days=int(raw["MA_FAST_DAYS"]),
            ma_slow_days=int(raw["MA_SLOW_DAYS"]),
            efficiency_ratio_threshold=Decimal(str(raw["EFFICIENCY_RATIO_THRESHOLD"])),
            stop_distance_atr_mult=Decimal(str(raw["STOP_DISTANCE_ATR_MULT"])),
            atr_lookback_days=int(raw["ATR_LOOKBACK_DAYS"]),
            min_holding_days=int(raw["MIN_HOLDING_DAYS"]),
            vol_target_pct_annual=Decimal(str(raw["VOL_TARGET_PCT_ANNUAL"])),
            instrument_vol_lookback_days=int(raw["INSTRUMENT_VOL_LOOKBACK_DAYS"]),
            roll_days_before_expiry=int(raw["ROLL_DAYS_BEFORE_EXPIRY"]),
            strategy_decommissioned=_coerce_bool_param(raw.get("STRATEGY_DECOMMISSIONED")),
            exit_auto_approve=_coerce_bool_param(raw.get("EXIT_AUTO_APPROVE")),
        )
        return self._v1_parameters

    def _build_bar_series(self, *, market_key: str, symbol: object, count: int) -> BarSeries | None:
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
                "efficiency_ratio": str(snapshot["efficiency_ratio"]),
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
    # Backtest-only order placement (Trust-with-Money charter PR A)
    #
    # Everything below this banner exists SOLELY to give a BACKTEST an
    # authoritative equity curve. Each entry point early-returns when
    # ``self._backtest_orders_enabled`` is False, so in LIVE mode the production
    # strategy is byte-for-byte the POST-only path above: it never reaches a
    # ``market_order`` call. The order mechanics (integer contracts on the mapped
    # front; roll = close-old + open-new) reuse G1/G2/G3 from
    # ``research/lean/projects/research_runner.py``.
    # ------------------------------------------------------------------

    def _tradeable_symbol(self, market_key, continuous_symbol):  # noqa: ANN001 -- LEAN Symbol; runtime
        """Resolve the LEAN ``Symbol`` to order against for ``market_key``.

        Futures (``/MES`` form) trade the MAPPED front contract —
        ``self.securities[continuous].mapped`` — which can be ``None`` before the
        first mapping on the chain edge (caller skips that cycle, G2). ETFs (bare
        ticker) trade their own subscription symbol directly.
        """
        if market_key.startswith("/"):
            try:
                return self.securities[continuous_symbol].mapped
            except Exception:  # noqa: BLE001 -- not mapped yet / not in securities
                return None
        return continuous_symbol

    def _contract_multiplier(self, market_key):  # noqa: ANN201 -- Decimal; LEAN runtime
        """Per-contract multiplier (point value) for ``market_key``, as ``Decimal``.

        Read from LEAN's ``symbol_properties.contract_multiplier`` so sizing uses
        the SAME multiplier LEAN applies to P&L (keeps size + MTM consistent).
        Falls back to ``Decimal('1')`` (the equity case, and a safe default if the
        property is unreadable).
        """
        continuous_symbol = self._market_subscriptions.get(market_key)
        tradeable = self._tradeable_symbol(market_key, continuous_symbol)
        try:
            props = self.securities[tradeable].symbol_properties
            multiplier = Decimal(str(props.contract_multiplier))
            if multiplier > 0:
                return multiplier
        except Exception:  # noqa: BLE001 -- unreadable property → safe default
            pass
        return Decimal("1")

    def _current_contract_qty(self, tradeable):  # noqa: ANN001 -- LEAN Symbol; runtime
        """Signed integer quantity currently held in ``tradeable`` (0 if none)."""
        try:
            return int(self.portfolio[tradeable].quantity)
        except Exception:  # noqa: BLE001 -- absent holding reads as flat
            return 0

    def _load_sizing_pipeline(self):  # noqa: ANN201 -- module; LEAN runtime
        """Load the canonical Stage 0-5 sizer (``services/risk/sizing.py``) by PATH.

        BACKTEST-ONLY, cached on the instance. ``services/risk/__init__.py``
        eagerly imports sqlalchemy + ``services.audit`` (absent in the LEAN
        image), so ``import services.risk.sizing`` crashes on the package init.
        Loading the module file directly bypasses ``__init__``; sizing.py's own
        imports are numpy / structlog / ``strategies.*`` / stdlib — all present in
        the LEAN runtime. ``services/`` is mounted read-only at ``/Lean/services``
        by the backtest driver (``research/lean/driver.py``). A unit test injects
        a pre-loaded module into ``self._sizing_pipeline_module`` to exercise the
        real pipeline without the container path.
        """
        if self._sizing_pipeline_module is not None:
            return self._sizing_pipeline_module
        import importlib.util
        import sys

        path = "/Lean/services/risk/sizing.py"
        if not os.path.exists(path):
            raise RuntimeError(
                f"backtest Stage 0-5 sizer unavailable: {path!r} not found "
                "(is services/ mounted read-only into the LEAN container? see "
                "research/lean/driver.py service_package_mount)"
            )
        spec = importlib.util.spec_from_file_location("v1_backtest_sizing_pipeline", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"backtest Stage 0-5 sizer: cannot create import spec for {path!r}")
        module = importlib.util.module_from_spec(spec)
        # Register BEFORE exec: sizing.py's ``@dataclass(slots=True)`` under
        # ``from __future__ import annotations`` makes the dataclass machinery look
        # up ``sys.modules[cls.__module__]`` during class creation. Without this the
        # load raises ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self._sizing_pipeline_module = module
        return module

    def _run_stage_0_5_sizing(
        self,
        *,
        signals_by_market: dict,  # type: ignore[type-arg]  -- {market: CandidateSignal}
        resolved: dict,  # type: ignore[type-arg]  -- {market: (tradeable, price, mult, qty)}
        active_universe: dict,  # type: ignore[type-arg]
        equity,  # noqa: ANN001  -- Decimal
        vol_target_pct_annual,  # noqa: ANN001  -- Decimal
        lookback: int,
        session_date,  # noqa: ANN001  -- date
    ) -> dict:  # type: ignore[type-arg]  -- {market: signed int}
        """Size this cycle's NEW-ENTRY batch via the canonical Stage 0-5 pipeline.

        ``signals_by_market`` maps each new-entry market (flat breakout entries +
        reversal flips) to the ``CandidateSignal`` whose ``.direction`` sets the
        side. Held positions are NOT passed here — they hold unchanged
        (anti-pyramiding), so the sizer only ever grows the book by genuinely new
        entries. Joint inverse-vol weighting + the Stage 2/3/4 caps run across the
        batch, so a multi-entry cycle is co-sized exactly as the live risk engine
        would. Returns the signed target-contract dict (absent market → 0, i.e. a
        Stage 0 / Stage 3 / Stage 5 drop).

        The covariance is built from the cycle's continuous-series returns; the
        single-contract notional from the MAPPED-front price x multiplier (the
        contract actually traded). Markets whose covariance window is degenerate
        are dropped by :func:`_annualized_covariance` and sized 0 this cycle.
        """
        sizing = self._load_sizing_pipeline()

        returns_by_market: dict = {}
        for market_key in signals_by_market:
            series = active_universe.get(market_key)
            if series is not None:
                returns_by_market[market_key] = _daily_returns_by_date(series.bars, lookback)
        cov = _annualized_covariance(returns_by_market)
        if cov is None:
            return {}
        sigma_index, sigma_annual = cov

        # Build candidate signals + metadata + momentum over the SURVIVING markets
        # only, so ``sigma_index`` covers every candidate (the sizer's invariant:
        # an active market missing from sigma_index raises SizingError).
        candidate_signals: list = []
        contracts_metadata: dict = {}
        momentum: dict = {}
        for market_key in sigma_index:
            _tradeable, price_dec, mult_dec, _current_qty = resolved[market_key]
            candidate_signals.append(signals_by_market[market_key])
            contracts_metadata[market_key] = sizing.ContractMetadata(
                market=market_key,
                single_contract_notional=mult_dec * price_dec,
                cluster=_V1_CLUSTER_MAP.get(market_key, market_key),
            )
            momentum[market_key] = _total_return(active_universe[market_key].bars, lookback)

        inputs = sizing.SizingInputs(
            candidate_signals=tuple(candidate_signals),
            equity=equity,
            contracts_metadata=contracts_metadata,
            sigma_annual=sigma_annual,
            sigma_index=tuple(sigma_index),
            momentum_z_scores=momentum,
            vol_target_pct_annual=vol_target_pct_annual,
            evaluated_at_utc=session_date.isoformat() if session_date is not None else "",
        )
        return dict(sizing.run_sizing_pipeline(inputs).target_contracts)

    def _place_backtest_orders(
        self,
        *,
        active_universe: dict,  # type: ignore[type-arg]
        entry_signals: tuple,  # type: ignore[type-arg]  -- tuple[CandidateSignal, ...]
        exit_result,  # noqa: ANN001  -- ExitGenerationResult | None
        equity,  # noqa: ANN001  -- Decimal-coercible
        vol_target_pct_annual,  # noqa: ANN001  -- Decimal
        instrument_vol_lookback_days: int,
        session_date,  # noqa: ANN001  -- date
    ) -> None:
        """Reconcile each market to its target position with real LEAN orders.

        BACKTEST ONLY — early-returns in live mode (the master gate). The cycle:

        1. Resolve every market with bars to its tradeable symbol (mapped front
           for futures), price + multiplier + real holding; skip a zero / unmapped
           price (reconcile next cycle, G2).
        2. Classify: NEW entries (flat + breakout) and reversal flips form the
           batch to size; held positions with no exit HOLD unchanged
           (anti-pyramiding — the sizer never re-sizes an open position); plain
           exits (trend_flip / decommission, or a reversal with no paired entry)
           target 0.
        3. Size the new-entry batch JOINTLY through the canonical Stage 0-5
           pipeline (:meth:`_run_stage_0_5_sizing`).
        4. Place ONE ``market_order`` per market for the signed delta vs the real
           holding (G1: integer contracts, never ``set_holdings``). Orders carry a
           ``v1_backtest`` tag so a roll/close is never tagged "Liquidated"
           (ruin-parser safety).
        """
        if not self._backtest_orders_enabled:
            return

        entry_by_market = {signal.market: signal for signal in entry_signals}
        exit_by_market: dict = {}
        if exit_result is not None:
            exit_by_market = {signal.market: signal for signal in exit_result.signals}

        # Step 1: resolve every market with bars to (tradeable, price, mult, qty).
        resolved: dict = {}
        for market_key in active_universe:
            continuous_symbol = self._market_subscriptions.get(market_key)
            if continuous_symbol is None:
                continue
            tradeable = self._tradeable_symbol(market_key, continuous_symbol)
            if tradeable is None or tradeable not in self.securities:
                # Mapped front not resolved yet on the chain edge — reconcile next
                # cycle once it maps (G2).
                continue
            price = self.securities[tradeable].price
            if not price or price <= 0:
                self.log(f"v1_backtest_order_skipped_zero_price market={market_key}")
                continue
            resolved[market_key] = (
                tradeable,
                Decimal(str(price)),
                self._contract_multiplier(market_key),
                self._current_contract_qty(tradeable),
            )

        # Step 2: classify. ``to_size`` maps a market to the CandidateSignal whose
        # direction sets the side (flat breakout entry, or a reversal's paired
        # entry); ``close`` targets 0. Held positions with no exit are in NEITHER
        # → they hold unchanged in step 4 (anti-pyramiding).
        to_size: dict = {}
        close: set = set()
        for market_key, (_tradeable, _price, _mult, current_qty) in resolved.items():
            exit_signal = exit_by_market.get(market_key)
            if exit_signal is not None:
                if exit_signal.exit_reason == "reversal":
                    paired_entry = entry_by_market.get(market_key)
                    if paired_entry is not None:
                        to_size[market_key] = paired_entry  # flip onto the new side
                    else:
                        close.add(market_key)  # defensive: reversal, no paired entry
                else:
                    close.add(market_key)  # trend_flip / decommission → close
                continue
            if current_qty == 0:
                entry_signal = entry_by_market.get(market_key)
                if entry_signal is not None:
                    to_size[market_key] = entry_signal  # new entry from flat
                # flat + no breakout → no order (target stays 0 in step 4)
            # else: held + no exit → hold unchanged (target = current_qty)

        # Step 3: joint Stage 0-5 sizing of the new-entry batch (signed targets).
        sized: dict = {}
        if to_size:
            sized = self._run_stage_0_5_sizing(
                signals_by_market=to_size,
                resolved=resolved,
                active_universe=active_universe,
                equity=Decimal(str(equity)),
                vol_target_pct_annual=Decimal(str(vol_target_pct_annual)),
                lookback=instrument_vol_lookback_days,
                session_date=session_date,
            )
            # Breadcrumb: entries wanted sizing but the sizer returned nothing —
            # Stage 0 dropped every candidate (1-contract notional > 50% equity) or
            # the covariance window was degenerate (``_annualized_covariance`` → {}).
            # Fail-safe (no order placed), but otherwise invisible when reading a
            # multi-year curve and wondering why a breakout produced no trade.
            if not sized:
                self.log(
                    f"v1_backtest_sizing_empty session_date={session_date} "
                    f"markets={sorted(to_size)}"
                )

        # Step 4: reconcile each resolved market to its signed target — ONE order.
        for market_key, (tradeable, _price, _mult, current_qty) in resolved.items():
            if market_key in to_size:
                target_qty = int(sized.get(market_key, 0))  # absent → Stage 0/3/5 drop → 0
            elif market_key in close:
                target_qty = 0
            else:
                target_qty = current_qty  # held + no exit → hold unchanged
            delta = target_qty - current_qty
            if delta == 0:
                continue
            self.market_order(tradeable, delta, tag=f"v1_backtest:{market_key}")
            self.log(
                f"v1_backtest_order_placed session_date={session_date} "
                f"market={market_key} current={current_qty} target={target_qty} delta={delta}"
            )

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
            self.log(f"v1_universe_data_missing missing_markets={list(result.missing_markets)}")
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

    def _fetch_live_positions(self):  # noqa: ANN201 — tuple[dict[str,Position], frozenset[str]] | None; LEAN runtime
        """GET the api's real open positions for the live cycle (PR-A2).

        Returns ``(positions_by_market, exits_in_flight)`` on success, or
        ``None`` on ANY failure (HTTP / network / non-2xx / malformed body).
        The caller FAILS SAFE on ``None`` — falls back to the empty
        ``self.portfolio`` snapshot (today's behavior: no spurious exits; IBKR
        bracket stops still protect held positions) and flags the heartbeat.

        Live mode only: under PaperBrokerage ``self.portfolio`` is empty (the
        api owns positions), which is why indicator exits were dormant
        (``Docs/decisions-log.md`` 2026-05-31). In backtest LEAN's own portfolio
        is authoritative, so the caller does not invoke this. Reuses the same
        base URL + bearer + short timeout as :meth:`_post_event` /
        :meth:`_fetch_active_decommission_flag`.
        """
        url = f"{self._api_base_url.rstrip('/')}/api/internal/lean/positions"
        try:
            req = urllib.request.Request(
                url=url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {self._api_bearer_token}",
                    "User-Agent": "trading-lean-local/0.1",
                },
            )
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECONDS) as response:
                if response.status >= 400:
                    self.log(f"lean_positions_fetch_failed http_status={response.status}")
                    return None
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.log(f"lean_positions_fetch_failed http_error status={exc.code} reason={exc.reason}")
            return None
        except urllib.error.URLError as exc:
            self.log(f"lean_positions_fetch_failed url_error reason={exc.reason}")
            return None
        except Exception as exc:  # noqa: BLE001 -- best-effort net I/O; fail-safe on anything
            self.log(f"lean_positions_fetch_failed unexpected exc={exc!r}")
            return None

        if not isinstance(payload, dict):
            self.log("lean_positions_fetch_failed malformed_body_not_dict")
            return None
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            self.log("lean_positions_fetch_failed malformed_body_no_positions")
            return None

        positions_by_market: dict[str, Position] = {}
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue
            try:
                position = _position_from_api_row(raw)
            except Exception as exc:  # noqa: BLE001 -- skip a malformed row, keep the rest
                self.log(f"lean_positions_row_skipped exc={exc!r} raw={raw!r}")
                continue
            # The api never returns quantity=0 rows, but defend: only held
            # positions belong in current_positions (the strategy treats
            # absence as FLAT).
            if position.direction is not Direction.FLAT:
                positions_by_market[position.market] = position

        raw_exits = payload.get("exits_in_flight")
        exits_in_flight = (
            frozenset(str(m) for m in raw_exits if isinstance(m, str))
            if isinstance(raw_exits, list)
            else frozenset()
        )

        self.log(
            f"lean_positions_fetched positions_count={len(positions_by_market)} "
            f"exits_in_flight_count={len(exits_in_flight)}"
        )
        return positions_by_market, exits_in_flight

    def _fetch_active_decommission_flag(self) -> bool | None:
        """GET the active ``STRATEGY_DECOMMISSIONED`` from the api (design §12.3).

        Reuses the same base URL + bearer as :meth:`_post_event`. Returns the
        coerced bool on success; returns ``None`` on ANY failure (HTTP error,
        network error, non-2xx, malformed body) so the caller FAILS OPEN to the
        ``lean.json`` default (design §12.4). Best-effort, short timeout — a
        param-fetch error must never crash the signal cycle.
        """
        url = f"{self._api_base_url.rstrip('/')}/api/internal/lean/parameters"
        try:
            req = urllib.request.Request(
                url=url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {self._api_bearer_token}",
                    "User-Agent": "trading-lean-local/0.1",
                },
            )
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECONDS) as response:
                if response.status >= 400:
                    self.log(f"lean_parameters_fetch_failed http_status={response.status}")
                    return None
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.log(
                f"lean_parameters_fetch_failed http_error status={exc.code} reason={exc.reason}"
            )
            return None
        except urllib.error.URLError as exc:
            self.log(f"lean_parameters_fetch_failed url_error reason={exc.reason}")
            return None
        except Exception as exc:  # noqa: BLE001  -- best-effort net I/O; fail-open on anything
            self.log(f"lean_parameters_fetch_failed unexpected exc={exc!r}")
            return None

        if not isinstance(payload, dict):
            self.log("lean_parameters_fetch_failed malformed_body_not_dict")
            return None
        params = payload.get("parameters")
        if not isinstance(params, dict):
            self.log("lean_parameters_fetch_failed malformed_body_no_parameters")
            return None
        # _coerce_bool_param collapses None / typos / non-"True" to False (the
        # SAFE default) — so a present-but-garbage value reads as "not
        # decommissioned" rather than tripping the kill-switch.
        return _coerce_bool_param(params.get("STRATEGY_DECOMMISSIONED"))

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
