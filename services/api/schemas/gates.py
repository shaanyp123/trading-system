"""services/api/schemas/gates.py — response models for ``GET /api/system/gates``.

Strategy §10 acceptance-gate tracker: pure classification of the raw
projections from ``services/api/repos/gates.py`` into per-gate views.
All thresholds live here as module constants, verbatim from
``Docs/crypto-perps-strategy.md`` §10 (Amendment B leaves §10 gates
unchanged). All numeric fields are Decimal-as-string per [A05].

Honesty contract (mirrors the repo docstring): a green here is the
*mechanical* component of each gate. A1's ±$1/±2% statement match and
A3's manual restart drills are operator-run evidence — the payload
carries an ``evidence_note`` per gate so the digest/API/Discord render
never overstates what was verified.

Status vocabulary: ``green`` (mechanical criterion met), ``red`` (data
present, criterion not met), ``insufficient_data`` (not enough
observations to judge). No new enum migrations — these are wire-payload
strings only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.api.repos.gates import (
    DecisionDayRow,
    FundingSourceStat,
    GateFillRow,
    StopOrderRow,
    TradeOpenRow,
)

# --- §10 thresholds (verbatim) ---------------------------------------------

A1_TARGET_FILLS: Final[int] = 15
A2_TARGET_OPENS: Final[int] = 10
A2_STOP_WINDOW_S: Final[int] = 10
A3_TARGET_RESTARTS: Final[int] = 2
A4_TARGET_CLEAN_DAYS: Final[int] = 30
B1_MIN_FILLS: Final[int] = 20
B1_TRAILING_WINDOW: Final[int] = 20
B1_MEDIAN_MAX_BPS: Final[Decimal] = Decimal("5")
B1_P90_MAX_BPS: Final[Decimal] = Decimal("12")
B2_MAX_FEE_BPS: Final[Decimal] = Decimal("6")
B3_MAX_RATIO: Final[Decimal] = Decimal("2")
PHASE_A_MIN_DAYS: Final[int] = 45

#: The venue funding source written by services/data/coinbase_market_data.py.
CDE_FUNDING_SOURCE: Final[str] = "coinbase_advanced"

_BPS_Q: Final[Decimal] = Decimal("0.0001")

GateStatus = Literal["green", "red", "insufficient_data"]


class GateView(BaseModel):
    """One §10 gate: value vs target + honest evidence framing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    title: str
    status: GateStatus
    value: str = Field(description="Human-readable current value.")
    target: str = Field(description="Human-readable §10 criterion.")
    evidence_note: str | None = Field(
        default=None,
        description="What this computation can and cannot prove.",
    )


