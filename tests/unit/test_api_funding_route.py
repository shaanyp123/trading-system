"""Unit tests for ``GET /api/system/funding`` (crypto-pivot §3.7/§3.9).

Route-level coverage via the conftest ASGI app + stubbed repos (same
pattern as ``test_api_cycle_route.py`` — no testcontainers):

  * empty DB — empty products/sweeps, null estimate/yield/buffer fields,
    the ``funding_basis`` honesty literal, HTTP 200
  * held + telemetry-only products — held products sort FIRST
  * est-funding math pinned to exact Decimals — long under positive rate
    PAYS (negative estimate), short RECEIVES (positive); sign convention
    per ``services/signal/crypto_trend.py::simulate`` (``E -= f``)
  * **Decimal-as-string on the wire** ([A05])
  * sweep totals — completed-only, per direction
  * ``yield_apy`` None default (no cash_manager yet) + set passthrough
  * liquidation-buffer ``1000`` no-risk sentinel → None; a normal value
    passes through with its as-of timestamp

Plus pure-mapper coverage (``services/api/schemas/funding.py``):
annualization, per-hour estimate skips, sentinel normalization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from services.api.repos.funding import (
    BalanceSnapshotAuditRow,
    CashBalanceSnapshotRow,
    CashSweepRow,
    FundingRateRow,
    HeldPositionRow,
    ProductMetadataRow,
    RewardTransactionRow,
)
from services.api.routes import system as system_route
from services.api.schemas.funding import (
    annualize_rate,
    estimate_funding_today_usd,
    normalize_liquidation_buffer,
)

NOW = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Stub repos
# ---------------------------------------------------------------------------


@dataclass
class _StubPhase1Repo:
    """Minimal Phase1QueryRepo stub — the route only calls one method."""

    account_id: UUID | None = None

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account_id


@dataclass
class _StubFundingRepo:
    latest_rates: list[FundingRateRow] = field(default_factory=list)
    today_rates: list[FundingRateRow] = field(default_factory=list)
    held: list[HeldPositionRow] = field(default_factory=list)
    metadata: list[ProductMetadataRow] = field(default_factory=list)
    sweeps: list[CashSweepRow] = field(default_factory=list)
    balance: BalanceSnapshotAuditRow | None = None
    cash_snapshot: CashBalanceSnapshotRow | None = None
    last_reward: RewardTransactionRow | None = None
    lifetime_rewards: Decimal = Decimal("0")
    seen_today_product_ids: list[str] = field(default_factory=list)
    held_queried_for: list[UUID] = field(default_factory=list)

    async def fetch_latest_funding_rates(self) -> list[FundingRateRow]:
        return self.latest_rates

    async def fetch_funding_rates_today(self, product_ids: Any) -> list[FundingRateRow]:
        self.seen_today_product_ids = list(product_ids)
        return self.today_rates

    async def fetch_held_positions_with_contract(self, account_id: UUID) -> list[HeldPositionRow]:
        self.held_queried_for.append(account_id)
        return self.held

    async def fetch_cash_sweeps_today(self) -> list[CashSweepRow]:
        return self.sweeps

    async def fetch_latest_product_metadata(self) -> list[ProductMetadataRow]:
        return self.metadata

    async def fetch_latest_balance_snapshot_audit(self) -> BalanceSnapshotAuditRow | None:
        return self.balance

    async def fetch_latest_cash_balance_snapshot(self) -> CashBalanceSnapshotRow | None:
        return self.cash_snapshot

    async def fetch_last_reward_credit(self) -> RewardTransactionRow | None:
        return self.last_reward

    async def fetch_lifetime_reward_credits_total(self) -> Decimal:
        return self.lifetime_rewards


def _wire(
    override_dep: Any,
    *,
    account_id: UUID | None,
    funding_repo: _StubFundingRepo | None = None,
    yield_apy: Decimal | None = None,
) -> _StubFundingRepo:
    repo = funding_repo or _StubFundingRepo()
    override_dep(system_route._get_repo, lambda: _StubPhase1Repo(account_id=account_id))
    override_dep(system_route._get_funding_repo, lambda: repo)
    # The route only reads settings.cash_yield_apy; a namespace stub keeps
    # the test independent of env-var plumbing.
    override_dep(system_route.get_settings, lambda: SimpleNamespace(cash_yield_apy=yield_apy))
    return repo


def _rate_row(
    product_id: str,
    *,
    rate: str,
    mark: str | None,
    observed: datetime = NOW,
) -> FundingRateRow:
    return FundingRateRow(
        product_id=product_id,
        observed_at_utc=observed,
        rate_per_interval=Decimal(rate),
        interval_hours=Decimal("1"),
        mark_price=Decimal(mark) if mark is not None else None,
    )


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


class TestSystemFundingRoute:
    async def test_empty_db_returns_empty_envelope(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        repo = _wire(override_dep, account_id=None)
        resp = await api_client.get("/api/system/funding")
        assert resp.status_code == 200
        body = resp.json()
        assert body["products"] == []
        assert body["sweeps_today"] == []
        assert body["est_funding_today_total_usd"] is None
        assert body["funding_basis"] == "estimated_from_hourly_telemetry"
        assert body["swept_to_yield_today_usd"] == "0"
        assert body["reclaimed_to_margin_today_usd"] == "0"
        assert body["yield_apy"] is None
        assert body["liquidation_buffer_pct"] is None
        assert body["liquidation_buffer_as_of_utc"] is None
        assert body["spot_usd_balance"] is None
        assert body["cbi_usdc_balance"] is None
        assert body["cash_balances_as_of_utc"] is None
        assert body["last_usdc_reward_amount"] is None
        assert body["last_usdc_reward_at_utc"] is None
        assert body["lifetime_usdc_rewards"] == "0"
        assert body["server_now"] is not None
        # No active account → held positions never queried.
        assert repo.held_queried_for == []

    async def test_cash_capture_fields_pass_through_as_decimal_strings(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """USDC-interest capture fields: exact Decimal-string passthrough ([A05])."""
        repo = _StubFundingRepo(
            cash_snapshot=CashBalanceSnapshotRow(
                snapshot_date_utc=NOW.date(),
                captured_at_utc=NOW,
                spot_usd=Decimal("1397.01"),
                cbi_usdc=Decimal("4300.55"),
            ),
            last_reward=RewardTransactionRow(
                venue_created_at_utc=NOW,
                amount=Decimal("0.48"),
                currency="USDC",
                transaction_type="interest",
            ),
            lifetime_rewards=Decimal("12.34"),
        )
        _wire(override_dep, account_id=None, funding_repo=repo)
        resp = await api_client.get("/api/system/funding")
        assert resp.status_code == 200
        body = resp.json()
        assert body["spot_usd_balance"] == "1397.01"
        assert body["cbi_usdc_balance"] == "4300.55"
        assert body["cash_balances_as_of_utc"] is not None
        assert body["last_usdc_reward_amount"] == "0.48"
        assert body["last_usdc_reward_at_utc"] is not None
        assert body["lifetime_usdc_rewards"] == "12.34"

    async def test_cash_snapshot_without_usdc_account_renders_null_not_zero(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """None (no account seen) stays distinct from a genuine zero balance."""
        repo = _StubFundingRepo(
            cash_snapshot=CashBalanceSnapshotRow(
                snapshot_date_utc=NOW.date(),
                captured_at_utc=NOW,
                spot_usd=Decimal("0"),
                cbi_usdc=None,
            ),
        )
        _wire(override_dep, account_id=None, funding_repo=repo)
        body = (await api_client.get("/api/system/funding")).json()
        assert body["spot_usd_balance"] == "0"
        assert body["cbi_usdc_balance"] is None

    async def test_held_products_sort_first(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        account = uuid4()
        repo = _StubFundingRepo(
            latest_rates=[
                # Alphabetically FIRST but unheld — must sort after the held one.
                _rate_row("AIP-20DEC30-CDE", rate="0.0000100", mark="100.0"),
                _rate_row("BIP-20DEC30-CDE", rate="0.0000125", mark="64000.0"),
            ],
            held=[
                HeldPositionRow(
                    market="BIP-20DEC30-CDE",
                    quantity=2,
                    market_root="BTC",
                    multiplier=Decimal("0.01"),
                ),
            ],
        )
        _wire(override_dep, account_id=account, funding_repo=repo)
        resp = await api_client.get("/api/system/funding")
        assert resp.status_code == 200
        products = resp.json()["products"]
        assert [p["product_id"] for p in products] == [
            "BIP-20DEC30-CDE",
            "AIP-20DEC30-CDE",
        ]
        held = products[0]
        assert held["held_contracts"] == 2
        assert held["asset"] == "BTC"  # market_root fallback (no metadata)
        assert held["contract_size"] == "0.01"  # contracts.multiplier fallback
        unheld = products[1]
        assert unheld["held_contracts"] == 0
        assert unheld["est_funding_today_usd"] is None
        # Today's-rows query covers the union of observed + held products.
        assert repo.seen_today_product_ids == ["AIP-20DEC30-CDE", "BIP-20DEC30-CDE"]

    async def test_long_position_pays_positive_funding(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """Sign convention: long x positive rate → NEGATIVE estimate (a cost).

        Two hourly rows @ rate 0.0000125, 2 long contracts, size 0.01,
        mark 64000: per hour -(0.0000125 x 2 x 0.01 x 64000) = -0.016 →
        total -0.032. Pinned exactly in Decimal.
        """
        account = uuid4()
        repo = _StubFundingRepo(
            latest_rates=[_rate_row("BIP-20DEC30-CDE", rate="0.0000125", mark="64000")],
            today_rates=[
                _rate_row("BIP-20DEC30-CDE", rate="0.0000125", mark="64000"),
                _rate_row("BIP-20DEC30-CDE", rate="0.0000125", mark="64000"),
            ],
            held=[
                HeldPositionRow(
                    market="BIP-20DEC30-CDE",
                    quantity=2,
                    market_root="BTC",
                    multiplier=Decimal("0.01"),
                ),
            ],
        )
        _wire(override_dep, account_id=account, funding_repo=repo)
        body = (await api_client.get("/api/system/funding")).json()
        product = body["products"][0]
        assert Decimal(product["est_funding_today_usd"]) == Decimal("-0.032")
        assert Decimal(body["est_funding_today_total_usd"]) == Decimal("-0.032")
        # Annualized: 0.0000125 x 8760 = 0.1095 (strategy funding units).
        assert Decimal(product["annualized_rate"]) == Decimal("0.1095")

    async def test_short_position_receives_positive_funding(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        account = uuid4()
        repo = _StubFundingRepo(
            latest_rates=[_rate_row("ETP-20DEC30-CDE", rate="0.0000125", mark="3400")],
            today_rates=[_rate_row("ETP-20DEC30-CDE", rate="0.0000125", mark="3400")],
            held=[
                HeldPositionRow(
                    market="ETP-20DEC30-CDE",
                    quantity=-4,
                    market_root="ETH",
                    multiplier=Decimal("0.1"),
                ),
            ],
        )
        _wire(override_dep, account_id=account, funding_repo=repo)
        body = (await api_client.get("/api/system/funding")).json()
        product = body["products"][0]
        # -(0.0000125 x (-4) x 0.1 x 3400) = +0.017 — the short is PAID.
        assert Decimal(product["est_funding_today_usd"]) == Decimal("0.017")
        assert Decimal(body["est_funding_today_total_usd"]) == Decimal("0.017")

    async def test_decimal_as_string_on_the_wire(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """[A05] — every money/rate value serializes as a JSON string."""
        account = uuid4()
        repo = _StubFundingRepo(
            latest_rates=[_rate_row("BIP-20DEC30-CDE", rate="0.0000125", mark="64000")],
            today_rates=[_rate_row("BIP-20DEC30-CDE", rate="0.0000125", mark="64000")],
            held=[
                HeldPositionRow(
                    market="BIP-20DEC30-CDE",
                    quantity=1,
                    market_root="BTC",
                    multiplier=Decimal("0.01"),
                ),
            ],
        )
        _wire(
            override_dep,
            account_id=account,
            funding_repo=repo,
            yield_apy=Decimal("0.035"),
        )
        body = (await api_client.get("/api/system/funding")).json()
        product = body["products"][0]
        for key in (
            "rate_per_interval",
            "interval_hours",
            "annualized_rate",
            "mark_price",
            "contract_size",
            "est_funding_today_usd",
        ):
            assert isinstance(product[key], str), key
        assert product["rate_per_interval"] == "0.0000125"
        assert isinstance(body["yield_apy"], str)
        assert body["yield_apy"] == "0.035"
        assert isinstance(body["swept_to_yield_today_usd"], str)

    async def test_sweep_totals_count_completed_only(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        account = uuid4()
        repo = _StubFundingRepo(
            sweeps=[
                CashSweepRow(
                    requested_at_utc=NOW,
                    completed_at_utc=NOW,
                    direction="to_yield",
                    amount_usd=Decimal("1200.50"),
                    status="completed",
                    reason="daily_sweep",
                ),
                CashSweepRow(
                    requested_at_utc=NOW,
                    completed_at_utc=None,
                    direction="to_yield",
                    amount_usd=Decimal("999.99"),
                    status="requested",  # in flight — must NOT count
                    reason="daily_sweep",
                ),
                CashSweepRow(
                    requested_at_utc=NOW,
                    completed_at_utc=NOW,
                    direction="to_margin",
                    amount_usd=Decimal("300.00"),
                    status="completed",
                    reason="margin_reclaim",
                ),
                CashSweepRow(
                    requested_at_utc=NOW,
                    completed_at_utc=None,
                    direction="to_margin",
                    amount_usd=Decimal("50.00"),
                    status="failed",  # moved nothing — must NOT count
                    reason="margin_reclaim",
                ),
            ],
        )
        _wire(override_dep, account_id=account, funding_repo=repo)
        body = (await api_client.get("/api/system/funding")).json()
        assert len(body["sweeps_today"]) == 4  # ledger shows everything
        assert Decimal(body["swept_to_yield_today_usd"]) == Decimal("1200.50")
        assert Decimal(body["reclaimed_to_margin_today_usd"]) == Decimal("300.00")
        assert body["sweeps_today"][0]["direction"] == "to_yield"
        assert body["sweeps_today"][0]["status"] == "completed"

    async def test_liq_buffer_sentinel_maps_to_none(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """Venue ``1000`` no-risk sentinel (decisions-log 2026-07-09) → None."""
        account = uuid4()
        repo = _StubFundingRepo(
            balance=BalanceSnapshotAuditRow(
                payload={"broker_liquidation_buffer_percentage": "1000"},
                recorded_at_utc=NOW,
            ),
        )
        _wire(override_dep, account_id=account, funding_repo=repo)
        body = (await api_client.get("/api/system/funding")).json()
        assert body["liquidation_buffer_pct"] is None
        # as-of still surfaces — the snapshot DID happen.
        assert body["liquidation_buffer_as_of_utc"] is not None

    async def test_liq_buffer_normal_value_passes_through(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        account = uuid4()
        repo = _StubFundingRepo(
            balance=BalanceSnapshotAuditRow(
                payload={"broker_liquidation_buffer_percentage": "42.5"},
                recorded_at_utc=NOW,
            ),
        )
        _wire(override_dep, account_id=account, funding_repo=repo)
        body = (await api_client.get("/api/system/funding")).json()
        assert Decimal(body["liquidation_buffer_pct"]) == Decimal("42.5")
        assert isinstance(body["liquidation_buffer_pct"], str)
        assert body["liquidation_buffer_as_of_utc"].startswith("2026-07-09T14:30:00")

    async def test_asset_label_prefers_metadata_root_unit(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        account = uuid4()
        repo = _StubFundingRepo(
            latest_rates=[_rate_row("BIP-20DEC30-CDE", rate="0.0000125", mark="64000")],
            metadata=[
                ProductMetadataRow(
                    product_id="BIP-20DEC30-CDE",
                    contract_size=Decimal("0.01"),
                    raw={"future_product_details": {"contract_root_unit": "BTC"}},
                ),
            ],
        )
        _wire(override_dep, account_id=account, funding_repo=repo)
        body = (await api_client.get("/api/system/funding")).json()
        product = body["products"][0]
        assert product["asset"] == "BTC"
        assert Decimal(product["contract_size"]) == Decimal("0.01")


# ---------------------------------------------------------------------------
# Pure mappers
# ---------------------------------------------------------------------------


class TestAnnualizeRate:
    def test_hourly_rate_times_8760(self) -> None:
        assert annualize_rate(Decimal("0.0000125"), Decimal("1")) == Decimal("0.1095")

    def test_eight_hour_interval(self) -> None:
        # 8760 / 8 = 1095 settlement windows per year.
        assert annualize_rate(Decimal("0.0001"), Decimal("8")) == Decimal("0.1095")

    def test_degenerate_interval_returns_none(self) -> None:
        assert annualize_rate(Decimal("0.0001"), Decimal("0")) is None


class TestEstimateFundingTodayUsd:
    def test_unheld_returns_none(self) -> None:
        rows = [_rate_row("X", rate="0.0000125", mark="100")]
        assert (
            estimate_funding_today_usd(rows, signed_contracts=0, contract_size=Decimal("1")) is None
        )

    def test_missing_contract_size_returns_none(self) -> None:
        rows = [_rate_row("X", rate="0.0000125", mark="100")]
        assert estimate_funding_today_usd(rows, signed_contracts=2, contract_size=None) is None

    def test_rows_without_mark_are_skipped(self) -> None:
        rows = [
            _rate_row("X", rate="0.0000125", mark=None),  # skipped
            _rate_row("X", rate="0.0000125", mark="64000"),
        ]
        est = estimate_funding_today_usd(rows, signed_contracts=2, contract_size=Decimal("0.01"))
        assert est == Decimal("-0.016")

    def test_no_usable_rows_returns_none(self) -> None:
        rows = [_rate_row("X", rate="0.0000125", mark=None)]
        assert (
            estimate_funding_today_usd(rows, signed_contracts=2, contract_size=Decimal("0.01"))
            is None
        )

    def test_negative_rate_pays_the_long(self) -> None:
        # Negative funding: the long RECEIVES.
        rows = [_rate_row("X", rate="-0.0000125", mark="64000")]
        est = estimate_funding_today_usd(rows, signed_contracts=2, contract_size=Decimal("0.01"))
        assert est == Decimal("0.016")


class TestNormalizeLiquidationBuffer:
    def test_none_passthrough(self) -> None:
        assert normalize_liquidation_buffer(None) is None

    def test_sentinel_maps_to_none(self) -> None:
        assert normalize_liquidation_buffer("1000") is None
        assert normalize_liquidation_buffer(Decimal("1000")) is None

    def test_normal_value_passes_through(self) -> None:
        assert normalize_liquidation_buffer("35.75") == Decimal("35.75")

    def test_garbage_degrades_to_none(self) -> None:
        assert normalize_liquidation_buffer("not-a-number") is None


@pytest.mark.parametrize("naive", [datetime(2026, 7, 9, 0, 0)])
def test_build_response_rejects_naive_now(naive: datetime) -> None:
    from services.api.schemas.funding import build_system_funding_response

    with pytest.raises(ValueError, match="tz-aware"):
        build_system_funding_response(
            latest_rates=[],
            today_rates=[],
            held_positions=[],
            metadata=[],
            sweeps_today=[],
            balance_snapshot=None,
            cash_snapshot=None,
            last_reward=None,
            lifetime_rewards=Decimal("0"),
            yield_apy=None,
            now=naive,
        )
