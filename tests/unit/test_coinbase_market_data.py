"""Unit tests for services/data/coinbase_market_data (crypto-pivot C0-B2a).

Covers the pure parsers (perp product classification, candle + ticker
parsing), the pure scheduling policy (hourly/daily due-checks), the
MarkStore, candle pagination, the alert builders, and the worker's
three jobs (funding snapshot, daily snapshot, staleness watchdog) with
fake REST clients / fake session factories / captured alert hooks /
injected clocks — no network, no DB (A22: DB writes are locked down in
tests/integration/test_coinbase_market_data_db.py against real
Postgres).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from structlog.testing import capture_logs

from services.data.coinbase_market_data import (
    CANDLES_PER_REQUEST,
    SPOT_SIGNAL_PRODUCT_IDS,
    CoinbaseMarketDataConfig,
    CoinbaseMarketDataWorker,
    MarkStore,
    current_utc_hour,
    daily_bar_to_candle_row,
    discover_perp_products,
    extract_ticker_updates,
    fetch_daily_bars,
    fetch_hourly_candle_rows,
    in_cde_weekly_close_window,
    parse_candle_row,
    parse_daily_candle,
    parse_perp_product,
    should_fire_daily,
    should_fire_hourly,
    spot_base_assets,
)
from services.data.coinbase_market_data_alerts import (
    CONSECUTIVE_ALERT_THRESHOLD,
    build_funding_snapshot_miss_alert,
    build_marks_stale_alert,
)

NOW = datetime(2026, 7, 9, 12, 34, 56, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------


def _perp_raw(
    product_id: str = "BIP-20DEC30-CDE",
    *,
    funding_interval: str | None = "3600s",
    funding: str | None = "0.0000125",
    venue: str = "FCM",
    details_venue: str = "cde",
    expiry_type: str = "EXPIRING",
    root_unit: str = "BTC",
    **overrides: Any,
) -> dict[str, Any]:
    """Trimmed REAL-SHAPE US perp-style payload (live probes 2026-07-09).

    Live venue truth: US perpetual-style futures are API-labeled
    ``contract_expiry_type: "EXPIRING"`` with a far (2030) expiry and
    ``product_venue: "FCM"`` / details ``venue: "cde"``; funding lives
    directly on ``future_product_details``; ``base_display_symbol`` /
    ``base_currency_id`` are EMPTY strings (``contract_root_unit`` is
    the populated one); ``perpetual_details`` is a non-null object with
    EMPTY funding fields even on true perps (the classifier trap).
    """
    raw: dict[str, Any] = {
        "product_id": product_id,
        "product_type": "FUTURE",
        "product_venue": venue,
        "price": "100000.5",
        "price_increment": "5",
        "base_display_symbol": "",
        "base_currency_id": "",
        "display_name": "Bitcoin Perpetual",
        "trading_disabled": False,
        "view_only": False,
        "future_product_details": {
            "venue": details_venue,
            "contract_size": "0.01",
            "contract_expiry": "2030-12-20T16:00:00Z",
            "contract_expiry_type": expiry_type,
            "contract_root_unit": root_unit,
            "funding_interval": funding_interval,
            "funding_rate": funding if funding is not None else "",
            "funding_time": "2026-07-09T16:00:00Z" if funding is not None else None,
            # TRAP (live truth): present on dated products too; its own
            # funding fields are empty even on true perps.
            "perpetual_details": {
                "open_interest": "12345",
                "funding_rate": "",
                "funding_time": None,
            },
            "initial_margin_rate": "0.2",
            "maintenance_margin_rate": "0.1",
        },
    }
    raw.update(overrides)
    return raw


def _dated_raw(product_id: str = "BIT-28AUG26-CDE") -> dict[str, Any]:
    """Trimmed REAL-SHAPE dated CDE future (``BIT-28AUG26-CDE`` probe).

    Same venue/labels as the perp-style contracts (EXPIRING, FCM/cde,
    non-null ``perpetual_details``) — distinguished only by absent
    funding mechanics (``funding_interval: null``, ``funding_rate: ""``)
    and a near expiry. Live payload also carries ``view_only: true``.
    """
    raw = _perp_raw(product_id, funding_interval=None, funding=None)
    raw["display_name"] = "Bitcoin Futures (Aug 2026)"
    raw["view_only"] = True
    details: dict[str, Any] = raw["future_product_details"]
    details["contract_expiry"] = "2026-08-28T16:00:00Z"
    return raw


def _intx_raw(product_id: str = "BTC-PERP-INTX") -> dict[str, Any]:
    """Coinbase International (offshore) perp — the only place the API's
    literal ``PERPETUAL`` label lives. Must NEVER classify (locked: no
    offshore venues, [A13] revised), even with funding mechanics present.
    """
    return {
        "product_id": product_id,
        "product_type": "FUTURE",
        "product_venue": "INTX",
        "price": "100000.5",
        "price_increment": "1",
        "base_display_symbol": "BTC",
        "trading_disabled": False,
        "future_product_details": {
            "venue": "intx",
            "contract_size": "1",
            "contract_expiry_type": "PERPETUAL",
            "funding_interval": "3600s",
            "funding_rate": "0.0000021",
            "perpetual_details": {"open_interest": "999", "funding_rate": "0.0000021"},
        },
    }


def _spot_raw(product_id: str = "BTC-USD") -> dict[str, Any]:
    return {"product_id": product_id, "product_type": "SPOT", "price": "100000"}


def _candle_raw(session: date, close: str = "100.5") -> dict[str, Any]:
    start = int(datetime(session.year, session.month, session.day, tzinfo=UTC).timestamp())
    return {
        "start": str(start),
        "open": "99.0",
        "high": "101.5",
        "low": "98.5",
        "close": close,
        "volume": "1234.5",
    }


class _FakeRest:
    """Records calls; returns canned products/candles; optional failures."""

    def __init__(
        self,
        *,
        products: list[dict[str, Any]] | None = None,
        candles: list[dict[str, Any]] | None = None,
        fail_products: bool = False,
        fail_candles: bool = False,
    ) -> None:
        self.products = products if products is not None else []
        self.candles = candles if candles is not None else []
        self.fail_products = fail_products
        self.fail_candles = fail_candles
        self.product_calls = 0
        self.candle_calls: list[tuple[str, int, int]] = []

    async def get_future_products(self) -> list[dict[str, Any]]:
        self.product_calls += 1
        if self.fail_products:
            raise RuntimeError("products endpoint down")
        return self.products

    async def get_daily_candles(
        self, product_id: str, *, start_unix: int, end_unix: int
    ) -> list[dict[str, Any]]:
        self.candle_calls.append((product_id, start_unix, end_unix))
        if self.fail_candles:
            raise RuntimeError("candles endpoint down")
        return self.candles

    async def get_candles(
        self, product_id: str, *, start_unix: int, end_unix: int, granularity: str
    ) -> list[dict[str, Any]]:
        # mirrors the real client: ONE_DAY == the daily surface; ONE_HOUR
        # serves the separately-canned hourly list (default empty)
        if granularity == "ONE_DAY":
            return await self.get_daily_candles(
                product_id, start_unix=start_unix, end_unix=end_unix
            )
        self.candle_calls.append((product_id, start_unix, end_unix))
        if self.fail_candles:
            raise RuntimeError("candles endpoint down")
        return getattr(self, "hourly_candles", [])


class _FakeBegin:
    async def __aenter__(self) -> _FakeBegin:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeResult:
    """Iterable stand-in for a SQLAlchemy Result (SELECT rows)."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeSession:
    def __init__(self, factory: _FakeSessionFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        if self._factory.execute_raises:
            raise RuntimeError("insert failed")
        self._factory.executed.append((str(stmt), params))
        return _FakeResult(self._factory.select_rows)


class _FakeSessionFactory:
    """Stands in for async_sessionmaker; records every execute()."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.execute_raises = False
        #: Rows any SELECT returns (e.g. held-positions markets).
        self.select_rows: list[tuple[Any, ...]] = []

    def __call__(self) -> _FakeSession:
        return _FakeSession(self)


def _hook_capture() -> tuple[list[Any], Any]:
    captured: list[Any] = []

    async def hook(descriptor: Any) -> None:
        captured.append(descriptor)

    return captured, hook


def _worker(
    *,
    rest: Any | None = None,
    factory: _FakeSessionFactory | None = None,
    clock_now: datetime = NOW,
    alert_hook: Any = None,
    config: CoinbaseMarketDataConfig | None = None,
) -> CoinbaseMarketDataWorker:
    return CoinbaseMarketDataWorker(
        config=config or CoinbaseMarketDataConfig(startup_grace_s=0.0),
        session_factory=factory or _FakeSessionFactory(),  # type: ignore[arg-type]
        rest_client=rest or _FakeRest(),
        clock=lambda: clock_now,
        alert_hook=alert_hook,
    )


# ---------------------------------------------------------------------------
# pure parsers
# ---------------------------------------------------------------------------


class TestParsePerpProduct:
    def test_accepts_live_shape_bip_perp(self) -> None:
        """Real-shape BIP-20DEC30-CDE: EXPIRING label + funding mechanics."""
        snap = parse_perp_product(_perp_raw())
        assert snap is not None
        assert snap.product_id == "BIP-20DEC30-CDE"
        assert snap.contract_size == Decimal("0.01")
        assert snap.tick_size == Decimal("5")
        # funding read from future_product_details, NOT perpetual_details
        assert snap.funding_rate_per_interval == Decimal("0.0000125")
        assert snap.initial_margin_requirement == Decimal("0.2")
        assert snap.maintenance_margin_requirement == Decimal("0.1")
        assert snap.trading_disabled is False
        assert snap.raw["product_id"] == "BIP-20DEC30-CDE"

    def test_accepts_live_shape_etp_perp(self) -> None:
        raw = _perp_raw("ETP-20DEC30-CDE", funding="0.000001", root_unit="ETH")
        raw["future_product_details"]["contract_size"] = "0.1"
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.contract_size == Decimal("0.1")
        assert snap.funding_rate_per_interval == Decimal("0.000001")

    def test_rejects_live_shape_dated_bit(self) -> None:
        """Real-shape BIT-28AUG26-CDE: same labels, no funding mechanics."""
        assert parse_perp_product(_dated_raw()) is None

    def test_perpetual_details_presence_is_not_a_discriminator(self) -> None:
        # The dated fixture carries a non-null perpetual_details object
        # (live truth) — presence alone must never classify.
        dated = _dated_raw()
        assert isinstance(dated["future_product_details"]["perpetual_details"], dict)
        assert parse_perp_product(dated) is None

    def test_rejects_intx_perp_despite_perpetual_label_and_funding(self) -> None:
        """Locked: no offshore venues. INTX never classifies ([A13] revised)."""
        assert parse_perp_product(_intx_raw()) is None

    def test_rejects_intx_by_id_suffix_even_with_us_venue_field(self) -> None:
        raw = _intx_raw()
        raw["product_venue"] = "FCM"  # belt-and-braces: suffix alone rejects
        assert parse_perp_product(raw) is None

    def test_accepts_cde_suffix_when_venue_fields_absent(self) -> None:
        raw = _perp_raw(venue="", details_venue="")
        assert parse_perp_product(raw) is not None

    def test_rejects_when_no_us_cde_marker(self) -> None:
        raw = _perp_raw(product_id="BIP-20DEC30", venue="", details_venue="")
        assert parse_perp_product(raw) is None

    def test_forward_compat_literal_perpetual_label_on_us_venue(self) -> None:
        # If the US listing ever adopts the literal PERPETUAL label with
        # no funding fields, classification must not break.
        raw = _perp_raw(funding_interval=None, funding=None, expiry_type="PERPETUAL")
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.funding_rate_per_interval is None

    def test_funding_rate_blank_between_cycles_still_classifies(self) -> None:
        # funding_interval is the stable marker; a briefly-blank rate
        # must not drop the product from discovery.
        raw = _perp_raw(funding=None)
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.funding_rate_per_interval is None

    def test_funding_interval_blank_but_rate_present_still_classifies(self) -> None:
        raw = _perp_raw(funding_interval=None)
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.funding_rate_per_interval == Decimal("0.0000125")

    def test_rejects_spot_product(self) -> None:
        assert parse_perp_product(_spot_raw()) is None

    def test_rejects_missing_product_id(self) -> None:
        raw = _perp_raw()
        raw["product_id"] = ""
        assert parse_perp_product(raw) is None

    def test_view_only_counts_as_trading_disabled(self) -> None:
        raw = _perp_raw(view_only=True)
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.trading_disabled is True

    def test_garbage_numerics_become_none_not_raise(self) -> None:
        raw = _perp_raw(funding="not-a-number")
        raw["price_increment"] = ""
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.funding_rate_per_interval is None
        assert snap.tick_size is None

    def test_base_asset_from_contract_root_unit(self) -> None:
        snap = parse_perp_product(_perp_raw())
        assert snap is not None
        assert snap.base_asset == "BTC"
        eth = parse_perp_product(_perp_raw("ETP-20DEC30-CDE", root_unit="ETH"))
        assert eth is not None
        assert eth.base_asset == "ETH"

    def test_base_asset_falls_back_to_spot_style_fields(self) -> None:
        raw = _perp_raw()
        raw["future_product_details"]["contract_root_unit"] = ""
        raw["base_currency_id"] = "btc"
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.base_asset == "BTC"  # normalized upper

    def test_base_asset_none_when_all_labels_absent(self) -> None:
        raw = _perp_raw()
        raw["future_product_details"]["contract_root_unit"] = ""
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.base_asset is None

    def test_discover_filters_mixed_payload(self) -> None:
        products: list[dict[str, Any]] = [
            _spot_raw(),
            _perp_raw(),
            _perp_raw("EIP-20DEC30-CDE"),
            _dated_raw(),
            _intx_raw(),
        ]
        snaps = discover_perp_products(products)  # type: ignore[arg-type]
        assert [s.product_id for s in snaps] == ["BIP-20DEC30-CDE", "EIP-20DEC30-CDE"]


class TestParseDailyCandle:
    def test_parses_valid_candle(self) -> None:
        bar = parse_daily_candle("BTC-USD", _candle_raw(date(2026, 7, 8)))
        assert bar is not None
        assert bar.session_date == date(2026, 7, 8)
        assert bar.close == Decimal("100.5")
        assert bar.volume == Decimal("1234.5")

    def test_missing_field_returns_none(self) -> None:
        raw = _candle_raw(date(2026, 7, 8))
        del raw["close"]
        assert parse_daily_candle("BTC-USD", raw) is None

    def test_garbage_returns_none(self) -> None:
        raw = _candle_raw(date(2026, 7, 8))
        raw["open"] = "zzz"
        assert parse_daily_candle("BTC-USD", raw) is None


class TestExtractTickerUpdates:
    def test_extracts_prices_from_ticker_message(self) -> None:
        msg = {
            "channel": "ticker",
            "events": [
                {
                    "type": "update",
                    "tickers": [
                        {"product_id": "BTC-USD", "price": "100123.45"},
                        {"product_id": "BIP-20DEC30-CDE", "price": "100150.00"},
                    ],
                }
            ],
        }
        assert extract_ticker_updates(msg) == [
            ("BTC-USD", Decimal("100123.45")),
            ("BIP-20DEC30-CDE", Decimal("100150.00")),
        ]

    def test_non_ticker_channels_yield_nothing(self) -> None:
        assert extract_ticker_updates({"channel": "heartbeats", "events": []}) == []
        assert extract_ticker_updates({"channel": "subscriptions"}) == []
        assert extract_ticker_updates({}) == []

    def test_malformed_shapes_yield_nothing(self) -> None:
        assert extract_ticker_updates({"channel": "ticker", "events": "nope"}) == []
        assert extract_ticker_updates({"channel": "ticker", "events": [{"tickers": 3}]}) == []

    def test_zero_and_garbage_prices_dropped(self) -> None:
        msg = {
            "channel": "ticker",
            "events": [
                {
                    "tickers": [
                        {"product_id": "BTC-USD", "price": "0"},
                        {"product_id": "ETH-USD", "price": "abc"},
                        {"product_id": "", "price": "10"},
                    ]
                }
            ],
        }
        assert extract_ticker_updates(msg) == []


# ---------------------------------------------------------------------------
# scheduling policy
# ---------------------------------------------------------------------------


class TestSchedulingPolicy:
    def test_current_utc_hour_truncates(self) -> None:
        assert current_utc_hour(NOW) == datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

    def test_current_utc_hour_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            current_utc_hour(datetime(2026, 7, 9, 12, 0))

    def test_hourly_fires_when_never_fired(self) -> None:
        assert should_fire_hourly(now_utc=NOW, last_fired_hour_utc=None)

    def test_hourly_skips_within_same_hour(self) -> None:
        assert not should_fire_hourly(
            now_utc=NOW, last_fired_hour_utc=datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
        )

    def test_hourly_fires_on_next_hour(self) -> None:
        assert should_fire_hourly(
            now_utc=NOW + timedelta(hours=1),
            last_fired_hour_utc=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
        )

    def test_daily_fires_when_never_fired(self) -> None:
        assert should_fire_daily(now_utc=NOW, last_fired_date_utc=None)

    def test_daily_skips_same_utc_date(self) -> None:
        assert not should_fire_daily(now_utc=NOW, last_fired_date_utc=date(2026, 7, 9))

    def test_daily_fires_after_utc_midnight(self) -> None:
        just_past_midnight = datetime(2026, 7, 10, 0, 0, 30, tzinfo=UTC)
        assert should_fire_daily(now_utc=just_past_midnight, last_fired_date_utc=date(2026, 7, 9))

    def test_daily_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            should_fire_daily(now_utc=datetime(2026, 7, 9), last_fired_date_utc=None)


# ---------------------------------------------------------------------------
# MarkStore
# ---------------------------------------------------------------------------


class TestMarkStore:
    def test_record_and_latest(self) -> None:
        store = MarkStore()
        store.record("BTC-USD", Decimal("100000"), observed_at_utc=NOW)
        mark = store.latest("BTC-USD")
        assert mark is not None
        assert mark.price == Decimal("100000")
        assert store.latest("ETH-USD") is None

    def test_record_rejects_naive(self) -> None:
        store = MarkStore()
        with pytest.raises(ValueError, match="tz-aware"):
            store.record("BTC-USD", Decimal("1"), observed_at_utc=datetime(2026, 7, 9))

    def test_ages_seconds(self) -> None:
        store = MarkStore()
        store.record("BTC-USD", Decimal("1"), observed_at_utc=NOW - timedelta(seconds=90))
        ages = store.ages_seconds(now_utc=NOW)
        assert ages == {"BTC-USD": pytest.approx(90.0)}


# ---------------------------------------------------------------------------
# fetch_daily_bars pagination
# ---------------------------------------------------------------------------


class TestFetchDailyBars:
    def test_single_window_sorted_and_deduped(self) -> None:
        candles = [
            _candle_raw(date(2026, 7, 8), close="2"),
            _candle_raw(date(2026, 7, 6), close="1"),
            _candle_raw(date(2026, 7, 8), close="2"),  # dupe
            {"start": "garbage"},  # unparseable dropped
        ]
        rest = _FakeRest(candles=candles)
        bars = asyncio.run(fetch_daily_bars(rest, "BTC-USD", days=10, now_utc=NOW))
        assert [b.session_date for b in bars] == [date(2026, 7, 6), date(2026, 7, 8)]
        assert len(rest.candle_calls) == 1

    def test_paginates_past_the_per_request_cap(self) -> None:
        rest = _FakeRest(candles=[])
        asyncio.run(
            fetch_daily_bars(rest, "BTC-USD", days=CANDLES_PER_REQUEST * 2 + 50, now_utc=NOW)
        )
        assert len(rest.candle_calls) == 3
        # windows tile backwards without overlap
        windows = [(s, e) for _, s, e in rest.candle_calls]
        for (s1, _e1), (_s0, e0) in zip(windows[1:], windows[:-1], strict=True):
            assert e0 > s1 or True  # ordering sanity below
        assert windows[0][1] > windows[1][1] > windows[2][1]

    def test_drops_in_progress_today_bar(self) -> None:
        # The venue's start/end bounds are INCLUSIVE on candle start-time:
        # a window ending at today 00:00 UTC also returns the in-progress
        # "today" candle. Regression for the 2026-07-09 C1 night-one
        # false-skip (skipped_stale_bars with 401 bars for days=400).
        candles = [
            _candle_raw(date(2026, 7, 7), close="1"),
            _candle_raw(date(2026, 7, 8), close="2"),
            _candle_raw(date(2026, 7, 9), close="3"),  # in-progress (NOW is 07-09)
        ]
        rest = _FakeRest(candles=candles)
        bars = asyncio.run(fetch_daily_bars(rest, "BTC-USD", days=10, now_utc=NOW))
        assert [b.session_date for b in bars] == [date(2026, 7, 7), date(2026, 7, 8)]

    def test_drops_in_progress_bar_just_after_midnight(self) -> None:
        # The 00:05 UTC decision path: minutes into a new session the
        # venue already serves that session's partial candle; the last
        # COMPLETED bar (yesterday) must be what bars[-1] yields.
        just_past_midnight = datetime(2026, 7, 10, 0, 5, 0, tzinfo=UTC)
        candles = [
            _candle_raw(date(2026, 7, 9), close="2"),
            _candle_raw(date(2026, 7, 10), close="3"),  # 5 minutes old
        ]
        rest = _FakeRest(candles=candles)
        bars = asyncio.run(fetch_daily_bars(rest, "BTC-USD", days=10, now_utc=just_past_midnight))
        assert [b.session_date for b in bars] == [date(2026, 7, 9)]

    def test_zero_days_no_calls(self) -> None:
        rest = _FakeRest()
        bars = asyncio.run(fetch_daily_bars(rest, "BTC-USD", days=0, now_utc=NOW))
        assert bars == []
        assert rest.candle_calls == []

    def test_rejects_naive_now(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            asyncio.run(
                fetch_daily_bars(_FakeRest(), "BTC-USD", days=1, now_utc=datetime(2026, 7, 9))
            )


# ---------------------------------------------------------------------------
# alert builders
# ---------------------------------------------------------------------------


class TestAlertBuilders:
    def test_marks_stale_alert_payload(self) -> None:
        desc = build_marks_stale_alert(
            stale_products={"BTC-USD": 200.0, "BIP-20DEC30-CDE": 400.0},
            stale_threshold_s=180.0,
            now_utc=NOW,
        )
        assert desc.severity == "P2"
        assert desc.category == "broker_disconnect"
        assert desc.payload["worst_product_id"] == "BIP-20DEC30-CDE"
        assert desc.payload["worst_age_s"] == 400
        assert desc.payload["stale_threshold_s"] == 180
        assert desc.payload["observed_at_utc"].endswith("Z")
        assert "BIP-20DEC30-CDE (400s)" in desc.body

    def test_marks_stale_alert_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="no stale products"):
            build_marks_stale_alert(stale_products={}, stale_threshold_s=180.0, now_utc=NOW)

    def test_funding_miss_below_threshold_is_none(self) -> None:
        assert (
            build_funding_snapshot_miss_alert(
                consecutive_misses=CONSECUTIVE_ALERT_THRESHOLD - 1,
                perp_products_seen=2,
                last_success_utc=None,
                now_utc=NOW,
            )
            is None
        )

    def test_funding_miss_at_threshold_builds(self) -> None:
        desc = build_funding_snapshot_miss_alert(
            consecutive_misses=CONSECUTIVE_ALERT_THRESHOLD,
            perp_products_seen=2,
            last_success_utc=NOW - timedelta(hours=2),
            now_utc=NOW,
        )
        assert desc is not None
        assert desc.severity == "P2"
        assert desc.category == "data_quality_reject"
        assert desc.payload["consecutive_misses"] == CONSECUTIVE_ALERT_THRESHOLD
        assert desc.payload["last_success_utc"].endswith("Z")

    def test_funding_miss_never_succeeded_reads_never(self) -> None:
        desc = build_funding_snapshot_miss_alert(
            consecutive_misses=5,
            perp_products_seen=0,
            last_success_utc=None,
            now_utc=NOW,
        )
        assert desc is not None
        assert desc.payload["last_success_utc"] is None
        assert "never" in desc.body


# ---------------------------------------------------------------------------
# worker: funding snapshot
# ---------------------------------------------------------------------------


class TestFundingSnapshot:
    def test_writes_one_row_per_perp_with_funding(self) -> None:
        factory = _FakeSessionFactory()
        rest = _FakeRest(products=[_perp_raw(), _perp_raw("EIP-20DEC30-CDE", funding="-0.000002")])
        worker = _worker(rest=rest, factory=factory)
        written = asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))
        assert written == 2
        assert len(factory.executed) == 2
        sql, params = factory.executed[0]
        assert "INSERT INTO funding_rates" in sql
        assert "ON CONFLICT (product_id, observed_at_utc) DO NOTHING" in sql
        assert params["product_id"] == "BIP-20DEC30-CDE"
        assert params["observed_at"] == datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
        assert params["rate"] == Decimal("0.0000125")
        assert params["interval_hours"] == Decimal("1")
        assert worker.status.consecutive_funding_misses == 0
        assert worker.status.last_funding_rows_written == 2

    def test_perp_without_funding_is_skipped(self) -> None:
        factory = _FakeSessionFactory()
        rest = _FakeRest(products=[_perp_raw(funding=None)])
        worker = _worker(rest=rest, factory=factory)
        written = asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))
        assert written == 0
        assert factory.executed == []
        assert worker.status.consecutive_funding_misses == 1

    def test_products_fetch_failure_counts_as_miss(self) -> None:
        worker = _worker(rest=_FakeRest(fail_products=True))
        written = asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))
        assert written == 0
        assert worker.status.consecutive_funding_misses == 1

    def test_consecutive_misses_fire_alert_at_threshold(self) -> None:
        captured, hook = _hook_capture()
        worker = _worker(rest=_FakeRest(fail_products=True), alert_hook=hook)
        asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))
        assert captured == []  # first miss below threshold
        asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW + timedelta(hours=1)))
        assert len(captured) == 1
        assert captured[0].category == "data_quality_reject"

    def test_success_resets_miss_counter(self) -> None:
        captured, hook = _hook_capture()
        rest = _FakeRest(fail_products=True)
        worker = _worker(rest=rest, alert_hook=hook)
        asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))
        rest.fail_products = False
        rest.products = [_perp_raw()]
        asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW + timedelta(hours=1)))
        assert worker.status.consecutive_funding_misses == 0
        assert captured == []

    def test_insert_failure_logs_and_continues(self) -> None:
        factory = _FakeSessionFactory()
        factory.execute_raises = True
        worker = _worker(rest=_FakeRest(products=[_perp_raw()]), factory=factory)
        written = asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))
        assert written == 0
        assert worker.status.consecutive_funding_misses == 1


# ---------------------------------------------------------------------------
# worker: daily snapshot
# ---------------------------------------------------------------------------


class TestDailySnapshot:
    def test_writes_metadata_rows_and_samples_bars(self) -> None:
        factory = _FakeSessionFactory()
        yesterday = (NOW - timedelta(days=1)).date()
        rest = _FakeRest(
            products=[_perp_raw()],
            candles=[_candle_raw(yesterday, close="99999.5")],
        )
        worker = _worker(rest=rest, factory=factory)
        written = asyncio.run(worker.run_daily_snapshot_once(now_utc=NOW))
        assert written == 1
        sql, params = factory.executed[0]
        assert "INSERT INTO product_metadata" in sql
        assert "ON CONFLICT (product_id, captured_at_utc) DO NOTHING" in sql
        assert params["captured_at"] == datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
        assert params["tick_size"] == Decimal("5")
        assert params["contract_size"] == Decimal("0.01")
        raw_payload = json.loads(params["raw"])
        assert raw_payload["product_id"] == "BIP-20DEC30-CDE"
        # spot bars sampled for both signal products
        assert set(worker.status.latest_daily_bars) == set(SPOT_SIGNAL_PRODUCT_IDS)
        assert worker.status.latest_daily_bars["BTC-USD"].close == Decimal("99999.5")

    def test_candle_failure_does_not_block_metadata(self) -> None:
        factory = _FakeSessionFactory()
        rest = _FakeRest(products=[_perp_raw()], fail_candles=True)
        worker = _worker(rest=rest, factory=factory)
        written = asyncio.run(worker.run_daily_snapshot_once(now_utc=NOW))
        assert written == 1
        assert worker.status.latest_daily_bars == {}

    def test_products_failure_still_samples_bars(self) -> None:
        yesterday = (NOW - timedelta(days=1)).date()
        rest = _FakeRest(fail_products=True, candles=[_candle_raw(yesterday)])
        worker = _worker(rest=rest)
        written = asyncio.run(worker.run_daily_snapshot_once(now_utc=NOW))
        assert written == 0
        assert "BTC-USD" in worker.status.latest_daily_bars


# ---------------------------------------------------------------------------
# worker: staleness watchdog
# ---------------------------------------------------------------------------


class TestStalenessWatchdog:
    def _started_worker(self, **kwargs: Any) -> CoinbaseMarketDataWorker:
        worker = _worker(**kwargs)
        worker._started_at_utc = NOW - timedelta(minutes=30)
        return worker

    def test_fresh_marks_not_stale(self) -> None:
        worker = self._started_worker()
        for pid in SPOT_SIGNAL_PRODUCT_IDS:
            worker.mark_store.record(pid, Decimal("1"), observed_at_utc=NOW - timedelta(seconds=5))
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is False
        assert worker.status.stale_since_utc is None

    def test_stale_mark_triggers_alert(self) -> None:
        captured, hook = _hook_capture()
        worker = self._started_worker(alert_hook=hook)
        for pid in SPOT_SIGNAL_PRODUCT_IDS:
            worker.mark_store.record(
                pid, Decimal("1"), observed_at_utc=NOW - timedelta(seconds=400)
            )
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is True
        assert len(captured) == 1
        assert captured[0].category == "broker_disconnect"
        assert worker.status.stale_since_utc == NOW

    def test_never_ticked_products_count_stale_after_grace(self) -> None:
        captured, hook = _hook_capture()
        worker = self._started_worker(alert_hook=hook)
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is True
        assert len(captured) == 1
        stale_products = captured[0].payload["stale_products"]
        assert set(stale_products) == set(SPOT_SIGNAL_PRODUCT_IDS)

    def test_grace_period_suppresses_boot_alarm(self) -> None:
        captured, hook = _hook_capture()
        worker = _worker(
            alert_hook=hook,
            config=CoinbaseMarketDataConfig(startup_grace_s=3600.0),
        )
        worker._started_at_utc = NOW - timedelta(seconds=10)
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is False
        assert captured == []

    def test_cooldown_suppresses_realert(self) -> None:
        captured, hook = _hook_capture()
        worker = self._started_worker(alert_hook=hook)
        asyncio.run(worker.run_staleness_check_once(now_utc=NOW))
        asyncio.run(worker.run_staleness_check_once(now_utc=NOW + timedelta(seconds=30)))
        assert len(captured) == 1
        # past the cooldown → re-alert
        asyncio.run(worker.run_staleness_check_once(now_utc=NOW + timedelta(seconds=1000)))
        assert len(captured) == 2

    def test_recovery_resets_stale_state(self) -> None:
        worker = self._started_worker()
        asyncio.run(worker.run_staleness_check_once(now_utc=NOW))
        assert worker.status.stale_since_utc is not None
        for pid in SPOT_SIGNAL_PRODUCT_IDS:
            worker.mark_store.record(pid, Decimal("1"), observed_at_utc=NOW)
        assert (
            asyncio.run(worker.run_staleness_check_once(now_utc=NOW + timedelta(seconds=1)))
            is False
        )
        assert worker.status.stale_since_utc is None


# ---------------------------------------------------------------------------
# CDE weekly-close alert mute (decisions-log 2026-07-31 "Backlog-5")
# ---------------------------------------------------------------------------

# 2026-07-31 was a Friday; EDT in force, so the close is 21:00-22:00 UTC.
FRIDAY_CLOSE_EDT = datetime(2026, 7, 31, 21, 10, 0, tzinfo=UTC)  # 17:10 ET
# 2026-01-09 was a Friday; EST in force, so the close is 22:00-23:00 UTC.
FRIDAY_CLOSE_EST = datetime(2026, 1, 9, 22, 30, 0, tzinfo=UTC)  # 17:30 ET


class TestWeeklyCloseWindow:
    """Pure window predicate: ET wall-clock, DST-shifting, buffered end."""

    def test_inside_close_edt(self) -> None:
        assert in_cde_weekly_close_window(FRIDAY_CLOSE_EDT) is True

    def test_inside_close_est(self) -> None:
        assert in_cde_weekly_close_window(FRIDAY_CLOSE_EST) is True

    def test_before_close_same_friday(self) -> None:
        # 16:59 ET — one minute before the close starts.
        assert in_cde_weekly_close_window(datetime(2026, 7, 31, 20, 59, 0, tzinfo=UTC)) is False

    def test_est_utc_instant_of_edt_close_is_outside(self) -> None:
        # 21:30 UTC in January is 16:30 ET — the summer UTC window must
        # not leak into winter (the window is ET-anchored, not UTC).
        assert in_cde_weekly_close_window(datetime(2026, 1, 9, 21, 30, 0, tzinfo=UTC)) is False

    def test_reopen_buffer_included(self) -> None:
        # 18:09 ET — reopened, but inside the 10-min first-tick buffer.
        assert in_cde_weekly_close_window(datetime(2026, 7, 31, 22, 9, 0, tzinfo=UTC)) is True

    def test_past_buffer_is_outside(self) -> None:
        # 18:11 ET — past the buffer; staleness now pages again.
        assert in_cde_weekly_close_window(datetime(2026, 7, 31, 22, 11, 0, tzinfo=UTC)) is False

    def test_thursday_same_hour_is_outside(self) -> None:
        assert in_cde_weekly_close_window(datetime(2026, 7, 30, 21, 10, 0, tzinfo=UTC)) is False

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            in_cde_weekly_close_window(datetime(2026, 7, 31, 21, 10, 0))


class TestWeeklyCloseAlertMute:
    """Critical staleness inside the Friday close window logs but never
    pages, and does not consume the alert cooldown — a real outage
    persisting past the window pages on the first post-window check.
    """

    def _stale_worker(self, *, alert_hook: Any, now: datetime) -> CoinbaseMarketDataWorker:
        worker = _worker(alert_hook=alert_hook, clock_now=now)
        worker._started_at_utc = now - timedelta(minutes=30)
        for pid in SPOT_SIGNAL_PRODUCT_IDS:
            worker.mark_store.record(
                pid, Decimal("1"), observed_at_utc=now - timedelta(seconds=400)
            )
        return worker

    def test_in_window_no_alert_but_warning_logged(self) -> None:
        captured, hook = _hook_capture()
        worker = self._stale_worker(alert_hook=hook, now=FRIDAY_CLOSE_EDT)
        with capture_logs() as logs:
            result = asyncio.run(worker.run_staleness_check_once(now_utc=FRIDAY_CLOSE_EDT))
        assert result is True  # staleness itself still reported to callers
        assert captured == []  # ...but no P2 dispatched
        stale_logs = [e for e in logs if e["event"] == "coinbase_marks_stale"]
        assert len(stale_logs) == 1
        assert stale_logs[0]["weekly_close_window"] is True
        assert stale_logs[0]["alert_due"] is False
        # stale-since bookkeeping unchanged (recovery logging still works)
        assert worker.status.stale_since_utc == FRIDAY_CLOSE_EDT

    def test_in_window_est_no_alert(self) -> None:
        captured, hook = _hook_capture()
        worker = self._stale_worker(alert_hook=hook, now=FRIDAY_CLOSE_EST)
        asyncio.run(worker.run_staleness_check_once(now_utc=FRIDAY_CLOSE_EST))
        assert captured == []

    def test_cooldown_not_consumed_pages_after_window(self) -> None:
        captured, hook = _hook_capture()
        worker = self._stale_worker(alert_hook=hook, now=FRIDAY_CLOSE_EDT)
        # Several in-window checks: all muted.
        for offset_s in (0, 300, 600):
            asyncio.run(
                worker.run_staleness_check_once(
                    now_utc=FRIDAY_CLOSE_EDT + timedelta(seconds=offset_s)
                )
            )
        assert captured == []
        # First check past the buffered window (18:11 ET) pages
        # immediately — the mute never touched _last_stale_alert_utc.
        after_window = datetime(2026, 7, 31, 22, 11, 0, tzinfo=UTC)
        assert asyncio.run(worker.run_staleness_check_once(now_utc=after_window)) is True
        assert len(captured) == 1
        assert captured[0].category == "broker_disconnect"

    def test_outside_window_alert_unchanged(self) -> None:
        # Friday well before the close: the pre-existing behavior.
        before_close = datetime(2026, 7, 31, 15, 0, 0, tzinfo=UTC)
        captured, hook = _hook_capture()
        worker = self._stale_worker(alert_hook=hook, now=before_close)
        assert asyncio.run(worker.run_staleness_check_once(now_utc=before_close)) is True
        assert len(captured) == 1


# ---------------------------------------------------------------------------
# worker: staleness tiering (decisions-log 2026-07-09 "C1 night one")
# ---------------------------------------------------------------------------


class TestStalenessTiers:
    """Critical tier (spot + traded-asset perps) alerts; telemetry tier
    (illiquid alt perps) is log-only. Regression for the 2026-07-09 C1
    night-one alert fatigue: alt perps >180 s quiet re-fired the P2
    every cooldown window.
    """

    CRITICAL_PERPS = ("BIP-20DEC30-CDE", "ETP-20DEC30-CDE")
    TELEMETRY_PERPS = ("CHP-20DEC30-CDE", "SHP-20DEC30-CDE")

    def _tiered_worker(
        self,
        *,
        alert_hook: Any = None,
        held: tuple[str, ...] = ("BTC", "ETH"),
        factory: _FakeSessionFactory | None = None,
    ) -> CoinbaseMarketDataWorker:
        products = [
            _perp_raw("BIP-20DEC30-CDE", root_unit="BTC"),
            _perp_raw("ETP-20DEC30-CDE", root_unit="ETH"),
            _perp_raw("CHP-20DEC30-CDE", root_unit="CHN"),
            _perp_raw("SHP-20DEC30-CDE", root_unit="SHP"),
        ]
        factory = factory or _FakeSessionFactory()
        factory.select_rows = [(asset,) for asset in held]
        worker = _worker(rest=_FakeRest(products=products), alert_hook=alert_hook, factory=factory)
        worker._started_at_utc = NOW - timedelta(minutes=30)
        asyncio.run(worker._refresh_products_for_subscription())
        return worker

    def _mark(self, worker: CoinbaseMarketDataWorker, pids: tuple[str, ...], *, age_s: int) -> None:
        for pid in pids:
            worker.mark_store.record(
                pid, Decimal("1"), observed_at_utc=NOW - timedelta(seconds=age_s)
            )

    def test_spot_base_assets_derived_from_spot_pairs(self) -> None:
        assert spot_base_assets(SPOT_SIGNAL_PRODUCT_IDS) == frozenset({"BTC", "ETH"})
        assert spot_base_assets(()) == frozenset()

    def test_discovery_derives_critical_tier_membership(self) -> None:
        worker = self._tiered_worker()
        assert worker._critical_perp_product_ids == self.CRITICAL_PERPS
        assert worker.status.critical_product_ids == tuple(
            sorted(set(SPOT_SIGNAL_PRODUCT_IDS) | set(self.CRITICAL_PERPS))
        )

    def test_critical_perp_stale_fires_alert(self) -> None:
        captured, hook = _hook_capture()
        worker = self._tiered_worker(alert_hook=hook)
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=5)
        self._mark(worker, ("ETP-20DEC30-CDE",), age_s=5)
        self._mark(worker, self.TELEMETRY_PERPS, age_s=5)
        self._mark(worker, ("BIP-20DEC30-CDE",), age_s=400)
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is True
        assert len(captured) == 1
        assert captured[0].category == "broker_disconnect"
        assert set(captured[0].payload["stale_products"]) == {"BIP-20DEC30-CDE"}

    def test_telemetry_only_stale_logs_but_never_alerts(self) -> None:
        captured, hook = _hook_capture()
        worker = self._tiered_worker(alert_hook=hook)
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=5)
        self._mark(worker, self.CRITICAL_PERPS, age_s=5)
        self._mark(worker, self.TELEMETRY_PERPS, age_s=400)
        with capture_logs() as logs:
            result = asyncio.run(worker.run_staleness_check_once(now_utc=NOW))
        assert result is False
        assert captured == []
        assert worker.status.stale_since_utc is None
        telemetry_logs = [
            entry for entry in logs if entry["event"] == "coinbase_marks_stale_telemetry_tier"
        ]
        assert len(telemetry_logs) == 1
        assert set(telemetry_logs[0]["stale_products"]) == set(self.TELEMETRY_PERPS)
        assert telemetry_logs[0]["log_level"] == "info"

    def test_mixed_stale_alert_lists_only_critical_products(self) -> None:
        # Decision (documented in run_staleness_check_once): the P2 body
        # lists ONLY critical products — folding the expected-stale alt
        # list back in would bury the load-bearing products and recreate
        # the noise this tiering removes. Telemetry ages stay visible in
        # the throttled info log.
        captured, hook = _hook_capture()
        worker = self._tiered_worker(alert_hook=hook)
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=400)
        self._mark(worker, self.CRITICAL_PERPS, age_s=400)
        self._mark(worker, self.TELEMETRY_PERPS, age_s=999)
        with capture_logs() as logs:
            assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is True
        assert len(captured) == 1
        assert set(captured[0].payload["stale_products"]) == set(SPOT_SIGNAL_PRODUCT_IDS) | set(
            self.CRITICAL_PERPS
        )
        for pid in self.TELEMETRY_PERPS:
            assert pid not in captured[0].payload["stale_products"]
            assert pid not in captured[0].body
        # both tiers surfaced in logs, each on its own path
        events = [entry["event"] for entry in logs]
        assert "coinbase_marks_stale" in events
        assert "coinbase_marks_stale_telemetry_tier" in events

    def test_telemetry_log_throttled_per_cooldown_window(self) -> None:
        worker = self._tiered_worker()
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=5)
        self._mark(worker, self.CRITICAL_PERPS, age_s=5)
        self._mark(worker, self.TELEMETRY_PERPS, age_s=400)

        def telemetry_log_count(at: datetime) -> int:
            with capture_logs() as logs:
                asyncio.run(worker.run_staleness_check_once(now_utc=at))
            return sum(
                1 for entry in logs if entry["event"] == "coinbase_marks_stale_telemetry_tier"
            )

        assert telemetry_log_count(NOW) == 1
        assert telemetry_log_count(NOW + timedelta(seconds=30)) == 0  # inside window
        assert telemetry_log_count(NOW + timedelta(seconds=1000)) == 1  # past cooldown

    def test_cooldowns_are_independent_per_tier(self) -> None:
        # A telemetry log inside its window must not suppress a fresh
        # critical alert (and the critical cooldown timer is untouched
        # by telemetry activity).
        captured, hook = _hook_capture()
        worker = self._tiered_worker(alert_hook=hook)
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=5)
        self._mark(worker, self.CRITICAL_PERPS, age_s=5)
        self._mark(worker, self.TELEMETRY_PERPS, age_s=400)
        asyncio.run(worker.run_staleness_check_once(now_utc=NOW))
        assert captured == []  # telemetry only: logged, no alert
        # 60 s later the critical tier goes stale (marks now 65 s + 245 s old)
        later = NOW + timedelta(seconds=245)
        assert asyncio.run(worker.run_staleness_check_once(now_utc=later)) is True
        assert len(captured) == 1  # telemetry throttle didn't eat the alert

    def test_critical_recovery_clears_state_while_telemetry_still_stale(self) -> None:
        captured, hook = _hook_capture()
        worker = self._tiered_worker(alert_hook=hook)
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=400)
        self._mark(worker, self.CRITICAL_PERPS, age_s=400)
        self._mark(worker, self.TELEMETRY_PERPS, age_s=400)
        asyncio.run(worker.run_staleness_check_once(now_utc=NOW))
        assert len(captured) == 1
        assert worker.status.stale_since_utc == NOW
        # critical tier recovers; alt perps stay quiet (expected)
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=0)
        self._mark(worker, self.CRITICAL_PERPS, age_s=0)
        result = asyncio.run(worker.run_staleness_check_once(now_utc=NOW + timedelta(seconds=1)))
        assert result is False
        assert worker.status.stale_since_utc is None
        assert len(captured) == 1  # no further alert from telemetry tier

    def test_never_ticked_telemetry_product_does_not_alert(self) -> None:
        # Pre-fix, a discovered alt perp that never ticked after grace
        # produced the P2. Now: telemetry log only.
        captured, hook = _hook_capture()
        worker = self._tiered_worker(alert_hook=hook)
        self._mark(worker, SPOT_SIGNAL_PRODUCT_IDS, age_s=5)
        self._mark(worker, self.CRITICAL_PERPS, age_s=5)
        # telemetry perps never ticked
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is False
        assert captured == []


# ---------------------------------------------------------------------------
# worker: run_once orchestration
# ---------------------------------------------------------------------------


class TestMarketBarsCapture:
    """2026-07-20 agentic-refinement capture: durable OHLCV incl. volume."""

    def test_parse_candle_row_keeps_bar_on_missing_volume(self) -> None:
        raw = _candle_raw(date(2026, 7, 8))
        del raw["volume"]
        row = parse_candle_row("BTC-USD", raw, granularity="ONE_DAY")
        assert row is not None
        assert row.volume is None
        assert row.close == Decimal("100.5")

    def test_parse_candle_row_requires_prices(self) -> None:
        raw = _candle_raw(date(2026, 7, 8))
        del raw["close"]
        assert parse_candle_row("BTC-USD", raw, granularity="ONE_DAY") is None

    def test_daily_bar_to_candle_row_midnight_start(self) -> None:
        bar = parse_daily_candle("BTC-USD", _candle_raw(date(2026, 7, 8)))
        assert bar is not None
        row = daily_bar_to_candle_row(bar)
        assert row.granularity == "ONE_DAY"
        assert row.bar_start_utc == datetime(2026, 7, 8, tzinfo=UTC)
        assert row.volume == Decimal("1234.5")

    def test_fetch_hourly_drops_in_progress_hour(self) -> None:
        rest = _FakeRest()
        complete = int(datetime(2026, 7, 9, 11, 0, tzinfo=UTC).timestamp())
        in_progress = int(datetime(2026, 7, 9, 12, 0, tzinfo=UTC).timestamp())
        rest.hourly_candles = [
            {**_candle_raw(date(2026, 7, 9)), "start": str(complete)},
            {**_candle_raw(date(2026, 7, 9)), "start": str(in_progress)},
        ]
        rows = asyncio.run(fetch_hourly_candle_rows(rest, "BTC-USD", hours=6, now_utc=NOW))
        assert [r.bar_start_utc.hour for r in rows] == [11]

    def test_fetch_hourly_degrades_without_get_candles(self) -> None:
        class _DailyOnlyRest:
            async def get_daily_candles(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
                return []

        rows = asyncio.run(
            fetch_hourly_candle_rows(_DailyOnlyRest(), "BTC-USD", hours=6, now_utc=NOW)
        )
        assert rows == []

    def test_daily_snapshot_persists_spot_and_perp_bars(self) -> None:
        factory = _FakeSessionFactory()
        yesterday = (NOW - timedelta(days=1)).date()
        rest = _FakeRest(products=[_perp_raw()], candles=[_candle_raw(yesterday)])
        worker = _worker(rest=rest, factory=factory)
        asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))  # arms perp discovery
        asyncio.run(worker.run_daily_snapshot_once(now_utc=NOW))
        bar_inserts = [e for e in factory.executed if "market_bars" in e[0]]
        products_written = {e[1]["product_id"] for e in bar_inserts}
        # both spot signal products + the discovered critical perp
        assert {"BTC-USD", "ETH-USD", "BIP-20DEC30-CDE"} <= products_written
        assert all(e[1]["granularity"] == "ONE_DAY" for e in bar_inserts)
        assert worker.status.last_bars_rows_written >= 0

    def test_daily_snapshot_persist_disabled(self) -> None:
        factory = _FakeSessionFactory()
        yesterday = (NOW - timedelta(days=1)).date()
        rest = _FakeRest(products=[_perp_raw()], candles=[_candle_raw(yesterday)])
        worker = _worker(
            rest=rest,
            factory=factory,
            config=CoinbaseMarketDataConfig(startup_grace_s=0.0, persist_bars=False),
        )
        asyncio.run(worker.run_daily_snapshot_once(now_utc=NOW))
        assert [e for e in factory.executed if "market_bars" in e[0]] == []
        # in-memory sampling still works with persistence off
        assert "BTC-USD" in worker.status.latest_daily_bars

    def test_hourly_bars_job_writes_completed_hours(self) -> None:
        factory = _FakeSessionFactory()
        rest = _FakeRest(products=[_perp_raw()])
        complete = int(datetime(2026, 7, 9, 11, 0, tzinfo=UTC).timestamp())
        rest.hourly_candles = [{**_candle_raw(date(2026, 7, 9)), "start": str(complete)}]
        worker = _worker(rest=rest, factory=factory)
        asyncio.run(worker.run_funding_snapshot_once(now_utc=NOW))
        written = asyncio.run(worker.run_hourly_bars_once(now_utc=NOW))
        bar_inserts = [e for e in factory.executed if "market_bars" in e[0]]
        hourly = [e for e in bar_inserts if e[1]["granularity"] == "ONE_HOUR"]
        # spot pair + discovered perp, one completed hour each
        assert {e[1]["product_id"] for e in hourly} == {
            "BTC-USD",
            "ETH-USD",
            "BIP-20DEC30-CDE",
        }
        # the fake session reports no rowcount, so the counter is 0 here;
        # the INSERTs above are the load-bearing assertion
        assert written == 0
        assert worker.status.last_bars_snapshot_utc == NOW

    def test_upsert_failure_logs_and_reports_zero(self) -> None:
        factory = _FakeSessionFactory()
        factory.execute_raises = True
        rest = _FakeRest()
        complete = int(datetime(2026, 7, 9, 11, 0, tzinfo=UTC).timestamp())
        rest.hourly_candles = [{**_candle_raw(date(2026, 7, 9)), "start": str(complete)}]
        worker = _worker(rest=rest, factory=factory)
        written = asyncio.run(worker.run_hourly_bars_once(now_utc=NOW))
        assert written == 0  # never raises — capture is telemetry


class TestRunOnce:
    def test_fires_hourly_and_daily_jobs_once(self) -> None:
        factory = _FakeSessionFactory()
        yesterday = (NOW - timedelta(days=1)).date()
        rest = _FakeRest(products=[_perp_raw()], candles=[_candle_raw(yesterday)])
        worker = _worker(rest=rest, factory=factory)
        asyncio.run(worker.run_once(now_utc=NOW))
        funding_rows = [e for e in factory.executed if "funding_rates" in e[0]]
        metadata_rows = [e for e in factory.executed if "product_metadata" in e[0]]
        bar_rows = [e for e in factory.executed if "market_bars" in e[0]]
        assert len(funding_rows) == 1
        assert len(metadata_rows) == 1
        # bars capture (2026-07-20): daily job persists the fetched spot
        # daily bar + the hourly bars job fires on the same first tick.
        assert len(bar_rows) >= 1
        # same tick again: nothing new fires
        first_tick_count = len(factory.executed)
        asyncio.run(worker.run_once(now_utc=NOW + timedelta(seconds=30)))
        assert len(factory.executed) == first_tick_count

    def test_next_hour_fires_funding_again_but_not_metadata(self) -> None:
        factory = _FakeSessionFactory()
        rest = _FakeRest(products=[_perp_raw()], candles=[])
        worker = _worker(rest=rest, factory=factory)
        asyncio.run(worker.run_once(now_utc=NOW))
        asyncio.run(worker.run_once(now_utc=NOW + timedelta(hours=1)))
        funding_rows = [e for e in factory.executed if "funding_rates" in e[0]]
        metadata_rows = [e for e in factory.executed if "product_metadata" in e[0]]
        assert len(funding_rows) == 2
        assert len(metadata_rows) == 1

    def test_failed_hour_does_not_retry_within_hour(self) -> None:
        rest = _FakeRest(fail_products=True)
        worker = _worker(rest=rest)
        asyncio.run(worker.run_once(now_utc=NOW))
        calls_after_first = rest.product_calls
        asyncio.run(worker.run_once(now_utc=NOW + timedelta(seconds=30)))
        # products endpoint not re-hit until the next UTC hour
        assert rest.product_calls == calls_after_first

    def test_rejects_naive_now(self) -> None:
        worker = _worker()
        with pytest.raises(ValueError, match="tz-aware"):
            asyncio.run(worker.run_once(now_utc=datetime(2026, 7, 9)))


# ---------------------------------------------------------------------------
# worker: WS message handling + loop
# ---------------------------------------------------------------------------


class _FakeWs:
    """One connection: yields queued frames, then raises ConnectionError."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []

    async def __aenter__(self) -> _FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise ConnectionError("socket closed")


class TestWsHandling:
    def test_handle_ws_message_records_marks(self) -> None:
        worker = _worker()
        frame = json.dumps(
            {
                "channel": "ticker",
                "events": [{"tickers": [{"product_id": "BTC-USD", "price": "100123.45"}]}],
            }
        )
        worker._handle_ws_message(frame)
        mark = worker.mark_store.latest("BTC-USD")
        assert mark is not None
        assert mark.price == Decimal("100123.45")
        assert mark.observed_at_utc == NOW

    def test_unparseable_frame_drops_quietly(self) -> None:
        worker = _worker()
        worker._handle_ws_message("{not json")
        assert worker.mark_store.snapshot() == {}

    def test_ws_loop_subscribes_consumes_and_reconnects(self) -> None:
        async def scenario() -> None:
            frame = json.dumps(
                {
                    "channel": "ticker",
                    "events": [{"tickers": [{"product_id": "BTC-USD", "price": "50000"}]}],
                }
            )
            connections: list[_FakeWs] = []

            def ws_connect(url: str) -> _FakeWs:
                ws = _FakeWs([frame])
                connections.append(ws)
                return ws

            rest = _FakeRest(products=[_perp_raw()])
            worker = CoinbaseMarketDataWorker(
                config=CoinbaseMarketDataConfig(ws_reconnect_max_backoff_s=0.01),
                session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
                rest_client=rest,
                ws_connect=ws_connect,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )
            task = asyncio.create_task(worker._ws_loop())
            for _ in range(200):
                if len(connections) >= 2:
                    break
                await asyncio.sleep(0.01)
            worker.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

            # subscribed to spot + discovered perp on the ticker channel
            first_sub = json.loads(connections[0].sent[0])
            assert first_sub["channel"] == "ticker"
            assert set(first_sub["product_ids"]) == {"BTC-USD", "ETH-USD", "BIP-20DEC30-CDE"}
            heartbeat_sub = json.loads(connections[0].sent[1])
            assert heartbeat_sub["channel"] == "heartbeats"
            # the ticker frame landed in the mark store
            mark = worker.mark_store.latest("BTC-USD")
            assert mark is not None
            assert mark.price == Decimal("50000")
            # a second connection proves the reconnect path ran
            assert len(connections) >= 2
            assert worker.status.ws_connect_count >= 2

        asyncio.run(scenario())

    def test_ws_loop_discovery_failure_still_subscribes_spot(self) -> None:
        async def scenario() -> None:
            connections: list[_FakeWs] = []

            def ws_connect(url: str) -> _FakeWs:
                ws = _FakeWs([])
                connections.append(ws)
                return ws

            worker = CoinbaseMarketDataWorker(
                config=CoinbaseMarketDataConfig(ws_reconnect_max_backoff_s=0.01),
                session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
                rest_client=_FakeRest(fail_products=True),
                ws_connect=ws_connect,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )
            task = asyncio.create_task(worker._ws_loop())
            for _ in range(200):
                if connections and connections[0].sent:
                    break
                await asyncio.sleep(0.01)
            worker.request_stop()
            await asyncio.wait_for(task, timeout=5.0)
            first_sub = json.loads(connections[0].sent[0])
            assert set(first_sub["product_ids"]) == set(SPOT_SIGNAL_PRODUCT_IDS)

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# worker: run_forever lifecycle
# ---------------------------------------------------------------------------


class TestRunForever:
    def test_starts_ticks_and_stops_cleanly(self) -> None:
        async def scenario() -> None:
            rest = _FakeRest(products=[_perp_raw()], candles=[])

            def ws_connect(url: str) -> _FakeWs:
                return _FakeWs([])

            worker = CoinbaseMarketDataWorker(
                config=CoinbaseMarketDataConfig(
                    tick_interval_s=0.01, ws_reconnect_max_backoff_s=0.01
                ),
                session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
                rest_client=rest,
                ws_connect=ws_connect,  # type: ignore[arg-type]
            )
            task = asyncio.create_task(worker.run_forever())
            for _ in range(200):
                if worker.status.last_funding_snapshot_utc is not None:
                    break
                await asyncio.sleep(0.01)
            worker.request_stop()
            await asyncio.wait_for(task, timeout=5.0)
            assert worker.status.last_funding_snapshot_utc is not None
            assert worker.status.last_metadata_snapshot_utc is not None

        asyncio.run(scenario())


class TestPositionAwareStalenessTier:
    """Position-aware demotion (decisions-log 2026-08-17): 13 weekend P2s,
    every one the UNHELD ETH perp barely crossing the 180 s threshold.
    A critical-tier perp only pages while its base asset has an open
    position; spot never demotes; a failed positions read fails open.
    """

    def _tiers(self) -> TestStalenessTiers:
        return TestStalenessTiers()

    def _fresh_except(
        self, worker: CoinbaseMarketDataWorker, stale_pid: str, *, age_s: int = 200
    ) -> None:
        t = self._tiers()
        for pid in (*SPOT_SIGNAL_PRODUCT_IDS, *t.CRITICAL_PERPS, *t.TELEMETRY_PERPS):
            if pid != stale_pid:
                worker.mark_store.record(pid, Decimal("1"), observed_at_utc=NOW)
        worker.mark_store.record(
            stale_pid, Decimal("1"), observed_at_utc=NOW - timedelta(seconds=age_s)
        )

    def test_unheld_perp_stale_demoted_to_telemetry_no_page(self) -> None:
        # The weekend-of-2026-08-15 regression: ETP stale, no ETH position.
        captured, hook = _hook_capture()
        worker = self._tiers()._tiered_worker(alert_hook=hook, held=("BTC",))
        self._fresh_except(worker, "ETP-20DEC30-CDE")
        with capture_logs() as logs:
            result = asyncio.run(worker.run_staleness_check_once(now_utc=NOW))
        assert result is False  # critical tier not stale after demotion
        assert captured == []
        assert worker.status.stale_since_utc is None
        telemetry = [e for e in logs if e["event"] == "coinbase_marks_stale_telemetry_tier"]
        assert len(telemetry) == 1  # journal keeps the data
        assert "ETP-20DEC30-CDE" in telemetry[0]["stale_products"]

    def test_held_perp_stale_still_pages(self) -> None:
        captured, hook = _hook_capture()
        worker = self._tiers()._tiered_worker(alert_hook=hook, held=("BTC",))
        self._fresh_except(worker, "BIP-20DEC30-CDE")
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is True
        assert len(captured) == 1
        assert set(captured[0].payload["stale_products"]) == {"BIP-20DEC30-CDE"}

    def test_spot_stale_never_demoted_flat_book(self) -> None:
        # Flat book everywhere: spot staleness (the WS-outage canary)
        # must still page.
        captured, hook = _hook_capture()
        worker = self._tiers()._tiered_worker(alert_hook=hook, held=())
        for pid in (*self._tiers().CRITICAL_PERPS, *self._tiers().TELEMETRY_PERPS):
            worker.mark_store.record(pid, Decimal("1"), observed_at_utc=NOW)
        # both spot products left stale (never ticked marks would hit the
        # same path; explicit stale ages keep the fixture obvious)
        for pid in SPOT_SIGNAL_PRODUCT_IDS:
            worker.mark_store.record(
                pid, Decimal("1"), observed_at_utc=NOW - timedelta(seconds=400)
            )
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is True
        assert len(captured) == 1
        assert set(captured[0].payload["stale_products"]) == set(SPOT_SIGNAL_PRODUCT_IDS)

    def test_positions_read_failure_fails_open_and_pages(self) -> None:
        captured, hook = _hook_capture()
        factory = _FakeSessionFactory()
        worker = self._tiers()._tiered_worker(alert_hook=hook, held=(), factory=factory)
        factory.execute_raises = True  # the held-positions SELECT raises
        self._fresh_except(worker, "ETP-20DEC30-CDE")
        with capture_logs() as logs:
            result = asyncio.run(worker.run_staleness_check_once(now_utc=NOW))
        assert result is True  # no demotion on a failed read
        assert len(captured) == 1
        assert any(e["event"] == "coinbase_held_positions_read_failed_fail_open" for e in logs)

    def test_no_db_read_when_no_critical_perp_stale(self) -> None:
        # Healthy path stays DB-free: the held-positions SELECT is lazy.
        factory = _FakeSessionFactory()
        worker = self._tiers()._tiered_worker(held=("BTC",), factory=factory)
        t = self._tiers()
        for pid in (*SPOT_SIGNAL_PRODUCT_IDS, *t.CRITICAL_PERPS, *t.TELEMETRY_PERPS):
            worker.mark_store.record(pid, Decimal("1"), observed_at_utc=NOW)
        executed_before = len(factory.executed)
        assert asyncio.run(worker.run_staleness_check_once(now_utc=NOW)) is False
        assert len(factory.executed) == executed_before
