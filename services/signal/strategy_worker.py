"""services/signal/strategy_worker.py — the crypto-perps strategy worker.

Crypto-pivot C1 wiring of delta spec §3.3 (``Docs/crypto-pivot-delta-spec.md``):
the runtime that drives the parity-locked signal engine
(:mod:`services.signal.crypto_trend`), the risk-side Decimal re-check
(:mod:`services.risk.sizing`), and the Coinbase execution adapter
(:mod:`services.execution.coinbase_adapter`) as one long-lived container.
The C0-B2b scope call (decisions-log 2026-07-09) deleted the LEAN-era
``order_placement_worker``; **this worker is its successor** — it drives
the adapter directly (announce-only mandate: no per-trade approval
anywhere) and re-feeds the kept downstream audit/DB pipeline
(``fill_processor`` + ``order_terminal_status_processor``) from Coinbase
order states.

Two loops, one process:

1. **Daily decision at 00:05 UTC** (strategy §5 cadence): load daily
   spot bars (00:00 UTC sampled, public candles — the §3.2 signal
   input), equity + positions from the venue
   (``futures_balance_summary`` / ``list_positions``), the Amendment B
   parameter head row from ``parameter_sets`` (minted by bootstrap) →
   :func:`~services.signal.crypto_trend.compute_targets` /
   :func:`~services.signal.crypto_trend.plan_delta` →
   :func:`~services.risk.sizing.run_sizing_pipeline` (independent
   Decimal re-check; §5.7 ``m_combined`` consumed, not re-derived) →
   Phase-A small-live clamps (strategy §10: ``E_effective =
   min(equity, $1,500)``, max 2 BTC / 4 ETH contracts — config knobs,
   scale-up is a config change) → the adapter's §5 execution ladder.
   Decision outcomes persist to ``strategy_decisions`` (the §3.8
   ``/cycle`` digest + §3.7 ``GET /api/system/cycle`` consume it);
   dedupe is by UTC date, so a worker starting mid-day runs a missed
   decision once and a restart never double-runs (gate A4/A3).

2. **30 s risk loop** (strategy §5 Layer-2 + §7): client-side 2xATR
   stop checks against live WS marks, native-stop fill detection,
   daily (-8%) / weekly (-16%) loss limits, the $1,500 hard-halt
   malfunction floor, liquidation-buffer check (when the venue surface
   exposes it), and the §7 data-outage policy (marks stale > 3 min ⇒
   protected-hold if the native stop is confirmed resting, else
   flatten). Loss/halt breaches drive the EXISTING kill-switch FSM
   transitions (``DAILY_LOSS_BREACH`` / ``DECOMMISSION_FLOOR`` — no
   new states, severities, or triggers). Every loop iteration
   heartbeats to ``strategy_worker_status`` (the §3.7/§3.9
   pipeline-freshness input) and touches the container-healthcheck
   file.

**Halt gate** — every decision dispatch path consults
:data:`~services.risk.signal_dispatch.RISK_STATES_PERMITTING_DISPATCH`
before sending orders; non-permitting states produce a logged skip and
a ``skipped_risk_state`` decision row, never an order. Risk-loop
*protective* exits (stops, loss-limit flattens, outage flattens)
deliberately bypass the gate: HALT_NEW halts NEW exposure, not
risk-reducing exits.

**Weekly loss limit** follows the parity-locked engine semantics
(strategy §7: halve ``V_target`` for 7 days), NOT an FSM halt — there
is no weekly trigger in the locked taxonomy and the validated backtest
depends on trading through the halved window. Client-stop hits likewise
flatten + lockout per §5 without an FSM transition (stops are routine
strategy exits, not malfunctions).

**Float ↔ Decimal boundary** ([A05], per the C0-B3 decisions-log entry):
the parity-locked engine computes in float64; targets leave it as
integers; everything money-shaped on this side of the boundary —
equity, marks, caps, order quantities, costs — is ``Decimal``.

A02 BINDS — ``services/signal/**`` forbidden path;
``risk-review-approved`` required. A25 — structlog only. A06 — tz-aware
UTC everywhere (00:05 UTC anchor; crypto has no sessions).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event
from services.data.coinbase_market_data import (
    DailyBar,
    HttpxCoinbaseRestClient,
    MarkStore,
    WsConnectFactory,
    extract_ticker_updates,
    fetch_daily_bars,
)
from services.execution.coinbase_adapter import (
    CoinbaseExecutionAdapter,
    LadderStageResult,
    select_perp_products,
)
from services.execution.coinbase_client import CoinbaseBrokerClient
from services.execution.types import (
    BrokerOrderState,
    BrokerPosition,
    FuturesBalanceSummary,
    PerpProductRef,
)
from services.risk.capital_events import (
    CAPITAL_EVENT_MODE_DURATION_SESSIONS,
    CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS,
    capital_event_session_count,
)
from services.risk.dispatch import apply_state_transition
from services.risk.fill_processor import (
    FillIngestPayload,
    FillProcessingError,
    UnsupportedFillScenarioError,
    process_fill_event,
)
from services.risk.multipliers import m_combined as compute_m_combined
from services.risk.order_terminal_status_processor import (
    TerminalStatusPayload,
    process_terminal_status_event,
)
from services.risk.signal_dispatch import RISK_STATES_PERMITTING_DISPATCH
from services.risk.sizing import (
    CryptoProductSpec,
    SizingError,
    SizingInputs,
    run_sizing_pipeline,
)
from services.risk.state_machine import (
    HaltSeverity,
    RiskState,
    TransitionTrigger,
    plan_invoke_kill_switch,
)
from services.signal.crypto_trend import (
    ASSETS,
    AssetDecisionRow,
    AssetState,
    CryptoTrendParams,
    compute_indicators,
    compute_targets,
    plan_delta,
)

log = structlog.get_logger()

#: Spot signal series per asset (strategy §4; locked spot symbols, not
#: CDE derivative IDs — [A13] holds for the perp side via runtime
#: discovery).
ASSET_TO_SPOT_PRODUCT: Final[dict[str, str]] = {"BTC": "BTC-USD", "ETH": "ETH-USD"}

#: Daily decision anchor (strategy §5: 00:05 UTC — 5 min after bar close).
DECISION_TIME_UTC: Final[time] = time(hour=0, minute=5, tzinfo=UTC)

#: Bars fetched for indicator warm-up. SMA(200) + EWMA seed + the 90-day
#: vol-ratio window need ≥ ~300 usable bars; 400 gives margin.
DEFAULT_BARS_HISTORY_DAYS: Final[int] = 400

#: Strategy §7 outage threshold (locked; changing it is an amendment).
DEFAULT_STALE_THRESHOLD_S: Final[float] = 180.0

#: Decision-record JSONB schema tag.
DECISION_SCHEMA_VERSION: Final[str] = "strategy_decision_v1"

#: Consecutive risk-tick failures before the worker invokes the
#: kill-switch with the UNHANDLED_EXCEPTION trigger (gate A4: the loop
#: must never die silently; ten misses = five minutes of blindness).
RISK_TICK_FAILURE_HALT_THRESHOLD: Final[int] = 10

DecisionStatus = Literal[
    "dispatching", "completed", "failed", "skipped_risk_state", "skipped_stale_bars"
]

#: Risk-review F1a: per-reason alert mapping for protective flattens.
#: EXISTING ``alert_category`` enum members ONLY (no enum migration —
#: delta spec §3.8's free-text-payload remap convention):
#:   * ``margin_auto_trim``  — risk-engine auto position reduction; the
#:     closest existing semantic for a client-2xATR-stop exit.
#:   * ``position_unprotected`` — §7 outage flatten of a position with
#:     no confirmed venue-resting stop.
#:   * ``margin_warn`` — liquidation-buffer breach (§3.8's explicit
#:     remap for this category).
#: ``daily_loss`` / ``halt_floor`` flatten reasons are deliberately
#: absent: their FSM transitions emit the ``kill_switch_invoked`` alert.
_FLATTEN_ALERTS: Final[dict[str, tuple[Literal["P0", "P1", "P2"], str]]] = {
    "client_stop": ("P2", "margin_auto_trim"),
    "outage_unprotected": ("P1", "position_unprotected"),
    "liq_buffer": ("P1", "margin_warn"),
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyWorkerConfig:
    """All tunables; prod values come from env via ``worker_main``."""

    env: Environment = "paper"
    phase_at_emit: PhaseAtEmit = 1
    risk_tick_interval_s: float = 30.0
    bars_history_days: int = DEFAULT_BARS_HISTORY_DAYS
    stale_threshold_s: float = DEFAULT_STALE_THRESHOLD_S
    #: Phase-A small-live sizing clamps (strategy §10 / delta spec §5 C1).
    #: ``e_effective_cap_usd=None`` and empty ``max_contracts`` disable
    #: them (the C2 scale-up config change).
    e_effective_cap_usd: Decimal | None = Decimal("1500")
    max_contracts: Mapping[str, int] = field(default_factory=lambda: {"BTC": 2, "ETH": 4})
    #: §7 liquidation-buffer floor, as a fraction of mark distance. The
    #: venue field's exact unit is a strategy §11 open question — the
    #: check normalizes percentage-shaped values (> 1) to fractions.
    liq_buffer_min_frac: Decimal = Decimal("0.30")
    #: Container healthcheck file — touched every risk tick.
    heartbeat_file: str | None = "/tmp/strategy-worker.heartbeat"  # noqa: S108
    ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    #: WS reconnect backoff cap for the marks feed.
    ws_reconnect_max_backoff_s: float = 60.0


# ---------------------------------------------------------------------------
# Pure policy helpers
# ---------------------------------------------------------------------------


def decision_due(*, now_utc: datetime, last_decision_date: date | None) -> date | None:
    """Return today's UTC date when the 00:05 UTC decision is due.

    A worker starting mid-day still runs the missed decision once
    (better a late decision on the same closed bar than a missed one —
    gate A4); the ``strategy_decisions`` UNIQUE row is the dedupe.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")
    now = now_utc.astimezone(UTC)
    today = now.date()
    anchor = datetime.combine(today, DECISION_TIME_UTC)
    if now < anchor:
        return None
    if last_decision_date is not None and last_decision_date >= today:
        return None
    return today


def params_from_canonical(canonical: Mapping[str, str]) -> CryptoTrendParams:
    """Build engine params from the DB parameter head row (Amendment B seed).

    The seed (``services.risk.crypto_parameters``) is the operational
    source of truth; this maps its Decimal-as-string values onto the
    engine's float twins at the boundary. Keys absent from the seed
    (e.g. the parametric ``funding_ann``) keep the engine defaults —
    exactly the C0-B3 contract.
    """

    def f(key: str) -> float:
        return float(canonical[key])

    def i(key: str) -> int:
        return int(canonical[key])

    def b(key: str) -> bool:
        return str(canonical.get(key, "False")).strip().lower() == "true"

    dd_tiers: tuple[tuple[float, float], ...]
    if b("DD_TIERS_REMOVED"):
        dd_tiers = ((9.9, 1.0),)
    else:
        dd_tiers = CryptoTrendParams().dd_tiers
    return replace(
        CryptoTrendParams(),
        sma_fast=i("SMA_FAST_DAYS"),
        sma_slow=i("SMA_SLOW_DAYS"),
        mom_lb=i("MOM_LOOKBACK_DAYS"),
        hysteresis_days=i("HYSTERESIS_DAYS"),
        hysteresis_hold=b("HYSTERESIS_HOLD"),
        ewma_lambda=f("EWMA_LAMBDA"),
        vol_floor=f("VOL_FLOOR"),
        vol_cap=f("VOL_CAP"),
        volratio_slow_window=i("VOLRATIO_SLOW_WINDOW"),
        volratio_block=f("VOLRATIO_BLOCK"),
        volratio_resume=f("VOLRATIO_RESUME"),
        funding_long_veto=f("FUNDING_LONG_VETO"),
        funding_short_gate=f("FUNDING_SHORT_GATE"),
        v_target=f("V_TARGET"),
        w_btc=f("W_BTC"),
        per_asset_cap=f("PER_ASSET_CAP"),
        gross_cap=f("GROSS_CAP"),
        deadband_abs=f("DEADBAND_ABS_USD"),
        deadband_frac=f("DEADBAND_FRAC"),
        band_edge_rebalance=b("BAND_EDGE"),
        cash_yield_ann=f("CASH_YIELD_ANN"),
        margin_frac=f("MARGIN_BUFFER_FRAC"),
        subscription_usd_month=f("SUBSCRIPTION_USD_MONTH"),
        atr_window=i("ATR_WINDOW"),
        client_stop_atr=f("CLIENT_STOP_ATR"),
        lockout_days=i("LOCKOUT_DAYS"),
        per_trade_risk_frac=f("PER_TRADE_RISK_FRAC"),
        daily_loss_limit=f("DAILY_LOSS_LIMIT"),
        weekly_loss_limit=f("WEEKLY_LOSS_LIMIT"),
        weekly_penalty_days=i("WEEKLY_PENALTY_DAYS"),
        dd_tiers=dd_tiers,
        eth_min_price=f("ETH_MIN_PRICE"),
    )


def _new_uuid7() -> UUID:
    """UUIDv7 for row ids minted client-side (dev-guide §3.9)."""
    import uuid_utils

    return UUID(str(uuid_utils.uuid7()))


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        s = str(value).strip()
        return Decimal(s) if s else None
    except (InvalidOperation, ValueError):
        return None


def equity_from_summary(summary: FuturesBalanceSummary) -> Decimal | None:
    """Marked account equity per strategy §6 (fail-safe: None, never 0).

    Prefers the venue's ``total_usd_balance`` (CFM futures equity + CBI
    swept cash — the cash-yield layer's sweeps still count as equity);
    falls back to CFM + CBI components, then CFM alone.
    """
    if summary.total_usd_balance is not None:
        return summary.total_usd_balance
    if summary.cfm_usd_balance is not None and summary.cbi_usd_balance is not None:
        return summary.cfm_usd_balance + summary.cbi_usd_balance
    return summary.cfm_usd_balance


def normalize_liq_buffer(value: Decimal) -> Decimal:
    """Normalize the venue liquidation-buffer field to a fraction.

    The exact unit is strategy §11 open question territory (percentage
    ``35`` vs fraction ``0.35``); values above 1 are treated as
    percentages. Conservative on both readings.
    """
    return value / Decimal(100) if value > 1 else value


def split_delta_legs(*, prior: int, target: int) -> list[tuple[int, str]]:
    """Split a signed contract delta into fill-processor-compatible legs.

    A reversal (long→short or short→long) becomes an explicit full close
    followed by an open — same total contracts traded (the §1.5 cost
    model is linear in contracts, so cost-equivalent), and each leg maps
    onto the downstream processors' supported ENTRY / EXIT_FULL_CLOSE
    scenarios.

    Returns ``[(delta, kind)]`` with kind ∈ {"open", "close", "expand",
    "reduce"}; empty when target == prior.
    """
    if target == prior:
        return []
    if prior == 0:
        return [(target, "open")]
    if target == 0:
        return [(-prior, "close")]
    if (prior > 0) != (target > 0):
        return [(-prior, "close"), (target, "open")]
    if abs(target) > abs(prior):
        return [(target - prior, "expand")]
    return [(target - prior, "reduce")]


