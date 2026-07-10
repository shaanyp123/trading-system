"""services/api/schemas/funding.py — response models for ``GET /api/system/funding``.

Crypto-pivot §3.7/§3.9: the funding + cash-yield telemetry projection
(the Today-page "funding + cash-yield strip" input, delta spec §3.9).

**Honesty label:** every per-day funding number here is an ESTIMATE
derived from the hourly telemetry the market-data worker captures
(``funding_rates`` rows) — NOT the venue's settled funding ledger. The
``funding_basis`` literal says so on the wire, and consumers must render
it as an estimate. The venue-settled truth arrives with the recon
fetcher's funding settlements (gate B3 comparison).

All numeric fields are Decimal-as-string per [A05] (Pydantic v2 emits
``Decimal`` fields as JSON strings). Rates originate as NUMERIC columns
written by the §3.2 worker; audit-payload values are Decimal-as-string
per the writer's discipline — both re-anchor via ``Decimal(str(x))``
at this boundary.

Every mapper below is a pure function so the unit tests pin the math
(``tests/unit/test_api_funding_route.py``) — no clock reads, no I/O.

**Funding sign convention** (matches the reference engine,
``services/signal/crypto_trend.py::simulate`` step 2: ``f = contracts x
mult x close x rate; E -= f`` — longs PAY a positive rate, shorts
RECEIVE): the per-hour estimate is ``-(rate_per_interval x
signed_contracts x contract_size x mark_price)``, so a long position
under positive funding shows a NEGATIVE (cost) estimate and a short
shows a positive one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.api.repos.funding import (
    BalanceSnapshotAuditRow,
    CashBalanceSnapshotRow,
    CashSweepRow,
    FundingRateRow,
    HeldPositionRow,
    ProductMetadataRow,
    RewardTransactionRow,
)

#: Hours in the annualization year (365 x 24). CDE funding settles hourly
#: (``interval_hours = 1``), so ``rate x 8760`` matches the strategy's
#: annualized funding units (``crypto_trend.CryptoTrendParams.funding_ann``).
HOURS_PER_YEAR: Final[Decimal] = Decimal("8760")

#: Venue no-risk sentinel: ``liquidation_buffer_percentage = 1000`` means
#: "no liquidation risk" (flat book), not a real percentage — live venue
#: truth per decisions-log 2026-07-09 (canary drill 1 probes). Mapped to
#: None so the UI renders "—" instead of a nonsense 1000%.
LIQ_BUFFER_NO_RISK_SENTINEL: Final[Decimal] = Decimal("1000")

#: Audit-payload key carrying the venue liquidation buffer (written by
#: ``services/reconciliation/eod_cycle.py`` on every 00:15 UTC cycle).
_LIQ_BUFFER_PAYLOAD_KEY: Final[str] = "broker_liquidation_buffer_percentage"


class FundingProductView(BaseModel):
    """One product's slice of the funding telemetry (held or observed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    asset: str | None = Field(
        default=None,
        description=(
            "Base-asset label (BTC/ETH) from the product_metadata raw "
            "payload, falling back to the contracts.market_root of a "
            "held position; None when neither source labels it."
        ),
    )
    rate_per_interval: Decimal | None = Field(
        default=None,
        description="Latest observed per-interval funding rate (None: telemetry gap).",
    )
    interval_hours: Decimal | None = None
    annualized_rate: Decimal | None = Field(
        default=None,
        description="rate_per_interval x (8760 / interval_hours) — strategy funding units.",
    )
    mark_price: Decimal | None = Field(
        default=None,
        description="Mark captured alongside the latest funding snapshot.",
    )
    observed_at_utc: datetime | None = None
    held_contracts: int = Field(
        default=0,
        description="Signed net contracts currently held in this product (0 = telemetry-only).",
    )
    contract_size: Decimal | None = None
    est_funding_today_usd: Decimal | None = Field(
        default=None,
        description=(
            "Estimated funding paid/received today (UTC) from the hourly "
            "telemetry — negative = the position pays. None for unheld "
            "products or when sizing inputs are missing."
        ),
    )


