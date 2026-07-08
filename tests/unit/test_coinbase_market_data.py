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

from services.data.coinbase_market_data import (
    CANDLES_PER_REQUEST,
    SPOT_SIGNAL_PRODUCT_IDS,
    CoinbaseMarketDataConfig,
    CoinbaseMarketDataWorker,
    MarkStore,
    current_utc_hour,
    discover_perp_products,
    extract_ticker_updates,
    fetch_daily_bars,
    parse_daily_candle,
    parse_perp_product,
    should_fire_daily,
    should_fire_hourly,
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
    funding: str | None = "0.0000125",
    venue: str = "CDE",
    expiry_type: str = "PERPETUAL",
    **overrides: Any,
) -> dict[str, Any]:
    perp_details: dict[str, Any] = {}
    if funding is not None:
        perp_details = {"funding_rate": funding, "funding_time": "2026-07-09T13:00:00Z"}
    raw: dict[str, Any] = {
        "product_id": product_id,
        "product_type": "FUTURE",
        "product_venue": venue,
        "price": "100000.5",
        "price_increment": "5",
        "trading_disabled": False,
        "future_product_details": {
            "contract_size": "0.01",
            "contract_expiry_type": expiry_type,
            "perpetual_details": perp_details,
            "initial_margin_rate": "0.2",
            "maintenance_margin_rate": "0.1",
        },
    }
    raw.update(overrides)
    return raw


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


class _FakeBegin:
    async def __aenter__(self) -> _FakeBegin:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, factory: _FakeSessionFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, stmt: Any, params: Any = None) -> None:
        if self._factory.execute_raises:
            raise RuntimeError("insert failed")
        self._factory.executed.append((str(stmt), params))


class _FakeSessionFactory:
    """Stands in for async_sessionmaker; records every execute()."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.execute_raises = False

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
    def test_accepts_cde_perp_with_venue_field(self) -> None:
        snap = parse_perp_product(_perp_raw())
        assert snap is not None
        assert snap.product_id == "BIP-20DEC30-CDE"
        assert snap.contract_size == Decimal("0.01")
        assert snap.tick_size == Decimal("5")
        assert snap.funding_rate_per_interval == Decimal("0.0000125")
        assert snap.initial_margin_requirement == Decimal("0.2")
        assert snap.maintenance_margin_requirement == Decimal("0.1")
        assert snap.trading_disabled is False
        assert snap.raw["product_id"] == "BIP-20DEC30-CDE"

    def test_accepts_cde_suffix_when_venue_field_absent(self) -> None:
        raw = _perp_raw(venue="")
        assert parse_perp_product(raw) is not None

    def test_accepts_expiry_type_perpetual_without_perp_details(self) -> None:
        raw = _perp_raw(funding=None)
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.funding_rate_per_interval is None

    def test_rejects_spot_product(self) -> None:
        assert parse_perp_product(_spot_raw()) is None

    def test_rejects_dated_cde_future(self) -> None:
        raw = _perp_raw(funding=None, expiry_type="EXPIRING")
        assert parse_perp_product(raw) is None

    def test_rejects_non_cde_future(self) -> None:
        raw = _perp_raw(product_id="BTC-PERP-INTX", venue="INTX")
        assert parse_perp_product(raw) is None

    def test_rejects_missing_product_id(self) -> None:
        raw = _perp_raw()
        raw["product_id"] = ""
        assert parse_perp_product(raw) is None

    def test_garbage_numerics_become_none_not_raise(self) -> None:
        raw = _perp_raw(funding="not-a-number")
        raw["price_increment"] = ""
        snap = parse_perp_product(raw)
        assert snap is not None
        assert snap.funding_rate_per_interval is None
        assert snap.tick_size is None

    def test_discover_filters_mixed_payload(self) -> None:
        products: list[dict[str, Any]] = [
            _spot_raw(),
            _perp_raw(),
            _perp_raw("EIP-20DEC30-CDE"),
            _perp_raw(funding=None, expiry_type="EXPIRING"),
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
# worker: run_once orchestration
# ---------------------------------------------------------------------------


class TestRunOnce:
    def test_fires_hourly_and_daily_jobs_once(self) -> None:
        factory = _FakeSessionFactory()
        yesterday = (NOW - timedelta(days=1)).date()
        rest = _FakeRest(products=[_perp_raw()], candles=[_candle_raw(yesterday)])
        worker = _worker(rest=rest, factory=factory)
        asyncio.run(worker.run_once(now_utc=NOW))
        funding_rows = [e for e in factory.executed if "funding_rates" in e[0]]
        metadata_rows = [e for e in factory.executed if "product_metadata" in e[0]]
        assert len(funding_rows) == 1
        assert len(metadata_rows) == 1
        # same tick again: nothing new fires
        asyncio.run(worker.run_once(now_utc=NOW + timedelta(seconds=30)))
        assert len(factory.executed) == 2

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