class SystemGatesResponse(BaseModel):
    """``GET /api/system/gates`` envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gates: list[GateView]
    days_live: int | None = Field(
        description="Days since the first recorded daily decision (None on a fresh DB)."
    )
    phase_a_min_days: int
    all_mechanical_green: bool
    scale_up_eligible: bool = Field(
        description=(
            "All mechanical gates green AND >= 45 days live. Operator-attested "
            "evidence (A1 statement match, A3 manual drills) still applies."
        )
    )
    server_now: datetime


# --- pure helpers -----------------------------------------------------------


def _fmt_bps(value: Decimal) -> str:
    return f"{value.quantize(_BPS_Q, rounding=ROUND_HALF_EVEN).normalize():f} bps"


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _p90(values: list[Decimal]) -> Decimal:
    """Nearest-rank 90th percentile (ceil(0.9 n), 1-indexed)."""
    ordered = sorted(values)
    n = len(ordered)
    rank = -(-9 * n // 10)  # ceil(0.9 * n) in integer math
    return ordered[max(rank - 1, 0)]


def fill_slippage_bps(fill: GateFillRow) -> Decimal | None:
    """Per-side realized slippage vs the signal's decision price.

    Calibration sign convention (services/risk/attribution.py): positive
    = paid worse than the decision mark (buy above / sell below). None
    when the linked signal or its decision price is unusable.
    """
    if fill.decision_price is None:
        return None
    if not fill.decision_price.is_finite() or not fill.fill_price.is_finite():
        return None
    if fill.decision_price <= 0:
        return None
    raw = ((fill.fill_price - fill.decision_price) / fill.decision_price) * Decimal(10000)
    if fill.direction == "sell":
        raw = -raw
    return raw.quantize(_BPS_Q, rounding=ROUND_HALF_EVEN)


def _fill_complete(fill: GateFillRow) -> bool:
    """A1 completeness: venue-ingested row carries price, quantity, and
    at least one fee component (commission / exchange_fee non-null)."""
    return (
        fill.fill_price > 0
        and fill.fill_quantity > 0
        and (fill.commission is not None or fill.exchange_fee is not None)
    )


def _a1_view(fills: list[GateFillRow]) -> GateView:
    # fills arrive newest-first; the streak counts back from the newest.
    streak = 0
    for fill in fills:
        if _fill_complete(fill):
            streak += 1
        else:
            break
    status: GateStatus = (
        "green"
        if streak >= A1_TARGET_FILLS
        else ("insufficient_data" if len(fills) < A1_TARGET_FILLS else "red")
    )
    return GateView(
        key="A1",
        title="Consecutive complete recorded fills",
        status=status,
        value=f"{streak} consecutive ({len(fills)} recent fills examined)",
        target=f">= {A1_TARGET_FILLS} consecutive, matched to statement within ±$1/±2%",
        evidence_note=(
            "Counts venue-ingested fills with complete price/qty/fee fields. "
            "The ±$1/±2% CFM-statement match is operator-run: "
            "scripts/operator_tools/reconcile_statement.py."
        ),
    )


def _a2_view(
    opens: list[TradeOpenRow],
    stops: list[StopOrderRow],
) -> GateView:
    window = timedelta(seconds=A2_STOP_WINDOW_S)
    covered = 0
    for open_row in opens:
        for stop in stops:
            if stop.contract_id != open_row.contract_id:
                continue
            delta = stop.placed_at_utc - open_row.entry_filled_at_utc
            if timedelta(0) <= delta <= window:
                covered += 1
                break
    trigger_observed = any(s.status == "filled" for s in stops)
    meets = covered >= A2_TARGET_OPENS and trigger_observed
    status: GateStatus = (
        "green" if meets else ("insufficient_data" if len(opens) < A2_TARGET_OPENS else "red")
    )
    trigger_txt = "stop trigger OBSERVED" if trigger_observed else "no stop trigger observed yet"
    return GateView(
        key="A2",
        title="Native stop resting within 10 s of every open",
        status=status,
        value=f"{covered}/{len(opens)} opens stop-covered within 10 s · {trigger_txt}",
        target=(
            f">= {A2_TARGET_OPENS} opens with a venue stop within "
            f"{A2_STOP_WINDOW_S} s, and >= 1 observed stop trigger"
        ),
        evidence_note=(
            "Pairs each trade's entry fill with a stop_market orders row placed "
            "within 10 s. Trigger evidence = any stop_market order reaching "
            "status=filled."
        ),
    )


def _a3_view(
    failure_times: list[datetime],
    latest_heartbeat: datetime | None,
) -> GateView:
    # A death followed by a later heartbeat = the process observably came
    # back. Deduped to UTC days so one crash burst counts once.
    recovery_days: set[date] = set()
    if latest_heartbeat is not None:
        for fired_at in failure_times:
            if fired_at < latest_heartbeat:
                recovery_days.add(fired_at.astimezone(UTC).date())
    count = len(recovery_days)
    status: GateStatus = "green" if count >= A3_TARGET_RESTARTS else "insufficient_data"
    return GateView(
        key="A3",
        title="Clean restarts mid-position",
        status=status,
        value=f"{count} observed crash-to-recovery days",
        target=f">= {A3_TARGET_RESTARTS} clean restarts (reconcile + re-arm, no duplicates)",
        evidence_note=(
            "Mechanical proxy: worker_failure alert days preceding the latest "
            "risk-loop heartbeat. Manual drills per deploy/strategy_worker/"
            "README.md also count — operator-attested, not computed here. "
            "'Clean' (no duplicate actions) is verified per-incident in the "
            "decisions-log, not mechanically."
        ),
    )


def _a4_view(
    decisions: list[DecisionDayRow],
    failure_times: list[datetime],
    *,
    now: datetime,
) -> GateView:
    if not decisions:
        return GateView(
            key="A4",
            title="Clean-day streak",
            status="insufficient_data",
            value="no decisions recorded",
            target=f">= {A4_TARGET_CLEAN_DAYS} days, zero unhandled exceptions / missed decisions",
            evidence_note=None,
        )
    by_date = {d.decision_date: d.status for d in decisions}
    failure_days = {t.astimezone(UTC).date() for t in failure_times}
    first_day = min(by_date)
    # Walk back from yesterday (today's decision/day is still in flight).
    day = now.astimezone(UTC).date() - timedelta(days=1)
    streak = 0
    while day >= first_day:
        status_for_day = by_date.get(day)
        dirty = (
            status_for_day is None  # missed decision day
            or status_for_day == "failed"  # unhandled decision exception
            or day in failure_days  # risk-loop / worker death that day
        )
        if dirty:
            break
        streak += 1
        day -= timedelta(days=1)
    gate_status: GateStatus = (
        "green"
        if streak >= A4_TARGET_CLEAN_DAYS
        else ("red" if streak == 0 else "insufficient_data")
    )
    return GateView(
        key="A4",
        title="Clean-day streak",
        status=gate_status,
        value=f"{streak} consecutive clean UTC days (through yesterday)",
        target=f">= {A4_TARGET_CLEAN_DAYS} days, zero unhandled exceptions / missed decisions",
        evidence_note=(
            "Clean day = a strategy_decisions row exists (completed or a "
            "deliberate skipped_* status), no failed decision, no "
            "worker_failure alert that UTC day."
        ),
    )


def _b1_view(fills: list[GateFillRow]) -> GateView:
    slippages = [
        s
        for fill in fills[:B1_TRAILING_WINDOW]
        if fill.order_type != "stop_market" and (s := fill_slippage_bps(fill)) is not None
    ]
    if len(slippages) < B1_MIN_FILLS:
        return GateView(
            key="B1",
            title="Realized slippage per side (trailing 20 fills)",
            status="insufficient_data",
            value=f"{len(slippages)} measurable fills (need {B1_MIN_FILLS})",
            target=f"median <= {B1_MEDIAN_MAX_BPS} bps, p90 <= {B1_P90_MAX_BPS} bps",
            evidence_note=(
                "Slippage measured vs the signal's decision price; stop-market "
                "fills excluded (their reference is the trigger, not a mark)."
            ),
        )
    med = _median(slippages)
    p90 = _p90(slippages)
    ok = med <= B1_MEDIAN_MAX_BPS and p90 <= B1_P90_MAX_BPS
    return GateView(
        key="B1",
        title="Realized slippage per side (trailing 20 fills)",
        status="green" if ok else "red",
        value=f"median {_fmt_bps(med)} · p90 {_fmt_bps(p90)} over {len(slippages)} fills",
        target=f"median <= {B1_MEDIAN_MAX_BPS} bps, p90 <= {B1_P90_MAX_BPS} bps",
        evidence_note=(
            "Slippage measured vs the signal's decision price; stop-market "
            "fills excluded (their reference is the trigger, not a mark)."
        ),
    )


def _b2_view(fills: list[GateFillRow]) -> GateView:
    total_fees = Decimal(0)
    total_notional = Decimal(0)
    measurable = 0
    for fill in fills[:B1_TRAILING_WINDOW]:
        if fill.contract_size is None or fill.contract_size <= 0:
            continue
        fees = (fill.commission or Decimal(0)) + (fill.exchange_fee or Decimal(0))
        notional = fill.fill_price * Decimal(fill.fill_quantity) * fill.contract_size
        if notional <= 0 or not notional.is_finite() or not fees.is_finite():
            continue
        total_fees += fees
        total_notional += notional
        measurable += 1
    if measurable == 0 or total_notional <= 0:
        return GateView(
            key="B2",
            title="Realized fee rate per side",
            status="insufficient_data",
            value="no measurable fills",
            target=f"<= {B2_MAX_FEE_BPS} bps of notional",
            evidence_note="Fees = commission + exchange_fee over fill notional.",
        )
    fee_bps = (total_fees / total_notional) * Decimal(10000)
    return GateView(
        key="B2",
        title="Realized fee rate per side",
        status="green" if fee_bps <= B2_MAX_FEE_BPS else "red",
        value=f"{_fmt_bps(fee_bps)} over {measurable} fills",
        target=f"<= {B2_MAX_FEE_BPS} bps of notional",
        evidence_note="Fees = commission + exchange_fee over fill notional.",
    )


def _b3_view(stats: list[FundingSourceStat]) -> GateView:
    cde = next((s for s in stats if s.source == CDE_FUNDING_SOURCE), None)
    proxies = [
        s
        for s in stats
        if s.source != CDE_FUNDING_SOURCE
        and s.mean_abs_annualized_rate is not None
        and s.mean_abs_annualized_rate > 0
    ]
    target = f"CDE funding within {B3_MAX_RATIO}x of the proxy (either direction)"
    if cde is None or cde.mean_abs_annualized_rate is None:
        return GateView(
            key="B3",
            title="CDE vs proxy funding",
            status="insufficient_data",
            value="no CDE funding observations in the window",
            target=target,
            evidence_note=None,
        )
    if not proxies:
        return GateView(
            key="B3",
            title="CDE vs proxy funding",
            status="insufficient_data",
            value=(
                f"CDE mean |annualized| {cde.mean_abs_annualized_rate:.4f} "
                f"({cde.observation_count} obs) · no proxy-source rows recorded"
            ),
            target=target,
            evidence_note=(
                "funding_rates currently carries only the coinbase_advanced "
                "source; the Binance-proxy comparison series is not being "
                "logged live. Gate stays insufficient_data until a proxy "
                "source lands (research backfill or a proxy logger)."
            ),
        )
    proxy = proxies[0]
    proxy_rate = proxy.mean_abs_annualized_rate
    assert proxy_rate is not None  # filtered above
    ratio = cde.mean_abs_annualized_rate / proxy_rate
    ok = (Decimal(1) / B3_MAX_RATIO) <= ratio <= B3_MAX_RATIO
    return GateView(
        key="B3",
        title="CDE vs proxy funding",
        status="green" if ok else "red",
        value=(
            f"ratio {ratio.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)} "
            f"(CDE {cde.observation_count} obs vs {proxy.source} "
            f"{proxy.observation_count} obs, trailing 30 d)"
        ),
        target=target,
        evidence_note=None,
    )


def build_system_gates_response(
    *,
    fills: list[GateFillRow],
    opens: list[TradeOpenRow],
    stops: list[StopOrderRow],
    failure_times: list[datetime],
    latest_heartbeat: datetime | None,
    decisions: list[DecisionDayRow],
    funding_stats: list[FundingSourceStat],
    now: datetime,
) -> SystemGatesResponse:
    """Assemble the §10 gate views from the repo projections (pure)."""
    gates = [
        _a1_view(fills),
        _a2_view(opens, stops),
        _a3_view(failure_times, latest_heartbeat),
        _a4_view(decisions, failure_times, now=now),
        _b1_view(fills),
        _b2_view(fills),
        _b3_view(funding_stats),
    ]
    days_live: int | None = None
    if decisions:
        days_live = (now.astimezone(UTC).date() - min(d.decision_date for d in decisions)).days
    all_green = all(g.status == "green" for g in gates)
    return SystemGatesResponse(
        gates=gates,
        days_live=days_live,
        phase_a_min_days=PHASE_A_MIN_DAYS,
        all_mechanical_green=all_green,
        scale_up_eligible=all_green and days_live is not None and days_live >= PHASE_A_MIN_DAYS,
        server_now=now,
    )


__all__ = [
    "A1_TARGET_FILLS",
    "A2_STOP_WINDOW_S",
    "A2_TARGET_OPENS",
    "A3_TARGET_RESTARTS",
    "A4_TARGET_CLEAN_DAYS",
    "B1_MEDIAN_MAX_BPS",
    "B1_MIN_FILLS",
    "B1_P90_MAX_BPS",
    "B1_TRAILING_WINDOW",
    "B2_MAX_FEE_BPS",
    "B3_MAX_RATIO",
    "CDE_FUNDING_SOURCE",
    "PHASE_A_MIN_DAYS",
    "GateView",
    "SystemGatesResponse",
    "build_system_gates_response",
    "fill_slippage_bps",
]