def _expiry_from_product(product: PerpProductRef) -> date:
    """Best-effort expiry for the ``contracts`` row (perp-style futures
    carry far expiries, e.g. ``BIP-20DEC30-CDE``); far-future fallback."""
    details = product.raw.get("future_product_details")
    if isinstance(details, Mapping):
        raw_expiry = details.get("contract_expiry")
        if isinstance(raw_expiry, str) and raw_expiry:
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(raw_expiry.replace("Z", "+00:00")).date()
    # Parse the DDMONYY token out of e.g. BIP-20DEC30-CDE.
    parts = product.product_id.split("-")
    if len(parts) >= 3 and len(parts[1]) == 7:
        with contextlib.suppress(ValueError):
            return datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=UTC).date()
    return date(2099, 12, 31)


# ---------------------------------------------------------------------------
# Per-asset runtime state (persisted JSONB)
# ---------------------------------------------------------------------------


@dataclass
class AssetRuntime:
    """Engine + protection state for one asset, JSONB-serializable.

    ``contracts`` mirrors venue truth (synced every reconcile); the
    hysteresis / lockout / vol-block fields are engine memory the venue
    cannot supply — they persist through ``strategy_worker_status`` /
    ``strategy_decisions.engine_state`` and survive restarts.
    """

    contracts: int = 0
    applied_dir: int = 0
    pending_dir: int = 0
    pending_count: int = 0
    lockout_until_ord: int = -1
    lockout_dir: int = 0
    vol_blocked: bool = False
    stopped_on_date: str | None = None  # ISO date of the bar-day a stop fired
    entry_vwap: str | None = None  # Decimal-as-string
    client_stop_level: str | None = None  # Decimal-as-string (2xATR soft stop)
    atrp_at_stop_set: str | None = None  # Decimal-as-string
    native_stop_order_id: str | None = None
    native_stop_client_order_id: str | None = None
    stop_rearm_version: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> AssetRuntime:
        known = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
        return cls(**known)

    def to_engine_state(self, *, decided_bar_date: date) -> AssetState:
        """Project onto the engine's :class:`AssetState` for one decision.

        ``bar_index`` semantics: the engine compares ``bar_index <
        lockout_until``; live uses ``date.toordinal()`` of the decided
        bar, so lockouts naturally span calendar days.
        """
        stopped_today = self.stopped_on_date == decided_bar_date.isoformat()
        return AssetState(
            contracts=float(self.contracts),
            entry_vwap=float(Decimal(self.entry_vwap)) if self.entry_vwap else 0.0,
            stop_level=(
                float(Decimal(self.client_stop_level)) if self.client_stop_level else math.nan
            ),
            applied_dir=self.applied_dir,
            pending_dir=self.pending_dir,
            pending_count=self.pending_count,
            lockout_until=self.lockout_until_ord,
            lockout_dir=self.lockout_dir,
            vol_blocked=self.vol_blocked,
            stopped_today=stopped_today,
        )

    def absorb_engine_state(self, engine: AssetState) -> None:
        """Copy back the fields ``compute_targets`` mutates (hysteresis
        counters, S3 latch, applied direction). Position/stop fields are
        owned by the dispatch/risk paths, not the engine projection."""
        self.applied_dir = engine.applied_dir
        self.pending_dir = engine.pending_dir
        self.pending_count = engine.pending_count
        self.vol_blocked = engine.vol_blocked


def serialize_engine_state(states: Mapping[str, AssetRuntime]) -> dict[str, Any]:
    return {asset: rt.to_json() for asset, rt in states.items()}


def deserialize_engine_state(raw: Mapping[str, Any]) -> dict[str, AssetRuntime]:
    out: dict[str, AssetRuntime] = {}
    for asset in ASSETS:
        entry = raw.get(asset)
        out[asset] = AssetRuntime.from_json(entry) if isinstance(entry, Mapping) else AssetRuntime()
    return out


# ---------------------------------------------------------------------------
# Marks feed (worker-local WS ticker; the §3.2 mark source shape)
# ---------------------------------------------------------------------------


def _default_ws_connect(url: str) -> AbstractAsyncContextManager[Any]:
    import websockets  # lazy: hosts without the package can import this module

    return websockets.connect(url, max_queue=512, open_timeout=30, close_timeout=5)


