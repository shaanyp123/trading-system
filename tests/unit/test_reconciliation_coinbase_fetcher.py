"""Unit tests for :mod:`services.reconciliation.coinbase_fetcher`.

Crypto-pivot C0 §3.5. Pure-policy tests with a fake transport at the
``CoinbaseBrokerClient`` Protocol seam — no live HTTP, no SDK. Locks:

* venue positions → :class:`ReconPosition` mapping (zero-qty dropped,
  short signs preserved, missing product_id skipped with a warning);
* Decimal boundaries end-to-end ([A05]) — cash/NLV derivation is exact
  Decimal arithmetic, never float;
* the error contract (load-bearing calls raise
  :class:`CoinbaseReconFetchError`; fills degrade softly);
* the empty-book case (no positions is a normal, clean snapshot).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import structlog

from services.execution.types import (
    BrokerError,
    BrokerFill,
    BrokerPosition,
    FuturesBalanceSummary,
    PerpProductRef,
)
from services.reconciliation.coinbase_fetcher import (
    DEFAULT_FILLS_LOOKBACK_HOURS,
    CoinbaseEodFetcher,
    CoinbaseReconFetchError,
    CoinbaseReconSnapshot,
    ReconPosition,
    recon_positions_from_broker,
)

_PULLED_AT = datetime(2026, 7, 10, 0, 15, tzinfo=UTC)

# Test-fixture product ids (fakes; production discovers real ids at
# runtime per [A13]).
_BTC = "BTC-PERP-CDE"
_ETH = "ETH-PERP-CDE"

#: product_id -> base_asset map mirroring what runtime discovery yields.
_MAP = {_BTC: "BTC", _ETH: "ETH"}


def _product(product_id: str, base_asset: str) -> PerpProductRef:
    return PerpProductRef(
        product_id=product_id,
        base_asset=base_asset,
        contract_size=Decimal("0.01"),
        tick_size=Decimal("1"),
        trading_disabled=False,
    )


def _pos(
    *,
    product_id: str = _BTC,
    contracts: str = "2",
    entry_vwap: str | None = "109000",
    mark_price: str | None = "110000",
    unrealized_pnl: str | None = "20",
) -> BrokerPosition:
    return BrokerPosition(
        product_id=product_id,
        contracts=Decimal(contracts),
        entry_vwap=Decimal(entry_vwap) if entry_vwap is not None else None,
        mark_price=Decimal(mark_price) if mark_price is not None else None,
        unrealized_pnl_usd=Decimal(unrealized_pnl) if unrealized_pnl is not None else None,
    )


def _summary(
    *,
    total: str | None = "100000",
    cbi: str | None = "60000",
    cfm: str | None = "40000",
    unrealized: str | None = "0",
    available_margin: str | None = "35000",
    initial_margin: str | None = "5000",
) -> FuturesBalanceSummary:
    def _d(v: str | None) -> Decimal | None:
        return Decimal(v) if v is not None else None

    return FuturesBalanceSummary(
        total_usd_balance=_d(total),
        cbi_usd_balance=_d(cbi),
        cfm_usd_balance=_d(cfm),
        available_margin=_d(available_margin),
        initial_margin=_d(initial_margin),
        unrealized_pnl=_d(unrealized),
        daily_realized_pnl=None,
        liquidation_threshold=None,
        liquidation_buffer_amount=None,
        liquidation_buffer_percentage=None,
        snapshot_at_utc=_PULLED_AT,
    )


def _fill(*, fill_id: str = "f1", product_id: str = _BTC) -> BrokerFill:
    return BrokerFill(
        fill_id=fill_id,
        order_id="o1",
        client_order_id=None,
        product_id=product_id,
        side="buy",
        contracts=Decimal("1"),
        price=Decimal("110000.5"),
        fee_usd=Decimal("0.11"),
        filled_at_utc=_PULLED_AT - timedelta(hours=3),
    )


class FakeBrokerClient:
    """Fake ``CoinbaseBrokerClient`` covering the surface the fetcher uses."""

    def __init__(
        self,
        *,
        positions: list[BrokerPosition] | None = None,
        summary: FuturesBalanceSummary | None = None,
        fills: list[BrokerFill] | None = None,
        products: list[PerpProductRef] | None = None,
        positions_error: BrokerError | None = None,
        summary_error: BrokerError | None = None,
        fills_error: BrokerError | None = None,
        products_error: BrokerError | None = None,
    ) -> None:
        self._positions = positions if positions is not None else []
        self._summary = summary if summary is not None else _summary()
        self._fills = fills if fills is not None else []
        self._products = (
            products if products is not None else [_product(_BTC, "BTC"), _product(_ETH, "ETH")]
        )
        self._positions_error = positions_error
        self._summary_error = summary_error
        self._fills_error = fills_error
        self._products_error = products_error
        self.list_fills_calls: list[datetime | None] = []

    async def list_perp_products(self) -> list[PerpProductRef]:
        if self._products_error is not None:
            raise self._products_error
        return list(self._products)

    async def list_positions(self) -> list[BrokerPosition]:
        if self._positions_error is not None:
            raise self._positions_error
        return list(self._positions)

    async def get_futures_balance_summary(self) -> FuturesBalanceSummary:
        if self._summary_error is not None:
            raise self._summary_error
        return self._summary

    async def list_fills(
        self,
        *,
        product_id: str | None = None,
        order_ids: list[str] | None = None,
        start_utc: datetime | None = None,
    ) -> list[BrokerFill]:
        del product_id, order_ids
        self.list_fills_calls.append(start_utc)
        if self._fills_error is not None:
            raise self._fills_error
        return list(self._fills)


def _broker_error(operation: str) -> BrokerError:
    return BrokerError(
        operation=operation,
        detail="boom",
        underlying_exception_class="RuntimeError",
        occurred_at_utc=_PULLED_AT,
    )


def _fetcher(client: FakeBrokerClient, **kwargs: object) -> CoinbaseEodFetcher:
    return CoinbaseEodFetcher(
        client=client,  # type: ignore[arg-type]  # structural fake
        clock=lambda: _PULLED_AT,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# recon_positions_from_broker (pure mapping)
# ---------------------------------------------------------------------------


class TestReconPositionsFromBroker:
    def test_maps_product_id_to_base_asset(self) -> None:
        # THE 2026-07-11 defect: positions_current is keyed by asset
        # ("BTC"), the venue reports product ids — normalization is the
        # planner-key contract.
        out = recon_positions_from_broker(
            [_pos(product_id=_BTC, contracts="2")], product_to_asset=_MAP
        )
        assert out == (ReconPosition(market="BTC", quantity=Decimal("2")),)

    def test_short_position_sign_preserved(self) -> None:
        out = recon_positions_from_broker(
            [_pos(product_id=_ETH, contracts="-4")], product_to_asset=_MAP
        )
        assert out == (ReconPosition(market="ETH", quantity=Decimal("-4")),)

    def test_zero_quantity_dropped(self) -> None:
        out = recon_positions_from_broker(
            [_pos(contracts="0"), _pos(product_id=_ETH, contracts="1")],
            product_to_asset=_MAP,
        )
        assert out == (ReconPosition(market="ETH", quantity=Decimal("1")),)

    def test_missing_product_id_skipped_with_warning(self) -> None:
        with structlog.testing.capture_logs() as captured:
            out = recon_positions_from_broker(
                [_pos(product_id="", contracts="3"), _pos(product_id=_BTC, contracts="1")],
                product_to_asset=_MAP,
            )
        assert out == (ReconPosition(market="BTC", quantity=Decimal("1")),)
        events = [c.get("event") for c in captured]
        assert "coinbase_recon_position_missing_product_id" in events

    def test_unmapped_product_id_passes_through_with_warning(self) -> None:
        # Fail-visible: an instrument discovery doesn't know must surface
        # as a recon break under its raw id, never silently normalize.
        with structlog.testing.capture_logs() as captured:
            out = recon_positions_from_broker(
                [_pos(product_id="SOL-MYSTERY-CDE", contracts="5")],
                product_to_asset=_MAP,
            )
        assert out == (ReconPosition(market="SOL-MYSTERY-CDE", quantity=Decimal("5")),)
        events = [c.get("event") for c in captured]
        assert "coinbase_recon_position_unmapped_product" in events

    def test_two_products_same_asset_aggregate(self) -> None:
        # Defensive SUM per resulting key (one CDE product per asset
        # today makes this a no-op in production).
        out = recon_positions_from_broker(
            [
                _pos(product_id=_BTC, contracts="2"),
                _pos(product_id="BTC-OTHER-CDE", contracts="-1"),
            ],
            product_to_asset={**_MAP, "BTC-OTHER-CDE": "BTC"},
        )
        assert out == (ReconPosition(market="BTC", quantity=Decimal("1")),)

    def test_empty_book_maps_to_empty_tuple(self) -> None:
        assert recon_positions_from_broker([], product_to_asset=_MAP) == ()

    def test_decimal_quantity_exact(self) -> None:
        # [A05]: quantities survive as exact Decimals (no float round-trip).
        out = recon_positions_from_broker([_pos(contracts="7")], product_to_asset=_MAP)
        assert out[0].quantity == Decimal(7)
        assert isinstance(out[0].quantity, Decimal)


# ---------------------------------------------------------------------------
# CoinbaseEodFetcher.fetch_snapshot
# ---------------------------------------------------------------------------


class TestFetchSnapshotHappyPath:
    async def test_snapshot_shape(self) -> None:
        client = FakeBrokerClient(
            positions=[_pos(product_id=_BTC, contracts="2"), _pos(product_id=_ETH, contracts="-4")],
            summary=_summary(total="100000", unrealized="150"),
            fills=[_fill()],
        )
        snap = await _fetcher(client).fetch_snapshot()
        assert isinstance(snap, CoinbaseReconSnapshot)
        assert snap.pulled_at_utc == _PULLED_AT
        assert snap.positions == (
            ReconPosition(market="BTC", quantity=Decimal("2")),
            ReconPosition(market="ETH", quantity=Decimal("-4")),
        )
        assert snap.product_to_asset == _MAP
        assert len(snap.position_details) == 2
        assert snap.cash_usd == Decimal("100000")
        assert snap.net_liquidation_usd == Decimal("100150")
        assert len(snap.fills) == 1
        assert snap.balance_summary.initial_margin == Decimal("5000")

    async def test_decimal_boundaries_exact(self) -> None:
        # [A05]: NLV derivation is exact Decimal arithmetic.
        client = FakeBrokerClient(
            summary=_summary(total="12345.6789", unrealized="-45.6700"),
        )
        snap = await _fetcher(client).fetch_snapshot()
        assert snap.cash_usd == Decimal("12345.6789")
        assert snap.net_liquidation_usd == Decimal("12300.0089")
        assert isinstance(snap.net_liquidation_usd, Decimal)

    async def test_fills_lookback_window_applied(self) -> None:
        client = FakeBrokerClient()
        await _fetcher(client).fetch_snapshot()
        assert client.list_fills_calls == [
            _PULLED_AT - timedelta(hours=DEFAULT_FILLS_LOOKBACK_HOURS)
        ]

    async def test_custom_fills_lookback(self) -> None:
        client = FakeBrokerClient()
        await _fetcher(client, fills_lookback_hours=48).fetch_snapshot()
        assert client.list_fills_calls == [_PULLED_AT - timedelta(hours=48)]

    async def test_empty_book_is_clean_snapshot(self) -> None:
        # No positions + no fills is a NORMAL state (flat book), not an
        # error — the recon planner reconciles cash/NAV regardless.
        client = FakeBrokerClient(positions=[], fills=[])
        snap = await _fetcher(client).fetch_snapshot()
        assert snap.positions == ()
        assert snap.position_details == ()
        assert snap.fills == ()
        assert snap.cash_usd == Decimal("100000")


class TestFetchSnapshotUnrealizedFallback:
    async def test_missing_unrealized_with_open_positions_warns(self) -> None:
        client = FakeBrokerClient(
            positions=[_pos(contracts="1")],
            summary=_summary(total="50000", unrealized=None),
        )
        with structlog.testing.capture_logs() as captured:
            snap = await _fetcher(client).fetch_snapshot()
        assert snap.net_liquidation_usd == Decimal("50000")
        events = [c.get("event") for c in captured]
        assert "coinbase_recon_unrealized_pnl_missing_with_open_positions" in events

    async def test_missing_unrealized_flat_book_no_warning(self) -> None:
        client = FakeBrokerClient(
            positions=[],
            summary=_summary(total="50000", unrealized=None),
        )
        with structlog.testing.capture_logs() as captured:
            snap = await _fetcher(client).fetch_snapshot()
        assert snap.net_liquidation_usd == Decimal("50000")
        events = [c.get("event") for c in captured]
        assert "coinbase_recon_unrealized_pnl_missing_with_open_positions" not in events


class TestFetchSnapshotErrorPaths:
    async def test_positions_transport_failure_raises(self) -> None:
        client = FakeBrokerClient(positions_error=_broker_error("list_positions"))
        with pytest.raises(CoinbaseReconFetchError) as excinfo:
            await _fetcher(client).fetch_snapshot()
        assert excinfo.value.operation == "list_positions"
        assert excinfo.value.detail == "boom"

    async def test_balance_summary_transport_failure_raises(self) -> None:
        client = FakeBrokerClient(summary_error=_broker_error("get_futures_balance_summary"))
        with pytest.raises(CoinbaseReconFetchError) as excinfo:
            await _fetcher(client).fetch_snapshot()
        assert excinfo.value.operation == "get_futures_balance_summary"

    async def test_missing_total_usd_balance_fails_safe(self) -> None:
        # No equity anchor → refuse; never reconcile against fabricated 0.
        client = FakeBrokerClient(summary=_summary(total=None))
        with pytest.raises(CoinbaseReconFetchError) as excinfo:
            await _fetcher(client).fetch_snapshot()
        assert excinfo.value.operation == "get_futures_balance_summary"
        assert "total_usd_balance" in excinfo.value.detail

    async def test_product_discovery_failure_raises(self) -> None:
        # Discovery is load-bearing post-2026-07-11: without the map,
        # every open position would fabricate a phantom break pair.
        client = FakeBrokerClient(
            positions=[_pos(contracts="1")],
            products_error=_broker_error("list_perp_products"),
        )
        with pytest.raises(CoinbaseReconFetchError) as excinfo:
            await _fetcher(client).fetch_snapshot()
        assert excinfo.value.operation == "list_perp_products"

    async def test_discovery_maps_no_open_position_fails_cycle(self) -> None:
        # The #358 zero-products defect class: positions exist but the
        # discovery map resolves none of them — soft-fail the cycle
        # instead of reconciling phantom market keys (which auto-halts).
        client = FakeBrokerClient(positions=[_pos(contracts="2")], products=[])
        with pytest.raises(CoinbaseReconFetchError) as excinfo:
            await _fetcher(client).fetch_snapshot()
        assert excinfo.value.operation == "list_perp_products"
        assert "phantom" in excinfo.value.detail

    async def test_flat_book_with_empty_discovery_is_clean(self) -> None:
        # No positions -> nothing to normalize; an empty discovery result
        # must not fail the cash/NAV reconciliation.
        client = FakeBrokerClient(positions=[], products=[])
        snap = await _fetcher(client).fetch_snapshot()
        assert snap.positions == ()
        assert snap.product_to_asset == {}

    async def test_fills_failure_degrades_not_fatal(self) -> None:
        client = FakeBrokerClient(
            positions=[_pos(contracts="1")],
            fills_error=_broker_error("list_fills"),
        )
        with structlog.testing.capture_logs() as captured:
            snap = await _fetcher(client).fetch_snapshot()
        assert snap.fills == ()
        assert snap.positions != ()  # rest of the snapshot intact
        events = [c.get("event") for c in captured]
        assert "coinbase_recon_fills_fetch_degraded" in events

    async def test_naive_clock_rejected(self) -> None:
        client = FakeBrokerClient()
        fetcher = CoinbaseEodFetcher(
            client=client,  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 7, 10, 0, 15),  # naive
        )
        with pytest.raises(ValueError, match="tz-aware"):
            await fetcher.fetch_snapshot()

    def test_non_positive_lookback_rejected(self) -> None:
        client = FakeBrokerClient()
        with pytest.raises(ValueError, match="fills_lookback_hours"):
            CoinbaseEodFetcher(
                client=client,  # type: ignore[arg-type]
                fills_lookback_hours=0,
            )


class TestModuleContract:
    def test_all_exports_present(self) -> None:
        from services.reconciliation import coinbase_fetcher as mod

        for name in mod.__all__:
            assert hasattr(mod, name), f"__all__ contains {name!r} but module lacks it"

    def test_eod_cycle_reexports_recon_position(self) -> None:
        # The positions_override seam's docs point consumers at
        # eod_cycle.ReconPosition; it must be the SAME class object.
        from services.reconciliation import eod_cycle

        assert eod_cycle.ReconPosition is ReconPosition

    def test_default_lookback_constant(self) -> None:
        # One EOD spacing (24h) + 2h cushion.
        assert DEFAULT_FILLS_LOOKBACK_HOURS == 26