class CashSweepView(BaseModel):
    """One §3.6 cash-sweep ledger row, projected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_at_utc: datetime
    completed_at_utc: datetime | None
    direction: str  # 'to_yield' | 'to_margin'
    amount_usd: Decimal
    status: str  # 'requested' | 'completed' | 'failed'
    reason: str


class SystemFundingResponse(BaseModel):
    """``GET /api/system/funding`` envelope.

    Fresh-DB contract: empty ``products``/``sweeps_today``, zero sweep
    totals, null estimate/yield/buffer fields — HTTP 200, never an error.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    products: list[FundingProductView]
    est_funding_today_total_usd: Decimal | None = Field(
        default=None,
        description="Sum of per-held-product estimates; None when no product has one.",
    )
    funding_basis: Literal["estimated_from_hourly_telemetry"] = "estimated_from_hourly_telemetry"
    sweeps_today: list[CashSweepView]
    swept_to_yield_today_usd: Decimal
    reclaimed_to_margin_today_usd: Decimal
    yield_apy: Decimal | None = Field(
        default=None,
        description=(
            "Interim manual APISettings.cash_yield_apy until the §3.6 "
            "cash_manager lands; None renders as '—'."
        ),
    )
    liquidation_buffer_pct: Decimal | None
    liquidation_buffer_as_of_utc: datetime | None
    # --- USDC-interest capture (decisions-log 2026-07-10) -------------
    # Load-bearing rendering context (the venue-balance-semantics
    # finding): spot USD IS trading equity (auto-swept in/out of
    # derivatives margin); CBI USDC is INVISIBLE to equity (buying
    # power only). The strip's tooltips must say so.
    spot_usd_balance: Decimal | None = Field(
        default=None,
        description=(
            "CBI spot USD available balance from the latest daily cash "
            "snapshot — part of trading equity (venue auto-sweep). None "
            "until the first capture."
        ),
    )
    cbi_usdc_balance: Decimal | None = Field(
        default=None,
        description=(
            "CBI USDC available balance (earns Coinbase One rewards; "
            "invisible to trading equity). None until the first capture."
        ),
    )
    cash_balances_as_of_utc: datetime | None = Field(
        default=None,
        description="Capture instant of the latest cash snapshot.",
    )
    last_usdc_reward_amount: Decimal | None = Field(
        default=None,
        description=(
            "Most recent persisted positive USDC ledger credit (rewards "
            "pay out weekly on Fridays). None until the first payout is "
            "captured."
        ),
    )
    last_usdc_reward_at_utc: datetime | None = None
    lifetime_usdc_rewards: Decimal = Field(
        default=Decimal("0"),
        description="Sum of all persisted positive USDC ledger credits.",
    )
    server_now: datetime


# ---------------------------------------------------------------------------
# Pure mappers (Decimal math pinned by tests)
# ---------------------------------------------------------------------------


def annualize_rate(rate_per_interval: Decimal, interval_hours: Decimal) -> Decimal | None:
    """``rate x (8760 / interval_hours)`` — None on a degenerate interval."""
    if interval_hours <= 0:
        return None
    return rate_per_interval * (HOURS_PER_YEAR / interval_hours)


def estimate_funding_today_usd(
    today_rows: list[FundingRateRow],
    *,
    signed_contracts: int,
    contract_size: Decimal | None,
) -> Decimal | None:
    """Sum today's hourly funding estimate for one held product.

    Per hourly row: ``-(rate_per_interval x signed_contracts x
    contract_size x mark_price)`` — the sign convention from the module
    docstring (long pays a positive rate). Rows without a captured mark
    are skipped (no honest price to size against). Returns None when the
    product isn't held, sizing inputs are missing, or no row was usable —
    the "n/a" contract, distinct from a genuine 0 estimate.
    """
    if signed_contracts == 0 or contract_size is None:
        return None
    total: Decimal | None = None
    for row in today_rows:
        if row.mark_price is None:
            continue
        hour_est = -(
            row.rate_per_interval * Decimal(signed_contracts) * contract_size * row.mark_price
        )
        total = hour_est if total is None else total + hour_est
    return total