class MarksFeed:
    """Worker-local WS ticker → :class:`MarkStore` (30 s risk-loop input).

    The strategy worker runs in its own container, so it cannot read the
    api-resident market-data worker's in-memory MarkStore; it consumes
    the same public WS ticker channel with the same parsing
    (:func:`extract_ticker_updates`) — one mark-source semantic, two
    subscribers. Reconnects with capped exponential backoff; staleness
    is judged by the risk loop via :meth:`ages_seconds`.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        ws_connect: WsConnectFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        max_backoff_s: float = 60.0,
    ) -> None:
        self._ws_url = ws_url
        self._ws_connect: WsConnectFactory = ws_connect or _default_ws_connect
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        self._max_backoff_s = max_backoff_s
        self._stop_event = asyncio.Event()
        self._product_ids: tuple[str, ...] = ()
        self._resubscribe = asyncio.Event()
        self.store = MarkStore()
        self._log = log.bind(component="strategy_worker.marks_feed")

    def set_products(self, product_ids: tuple[str, ...]) -> None:
        if product_ids and product_ids != self._product_ids:
            self._product_ids = tuple(sorted(product_ids))
            self._resubscribe.set()
            self._log.info("marks_feed_products_set", product_ids=list(self._product_ids))

    def latest_price(self, product_id: str) -> Decimal | None:
        mark = self.store.latest(product_id)
        return mark.price if mark is not None else None

    def age_seconds(self, product_id: str, *, now_utc: datetime) -> float | None:
        mark = self.store.latest(product_id)
        if mark is None:
            return None
        return max((now_utc - mark.observed_at_utc).total_seconds(), 0.0)

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        backoff_s = 1.0
        while not self._stop_event.is_set():
            if not self._product_ids:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._resubscribe.wait(), timeout=1.0)
                self._resubscribe.clear()
                continue
            try:
                async with self._ws_connect(self._ws_url) as ws:
                    self._resubscribe.clear()
                    await ws.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "channel": "ticker",
                                "product_ids": list(self._product_ids),
                            }
                        )
                    )
                    await ws.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))
                    self._log.info("marks_feed_connected", product_ids=list(self._product_ids))
                    backoff_s = 1.0
                    while not self._stop_event.is_set():
                        if self._resubscribe.is_set():
                            break  # reconnect with the new product set
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except TimeoutError:
                            continue
                        self._handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception("marks_feed_disconnected", reconnect_backoff_s=backoff_s)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff_s)
                backoff_s = min(backoff_s * 2, self._max_backoff_s)

    def _handle_message(self, raw: object) -> None:
        try:
            message = json.loads(raw if isinstance(raw, str | bytes) else str(raw))
        except (ValueError, TypeError):
            return
        if not isinstance(message, dict):
            return
        now_utc = self._clock()
        for product_id, price in extract_ticker_updates(message):
            self.store.record(product_id, price, observed_at_utc=now_utc)


# ---------------------------------------------------------------------------
# Persistence + audit I/O (all SQL lives here; unit tests fake this class)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskStateSnapshot:
    state: str
    severity: str | None
    convalescent_counter: int
    capital_event_active: bool


@dataclass(frozen=True, slots=True)
class DecisionRow:
    decision_date: date
    status: DecisionStatus
    equity_usd: Decimal | None
    outcome: dict[str, Any]
    engine_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorkerStatusRow:
    day_start_date: date | None
    day_start_equity_usd: Decimal | None
    weekly_halved_until: date | None
    flatten_seq: int
    engine_state: dict[str, Any]
    last_decision_date: date | None


class StrategyWorkerStore:
    """All DB reads/writes + downstream-processor feeds for the worker.

    One class so unit tests can substitute an in-memory fake (anti-
    pattern [A22]: unit tests never write real audit events). Every
    audit write in here routes through ``append_audit_event`` ([A01])
    and uses only the locked taxonomy ([A04]).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        env: Environment,
        phase_at_emit: PhaseAtEmit = 1,
    ) -> None:
        self._sf = session_factory
        self._env: Environment = env
        self._phase: PhaseAtEmit = phase_at_emit
        self._log = log.bind(component="strategy_worker.store")

    # -- reads ---------------------------------------------------------------

    async def fetch_active_account_id(self) -> UUID | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM accounts WHERE active_to IS NULL "
                        "ORDER BY active_from DESC LIMIT 1"
                    )
                )
            ).fetchone()
        return UUID(str(row.id)) if row is not None else None

    async def fetch_risk_state(self, account_id: UUID) -> RiskStateSnapshot | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state, severity, convalescent_session_count, "
                        "       capital_event_active_until_session_no "
                        "FROM risk_state "
                        "WHERE account_id = :acct AND is_current = TRUE"
                    ),
                    {"acct": account_id},
                )
            ).fetchone()
        if row is None:
            return None
        return RiskStateSnapshot(
            state=str(row.state),
            severity=str(row.severity) if row.severity is not None else None,
            convalescent_counter=int(row.convalescent_session_count or 0),
            capital_event_active=row.capital_event_active_until_session_no is not None,
        )

    async def fetch_last_threshold_met_capital_event_date(self, account_id: UUID) -> date | None:
        """UTC calendar date of the most recent threshold-met capital event.

        Source of truth for the m_capital_event session count: the
        UTC-day delta from this date (see
        ``services.risk.capital_events.capital_event_session_count``).
        The ``risk_state.capital_event_*_session_no`` fields are Phase-0
        absolute-counter placeholders and are NOT read for sizing.
        Below-threshold events never start the mode, hence the
        ``threshold_met`` filter.
        """
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT MAX((effective_at_utc AT TIME ZONE 'UTC')::date) "
                        "       AS event_day "
                        "FROM capital_events "
                        "WHERE account_id = :acct AND threshold_met = TRUE"
                    ),
                    {"acct": account_id},
                )
            ).fetchone()
        if row is None or row.event_day is None:
            return None
        event_day: date = row.event_day
        return event_day

    async def fetch_parameter_head(self) -> tuple[str, dict[str, str]] | None:
        """The active ``parameter_sets`` row (bootstrap-minted Amendment B seed)."""
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT parameter_set_hash, parameters FROM parameter_sets "
                        "WHERE last_active_at IS NULL "
                        "ORDER BY first_active_at DESC LIMIT 1"
                    )
                )
            ).fetchone()
        if row is None:
            return None
        raw = row.parameters
        params = raw if isinstance(raw, dict) else json.loads(str(raw))
        return str(row.parameter_set_hash), {str(k): str(v) for k, v in params.items()}

    async def fetch_slippage_head_id(self) -> UUID | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM slippage_calibration_versions WHERE is_head = TRUE LIMIT 1"
                    )
                )
            ).fetchone()
        return UUID(str(row.id)) if row is not None else None

    async def fetch_decision(self, account_id: UUID, decision_date: date) -> DecisionRow | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT decision_date, status, equity_usd, outcome, engine_state "
                        "FROM strategy_decisions "
                        "WHERE account_id = :acct AND decision_date = :d"
                    ),
                    {"acct": account_id, "d": decision_date},
                )
            ).fetchone()
        if row is None:
            return None
        outcome = row.outcome if isinstance(row.outcome, dict) else json.loads(str(row.outcome))
        engine_state = (
            row.engine_state
            if isinstance(row.engine_state, dict)
            else json.loads(str(row.engine_state))
        )
        raw_status = str(row.status)
        status: DecisionStatus
        if raw_status in (
            "dispatching",
            "completed",
            "failed",
            "skipped_risk_state",
            "skipped_stale_bars",
        ):
            status = raw_status  # type: ignore[assignment]  # narrowed by the check above
        else:  # pragma: no cover - CHECK constraint forbids
            status = "failed"
        return DecisionRow(
            decision_date=row.decision_date,
            status=status,
            equity_usd=_dec(row.equity_usd),
            outcome=outcome,
            engine_state=engine_state,
        )

    async def fetch_equity_on_or_before(
        self, account_id: UUID, on_date: date, *, lookback_days: int = 3
    ) -> Decimal | None:
        """Decision-time equity at ``on_date`` (or the nearest earlier
        decision within ``lookback_days``) — the weekly-loss window input."""
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT equity_usd FROM strategy_decisions "
                        "WHERE account_id = :acct AND decision_date <= :d "
                        "  AND decision_date >= :floor AND equity_usd IS NOT NULL "
                        "ORDER BY decision_date DESC LIMIT 1"
                    ),
                    {
                        "acct": account_id,
                        "d": on_date,
                        "floor": on_date - timedelta(days=lookback_days),
                    },
                )
            ).fetchone()
        return _dec(row.equity_usd) if row is not None else None

    async def fetch_month_max_equity(self, account_id: UUID, month_start: date) -> Decimal | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT MAX(equity_usd) AS m FROM strategy_decisions "
                        "WHERE account_id = :acct AND decision_date >= :start"
                    ),
                    {"acct": account_id, "start": month_start},
                )
            ).fetchone()
        return _dec(row.m) if row is not None else None

    async def fetch_worker_status(self, account_id: UUID) -> WorkerStatusRow | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT day_start_date, day_start_equity_usd, weekly_halved_until, "
                        "       flatten_seq, engine_state, last_decision_date "
                        "FROM strategy_worker_status WHERE account_id = :acct"
                    ),
                    {"acct": account_id},
                )
            ).fetchone()
        if row is None:
            return None
        engine_state = (
            row.engine_state
            if isinstance(row.engine_state, dict)
            else json.loads(str(row.engine_state))
        )
        return WorkerStatusRow(
            day_start_date=row.day_start_date,
            day_start_equity_usd=_dec(row.day_start_equity_usd),
            weekly_halved_until=row.weekly_halved_until,
            flatten_seq=int(row.flatten_seq or 0),
            engine_state=engine_state,
            last_decision_date=row.last_decision_date,
        )

    # -- writes --------------------------------------------------------------

    async def upsert_worker_status(
        self,
        account_id: UUID,
        *,
        now_utc: datetime,
        tick_increment: bool,
        marks_stale: bool,
        day_start_date: date | None,
        day_start_equity_usd: Decimal | None,
        weekly_halved_until: date | None,
        flatten_seq: int,
        engine_state: dict[str, Any],
        last_decision_date: date | None,
    ) -> None:
        """Heartbeat + state upsert — the §3.7/§3.9 freshness surface."""
        async with self._sf() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO strategy_worker_status ("
                        "    account_id, env, risk_loop_heartbeat_utc, risk_loop_tick_count,"
                        "    marks_stale, day_start_date, day_start_equity_usd,"
                        "    weekly_halved_until, flatten_seq, engine_state,"
                        "    last_decision_date, updated_at_utc"
                        ") VALUES ("
                        "    :acct, :env, :hb, 1, :stale, :dsd, :dse,"
                        "    :whu, :fseq, CAST(:state AS JSONB), :ldd, :now"
                        ") ON CONFLICT (account_id) DO UPDATE SET"
                        "    risk_loop_heartbeat_utc = :hb,"
                        "    risk_loop_tick_count = strategy_worker_status.risk_loop_tick_count"
                        "        + :tick,"
                        "    marks_stale = :stale,"
                        "    day_start_date = :dsd,"
                        "    day_start_equity_usd = :dse,"
                        "    weekly_halved_until = :whu,"
                        "    flatten_seq = :fseq,"
                        "    engine_state = CAST(:state AS JSONB),"
                        "    last_decision_date = :ldd,"
                        "    updated_at_utc = :now"
                    ),
                    {
                        "acct": account_id,
                        "env": self._env,
                        "hb": now_utc,
                        "tick": 1 if tick_increment else 0,
                        "stale": marks_stale,
                        "dsd": day_start_date,
                        "dse": day_start_equity_usd,
                        "whu": weekly_halved_until,
                        "fseq": flatten_seq,
                        "state": json.dumps(engine_state, default=str),
                        "ldd": last_decision_date,
                        "now": now_utc,
                    },
                )

    async def insert_decision(
        self,
        account_id: UUID,
        *,
        decision_date: date,
        now_utc: datetime,
        status: DecisionStatus,
        equity_usd: Decimal | None,
        equity_for_sizing_usd: Decimal | None,
        v_target: Decimal | None,
        m_combined_value: Decimal | None,
        weekly_halved: bool,
        parameter_set_hash: str | None,
        outcome: dict[str, Any],
        engine_state: dict[str, Any],
    ) -> None:
        async with self._sf() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO strategy_decisions ("
                        "    account_id, env, decision_date, decided_at_utc, status,"
                        "    equity_usd, equity_for_sizing_usd, v_target, m_combined,"
                        "    weekly_halved, parameter_set_hash, outcome, engine_state,"
                        "    updated_at_utc"
                        ") VALUES ("
                        "    :acct, :env, :d, :now, :status,"
                        "    :eq, :eqs, :vt, :mc,"
                        "    :wh, :psh, CAST(:outcome AS JSONB), CAST(:state AS JSONB),"
                        "    :now"
                        ") ON CONFLICT (account_id, decision_date) DO NOTHING"
                    ),
                    {
                        "acct": account_id,
                        "env": self._env,
                        "d": decision_date,
                        "now": now_utc,
                        "status": status,
                        "eq": equity_usd,
                        "eqs": equity_for_sizing_usd,
                        "vt": v_target,
                        "mc": m_combined_value,
                        "wh": weekly_halved,
                        "psh": parameter_set_hash,
                        "outcome": json.dumps(outcome, default=str),
                        "state": json.dumps(engine_state, default=str),
                    },
                )

    async def update_decision(
        self,
        account_id: UUID,
        *,
        decision_date: date,
        now_utc: datetime,
        status: DecisionStatus,
        outcome: dict[str, Any],
        engine_state: dict[str, Any],
    ) -> None:
        async with self._sf() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE strategy_decisions SET"
                        "    status = :status,"
                        "    outcome = CAST(:outcome AS JSONB),"
                        "    engine_state = CAST(:state AS JSONB),"
                        "    updated_at_utc = :now "
                        "WHERE account_id = :acct AND decision_date = :d"
                    ),
                    {
                        "acct": account_id,
                        "d": decision_date,
                        "now": now_utc,
                        "status": status,
                        "outcome": json.dumps(outcome, default=str),
                        "state": json.dumps(engine_state, default=str),
                    },
                )

    async def ensure_contract_row(self, *, asset: str, product: PerpProductRef) -> UUID:
        """Idempotent ``contracts`` row for a runtime-discovered perp product.

        Venue-agnostic column reuse per delta spec §3.7: ``multiplier``
        carries the nano contract size (0.01 BTC / 0.10 ETH) so the
        fill-processor's P&L/NAV math scales correctly;
        ``ibkr_local_symbol`` (legacy column name) carries the CDE
        product_id.
        """
        expiry = _expiry_from_product(product)
        async with self._sf() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO contracts ("
                        "    market_root, ibkr_local_symbol, expiry_date,"
                        "    point_value, multiplier, exchange, exchange_calendar_code, active"
                        ") VALUES ("
                        "    :root, :symbol, :expiry, :pv, :mult, 'CDE', 'CDE', TRUE"
                        ") ON CONFLICT (market_root, expiry_date) DO NOTHING"
                    ),
                    {
                        "root": asset,
                        "symbol": product.product_id,
                        "expiry": expiry,
                        "pv": product.contract_size,
                        "mult": product.contract_size,
                    },
                )
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM contracts "
                        "WHERE market_root = :root AND expiry_date = :expiry LIMIT 1"
                    ),
                    {"root": asset, "expiry": expiry},
                )
            ).fetchone()
        if row is None:  # pragma: no cover - defensive; insert-or-exists above
            raise RuntimeError(f"contracts row missing after ensure for {asset}")
        return UUID(str(row.id))

    async def insert_signal_row(
        self,
        account_id: UUID,
        *,
        market: str,
        contract_id: UUID | None,
        now_utc: datetime,
        session_date: date,
        direction: Literal["long", "short", "flat"],
        signal_type: Literal["entry", "exit"],
        decision_price: Decimal,
        target_contracts: int,
        sizing_trace: dict[str, Any],
        strategy_hash: str,
        parameter_set_hash: str,
        slippage_calibration_version_id: UUID,
        rationale: dict[str, Any],
    ) -> UUID:
        """INSERT the signals row + SIGNAL_EMITTED audit event (audit-first)."""
        async with self._sf() as audit_session:
            record = await append_audit_event(
                audit_session,
                AuditEventType.SIGNAL_EMITTED,
                {
                    "market": market,
                    "direction": direction,
                    "signal_type": signal_type,
                    "decision_price": str(decision_price),
                    "target_contracts": target_contracts,
                    "source": "strategy_worker",
                    "announce_only": True,
                    "rationale": rationale,
                    "ts": now_utc.isoformat(),
                },
                account_id=account_id,
                env=self._env,
                phase_at_emit=self._phase,
            )
        async with self._sf() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO signals ("
                            "    account_id, env, market, contract_id, emitted_at_utc,"
                            "    session_date, direction, signal_type, strategy_hash,"
                            "    parameter_set_hash, slippage_calibration_version_id,"
                            "    decision_price, target_contracts, sizing_trace, status"
                            ") VALUES ("
                            "    :acct, :env, :market, :cid, :now,"
                            "    :sd, :dir, :stype, :shash,"
                            "    :phash, :slip,"
                            "    :price, :tc, CAST(:trace AS JSONB), 'working'"
                            ") RETURNING id"
                        ),
                        {
                            "acct": account_id,
                            "env": self._env,
                            "market": market,
                            "cid": contract_id,
                            "now": now_utc,
                            "sd": session_date,
                            "dir": direction,
                            "stype": signal_type,
                            "shash": strategy_hash,
                            "phash": parameter_set_hash,
                            "slip": slippage_calibration_version_id,
                            "price": decision_price,
                            "tc": target_contracts,
                            "trace": json.dumps(sizing_trace, default=str),
                        },
                    )
                ).fetchone()
        assert row is not None
        self._log.info(
            "strategy_worker_signal_emitted",
            signal_id=str(row.id),
            market=market,
            direction=direction,
            signal_type=signal_type,
            target_contracts=target_contracts,
            audit_event_uuid=str(record.event_uuid),
        )
        return UUID(str(row.id))

    async def update_signal_status(self, signal_id: UUID, *, status: str) -> None:
        async with self._sf() as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE signals SET status = :status WHERE id = :sid"),
                    {"status": status, "sid": signal_id},
                )

    async def order_row_exists(self, client_order_id: str) -> str | None:
        """Return the existing orders.status for a client_order_id (or None).

        The crash-recovery dedupe: a leg whose orders row is already
        terminal must not be re-propagated (fills UNIQUE is
        (broker_fill_id, created_at) — created_at differs per insert, so
        the status check is the real guard).
        """
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status FROM orders WHERE client_order_id = :cid "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"cid": client_order_id},
                )
            ).fetchone()
        return str(row.status) if row is not None else None

    async def insert_order_row(
        self,
        account_id: UUID,
        *,
        signal_id: UUID,
        client_order_id: str,
        broker_order_id: str | None,
        market: str,
        contract_id: UUID | None,
        direction: Literal["buy", "sell"],
        order_type: str,
        quantity: int,
        limit_price: Decimal | None,
        stop_price: Decimal | None,
        now_utc: datetime,
        strategy_hash: str,
        parameter_set_hash: str,
        audit_payload: dict[str, Any],
    ) -> UUID:
        """Audit-first ORDER_PLACED event, then the orders row (§2.10.1).

        One row per execution LEG (the §5 ladder's per-stage venue
        orders are recorded in the audit payload's ``stages``) so the
        fill-processor's per-order filled/partially_filled semantics
        stay truthful against the leg's requested quantity. The row id
        is minted client-side (uuid7 per dev-guide §3.9) so the audit
        event can carry it while still committing FIRST — risk-review
        F9 (matches :meth:`insert_signal_row`'s ordering).
        """
        order_id = _new_uuid7()
        async with self._sf() as audit_session:
            await append_audit_event(
                audit_session,
                AuditEventType.ORDER_PLACED,
                {
                    "order_id": str(order_id),
                    "signal_id": str(signal_id),
                    "client_order_id": client_order_id,
                    "broker_order_id": broker_order_id,
                    "market": market,
                    "direction": direction,
                    "order_type": order_type,
                    "quantity": quantity,
                    "ts": now_utc.isoformat(),
                    **audit_payload,
                },
                account_id=account_id,
                env=self._env,
                phase_at_emit=self._phase,
            )
        async with self._sf() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO orders ("
                        "    id, account_id, env, signal_id, client_order_id,"
                        "    broker_order_id,"
                        "    market, contract_id, direction, order_type, quantity,"
                        "    limit_price, stop_price, placed_at_utc, status, retry_n,"
                        "    strategy_hash, parameter_set_hash"
                        ") VALUES ("
                        "    :oid, :acct, :env, :sig, :cid, :boid,"
                        "    :market, :contract, :dir, :otype, :qty,"
                        "    :lim, :stop, :now, 'pending', 0,"
                        "    :shash, :phash"
                        ")"
                    ),
                    {
                        "oid": order_id,
                        "acct": account_id,
                        "env": self._env,
                        "sig": signal_id,
                        "cid": client_order_id,
                        "boid": broker_order_id,
                        "market": market,
                        "contract": contract_id,
                        "dir": direction,
                        "otype": order_type,
                        "qty": quantity,
                        "lim": limit_price,
                        "stop": stop_price,
                        "now": now_utc,
                        "shash": strategy_hash,
                        "phash": parameter_set_hash,
                    },
                )
        return order_id

    async def process_fill(
        self,
        *,
        client_order_id: str,
        payload: FillIngestPayload,
    ) -> bool:
        """Feed one aggregated leg fill into the kept fill pipeline.

        Returns True when fully propagated; False when the scenario is
        outside the processor's supported set (EXIT_PARTIAL /
        EXIT_REVERSAL are pre-existing Phase-2 deferrals) — the caller
        falls back to :meth:`minimal_fill_fallback` so ``orders`` +
        ``fills`` stay truthful even when position/trade propagation
        defers.
        """
        try:
            result = await process_fill_event(
                session_factory=self._sf,
                client_order_id=client_order_id,
                payload=payload,
                env=self._env,
                phase_at_emit=self._phase,
            )
        except UnsupportedFillScenarioError as exc:
            self._log.warning(
                "strategy_worker_fill_scenario_deferred",
                client_order_id=client_order_id,
                error_code=exc.error_code,
                detail=str(exc),
            )
            return False
        except FillProcessingError as exc:
            # 2026-07-12 incident: EXIT_NO_PRIOR_TRADE escaped this seam
            # and killed the decision AFTER the venue fill — the worst
            # possible ordering (venue traded, backend refused to record
            # it, recon broke on the gap, auto-halt). No fill
            # CLASSIFICATION/PLANNING error may ever again abort a
            # decision post-execution — every FillProcessingError raise
            # site fires BEFORE apply_fill_plan writes anything, so this
            # catch can never mask a partial apply. Deliberately NOT
            # `except Exception`: AuditWriteFailure and raw DB errors
            # from the apply step MUST still abort (§2.10.1 fail-closed).
            self._log.error(
                "strategy_worker_fill_propagation_failed_fallback",
                client_order_id=client_order_id,
                error_code=exc.error_code,
                detail=str(exc),
                note=(
                    "full position/trade propagation failed; falling back to "
                    "minimal orders/fills recording — positions_current/trades "
                    "may need the replay_leg_fill operator tool"
                ),
            )
            return False
        if result is None:
            self._log.warning("strategy_worker_fill_unknown_order", client_order_id=client_order_id)
        return True

    async def minimal_fill_fallback(
        self,
        account_id: UUID,
        *,
        client_order_id: str,
        payload: FillIngestPayload,
        market: str,
        fully_filled: bool,
    ) -> None:
        """ORDER_FILLED audit + fills INSERT + orders UPDATE for scenarios
        the fill processor defers (partial reduces). Position/trade rows
        are NOT mutated here — the divergence is logged loudly and the
        00:15 UTC recon cycle (§3.5) reconciles the read models; the
        worker's own position truth is the venue."""
        order_status = await self.order_row_exists(client_order_id)
        if order_status in ("filled", "cancelled", "rejected"):
            return
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, created_at FROM orders WHERE client_order_id = :cid "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"cid": client_order_id},
                )
            ).fetchone()
        if row is None:
            self._log.error(
                "strategy_worker_fill_fallback_order_missing",
                client_order_id=client_order_id,
            )
            return
        order_id = UUID(str(row.id))
        async with self._sf() as audit_session:
            await append_audit_event(
                audit_session,
                AuditEventType.ORDER_FILLED,
                {
                    "order_id": str(order_id),
                    "client_order_id": client_order_id,
                    "market": market,
                    "fill_quantity": payload.fill_quantity,
                    "fill_price": str(payload.fill_price),
                    "commission_usd": str(payload.commission_usd),
                    "note": (
                        "minimal propagation: fill scenario outside fill_processor's "
                        "supported set (EXIT_PARTIAL deferral); positions/trades "
                        "reconcile via the EOD cycle"
                    ),
                    "ts": payload.filled_at_utc.isoformat(),
                },
                account_id=account_id,
                env=self._env,
                phase_at_emit=self._phase,
            )
        async with self._sf() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO fills ("
                        "    account_id, env, order_id, broker_fill_id, filled_at_utc,"
                        "    fill_price, fill_quantity, commission"
                        ") VALUES ("
                        "    :acct, :env, :oid, :bfid, :ts, :price, :qty, :comm"
                        ")"
                    ),
                    {
                        "acct": account_id,
                        "env": self._env,
                        "oid": order_id,
                        "bfid": payload.broker_fill_id,
                        "ts": payload.filled_at_utc,
                        "price": payload.fill_price,
                        "qty": payload.fill_quantity,
                        "comm": payload.commission_usd,
                    },
                )
                await session.execute(
                    text(
                        "UPDATE orders SET status = :status "
                        "WHERE id = :oid AND created_at = :created"
                    ),
                    {
                        "status": "filled" if fully_filled else "partially_filled",
                        "oid": order_id,
                        "created": row.created_at,
                    },
                )

    async def process_terminal(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        status_kind: Literal["cancelled", "rejected"],
        reason: str | None,
        now_utc: datetime,
    ) -> None:
        await process_terminal_status_event(
            session_factory=self._sf,
            client_order_id=client_order_id,
            payload=TerminalStatusPayload(
                broker_order_id=broker_order_id,
                status_kind=status_kind,
                rejection_reason=reason,
                observed_at_utc=now_utc,
            ),
            env=self._env,
            phase_at_emit=self._phase,
        )

    async def insert_alert(
        self,
        account_id: UUID,
        *,
        severity: Literal["P0", "P1", "P2"],
        category: str,
        message: str,
        detail: dict[str, Any],
        now_utc: datetime,
    ) -> None:
        """INSERT an ``alerts`` row — the operator-visibility surface the
        Discord alert path + alerts API read (risk-review F1a: a
        worker-driven halt/flatten must never be silent).

        ``category`` is an EXISTING ``alert_category`` enum member ONLY
        (no enum migration; delta spec §3.8's free-text-payload remap
        convention — specifics ride ``detail``). **Alert-after-action +
        never raises:** a failed alert write must not block or unwind
        the protective action it reports; failure logs loudly and the
        audit chain remains the durable record.
        """
        try:
            async with self._sf() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO alerts ("
                            "    account_id, fired_at_utc, severity, category,"
                            "    message, detail"
                            ") VALUES ("
                            "    :acct, :ts, :sev, CAST(:cat AS alert_category),"
                            "    :msg, CAST(:detail AS JSONB)"
                            ")"
                        ),
                        {
                            "acct": account_id,
                            "ts": now_utc,
                            "sev": severity,
                            "cat": category,
                            "msg": message,
                            "detail": json.dumps(detail, default=str),
                        },
                    )
            self._log.info(
                "strategy_worker_alert_inserted",
                severity=severity,
                category=category,
                message=message,
            )
        except Exception:
            self._log.exception(
                "strategy_worker_alert_insert_failed",
                severity=severity,
                category=category,
                message=message,
                detail=detail,
            )

    async def invoke_kill_switch(
        self,
        account_id: UUID,
        *,
        trigger: TransitionTrigger,
        now_utc: datetime,
    ) -> bool:
        """Drive the EXISTING FSM transition to HALT_NEW (no new states).

        Returns True when a transition was applied; False when already
        halted (facade short-circuit per the policy-layer contract) or
        when no risk_state row exists.
        """
        snapshot = await self.fetch_risk_state(account_id)
        if snapshot is None:
            self._log.error("strategy_worker_kill_switch_no_risk_state_row")
            return False
        if snapshot.state == RiskState.HALT_NEW.value:
            return False
        plan = plan_invoke_kill_switch(
            current_state=RiskState(snapshot.state),
            current_severity=HaltSeverity(snapshot.severity) if snapshot.severity else None,
            convalescent_counter=snapshot.convalescent_counter,
            trigger=trigger,
            triggered_by="risk_engine",
            timestamp_utc=now_utc.isoformat(),
        )
        async with self._sf() as session:
            applied = await apply_state_transition(
                plan=plan,
                db=session,
                account_id=account_id,
                env=self._env,
                phase_at_emit=self._phase,
            )
        self._log.warning(
            "strategy_worker_kill_switch_invoked",
            trigger=trigger.value,
            new_state=applied.new_state,
            new_severity=applied.new_severity,
            audit_event_uuid=str(applied.state_transition_audit_event_uuid),
        )
        return True


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


