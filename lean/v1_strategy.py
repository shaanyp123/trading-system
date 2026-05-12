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

**Brokerage configuration:** LEAN Local routes orders to IBKR via ``ib-async``
when ``live-mode = true`` and ``live-mode-brokerage = InteractiveBrokersBrokerage``
in ``lean.json``. The ``ib_gateway`` Docker container hosts the TWS API session
(see ``deploy/ibkr/README.md``, Pivot-PR-B). For paper trading, the wrapper
flows the same code paths but ``ib_gateway`` is configured with paper credentials
and port 4004 (externally-published socat port; internal gateway on
127.0.0.1:4002 — see ``deploy/ibkr/README.md`` Step 4).

API convention: snake_case (QC migrated the Python API from PascalCase to
snake_case ~2024; the local LEAN runtime accepts both but snake_case is the
documented preferred form). Method names, enum values, and keyword arguments
are all snake_case / SCREAMING_SNAKE_CASE. Class names (QCAlgorithm, Slice,
Resolution, BrokerageName, etc.) remain PascalCase per Python convention.

Day 29+ (post-pivot) status:
- Brokerage: ``InteractiveBrokersBrokerage`` / ``Margin`` matching IBKR Pro.
- Parameters: read from LEAN's parameter map (``self.get_parameter``) with
  ``strategies.v1_trend_following.parameters.V1_DEFAULTS`` as fallback.
- Warmup: 200 trading days (longest indicator lookback = MA_SLOW_DAYS).
- ``on_daily_signal_cycle``: HTTP POSTs ``signal_emitted`` events to the
  backend. Replaces the pre-pivot ``self.object_store.save(...)`` write.
- Heartbeat: separate POST to the backend's liveness probe endpoint when
  warming up (so the operator can confirm LEAN is alive even before the first
  signal cycle).

Strategy wiring (assemble ``BarSeries`` via ``self.history``, position snapshot
via ``self.portfolio``, call ``V1TrendFollowing.generate_signals``) is the
remaining work that lands inside this file. Pivot-PR-A ships the scaffold +
HTTP POST plumbing; full strategy wiring follows in Pivot-PR-D when the
signal dispatcher is wired end-to-end.

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

# Local strategy imports — `./strategies/` is mounted into the container by
# docker-compose. Wired in Pivot-PR-D when the dispatcher routes approved
# signals to the execution layer; not used by the Pivot-PR-A scaffold below.
#
# from v1_trend_following.parameters import default_v1_parameters
# from v1_trend_following.strategy import V1TrendFollowing


# Phase 1 sub-universe — keep aligned with `strategies/v1_trend_following/parameters.py`
# `V1_CANDIDATE_UNIVERSE`. The Week 2 sub-universe verification step gates this
# list; do not add markets here without updating the canonical source.
PHASE1_FUTURES = ("MES", "MNQ", "MYM", "M2K", "MGC", "MCL", "MBT")
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
    "STARTING_CASH_USD": "15000",
}


# Backend POST configuration — read from env vars at algorithm initialize.
# The container entrypoint sets these from sops-decrypted secrets.
_API_BASE_URL_DEFAULT = "http://api:8000"
_API_TIMEOUT_SECONDS = 10.0


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

        # Brokerage MODEL — controls backtest fees/slippage simulation. Match
        # IBKR Margin since post-pivot Phase 1 runs LIVE against IBKR via
        # `ib-async` through `ib_gateway` container. The LIVE broker is
        # configured in lean.json's `environments.<env>.live-mode-brokerage`
        # which is set to `InteractiveBrokersBrokerage` for both paper and
        # live (only the credentials + port differ).
        self.set_brokerage_model(
            BrokerageName.INTERACTIVE_BROKERS_BROKERAGE,  # noqa: F405
            AccountType.MARGIN,  # noqa: F405
        )

        # Subscribe to micro futures (continuous contract via QC's `Future` API).
        for ticker in PHASE1_FUTURES:
            future = self.add_future(
                f"/{ticker}",
                resolution=Resolution.DAILY,  # noqa: F405
                extended_market_hours=False,
                data_mapping_mode=DataMappingMode.OPEN_INTEREST,  # noqa: F405
                contract_depth_offset=0,
            )
            future.set_filter(0, 90)  # rolling 0-90 day window for contract selection

        # Subscribe to bond ETFs.
        for ticker in PHASE1_ETFS:
            self.add_equity(
                ticker,
                resolution=Resolution.DAILY,  # noqa: F405
                extended_market_hours=False,
            )

        # Warmup — longest indicator lookback is MA_SLOW_DAYS (default 200).
        self.set_warm_up(int(params["MA_SLOW_DAYS"]), Resolution.DAILY)  # noqa: F405

        # Daily 17:30 ET scheduled action — fires after CME settlement.
        self.schedule.on(
            self.date_rules.every_day(),
            self.time_rules.at(17, 30),
            self.on_daily_signal_cycle,
        )

        self._params = params
        self.log(
            f"v1_strategy initialized (post-pivot 2026-05-12) "
            f"live_mode={self.live_mode} api_base={self._api_base_url} "
            f"params_keys={sorted(params.keys())}"
        )

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
        """17:30 ET daily — POST each emitted signal to backend `/api/internal/lean/signals`.

        Post-pivot 2026-05-12 scaffold: emits a heartbeat POST per cycle so the
        backend's `liveness_probes` table sees the algorithm alive. Strategy
        wiring (assemble bars, call generate_signals, POST each signal) lands
        in Pivot-PR-D when the dispatcher routes approved signals to execution.
        """
        if self.is_warming_up:
            return

        session_date = self.time.date().isoformat()
        equity = self.portfolio.total_portfolio_value
        msg = (
            f"signal_cycle_tick utc={self.utc_time} et={self.time} "
            f"session_date={session_date} equity={equity}"
        )
        self.log(msg)

        # Post-pivot scaffold: heartbeat only. The full strategy wiring lands in
        # Pivot-PR-D. The shape of the POST is:
        #
        #     POST /api/internal/lean/signals
        #     Authorization: Bearer <token>
        #     Content-Type: application/json
        #
        #     {
        #       "event_type": "lean_cycle_heartbeat",
        #       "session_date_et": "<YYYY-MM-DD>",
        #       "equity_usd": "<Decimal-as-string>",
        #       "live_mode": <bool>
        #     }
        #
        # Pivot-PR-D will switch this to N parallel POSTs per signal with
        # event_type="signal_emitted" and the full sizing_trace payload per
        # backend-spec §4.5.1-replacement.
        self._post_event(
            "lean_cycle_heartbeat",
            extra={
                "session_date_et": session_date,
                "equity_usd": str(equity),
                "live_mode": bool(self.live_mode),
            },
        )
        return

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
            "algorithm_id": "v1_trend_following",
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
            self.log(f"lean_signal_post_http_error status={exc.code} event_type={event_type} reason={exc.reason}")
        except urllib.error.URLError as exc:
            self.log(f"lean_signal_post_url_error event_type={event_type} reason={exc.reason}")
        except Exception as exc:  # noqa: BLE001  -- best-effort net I/O
            self.log(f"lean_signal_post_unexpected_error event_type={event_type} exc={exc!r}")