def asset_label_from_metadata(raw: dict[str, Any]) -> str | None:
    """Best-effort base-asset label from the venue's raw product payload.

    Prefers ``future_product_details.contract_root_unit`` (the CDE
    products' base asset, e.g. ``BTC``), then the spot-style
    ``base_currency_id``/``base_display_symbol``. None when the payload
    carries neither — the caller falls back to ``contracts.market_root``.
    """
    details = raw.get("future_product_details")
    if isinstance(details, dict):
        root_unit = details.get("contract_root_unit")
        if isinstance(root_unit, str) and root_unit:
            return root_unit
    for key in ("base_currency_id", "base_display_symbol"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def normalize_liquidation_buffer(raw: Any) -> Decimal | None:
    """Audit-payload buffer value → Decimal, with the no-risk sentinel mapped.

    The payload carries Decimal-as-string (or None); the venue's ``1000``
    sentinel means "no liquidation risk" (see
    :data:`LIQ_BUFFER_NO_RISK_SENTINEL`) and maps to None. Unparseable
    values also degrade to None — never a 500 over a display field.
    """
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if value == LIQ_BUFFER_NO_RISK_SENTINEL:
        return None
    return value


def _sum_completed_sweeps(sweeps: list[CashSweepRow], *, direction: str) -> Decimal:
    """Sum COMPLETED sweep amounts for one direction.

    Completed-only by design: a ``requested`` row is in flight and a
    ``failed`` row moved nothing — counting either would overstate "what
    is currently swept" (the table's stated purpose, C0-B1 migration
    docstring).
    """
    total = Decimal("0")
    for sweep in sweeps:
        if sweep.direction == direction and sweep.status == "completed":
            total += sweep.amount_usd
    return total


def build_system_funding_response(
    *,
    latest_rates: list[FundingRateRow],
    today_rates: list[FundingRateRow],
    held_positions: list[HeldPositionRow],
    metadata: list[ProductMetadataRow],
    sweeps_today: list[CashSweepRow],
    balance_snapshot: BalanceSnapshotAuditRow | None,
    cash_snapshot: CashBalanceSnapshotRow | None,
    last_reward: RewardTransactionRow | None,
    lifetime_rewards: Decimal,
    yield_apy: Decimal | None,
    now: datetime,
) -> SystemFundingResponse:
    """Assemble the full response from repo rows. Pure — tests pin it.

    Product universe = union of (products with a recent funding
    snapshot) U (products currently held) so a held position with a
    telemetry gap still renders (with null rate fields) rather than
    silently vanishing. Held products sort first, then product_id.
    """
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware (dev-guide §3 / A06); got naive")

    held_by_product: dict[str, HeldPositionRow] = {}
    contracts_by_product: dict[str, int] = {}
    for pos in held_positions:
        # A market can legally span >1 positions_current row (roll
        # boundary); net the signed quantities, keep the first
        # enrichment row for market_root/multiplier.
        contracts_by_product[pos.market] = contracts_by_product.get(pos.market, 0) + pos.quantity
        held_by_product.setdefault(pos.market, pos)

    rates_by_product = {r.product_id: r for r in latest_rates}
    metadata_by_product = {m.product_id: m for m in metadata}
    today_by_product: dict[str, list[FundingRateRow]] = {}
    for row in today_rates:
        today_by_product.setdefault(row.product_id, []).append(row)

    product_ids = set(rates_by_product) | set(contracts_by_product)
    views: list[FundingProductView] = []
    total_est: Decimal | None = None
    for product_id in sorted(product_ids):
        rate_row = rates_by_product.get(product_id)
        meta_row = metadata_by_product.get(product_id)
        held_row = held_by_product.get(product_id)
        held_contracts = contracts_by_product.get(product_id, 0)

        asset: str | None = None
        if meta_row is not None:
            asset = asset_label_from_metadata(meta_row.raw)
        if asset is None and held_row is not None:
            asset = held_row.market_root

        # contract_size: the daily metadata snapshot is authoritative;
        # the contracts.multiplier the strategy worker wrote from the
        # same runtime discovery ([A13]) is the fallback for a held
        # product whose metadata snapshot hasn't landed yet.
        contract_size = meta_row.contract_size if meta_row is not None else None
        if contract_size is None and held_row is not None:
            contract_size = held_row.multiplier

        annualized: Decimal | None = None
        if rate_row is not None:
            annualized = annualize_rate(rate_row.rate_per_interval, rate_row.interval_hours)

        est_today: Decimal | None = None
        if held_contracts != 0:
            est_today = estimate_funding_today_usd(
                today_by_product.get(product_id, []),
                signed_contracts=held_contracts,
                contract_size=contract_size,
            )
            if est_today is not None:
                total_est = est_today if total_est is None else total_est + est_today

        views.append(
            FundingProductView(
                product_id=product_id,
                asset=asset,
                rate_per_interval=rate_row.rate_per_interval if rate_row else None,
                interval_hours=rate_row.interval_hours if rate_row else None,
                annualized_rate=annualized,
                mark_price=rate_row.mark_price if rate_row else None,
                observed_at_utc=rate_row.observed_at_utc if rate_row else None,
                held_contracts=held_contracts,
                contract_size=contract_size,
                est_funding_today_usd=est_today,
            )
        )

    # Held products first (the operator's book leads the table).
    views.sort(key=lambda v: (v.held_contracts == 0, v.product_id))

    liq_buffer: Decimal | None = None
    liq_as_of: datetime | None = None
    if balance_snapshot is not None:
        liq_buffer = normalize_liquidation_buffer(
            balance_snapshot.payload.get(_LIQ_BUFFER_PAYLOAD_KEY)
        )
        liq_as_of = balance_snapshot.recorded_at_utc

    return SystemFundingResponse(
        products=views,
        est_funding_today_total_usd=total_est,
        sweeps_today=[
            CashSweepView(
                requested_at_utc=s.requested_at_utc,
                completed_at_utc=s.completed_at_utc,
                direction=s.direction,
                amount_usd=s.amount_usd,
                status=s.status,
                reason=s.reason,
            )
            for s in sweeps_today
        ],
        swept_to_yield_today_usd=_sum_completed_sweeps(sweeps_today, direction="to_yield"),
        reclaimed_to_margin_today_usd=_sum_completed_sweeps(sweeps_today, direction="to_margin"),
        yield_apy=yield_apy,
        liquidation_buffer_pct=liq_buffer,
        liquidation_buffer_as_of_utc=liq_as_of,
        spot_usd_balance=cash_snapshot.spot_usd if cash_snapshot else None,
        cbi_usdc_balance=cash_snapshot.cbi_usdc if cash_snapshot else None,
        cash_balances_as_of_utc=cash_snapshot.captured_at_utc if cash_snapshot else None,
        last_usdc_reward_amount=last_reward.amount if last_reward else None,
        last_usdc_reward_at_utc=last_reward.venue_created_at_utc if last_reward else None,
        lifetime_usdc_rewards=lifetime_rewards,
        server_now=now,
    )


__all__ = [
    "HOURS_PER_YEAR",
    "LIQ_BUFFER_NO_RISK_SENTINEL",
    "CashSweepView",
    "FundingProductView",
    "SystemFundingResponse",
    "annualize_rate",
    "asset_label_from_metadata",
    "build_system_funding_response",
    "estimate_funding_today_usd",
    "normalize_liquidation_buffer",
]