@dataclass
class _DecisionInputs:
    """Everything the daily decision gathered before computing targets."""

    params: CryptoTrendParams
    parameter_set_hash: str
    canonical: dict[str, str]
    equity: Decimal
    equity_for_sizing: Decimal
    products: dict[str, PerpProductRef]
    rows: dict[str, AssetDecisionRow]
    marks: dict[str, Decimal]
    slippage_head_id: UUID


class StrategyWorker:
    """00:05 UTC daily decision + 30 s risk loop (delta spec §3.3)."""

    def __init__(
        self,
        *,
        config: StrategyWorkerConfig,
        store: StrategyWorkerStore,
        broker: CoinbaseBrokerClient,
        adapter: CoinbaseExecutionAdapter,
        market_rest: Any | None = None,
        marks_feed: MarksFeed | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._broker = broker
        self._adapter = adapter
        self._market_rest: Any = market_rest or HttpxCoinbaseRestClient()
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        self.marks = marks_feed or MarksFeed(
            ws_url=config.ws_url,
            clock=self._clock,
            max_backoff_s=config.ws_reconnect_max_backoff_s,
        )
        self._stop_event = asyncio.Event()
        self._trade_lock = asyncio.Lock()
        self._decision_task: asyncio.Task[None] | None = None
        self._log = log.bind(worker="strategy_worker")

        self.account_id: UUID | None = None
        self.state: dict[str, AssetRuntime] = {a: AssetRuntime() for a in ASSETS}
        self.products: dict[str, PerpProductRef] = {}
        self._day_start_date: date | None = None
        self._day_start_equity: Decimal | None = None
        self._weekly_halved_until: date | None = None
        self._flatten_seq: int = 0
        self._last_decision_date: date | None = None
        self._consecutive_tick_failures = 0
        self._tick_failure_halt_fired = False

    # -- lifecycle ------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop_event.set()
        self.marks.request_stop()

    async def run_forever(self) -> None:
        """Startup recovery → marks feed task + tick loop until stopped."""
        self._log.info(
            "strategy_worker_started",
            env=self._config.env,
            risk_tick_interval_s=self._config.risk_tick_interval_s,
            e_effective_cap_usd=(
                str(self._config.e_effective_cap_usd)
                if self._config.e_effective_cap_usd is not None
                else None
            ),
            max_contracts=dict(self._config.max_contracts),
        )
        await self.startup_recovery()
        marks_task = asyncio.create_task(
            self.marks.run_forever(), name="strategy_worker.marks_feed"
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_tick()
                    self._consecutive_tick_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._consecutive_tick_failures += 1
                    self._log.exception(
                        "strategy_worker_tick_failed",
                        consecutive_failures=self._consecutive_tick_failures,
                    )
                    await self._maybe_halt_on_tick_failures()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.risk_tick_interval_s,
                    )
        finally:
            if self._decision_task is not None and not self._decision_task.done():
                self._decision_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._decision_task
            marks_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await marks_task
            self._log.info("strategy_worker_stopped")

    async def _maybe_halt_on_tick_failures(self) -> None:
        """Invoke HALT_NEW after RISK_TICK_FAILURE_HALT_THRESHOLD consecutive
        tick failures. The ``_tick_failure_halt_fired`` latch sets ONLY after
        ``invoke_kill_switch`` returns — a raised invoke leaves the latch
        unset so the next 30 s tick retries (fail-loud-with-retry, the
        dispatch-layer lock contract). Latching before the call was fail-open:
        one transient invoke failure and the worker never halted."""
        if (
            self._consecutive_tick_failures >= RISK_TICK_FAILURE_HALT_THRESHOLD
            and not self._tick_failure_halt_fired
            and self.account_id is not None
        ):
            now = self._clock()
            try:
                applied = await self._store.invoke_kill_switch(
                    self.account_id,
                    trigger=TransitionTrigger.UNHANDLED_EXCEPTION,
                    now_utc=now,
                )
            except Exception:
                self._log.exception(
                    "strategy_worker_tick_failure_halt_invoke_failed",
                    consecutive_failures=self._consecutive_tick_failures,
                    note="latch unset; next risk tick retries the kill switch",
                )
                return
            self._tick_failure_halt_fired = True
            await self._store.insert_alert(
                self.account_id,
                severity="P1",
                category="kill_switch_invoked",
                message=(
                    f"risk loop failed {self._consecutive_tick_failures} consecutive "
                    "ticks — HALT_NEW (unhandled_exception); see worker logs"
                ),
                detail={
                    "trigger": TransitionTrigger.UNHANDLED_EXCEPTION.value,
                    "consecutive_failures": self._consecutive_tick_failures,
                    "source": "strategy_worker_risk_loop",
                    "transition_applied": applied,
                },
                now_utc=now,
            )

    async def startup_recovery(self) -> None:
        """§7 restart rule: reconcile venue state, re-arm missing stops,
        rebuild engine state — BEFORE either loop starts."""
        self.account_id = await self._store.fetch_active_account_id()
        if self.account_id is None:
            raise RuntimeError(
                "no active accounts row — run bootstrap_live_account before the worker"
            )
        status = await self._store.fetch_worker_status(self.account_id)
        if status is not None:
            self.state = deserialize_engine_state(status.engine_state)
            self._day_start_date = status.day_start_date
            self._day_start_equity = status.day_start_equity_usd
            self._weekly_halved_until = status.weekly_halved_until
            self._flatten_seq = status.flatten_seq
            self._last_decision_date = status.last_decision_date

        await self._refresh_products()
        snapshot = await self._adapter.startup_reconcile()

        # Venue positions are truth for contracts/entry refs.
        venue_by_asset: dict[str, tuple[int, Decimal | None]] = {}
        for pos in snapshot.positions:
            asset = self._asset_for_product_id(pos.product_id)
            if asset is not None and pos.contracts != 0:
                venue_by_asset[asset] = (int(pos.contracts), pos.entry_vwap)
        for asset in ASSETS:
            rt = self.state[asset]
            venue_contracts, venue_vwap = venue_by_asset.get(asset, (0, None))
            if rt.contracts != venue_contracts:
                self._log.warning(
                    "strategy_worker_state_venue_divergence",
                    asset=asset,
                    tracked_contracts=rt.contracts,
                    venue_contracts=venue_contracts,
                    note="venue wins; engine memory fields retained",
                )
                rt.contracts = venue_contracts
            if venue_contracts != 0 and rt.entry_vwap is None and venue_vwap is not None:
                rt.entry_vwap = str(venue_vwap)

        # Re-arm missing native stops before doing anything else (§7).
        for asset in ASSETS:
            rt = self.state[asset]
            product = self.products.get(asset)
            if rt.contracts == 0 or product is None:
                continue
            if product.product_id in snapshot.unprotected_product_ids:
                await self._arm_native_stop(asset, reason="startup_rearm")

        await self._persist_status(now_utc=self._clock(), tick_increment=False)
        self._log.info(
            "strategy_worker_startup_recovery_completed",
            positions={a: self.state[a].contracts for a in ASSETS},
            unprotected=list(snapshot.unprotected_product_ids),
            last_decision_date=(
                self._last_decision_date.isoformat() if self._last_decision_date else None
            ),
        )

    # -- tick -----------------------------------------------------------------

    async def run_tick(self, *, now_utc: datetime | None = None) -> None:
        """One 30 s iteration: heartbeat → decision-due check → risk checks."""
        now = now_utc if now_utc is not None else self._clock()
        if now.tzinfo is None:
            raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")
        self._touch_heartbeat_file()

        due = decision_due(now_utc=now, last_decision_date=self._last_decision_date)
        if due is not None and (self._decision_task is None or self._decision_task.done()):
            self._decision_task = asyncio.create_task(
                self._run_decision_guarded(due), name="strategy_worker.daily_decision"
            )

        await self.run_risk_checks(now_utc=now)
        await self._persist_status(now_utc=now, tick_increment=True)

    def _touch_heartbeat_file(self) -> None:
        if self._config.heartbeat_file:
            with contextlib.suppress(OSError):
                Path(self._config.heartbeat_file).touch()

    async def _persist_status(self, *, now_utc: datetime, tick_increment: bool) -> None:
        if self.account_id is None:
            return
        marks_stale = self._marks_stale(now_utc=now_utc)
        await self._store.upsert_worker_status(
            self.account_id,
            now_utc=now_utc,
            tick_increment=tick_increment,
            marks_stale=marks_stale,
            day_start_date=self._day_start_date,
            day_start_equity_usd=self._day_start_equity,
            weekly_halved_until=self._weekly_halved_until,
            flatten_seq=self._flatten_seq,
            engine_state=serialize_engine_state(self.state),
            last_decision_date=self._last_decision_date,
        )

    def _marks_stale(self, *, now_utc: datetime) -> bool:
        """True when any OPEN position's perp mark is stale (> §7 threshold)."""
        for asset in ASSETS:
            rt = self.state[asset]
            product = self.products.get(asset)
            if rt.contracts == 0 or product is None:
                continue
            age = self.marks.age_seconds(product.product_id, now_utc=now_utc)
            if age is None or age >= self._config.stale_threshold_s:
                return True
        return False

    # -- risk loop -------------------------------------------------------------

    async def run_risk_checks(self, *, now_utc: datetime) -> None:
        """Strategy §5 Layer-2 + §7: stops, outage, loss limits, halt floor."""
        if self.account_id is None:
            return

        # 1) Venue snapshots (bounded by the client's per-call timeout).
        summary: FuturesBalanceSummary | None
        try:
            summary = await self._broker.get_futures_balance_summary()
        except Exception:
            self._log.warning("strategy_worker_balance_fetch_failed")
            summary = None
        venue_positions: dict[str, BrokerPosition] | None
        try:
            venue_positions = {p.product_id: p for p in await self._broker.list_positions()}
        except Exception:
            self._log.warning("strategy_worker_positions_fetch_failed")
            venue_positions = None

        # 2) Native-stop fill / external-change detection.
        if venue_positions is not None:
            await self._detect_external_position_changes(venue_positions, now_utc=now_utc)

        # 2b) Re-arm any missing native stop (gate A2 posture: a position
        # must never sit without its venue-resting backstop; a failed arm
        # retries every tick and logs each attempt).
        for asset in ASSETS:
            rt = self.state[asset]
            if rt.contracts != 0 and rt.native_stop_order_id is None:
                await self._arm_native_stop(asset, reason="risk_loop_rearm")

        # 3) Data-outage policy (§7): stale marks ⇒ protected-hold / flatten.
        if self._marks_stale(now_utc=now_utc):
            await self._apply_outage_policy(now_utc=now_utc)
        else:
            # 4) Client-side 2xATR soft stops off live marks (§5 Layer 2).
            await self._check_client_stops(now_utc=now_utc)

        # 5) Equity-based rules (skip when the balance surface is down).
        if summary is None:
            return
        equity = equity_from_summary(summary)
        if equity is None:
            self._log.warning("strategy_worker_equity_unavailable_fail_safe")
            return

        today = now_utc.astimezone(UTC).date()
        if self._day_start_date != today:
            self._day_start_date = today
            self._day_start_equity = equity

        risk_snapshot = await self._store.fetch_risk_state(self.account_id)
        already_halted = (
            risk_snapshot is not None and risk_snapshot.state == RiskState.HALT_NEW.value
        )

        canonical = await self._canonical_params()
        halt_floor = _dec(canonical.get("HALT_EQUITY_USD")) or Decimal("1500")
        daily_limit = _dec(canonical.get("DAILY_LOSS_LIMIT")) or Decimal("-0.08")
        weekly_limit = _dec(canonical.get("WEEKLY_LOSS_LIMIT")) or Decimal("-0.16")

        # 5a) Hard halt — the $1,500 malfunction circuit-breaker (§7 /
        # Amendment A). DECOMMISSION_FLOOR is the existing incident-review
        # trigger for the equity floor; restart is manual-flag-removal.
        # Risk-review F4: the flatten runs even when ALREADY halted (a
        # halted book sliding through the floor must still be flattened);
        # only the FSM transition itself is retrip-guarded.
        if equity <= halt_floor:
            has_positions = any(self.state[a].contracts != 0 for a in ASSETS)
            if has_positions:
                self._log.error(
                    "strategy_worker_hard_halt_floor_breached",
                    equity_usd=str(equity),
                    halt_floor_usd=str(halt_floor),
                    already_halted=already_halted,
                )
                await self._flatten_all(reason="halt_floor", now_utc=now_utc)
            if not already_halted:
                await self._store.invoke_kill_switch(
                    self.account_id,
                    trigger=TransitionTrigger.DECOMMISSION_FLOOR,
                    now_utc=now_utc,
                )
                await self._store.insert_alert(
                    self.account_id,
                    severity="P0",
                    category="kill_switch_invoked",
                    message=(
                        f"hard-halt equity floor breached: equity {equity} <= "
                        f"{halt_floor} — flattened + HALT_NEW (decommission_floor)"
                    ),
                    detail={
                        "trigger": TransitionTrigger.DECOMMISSION_FLOOR.value,
                        "equity_usd": str(equity),
                        "halt_floor_usd": str(halt_floor),
                        "source": "strategy_worker_risk_loop",
                    },
                    now_utc=now_utc,
                )
            elif has_positions:
                # Already halted, but positions existed below the floor —
                # the flatten itself must still be visible (F1a + F4).
                await self._store.insert_alert(
                    self.account_id,
                    severity="P0",
                    category="kill_switch_invoked",
                    message=(
                        f"equity {equity} <= floor {halt_floor} while already "
                        "HALT_NEW — open positions flattened (no FSM retrip)"
                    ),
                    detail={
                        "equity_usd": str(equity),
                        "halt_floor_usd": str(halt_floor),
                        "already_halted": True,
                        "source": "strategy_worker_risk_loop",
                    },
                    now_utc=now_utc,
                )
            return

        # 5b) Daily loss limit (from 00:00 UTC baseline).
        if not already_halted and self._day_start_equity is not None and self._day_start_equity > 0:
            daily_pnl_frac = equity / self._day_start_equity - 1
            if daily_pnl_frac < daily_limit:
                self._log.error(
                    "strategy_worker_daily_loss_limit_breached",
                    daily_pnl_frac=str(daily_pnl_frac),
                    limit=str(daily_limit),
                    equity_usd=str(equity),
                    day_start_equity_usd=str(self._day_start_equity),
                )
                await self._flatten_all(reason="daily_loss", now_utc=now_utc)
                await self._store.invoke_kill_switch(
                    self.account_id,
                    trigger=TransitionTrigger.DAILY_LOSS_BREACH,
                    now_utc=now_utc,
                )
                await self._store.insert_alert(
                    self.account_id,
                    severity="P1",
                    category="kill_switch_invoked",
                    message=(
                        f"daily loss limit breached ({daily_pnl_frac:.4f} < "
                        f"{daily_limit}) — flattened + HALT_NEW (daily_loss_breach)"
                    ),
                    detail={
                        "trigger": TransitionTrigger.DAILY_LOSS_BREACH.value,
                        "daily_pnl_frac": str(daily_pnl_frac),
                        "limit": str(daily_limit),
                        "equity_usd": str(equity),
                        "day_start_equity_usd": str(self._day_start_equity),
                        "source": "strategy_worker_risk_loop",
                    },
                    now_utc=now_utc,
                )
                return

        # 5c) Weekly loss limit — engine semantics: halve V_target for 7
        # days (strategy §7); NOT an FSM halt.
        week_ago_equity = await self._store.fetch_equity_on_or_before(
            self.account_id, today - timedelta(days=7)
        )
        if week_ago_equity is not None and week_ago_equity > 0:
            weekly_pnl_frac = equity / week_ago_equity - 1
            if weekly_pnl_frac < weekly_limit:
                new_until = today + timedelta(days=7)
                if self._weekly_halved_until is None or self._weekly_halved_until < new_until:
                    self._weekly_halved_until = new_until
                    self._log.warning(
                        "strategy_worker_weekly_loss_limit_tripped",
                        weekly_pnl_frac=str(weekly_pnl_frac),
                        limit=str(weekly_limit),
                        v_target_halved_until=new_until.isoformat(),
                    )

        # 5d) Liquidation buffer (§7 belt-and-suspenders; venue-exposed).
        if summary.liquidation_buffer_percentage is not None:
            buffer_frac = normalize_liq_buffer(summary.liquidation_buffer_percentage)
            if buffer_frac < self._config.liq_buffer_min_frac:
                self._log.error(
                    "strategy_worker_liquidation_buffer_breached",
                    buffer_frac=str(buffer_frac),
                    floor=str(self._config.liq_buffer_min_frac),
                )
                await self._force_reduce_half(now_utc=now_utc)

    async def _detect_external_position_changes(
        self, venue_positions: Mapping[str, BrokerPosition], *, now_utc: datetime
    ) -> None:
        """Detect native-stop fills (and any other venue-side position
        change) by diffing tracked state against ``list_positions``.

        Runs under ``_trade_lock`` (risk-review C2): a stale venue
        snapshot interleaved with a decision leg executing could
        otherwise cancel a freshly-armed stop / just-placed order and
        zero tracking on a real position. Same lock discipline as the
        flatten path."""
        async with self._trade_lock:
            await self._detect_external_position_changes_locked(venue_positions, now_utc=now_utc)

    async def _detect_external_position_changes_locked(
        self, venue_positions: Mapping[str, BrokerPosition], *, now_utc: datetime
    ) -> None:
        for asset in ASSETS:
            rt = self.state[asset]
            product = self.products.get(asset)
            if product is None:
                continue
            pos = venue_positions.get(product.product_id)
            venue_contracts = int(pos.contracts) if pos is not None else 0
            if venue_contracts == rt.contracts:
                continue
            stop_filled = False
            if rt.native_stop_order_id:
                with contextlib.suppress(Exception):
                    stop_state = await self._broker.get_order(rt.native_stop_order_id)
                    if stop_state.status == "filled":
                        stop_filled = True
                        await self._handle_native_stop_fill(
                            asset, stop_state=stop_state, now_utc=now_utc
                        )
            if not stop_filled:
                self._log.error(
                    "strategy_worker_external_position_change",
                    asset=asset,
                    tracked_contracts=rt.contracts,
                    venue_contracts=venue_contracts,
                    note=(
                        "position changed outside the worker (manual action or "
                        "venue liquidation); syncing to venue truth"
                    ),
                )
                if venue_contracts == 0:
                    # 2026-07-12 incident: _clear_position_state forgets the
                    # native stop id WITHOUT cancelling it at the venue — a
                    # resting stop on a flat book is a naked order that would
                    # open an unintended position. Cancel venue-side FIRST,
                    # then VERIFY the book is actually clean (risk-review B1:
                    # the venue can reject a cancel inside an HTTP 200 and the
                    # adapter's batch call surfaces no exception). On failure
                    # OR surviving orders, tracked contracts stay UNSYNCED so
                    # this branch re-fires on the next 30 s tick and retries.
                    if rt.native_stop_order_id or rt.native_stop_client_order_id:
                        try:
                            await self._adapter.cancel_all_orders(product.product_id)
                            remaining = await self._broker.list_open_orders(product.product_id)
                        except Exception:
                            self._log.exception(
                                "strategy_worker_external_flat_cancel_failed",
                                asset=asset,
                                note=(
                                    "tracked contracts left unsynced so the "
                                    "next tick retries the cancel"
                                ),
                            )
                            continue
                        if remaining:
                            self._log.error(
                                "strategy_worker_external_flat_cancel_rejected",
                                asset=asset,
                                product_id=product.product_id,
                                remaining_open_orders=len(remaining),
                                note=(
                                    "venue accepted the cancel request but "
                                    "orders remain open; retrying next tick"
                                ),
                            )
                            continue
                        self._log.warning(
                            "strategy_worker_external_flat_orders_cancelled",
                            asset=asset,
                            product_id=product.product_id,
                        )
                    rt.contracts = 0
                    self._clear_position_state(rt)
                else:
                    rt.contracts = venue_contracts
                    await self._arm_native_stop(asset, reason="external_change_rearm")

    async def _handle_native_stop_fill(
        self, asset: str, *, stop_state: BrokerOrderState, now_utc: datetime
    ) -> None:
        """The venue-resting 3xATR backstop fired: propagate + lockout."""
        rt = self.state[asset]
        prior = rt.contracts
        self._log.warning(
            "strategy_worker_native_stop_filled",
            asset=asset,
            prior_contracts=prior,
            order_id=stop_state.order_id,
            avg_fill_price=str(stop_state.avg_fill_price) if stop_state.avg_fill_price else None,
        )
        payload = FillIngestPayload(
            broker_fill_id=f"{stop_state.client_order_id}:agg",
            cumulative_filled_quantity=int(stop_state.filled_contracts),
            fill_quantity=int(stop_state.filled_contracts),
            fill_price=stop_state.avg_fill_price or Decimal(0),
            commission_usd=stop_state.total_fees_usd,
            filled_at_utc=now_utc,
        )
        propagated = await self._store.process_fill(
            client_order_id=stop_state.client_order_id, payload=payload
        )
        if not propagated and self.account_id is not None:
            await self._store.minimal_fill_fallback(
                self.account_id,
                client_order_id=stop_state.client_order_id,
                payload=payload,
                market=asset,
                fully_filled=True,
            )
        self._apply_stop_lockout(rt, now_utc=now_utc)

    def _apply_stop_lockout(self, rt: AssetRuntime, *, now_utc: datetime) -> None:
        """§5 exit rule: after a stop-out, same-direction re-entry is
        locked for 2 daily closes; direction/hysteresis state resets."""
        bar_day = now_utc.astimezone(UTC).date()
        rt.lockout_dir = 1 if rt.contracts > 0 else -1
        # `+ 2` intentionally mirrors the parity-locked engine's
        # `lockout_days` / the Amendment B seed's LOCKOUT_DAYS=2 (§5,
        # locked). A parameter amendment changing LOCKOUT_DAYS must touch
        # this literal too (and the twin in `_flatten_position`).
        rt.lockout_until_ord = bar_day.toordinal() + 2
        rt.stopped_on_date = bar_day.isoformat()
        rt.contracts = 0
        self._clear_position_state(rt)

    def _clear_position_state(self, rt: AssetRuntime) -> None:
        """Reset protection + direction memory (contracts untouched —
        the caller owns position truth). Lockout fields survive."""
        rt.entry_vwap = None
        rt.client_stop_level = None
        rt.atrp_at_stop_set = None
        rt.native_stop_order_id = None
        rt.native_stop_client_order_id = None
        rt.applied_dir = 0
        rt.pending_dir = 0
        rt.pending_count = 0

    async def _apply_outage_policy(self, *, now_utc: datetime) -> None:
        """§7 data-outage row: protected-hold when the native stop is
        confirmed resting; otherwise flatten as soon as an order path
        works."""
        try:
            open_orders = await self._broker.list_open_orders()
        except Exception:
            self._log.error(
                "strategy_worker_outage_order_path_down",
                note="marks stale AND order endpoints failing; will retry next tick",
            )
            return
        for asset in ASSETS:
            rt = self.state[asset]
            product = self.products.get(asset)
            if rt.contracts == 0 or product is None:
                continue
            closing_side = "sell" if rt.contracts > 0 else "buy"
            covered = any(
                o.kind == "stop_limit"
                and o.product_id == product.product_id
                and o.side == closing_side
                and o.status in ("open", "queued")
                and o.contracts >= abs(Decimal(rt.contracts))
                for o in open_orders
            )
            if covered:
                self._log.warning(
                    "strategy_worker_outage_protected_hold",
                    asset=asset,
                    contracts=rt.contracts,
                )
            else:
                self._log.error(
                    "strategy_worker_outage_flattening_unprotected",
                    asset=asset,
                    contracts=rt.contracts,
                )
                await self._flatten_position(asset, reason="outage_unprotected", now_utc=now_utc)

    async def _check_client_stops(self, *, now_utc: datetime) -> None:
        for asset in ASSETS:
            rt = self.state[asset]
            product = self.products.get(asset)
            if rt.contracts == 0 or product is None or rt.client_stop_level is None:
                continue
            mark = self.marks.latest_price(product.product_id)
            if mark is None:
                continue
            stop_level = Decimal(rt.client_stop_level)
            breached = (rt.contracts > 0 and mark <= stop_level) or (
                rt.contracts < 0 and mark >= stop_level
            )
            if breached:
                self._log.warning(
                    "strategy_worker_client_stop_breached",
                    asset=asset,
                    contracts=rt.contracts,
                    mark=str(mark),
                    stop_level=str(stop_level),
                )
                await self._flatten_position(
                    asset, reason="client_stop", now_utc=now_utc, lockout=True
                )

    async def _force_reduce_half(self, *, now_utc: datetime) -> None:
        """§7 liquidation-buffer force-reduce: halve each open position
        (converges to compliance across ticks; integer flooring)."""
        for asset in ASSETS:
            rt = self.state[asset]
            if rt.contracts == 0:
                continue
            reduce_by = abs(rt.contracts) // 2
            if reduce_by == 0:
                reduce_by = abs(rt.contracts)  # 1-contract position: close it
            await self._flatten_position(
                asset, reason="liq_buffer", now_utc=now_utc, contracts=reduce_by
            )

    async def _preempt_decision_dispatch(self, *, reason: str) -> None:
        """Cancel an in-flight decision task so a protective exit never
        queues behind a ladder stage (risk-review F6: a stop waiting up
        to 10 min behind stage-1's post-only window would stall the
        heartbeat and cycle the container). The decision's leg-grain
        crash-resume machinery + deterministic client_order_ids make the
        interruption safe — the next tick resumes (or the halt gate
        turns the resume into a recorded skip)."""
        task = self._decision_task
        if task is not None and not task.done():
            self._log.warning(
                "strategy_worker_decision_preempted_by_protective_exit",
                reason=reason,
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _flatten_all(self, *, reason: str, now_utc: datetime) -> None:
        """§7 flatten path: cancel everything first (a resting stop must
        not fill concurrently with the flatten), then market-flatten."""
        await self._preempt_decision_dispatch(reason=reason)
        with contextlib.suppress(Exception):
            await self._adapter.cancel_all_orders()
        for asset in ASSETS:
            if self.state[asset].contracts != 0:
                await self._flatten_position(
                    asset, reason=reason, now_utc=now_utc, cancel_stop=False
                )

    async def _flatten_position(
        self,
        asset: str,
        *,
        reason: str,
        now_utc: datetime,
        lockout: bool = False,
        contracts: int | None = None,
        cancel_stop: bool = True,
    ) -> None:
        """Market-flatten (fully or partially) with full DB propagation.

        Protective exits deliberately bypass the dispatch halt gate —
        HALT_NEW halts NEW exposure, not risk-reducing exits — and
        PREEMPT any in-flight decision dispatch (risk-review F6).
        """
        rt = self.state[asset]
        product = self.products.get(asset)
        if rt.contracts == 0 or product is None:
            return
        if self.account_id is None:
            return
        await self._preempt_decision_dispatch(reason=reason)
        flatten_contracts = (
            abs(rt.contracts) if contracts is None else min(contracts, abs(rt.contracts))
        )
        if flatten_contracts <= 0:
            return
        signed = flatten_contracts * (1 if rt.contracts > 0 else -1)
        full_close = flatten_contracts == abs(rt.contracts)

        async with self._trade_lock:
            if cancel_stop:
                with contextlib.suppress(Exception):
                    await self._adapter.cancel_all_orders(product.product_id)
            # Persist the flatten counter BEFORE placing (deterministic
            # client_order_id survives a crash-restart — gate A3).
            self._flatten_seq += 1
            seq = self._flatten_seq
            await self._persist_status(now_utc=now_utc, tick_increment=False)

            head = await self._store.fetch_parameter_head()
            param_hash = head[0] if head is not None else "0" * 64
            slippage_id = await self._store.fetch_slippage_head_id()
            mark = self.marks.latest_price(product.product_id) or Decimal(0)

            signal_id: UUID | None = None
            if slippage_id is not None:
                signal_id = await self._store.insert_signal_row(
                    self.account_id,
                    market=asset,
                    contract_id=await self._store.ensure_contract_row(asset=asset, product=product),
                    now_utc=now_utc,
                    session_date=now_utc.astimezone(UTC).date(),
                    direction="flat",
                    signal_type="exit",
                    decision_price=mark,
                    target_contracts=flatten_contracts,
                    sizing_trace={"reason": reason, "source": "risk_loop"},
                    strategy_hash=_strategy_hash(),
                    parameter_set_hash=param_hash,
                    slippage_calibration_version_id=slippage_id,
                    rationale={"reason": reason},
                )
            else:
                self._log.error(
                    "strategy_worker_flatten_signal_row_skipped_no_slippage_head",
                    asset=asset,
                    reason=reason,
                )

            result = await self._adapter.execute_market_flatten(
                product=product,
                position_contracts=Decimal(signed),
                decision_date=now_utc.astimezone(UTC).date(),
                decision_seq=seq,
            )
            if signal_id is not None:
                await self._propagate_leg_result(
                    asset=asset,
                    product=product,
                    signal_id=signal_id,
                    leg_delta=-signed,
                    stage_results=[result],
                    order_type="market_emergency",
                    now_utc=now_utc,
                    full_close=full_close,
                )

            filled = int(result.filled_contracts)
            if filled < flatten_contracts:
                self._log.error(
                    "strategy_worker_flatten_partial",
                    asset=asset,
                    requested=flatten_contracts,
                    filled=filled,
                    reason=reason,
                )
            rt.contracts = rt.contracts - filled * (1 if rt.contracts > 0 else -1)
            if lockout and reason == "client_stop":
                # lockout applies to the stopped direction irrespective of
                # how much actually filled. The `+ 2` intentionally mirrors
                # the parity-locked engine's `lockout_days` (LOCKOUT_DAYS=2
                # in the Amendment B seed) — a parameter amendment that
                # changes LOCKOUT_DAYS must touch this literal too (and
                # `_apply_stop_lockout` below).
                stopped_dir = 1 if signed > 0 else -1
                bar_day = now_utc.astimezone(UTC).date()
                rt.lockout_dir = stopped_dir
                rt.lockout_until_ord = bar_day.toordinal() + 2
                rt.stopped_on_date = bar_day.isoformat()
            if rt.contracts == 0:
                self._clear_position_state(rt)
            else:
                await self._arm_native_stop(asset, reason=f"{reason}_resize")
            await self._persist_status(now_utc=now_utc, tick_increment=False)

        # Alert-after-action (risk-review F1a): a protective flatten must
        # reach the operator's alert surface. daily_loss / halt_floor
        # flattens are covered by the kill_switch_invoked alert their FSM
        # transition emits — no per-position duplicate for those.
        alert_spec = _FLATTEN_ALERTS.get(reason)
        if alert_spec is not None:
            severity, category = alert_spec
            await self._store.insert_alert(
                self.account_id,
                severity=severity,
                category=category,
                message=(
                    f"strategy worker protective flatten: {asset} "
                    f"{flatten_contracts} contract(s), reason={reason}"
                ),
                detail={
                    "asset": asset,
                    "product_id": product.product_id,
                    "reason": reason,
                    "requested_contracts": flatten_contracts,
                    "filled_contracts": filled,
                    "remaining_contracts": rt.contracts,
                    "avg_fill_price": str(result.avg_fill_price)
                    if result.avg_fill_price is not None
                    else None,
                    "source": "strategy_worker_risk_loop",
                },
                now_utc=now_utc,
            )

    # -- daily decision ----------------------------------------------------------

    async def _run_decision_guarded(self, decision_date: date) -> None:
        try:
            await self.run_daily_decision(decision_date)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception(
                "strategy_worker_decision_failed", decision_date=decision_date.isoformat()
            )
            if self.account_id is not None:
                with contextlib.suppress(Exception):
                    await self._store.update_decision(
                        self.account_id,
                        decision_date=decision_date,
                        now_utc=self._clock(),
                        status="failed",
                        outcome={"error": "unhandled exception; see logs"},
                        engine_state=serialize_engine_state(self.state),
                    )

    async def run_daily_decision(self, decision_date: date) -> None:
        """The 00:05 UTC decision (strategy §5 cadence; delta spec §3.3)."""
        if self.account_id is None:
            return
        now = self._clock()
        existing = await self._store.fetch_decision(self.account_id, decision_date)
        if existing is not None and existing.status != "dispatching":
            self._log.info(
                "strategy_worker_decision_already_done",
                decision_date=decision_date.isoformat(),
                status=existing.status,
            )
            self._last_decision_date = decision_date
            return
        if existing is not None:
            # Crash-recovery resume: the engine state stored with the row is
            # POST-compute — re-running compute_targets would double-advance
            # the hysteresis counters for the same bar. Restore + dispatch
            # only the still-pending legs (deterministic client_order_ids
            # make re-execution idempotent — gate A3).
            await self._resume_decision(existing, decision_date)
            return

        # Halt gate — EVERY dispatch path checks the risk state first.
        risk_snapshot = await self._store.fetch_risk_state(self.account_id)
        risk_state = risk_snapshot.state if risk_snapshot is not None else None
        if risk_state is not None and risk_state not in RISK_STATES_PERMITTING_DISPATCH:
            self._log.warning(
                "strategy_worker_decision_skipped_risk_state",
                decision_date=decision_date.isoformat(),
                risk_state=risk_state,
            )
            await self._store.insert_decision(
                self.account_id,
                decision_date=decision_date,
                now_utc=now,
                status="skipped_risk_state",
                equity_usd=None,
                equity_for_sizing_usd=None,
                v_target=None,
                m_combined_value=None,
                weekly_halved=False,
                parameter_set_hash=None,
                outcome={
                    "schema_version": DECISION_SCHEMA_VERSION,
                    "skip_reason": f"risk_state={risk_state} does not permit dispatch",
                },
                engine_state=serialize_engine_state(self.state),
            )
            self._last_decision_date = decision_date
            return

        inputs = await self._gather_decision_inputs(decision_date, now_utc=now)
        if inputs is None:
            # Transient input failure (bars/balance/params): do NOT mark the
            # date done — the next 30 s tick retries. A terminal skip
            # (skipped_stale_bars past the grace deadline) wrote its own
            # decision row, which short-circuits the retry above.
            return

        is_convalescent = risk_state == RiskState.CONVALESCENT.value
        last_capital_event_day = await self._store.fetch_last_threshold_met_capital_event_date(
            self.account_id
        )
        capital_event_sessions = capital_event_session_count(
            decision_date=decision_date,
            last_threshold_met_event_date_utc=last_capital_event_day,
        )
        if 1 <= capital_event_sessions <= CAPITAL_EVENT_MODE_DURATION_SESSIONS:
            self._log.info(
                "strategy_worker_capital_event_window_active",
                capital_event_session_count=capital_event_sessions,
                capital_event_date=last_capital_event_day.isoformat()
                if last_capital_event_day is not None
                else None,
                half_size=capital_event_sessions <= CAPITAL_EVENT_VOL_NORMALIZED_AFTER_SESSIONS,
            )
        elif risk_snapshot is not None and risk_snapshot.capital_event_active:
            if capital_event_sessions == 0 and last_capital_event_day is not None:
                # Event recorded earlier TODAY (a mid-day catch-up decision
                # can land here): the event day is session 0 — the half-size
                # window starts with tomorrow's decision.
                self._log.info(
                    "strategy_worker_capital_event_window_pending",
                    capital_event_date=last_capital_event_day.isoformat(),
                )
            else:
                # risk_state still carries the Phase-0 absolute-counter window
                # fields from a lapsed (or pre-crypto) event — nothing clears
                # them. The date-derived count is authoritative for sizing.
                self._log.info(
                    "strategy_worker_capital_event_window_lapsed",
                    capital_event_session_count=capital_event_sessions,
                    capital_event_date=last_capital_event_day.isoformat()
                    if last_capital_event_day is not None
                    else None,
                )
        monthly_dd = await self._monthly_dd(inputs.equity, decision_date)
        m_comb = compute_m_combined(capital_event_sessions, is_convalescent, monthly_dd)

        weekly_halved = (
            self._weekly_halved_until is not None and decision_date <= self._weekly_halved_until
        )
        v_target_eff = inputs.params.v_target * (0.5 if weekly_halved else 1.0)

        decided_bar_date = decision_date - timedelta(days=1)
        engine_states = {
            a: self.state[a].to_engine_state(decided_bar_date=decided_bar_date) for a in ASSETS
        }
        targets = compute_targets(
            bar_index=decided_bar_date.toordinal(),
            rows=inputs.rows,
            states=engine_states,
            equity=float(inputs.equity_for_sizing),
            v_target=v_target_eff,
            dd_mult=1.0,  # Amendment B: dd tiers removed (DD_TIERS_REMOVED)
            p=inputs.params,
        )
        for asset in ASSETS:
            self.state[asset].absorb_engine_state(engine_states[asset])

        # Independent Decimal re-check (§3.4 division of labor).
        engine_targets = {a: int(targets[a]) for a in ASSETS}
        try:
            sizing = run_sizing_pipeline(
                SizingInputs(
                    equity_usd=inputs.equity_for_sizing,
                    m_combined=m_comb,
                    engine_targets=engine_targets,
                    marks=inputs.marks,
                    specs={
                        a: CryptoProductSpec(
                            asset=a,
                            product_id=p.product_id,
                            contract_size=p.contract_size,
                        )
                        for a, p in inputs.products.items()
                    },
                    per_asset_cap_frac=_dec(inputs.canonical.get("PER_ASSET_CAP"))
                    or Decimal("1.4"),
                    gross_cap_frac=_dec(inputs.canonical.get("GROSS_CAP")) or Decimal("3.0"),
                    parameter_set_hash=inputs.parameter_set_hash,
                )
            )
        except SizingError:
            self._log.exception("strategy_worker_sizing_failed_no_trade")
            await self._store.insert_decision(
                self.account_id,
                decision_date=decision_date,
                now_utc=now,
                status="failed",
                equity_usd=inputs.equity,
                equity_for_sizing_usd=inputs.equity_for_sizing,
                v_target=Decimal(str(v_target_eff)),
                m_combined_value=m_comb,
                weekly_halved=weekly_halved,
                parameter_set_hash=inputs.parameter_set_hash,
                outcome={
                    "schema_version": DECISION_SCHEMA_VERSION,
                    "skip_reason": "sizing pipeline rejected inputs (see logs)",
                    "engine_targets": engine_targets,
                },
                engine_state=serialize_engine_state(self.state),
            )
            self._last_decision_date = decision_date
            return

        # Phase-A small-live clamps (strategy §10; config — not engine math).
        final_targets: dict[str, int] = {}
        for asset in ASSETS:
            target = sizing.target_contracts.get(asset, 0)
            cap = self._config.max_contracts.get(asset)
            if cap is not None and abs(target) > cap:
                clamped = cap if target > 0 else -cap
                self._log.warning(
                    "strategy_worker_phase_a_contract_clamp",
                    asset=asset,
                    sized_target=target,
                    clamped_target=clamped,
                    max_contracts=cap,
                )
                target = clamped
            final_targets[asset] = target

        # Dead-band + Amendment B band-edge on the FINAL targets.
        outcome_assets: dict[str, Any] = {}
        for asset in ASSETS:
            rt = self.state[asset]
            row = inputs.rows[asset]
            product = inputs.products.get(asset)
            delta = 0.0
            if product is not None and not math.isnan(row.close):
                delta = plan_delta(
                    target=float(final_targets[asset]),
                    current_contracts=float(rt.contracts),
                    close=row.close,
                    mult=float(product.contract_size),
                    equity=float(inputs.equity_for_sizing),
                    p=inputs.params,
                )
            effective_target = rt.contracts + int(delta)
            legs = split_delta_legs(prior=rt.contracts, target=effective_target)
            outcome_assets[asset] = {
                "row": {
                    "trend": None if math.isnan(row.trend) else round(row.trend, 6),
                    "sigma_ann": None if math.isnan(row.sigma_ann) else round(row.sigma_ann, 6),
                    "vol_ratio": None if math.isnan(row.vol_ratio) else round(row.vol_ratio, 6),
                    "atrp": None if math.isnan(row.atrp) else round(row.atrp, 6),
                    "close": None if math.isnan(row.close) else row.close,
                },
                "engine_target": engine_targets[asset],
                "sized_target": sizing.target_contracts.get(asset, 0),
                "final_target": effective_target,
                "prior_contracts": rt.contracts,
                "planned_delta": int(delta),
                "action": (
                    "hold" if not legs else ("buy" if effective_target > rt.contracts else "sell")
                ),
                "legs": [
                    {"seq": i, "kind": kind, "delta": leg_delta, "status": "pending"}
                    for i, (leg_delta, kind) in enumerate(legs)
                ],
                "est_cost_usd": None,
                "mark": str(inputs.marks[asset]) if asset in inputs.marks else None,
            }

        outcome: dict[str, Any] = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "decided_bar_date": decided_bar_date.isoformat(),
            "assets": outcome_assets,
            "sizing_trace": sizing.sizing_trace,
            "weekly_halved": weekly_halved,
            "m_combined": str(m_comb),
            "capital_event_session_count": capital_event_sessions,
        }

        await self._store.insert_decision(
            self.account_id,
            decision_date=decision_date,
            now_utc=now,
            status="dispatching",
            equity_usd=inputs.equity,
            equity_for_sizing_usd=inputs.equity_for_sizing,
            v_target=Decimal(str(v_target_eff)),
            m_combined_value=m_comb,
            weekly_halved=weekly_halved,
            parameter_set_hash=inputs.parameter_set_hash,
            outcome=outcome,
            engine_state=serialize_engine_state(self.state),
        )

        await self._dispatch_decision(
            decision_date=decision_date,
            inputs=inputs,
            outcome=outcome,
        )
        self._last_decision_date = decision_date
        await self._persist_status(now_utc=self._clock(), tick_increment=False)

    async def _resume_decision(self, existing: DecisionRow, decision_date: date) -> None:
        """Resume a decision that crashed mid-dispatch (status='dispatching')."""
        assert self.account_id is not None
        now = self._clock()
        self._log.warning(
            "strategy_worker_decision_resuming",
            decision_date=decision_date.isoformat(),
        )
        if existing.engine_state:
            self.state = deserialize_engine_state(existing.engine_state)

        snapshot = await self._store.fetch_risk_state(self.account_id)
        if snapshot is not None and snapshot.state not in RISK_STATES_PERMITTING_DISPATCH:
            self._log.warning(
                "strategy_worker_resume_blocked_risk_state",
                decision_date=decision_date.isoformat(),
                risk_state=snapshot.state,
            )
            await self._store.update_decision(
                self.account_id,
                decision_date=decision_date,
                now_utc=now,
                status="skipped_risk_state",
                outcome=existing.outcome,
                engine_state=serialize_engine_state(self.state),
            )
            self._last_decision_date = decision_date
            return

        inputs = await self._gather_decision_inputs(
            decision_date, now_utc=now, allow_stale_bars=True
        )
        if inputs is None:
            return  # transient; retried next tick
        await self._dispatch_decision(
            decision_date=decision_date,
            inputs=inputs,
            outcome=existing.outcome,
        )
        self._last_decision_date = decision_date
        await self._persist_status(now_utc=self._clock(), tick_increment=False)

    async def _dispatch_decision(
        self,
        *,
        decision_date: date,
        inputs: _DecisionInputs,
        outcome: dict[str, Any],
    ) -> None:
        assert self.account_id is not None
        assets_outcome = outcome.get("assets")
        if not isinstance(assets_outcome, dict):
            assets_outcome = {}
        for asset in ASSETS:
            rt = self.state[asset]
            product = inputs.products.get(asset)
            entry_raw = assets_outcome.get(asset)
            if not isinstance(entry_raw, dict):
                continue
            entry: dict[str, Any] = entry_raw
            legs_raw = entry.get("legs")
            legs: list[dict[str, Any]] = legs_raw if isinstance(legs_raw, list) else []
            if not legs or product is None:
                continue
            total_fees = Decimal(0)
            for leg in legs:
                if leg.get("status") != "pending":
                    continue  # resume path: leg already executed / skipped pre-crash
                # Halt gate re-check per dispatch (state can change mid-decision).
                snapshot = await self._store.fetch_risk_state(self.account_id)
                if snapshot is not None and snapshot.state not in RISK_STATES_PERMITTING_DISPATCH:
                    self._log.warning(
                        "strategy_worker_leg_skipped_risk_state",
                        asset=asset,
                        risk_state=snapshot.state,
                        leg=leg,
                    )
                    leg["status"] = "skipped_risk_state"
                    continue
                async with self._trade_lock:
                    await self._execute_leg(
                        asset=asset,
                        product=product,
                        leg=leg,
                        inputs=inputs,
                        decision_date=decision_date,
                        now_utc=self._clock(),
                    )
                fees = leg.get("fees_usd")
                if fees:
                    total_fees += Decimal(str(fees))
            entry["est_cost_usd"] = str(total_fees)
            entry["final_contracts"] = rt.contracts

            # Stop management after the asset's legs complete.
            if rt.contracts != 0:
                await self._arm_native_stop(
                    asset,
                    reason="decision",
                    atrp_today=inputs.rows[asset].atrp,
                )
                entry["stop"] = {
                    "client_stop_level": rt.client_stop_level,
                    "native_stop_order_id": rt.native_stop_order_id,
                }
            elif product is not None:
                with contextlib.suppress(Exception):
                    await self._adapter.cancel_all_orders(product.product_id)

            await self._store.update_decision(
                self.account_id,
                decision_date=decision_date,
                now_utc=self._clock(),
                status="dispatching",
                outcome=outcome,
                engine_state=serialize_engine_state(self.state),
            )

        await self._store.update_decision(
            self.account_id,
            decision_date=decision_date,
            now_utc=self._clock(),
            status="completed",
            outcome=outcome,
            engine_state=serialize_engine_state(self.state),
        )
        self._log.info(
            "strategy_worker_decision_completed",
            decision_date=decision_date.isoformat(),
            summary={
                a: {
                    "target": (assets_outcome.get(a) or {}).get("final_target"),
                    "action": (assets_outcome.get(a) or {}).get("action"),
                }
                for a in ASSETS
            },
        )

    async def _execute_leg(
        self,
        *,
        asset: str,
        product: PerpProductRef,
        leg: dict[str, Any],
        inputs: _DecisionInputs,
        decision_date: date,
        now_utc: datetime,
    ) -> None:
        assert self.account_id is not None
        rt = self.state[asset]
        leg_delta = int(leg["delta"])
        seq = int(leg["seq"])
        kind = str(leg["kind"])
        direction: Literal["long", "short", "flat"]
        signal_type: Literal["entry", "exit"]
        if kind in ("close", "reduce"):
            direction, signal_type = "flat", "exit"
        else:
            direction = "long" if leg_delta > 0 else "short"
            signal_type = "entry"

        contract_id = await self._store.ensure_contract_row(asset=asset, product=product)
        mark = inputs.marks.get(asset) or Decimal(0)
        signal_id = await self._store.insert_signal_row(
            self.account_id,
            market=asset,
            contract_id=contract_id,
            now_utc=now_utc,
            session_date=decision_date,
            direction=direction,
            signal_type=signal_type,
            decision_price=mark,
            target_contracts=abs(leg_delta),
            sizing_trace={"leg": dict(leg), "source": "daily_decision"},
            strategy_hash=_strategy_hash(),
            parameter_set_hash=inputs.parameter_set_hash,
            slippage_calibration_version_id=inputs.slippage_head_id,
            rationale={
                "kind": kind,
                "decided_bar_date": (decision_date - timedelta(days=1)).isoformat(),
            },
        )
        leg["signal_id"] = str(signal_id)

        result = await self._adapter.execute_target_delta(
            product=product,
            delta_contracts=Decimal(leg_delta),
            decision_date=decision_date,
            decision_seq=seq,
        )
        prior_contracts = rt.contracts
        full_close = kind == "close" and result.fully_filled
        await self._propagate_leg_result(
            asset=asset,
            product=product,
            signal_id=signal_id,
            leg_delta=leg_delta,
            stage_results=list(result.stages),
            order_type="limit_marketable",
            now_utc=now_utc,
            full_close=full_close,
        )

        filled_signed = int(result.filled_contracts) * (1 if leg_delta > 0 else -1)
        new_contracts = prior_contracts + filled_signed
        self._update_entry_ref_and_stop(
            rt,
            prior_contracts=prior_contracts,
            new_contracts=new_contracts,
            fill_price=result.avg_fill_price or mark,
            atrp_today=inputs.rows[asset].atrp,
            params=inputs.params,
        )
        rt.contracts = new_contracts

        leg["filled"] = int(result.filled_contracts)
        leg["fees_usd"] = str(result.total_fees_usd)
        leg["avg_fill_price"] = (
            str(result.avg_fill_price) if result.avg_fill_price is not None else None
        )
        leg["stages"] = [
            {
                "stage": s.stage,
                "client_order_id": s.client_order_id,
                "order_id": s.order_id or None,
                "requested": str(s.requested_contracts),
                "filled": str(s.filled_contracts),
            }
            for s in result.stages
        ]
        if result.fully_filled:
            leg["status"] = "filled"
        elif result.filled_contracts > 0:
            leg["status"] = "partial"
        else:
            leg["status"] = "failed"

        if not result.fully_filled:
            # The market stage itself failed — halt-worthy, not retry-worthy
            # (adapter contract).
            self._log.error(
                "strategy_worker_ladder_incomplete_halting",
                asset=asset,
                requested=str(result.requested_contracts),
                filled=str(result.filled_contracts),
            )
            await self._store.invoke_kill_switch(
                self.account_id,
                trigger=TransitionTrigger.UNHANDLED_EXCEPTION,
                now_utc=now_utc,
            )
            await self._store.insert_alert(
                self.account_id,
                severity="P1",
                category="kill_switch_invoked",
                message=(
                    f"execution ladder incomplete on {asset} (venue failed the "
                    "market stage) — HALT_NEW (unhandled_exception)"
                ),
                detail={
                    "trigger": TransitionTrigger.UNHANDLED_EXCEPTION.value,
                    "asset": asset,
                    "product_id": product.product_id,
                    "requested_contracts": str(result.requested_contracts),
                    "filled_contracts": str(result.filled_contracts),
                    "source": "strategy_worker_decision_dispatch",
                },
                now_utc=now_utc,
            )

    def _update_entry_ref_and_stop(
        self,
        rt: AssetRuntime,
        *,
        prior_contracts: int,
        new_contracts: int,
        fill_price: Decimal,
        atrp_today: float,
        params: CryptoTrendParams,
    ) -> None:
        """Mirror the engine's entry-VWAP + stop-reset rules on real fills.

        ``entry_ref`` = VWAP of the current position's fills (strategy §5);
        the 2xATR client stop resets only when the position opens, flips,
        or expands — reduces keep the prior stop basis.
        """
        if new_contracts == 0:
            rt.entry_vwap = None
            rt.client_stop_level = None
            return
        opened_or_flipped = prior_contracts == 0 or prior_contracts * new_contracts < 0
        expanded = abs(new_contracts) > abs(prior_contracts) and not opened_or_flipped
        if opened_or_flipped:
            rt.entry_vwap = str(fill_price)
        elif expanded and rt.entry_vwap is not None:
            add = abs(new_contracts - prior_contracts)
            vwap = (Decimal(rt.entry_vwap) * abs(prior_contracts) + fill_price * add) / abs(
                new_contracts
            )
            rt.entry_vwap = str(vwap)
        elif rt.entry_vwap is None:
            rt.entry_vwap = str(fill_price)
        if (opened_or_flipped or expanded) and not math.isnan(atrp_today):
            sign = 1 if new_contracts > 0 else -1
            entry_ref = Decimal(rt.entry_vwap)
            stop = entry_ref * (
                Decimal(1) - sign * Decimal(str(params.client_stop_atr)) * Decimal(str(atrp_today))
            )
            rt.client_stop_level = str(stop)
            rt.atrp_at_stop_set = str(Decimal(str(atrp_today)))

    async def _propagate_leg_result(
        self,
        *,
        asset: str,
        product: PerpProductRef,
        signal_id: UUID,
        leg_delta: int,
        stage_results: list[LadderStageResult],
        order_type: str,
        now_utc: datetime,
        full_close: bool,
    ) -> None:
        """Feed one leg's aggregate outcome into the kept downstream
        pipeline: orders row + ORDER_PLACED, then ORDER_FILLED (via the
        fill processor) or the terminal-status processor."""
        assert self.account_id is not None
        cid = stage_results[0].client_order_id
        existing_status = await self._store.order_row_exists(cid)
        if existing_status in ("filled", "cancelled", "rejected"):
            self._log.info(
                "strategy_worker_leg_already_propagated",
                client_order_id=cid,
                status=existing_status,
            )
            return

        total_filled = sum((s.filled_contracts for s in stage_results), Decimal(0))
        total_fees = sum((s.fees_usd for s in stage_results), Decimal(0))
        weighted = sum(
            ((s.avg_fill_price or Decimal(0)) * s.filled_contracts for s in stage_results),
            Decimal(0),
        )
        avg_price = weighted / total_filled if total_filled > 0 else None
        broker_order_id = next((s.order_id for s in stage_results if s.order_id), None)

        contract_id = await self._store.ensure_contract_row(asset=asset, product=product)
        head = await self._store.fetch_parameter_head()
        param_hash = head[0] if head is not None else "0" * 64
        if existing_status is None:
            await self._store.insert_order_row(
                self.account_id,
                signal_id=signal_id,
                client_order_id=cid,
                broker_order_id=broker_order_id,
                market=asset,
                contract_id=contract_id,
                direction="buy" if leg_delta > 0 else "sell",
                order_type=order_type,
                quantity=abs(leg_delta),
                limit_price=None,
                stop_price=None,
                now_utc=now_utc,
                strategy_hash=_strategy_hash(),
                parameter_set_hash=param_hash,
                audit_payload={
                    "product_id": product.product_id,
                    "stages": [
                        {
                            "stage": s.stage,
                            "client_order_id": s.client_order_id,
                            "order_id": s.order_id or None,
                            "filled": str(s.filled_contracts),
                            "fees_usd": str(s.fees_usd),
                        }
                        for s in stage_results
                    ],
                },
            )

        if total_filled > 0:
            payload = FillIngestPayload(
                broker_fill_id=f"{cid}:agg",
                cumulative_filled_quantity=int(total_filled),
                fill_quantity=int(total_filled),
                fill_price=avg_price or Decimal(0),
                commission_usd=total_fees,
                filled_at_utc=now_utc,
            )
            propagated = await self._store.process_fill(client_order_id=cid, payload=payload)
            if not propagated:
                await self._store.minimal_fill_fallback(
                    self.account_id,
                    client_order_id=cid,
                    payload=payload,
                    market=asset,
                    fully_filled=int(total_filled) >= abs(leg_delta),
                )
            new_signal_status = (
                "filled" if int(total_filled) >= abs(leg_delta) else "partially_filled"
            )
            await self._store.update_signal_status(signal_id, status=new_signal_status)
            if full_close:
                await self._store.update_signal_status(signal_id, status="closed")
        else:
            await self._store.process_terminal(
                client_order_id=cid,
                broker_order_id=broker_order_id or "",
                status_kind="cancelled",
                reason="ladder produced no fills",
                now_utc=now_utc,
            )
            await self._store.update_signal_status(signal_id, status="cancelled")

    async def _arm_native_stop(
        self,
        asset: str,
        *,
        reason: str,
        atrp_today: float | None = None,
    ) -> None:
        """Place/replace the §5 Layer-1 3xATR venue-resting backstop.

        Uses the atrp captured when the stop basis was set (so a
        size-only re-arm keeps the same trigger); falls back to today's
        atrp, then to a fresh indicator computation (startup re-arm on
        an empty state).
        """
        rt = self.state[asset]
        product = self.products.get(asset)
        if rt.contracts == 0 or product is None or self.account_id is None:
            return
        atrp: Decimal | None = None
        if rt.atrp_at_stop_set is not None:
            atrp = Decimal(rt.atrp_at_stop_set)
        elif atrp_today is not None and not math.isnan(atrp_today):
            atrp = Decimal(str(atrp_today))
        else:
            computed = await self._compute_current_atrp(asset)
            if computed is not None:
                atrp = Decimal(str(computed))
        entry_ref = Decimal(rt.entry_vwap) if rt.entry_vwap else None
        if entry_ref is None:
            entry_ref = self.marks.latest_price(product.product_id)
        if atrp is None or atrp <= 0 or entry_ref is None or entry_ref <= 0:
            self._log.error(
                "strategy_worker_native_stop_inputs_missing",
                asset=asset,
                reason=reason,
                atrp=str(atrp) if atrp is not None else None,
                entry_ref=str(entry_ref) if entry_ref is not None else None,
            )
            return
        rt.stop_rearm_version += 1
        now = self._clock()
        try:
            result = await self._adapter.ensure_native_stop(
                product=product,
                position_contracts=Decimal(rt.contracts),
                entry_ref=entry_ref,
                atrp=atrp,
                decision_date=now.astimezone(UTC).date(),
                decision_seq=0,
                version=rt.stop_rearm_version,
            )
        except Exception:  # incl. StopVerificationError (gate A2 contract)
            self._log.exception(
                "strategy_worker_native_stop_unverified",
                asset=asset,
                reason=reason,
                note=(
                    "position is UNPROTECTED per gate A2 — re-arm retries every "
                    "risk tick; the 2xATR client stop remains the interim layer "
                    "and the §7 outage policy flattens if marks also go stale"
                ),
            )
            rt.native_stop_order_id = None
            rt.native_stop_client_order_id = None
            return
        prior_stop_cid = rt.native_stop_client_order_id
        if (
            result.action == "replaced"
            and prior_stop_cid is not None
            and prior_stop_cid != result.client_order_id
        ):
            with contextlib.suppress(Exception):
                await self._store.process_terminal(
                    client_order_id=prior_stop_cid,
                    broker_order_id=rt.native_stop_order_id or "",
                    status_kind="cancelled",
                    reason="native stop replaced",
                    now_utc=now,
                )
        rt.native_stop_order_id = result.order_id
        rt.native_stop_client_order_id = result.client_order_id
        if rt.atrp_at_stop_set is None:
            rt.atrp_at_stop_set = str(atrp)
        if rt.client_stop_level is None and entry_ref is not None:
            sign = 1 if rt.contracts > 0 else -1
            # Decimal("2.0") intentionally mirrors the parity-locked
            # engine's `client_stop_atr` / the Amendment B seed's
            # CLIENT_STOP_ATR="2.0" (§5, locked). Only used in this
            # startup-rearm fallback where no CryptoTrendParams is in
            # scope; a parameter amendment changing CLIENT_STOP_ATR must
            # touch this literal too.
            client_atr = Decimal("2.0")
            rt.client_stop_level = str(entry_ref * (Decimal(1) - sign * client_atr * atrp))
        if result.action in ("placed", "replaced"):
            await self._record_stop_order_row(
                asset=asset,
                product=product,
                result_client_order_id=result.client_order_id,
                result_order_id=result.order_id,
                trigger_price=result.trigger_price,
                limit_price=result.limit_price,
                contracts=int(result.contracts),
                now_utc=now,
            )

    async def _record_stop_order_row(
        self,
        *,
        asset: str,
        product: PerpProductRef,
        result_client_order_id: str,
        result_order_id: str,
        trigger_price: Decimal,
        limit_price: Decimal,
        contracts: int,
        now_utc: datetime,
    ) -> None:
        """Orders row for the venue-resting stop so its eventual fill can
        propagate through the fill pipeline (same-signal Option-B shape:
        the stop reuses the asset's most recent signal via a fresh
        exit-signal row)."""
        assert self.account_id is not None
        if await self._store.order_row_exists(result_client_order_id) is not None:
            return
        slippage_id = await self._store.fetch_slippage_head_id()
        if slippage_id is None:
            self._log.error("strategy_worker_stop_order_row_skipped_no_slippage_head")
            return
        head = await self._store.fetch_parameter_head()
        param_hash = head[0] if head is not None else "0" * 64
        contract_id = await self._store.ensure_contract_row(asset=asset, product=product)
        rt = self.state[asset]
        signal_id = await self._store.insert_signal_row(
            self.account_id,
            market=asset,
            contract_id=contract_id,
            now_utc=now_utc,
            session_date=now_utc.astimezone(UTC).date(),
            direction="flat",
            signal_type="exit",
            decision_price=trigger_price,
            target_contracts=contracts,
            sizing_trace={"source": "native_stop", "trigger_price": str(trigger_price)},
            strategy_hash=_strategy_hash(),
            parameter_set_hash=param_hash,
            slippage_calibration_version_id=slippage_id,
            rationale={"kind": "native_stop_backstop", "atr_mult": "3.0"},
        )
        await self._store.insert_order_row(
            self.account_id,
            signal_id=signal_id,
            client_order_id=result_client_order_id,
            broker_order_id=result_order_id,
            market=asset,
            contract_id=contract_id,
            direction="sell" if rt.contracts > 0 else "buy",
            order_type="stop_market",
            quantity=contracts,
            limit_price=limit_price,
            stop_price=trigger_price,
            now_utc=now_utc,
            strategy_hash=_strategy_hash(),
            parameter_set_hash=param_hash,
            audit_payload={"product_id": product.product_id, "purpose": "native_stop"},
        )

    # -- decision inputs ---------------------------------------------------------

    async def _gather_decision_inputs(
        self,
        decision_date: date,
        *,
        now_utc: datetime,
        allow_stale_bars: bool = False,
    ) -> _DecisionInputs | None:
        """Gather everything the decision needs; None = retry next tick.

        ``allow_stale_bars=True`` (crash-recovery resume) accepts the
        latest available bar's indicators — the resume path never
        re-computes targets, it only needs atrp/marks for stop arming.
        """
        assert self.account_id is not None
        head = await self._store.fetch_parameter_head()
        if head is None:
            self._log.error(
                "strategy_worker_no_parameter_head",
                note="run bootstrap_live_account --mint-from-defaults first",
            )
            return None
        parameter_set_hash, canonical = head
        params = params_from_canonical(canonical)

        slippage_head_id = await self._store.fetch_slippage_head_id()
        if slippage_head_id is None:
            self._log.error(
                "strategy_worker_no_slippage_head_fail_closed",
                note=(
                    "signals rows require a slippage_calibration_versions head; "
                    "seed the bootstrap calibration before first dispatch "
                    "(deploy/strategy_worker/README.md step 2)"
                ),
            )
            return None

        try:
            summary = await self._broker.get_futures_balance_summary()
        except Exception:
            self._log.exception("strategy_worker_decision_balance_fetch_failed")
            return None
        equity = equity_from_summary(summary)
        if equity is None or equity <= 0:
            self._log.error("strategy_worker_decision_equity_unavailable")
            return None
        cap = self._config.e_effective_cap_usd
        equity_for_sizing = min(equity, cap) if cap is not None else equity

        await self._refresh_products()
        products = dict(self.products)

        decided_bar_date = decision_date - timedelta(days=1)
        rows: dict[str, AssetDecisionRow] = {}
        for asset in ASSETS:
            spot = ASSET_TO_SPOT_PRODUCT[asset]
            try:
                bars = await fetch_daily_bars(
                    self._market_rest,
                    spot,
                    days=self._config.bars_history_days,
                    now_utc=now_utc,
                )
            except Exception:
                self._log.exception(
                    "strategy_worker_bars_fetch_failed", asset=asset, product_id=spot
                )
                bars = []
            row = _decision_row_from_bars(
                bars, params, decided_bar_date, allow_stale=allow_stale_bars
            )
            if row is None:
                self._log.warning(
                    "strategy_worker_decision_bars_not_ready",
                    asset=asset,
                    decided_bar_date=decided_bar_date.isoformat(),
                    bars=len(bars),
                )
                # Grace window: venue candles can lag a few minutes past
                # 00:05; retry each tick until the deadline, THEN record a
                # terminal skip (never trade on a stale bar — §7 posture,
                # and never silently miss a decision day — gate A4).
                deadline = datetime.combine(decision_date, time(hour=1, minute=0, tzinfo=UTC))
                if now_utc.astimezone(UTC) < deadline:
                    return None
                await self._store.insert_decision(
                    self.account_id,
                    decision_date=decision_date,
                    now_utc=now_utc,
                    status="skipped_stale_bars",
                    equity_usd=equity,
                    equity_for_sizing_usd=equity_for_sizing,
                    v_target=None,
                    m_combined_value=None,
                    weekly_halved=False,
                    parameter_set_hash=parameter_set_hash,
                    outcome={
                        "schema_version": DECISION_SCHEMA_VERSION,
                        "skip_reason": (
                            f"no completed daily bar for {asset} at "
                            f"{decided_bar_date.isoformat()} by 01:00 UTC — "
                            "refusing to trade on stale signal input "
                            "(conservative §7 posture)"
                        ),
                    },
                    engine_state=serialize_engine_state(self.state),
                )
                return None
            rows[asset] = row

        marks: dict[str, Decimal] = {}
        for asset, product in products.items():
            price = self.marks.latest_price(product.product_id)
            if price is None:
                with contextlib.suppress(Exception):
                    book = await self._broker.get_best_bid_ask(product.product_id)
                    price = book.mid
            if price is not None:
                marks[asset] = price

        return _DecisionInputs(
            params=params,
            parameter_set_hash=parameter_set_hash,
            canonical=canonical,
            equity=equity,
            equity_for_sizing=equity_for_sizing,
            products=products,
            rows=rows,
            marks=marks,
            slippage_head_id=slippage_head_id,
        )

    async def _monthly_dd(self, equity: Decimal, decision_date: date) -> Decimal:
        assert self.account_id is not None
        month_start = decision_date.replace(day=1)
        month_max = await self._store.fetch_month_max_equity(self.account_id, month_start)
        if month_max is None or month_max <= 0 or equity >= month_max:
            return Decimal(0)
        return equity / month_max - 1

    async def _canonical_params(self) -> dict[str, str]:
        head = await self._store.fetch_parameter_head()
        return head[1] if head is not None else {}

    async def _compute_current_atrp(self, asset: str) -> float | None:
        """Fresh ATRp from spot bars (startup stop re-arm fallback)."""
        try:
            bars = await fetch_daily_bars(
                self._market_rest,
                ASSET_TO_SPOT_PRODUCT[asset],
                days=60,
                now_utc=self._clock(),
            )
        except Exception:
            self._log.exception("strategy_worker_atrp_bars_fetch_failed", asset=asset)
            return None
        if len(bars) < 20:
            return None
        frame = _bars_to_frame(bars)
        indicators = compute_indicators(frame, CryptoTrendParams())
        atrp = float(indicators["atrp"].iloc[-1])
        return None if math.isnan(atrp) else atrp

    async def _refresh_products(self) -> None:
        """Runtime product discovery ([A13]: never hardcode CDE IDs)."""
        try:
            refs = await self._broker.list_perp_products()
        except Exception:
            self._log.exception("strategy_worker_product_discovery_failed")
            return
        self.products = select_perp_products(refs, assets=ASSETS)
        subscribe = tuple(ASSET_TO_SPOT_PRODUCT.values()) + tuple(
            p.product_id for p in self.products.values()
        )
        self.marks.set_products(subscribe)

    def _asset_for_product_id(self, product_id: str) -> str | None:
        for asset, product in self.products.items():
            if product.product_id == product_id:
                return asset
        return None


def _strategy_hash() -> str:
    """CHAR(40) strategy identity for signals/orders rows.

    The crypto engine's identity is the parity-locked module + Amendment
    B profile; there is no strategy_versions registry row yet (it died
    with the V1 stack). A stable, grep-able constant keeps the column
    truthful until a registry successor lands.
    """
    return "crypto-trend-amendment-b-0000000000000000"[:40].ljust(40, "0")


def _bars_to_frame(bars: list[DailyBar]) -> Any:
    """DailyBar list → the engine's expected OHLC frame (floats)."""
    import pandas as pd

    return pd.DataFrame(
        {
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
        },
        index=pd.DatetimeIndex([pd.Timestamp(b.session_date) for b in bars]),
    )


def _decision_row_from_bars(
    bars: list[DailyBar],
    params: CryptoTrendParams,
    decided_bar_date: date,
    *,
    allow_stale: bool = False,
) -> AssetDecisionRow | None:
    """Indicator row for the decided bar; None when the bar is missing
    (venue candle lag) — the caller refuses to trade on stale input.
    ``allow_stale`` accepts the latest bar (resume path: atrp only)."""
    if not bars:
        return None
    if bars[-1].session_date != decided_bar_date and not allow_stale:
        return None
    frame = _bars_to_frame(bars)
    indicators = compute_indicators(frame, params)
    last = indicators.iloc[-1]
    return AssetDecisionRow(
        trend=float(last["trend"]),
        vol_ratio=float(last["vol_ratio"]),
        sigma_ann=float(last["sigma_ann"]),
        atrp=float(last["atrp"]),
        close=float(last["close"]),
    )


__all__ = [
    "ASSET_TO_SPOT_PRODUCT",
    "DECISION_SCHEMA_VERSION",
    "DECISION_TIME_UTC",
    "DEFAULT_BARS_HISTORY_DAYS",
    "DEFAULT_STALE_THRESHOLD_S",
    "RISK_TICK_FAILURE_HALT_THRESHOLD",
    "AssetRuntime",
    "DecisionRow",
    "MarksFeed",
    "RiskStateSnapshot",
    "StrategyWorker",
    "StrategyWorkerConfig",
    "StrategyWorkerStore",
    "WorkerStatusRow",
    "decision_due",
    "deserialize_engine_state",
    "equity_from_summary",
    "normalize_liq_buffer",
    "params_from_canonical",
    "serialize_engine_state",
    "split_delta_legs",
]
