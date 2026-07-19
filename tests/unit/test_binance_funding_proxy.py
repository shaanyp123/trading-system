"""Unit tests for ``services/data/binance_funding_proxy.py`` (gate-B3 proxy).

Coverage follows the data-job test style (pure parsers with dict
fixtures + fakes; the live HTTP contract is pinned by the A27 runbook
``deploy/binance_funding_proxy/README.md``, not mocked transport):

  * ``/fapi/v1/fundingRate`` entry parser — live-shaped payloads,
    ms-epoch timestamps, Decimal anchoring, malformed rows → None
  * symbol derivation from the spot signal universe (no hardcoded list)
  * the daily job — idempotent INSERT parameters (source='binance_proxy',
    interval_hours=8, UNIQUE-conflict DO NOTHING), per-symbol error
    isolation, never-raises contract, new-vs-existing row counting
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from services.data.binance_funding_proxy import (
    BINANCE_FUNDING_INTERVAL_HOURS,
    FUNDING_SOURCE_BINANCE_PROXY,
    BinanceFundingProxyJob,
    make_funding_proxy_callback,
    parse_funding_entry,
    proxy_symbols_for_spot_products,
)

NOW = datetime(2026, 7, 19, 0, 40, tzinfo=UTC)
TODAY = date(2026, 7, 19)

#: 2026-07-18 16:00:00 UTC in epoch milliseconds (a real settlement slot).
FUNDING_TIME_MS = 1784563200000


def _entry(
    *,
    symbol: str = "BTCUSDT",
    funding_time: int = FUNDING_TIME_MS,
    rate: str = "0.00010000",
    mark_price: str | None = "64123.40000000",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": symbol,
        "fundingTime": funding_time,
        "fundingRate": rate,
    }
    if mark_price is not None:
        payload["markPrice"] = mark_price
    return payload


class TestParseFundingEntry:
    def test_accepts_live_shape(self) -> None:
        parsed = parse_funding_entry(_entry())
        assert parsed is not None
        assert parsed.symbol == "BTCUSDT"
        assert parsed.funding_rate == Decimal("0.00010000")
        assert parsed.mark_price == Decimal("64123.40000000")
        assert parsed.funding_time_utc == datetime.fromtimestamp(FUNDING_TIME_MS / 1000, tz=UTC)
        assert parsed.funding_time_utc.tzinfo is not None

    def test_negative_rate_preserved(self) -> None:
        parsed = parse_funding_entry(_entry(rate="-0.00025000"))
        assert parsed is not None
        assert parsed.funding_rate == Decimal("-0.00025000")

    def test_missing_mark_price_degrades_to_none(self) -> None:
        parsed = parse_funding_entry(_entry(mark_price=None))
        assert parsed is not None
        assert parsed.mark_price is None

    def test_empty_mark_price_degrades_to_none(self) -> None:
        parsed = parse_funding_entry(_entry(mark_price=""))
        assert parsed is not None
        assert parsed.mark_price is None

    def test_missing_symbol_returns_none(self) -> None:
        payload = _entry()
        del payload["symbol"]
        assert parse_funding_entry(payload) is None

    def test_unparseable_rate_returns_none(self) -> None:
        assert parse_funding_entry(_entry(rate="not-a-number")) is None

    def test_non_finite_rate_returns_none(self) -> None:
        assert parse_funding_entry(_entry(rate="NaN")) is None
        assert parse_funding_entry(_entry(rate="Infinity")) is None

    def test_string_funding_time_returns_none(self) -> None:
        payload = _entry()
        payload["fundingTime"] = str(FUNDING_TIME_MS)
        assert parse_funding_entry(payload) is None

    def test_bool_funding_time_returns_none(self) -> None:
        payload = _entry()
        payload["fundingTime"] = True
        assert parse_funding_entry(payload) is None


class TestProxySymbolDerivation:
    def test_derives_from_spot_signal_universe(self) -> None:
        assert proxy_symbols_for_spot_products(("BTC-USD", "ETH-USD")) == (
            "BTCUSDT",
            "ETHUSDT",
        )

    def test_dedupes_and_sorts(self) -> None:
        assert proxy_symbols_for_spot_products(("ETH-USD", "BTC-USD", "eth-usd", "")) == (
            "BTCUSDT",
            "ETHUSDT",
        )


# ---------------------------------------------------------------------------
# Job fakes (mirrors the usdc_rewards test harness)
# ---------------------------------------------------------------------------


class _FakeFundingClient:
    def __init__(
        self,
        rows_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        raises_for: set[str] | None = None,
    ) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.raises_for = raises_for or set()
        self.calls: list[tuple[str, int]] = []

    async def get_funding_history(
        self, symbol: str, *, start_time_ms: int, limit: int = 1000
    ) -> list[dict[str, Any]]:
        self.calls.append((symbol, start_time_ms))
        if symbol in self.raises_for:
            raise RuntimeError("binance down")
        return self.rows_by_symbol.get(symbol, [])


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


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

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        if self._factory.execute_raises:
            raise RuntimeError("insert failed")
        self._factory.executed.append((str(stmt), params))
        key = None
        if isinstance(params, dict):
            key = (params.get("product_id"), params.get("observed_at"))
        rowcount = 0 if key in self._factory.existing else 1
        return _FakeResult(rowcount)


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.existing: set[Any] = set()
        self.execute_raises = False

    def __call__(self) -> _FakeSession:
        return _FakeSession(self)


def _job(
    client: _FakeFundingClient,
    factory: _FakeSessionFactory | None = None,
    *,
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
) -> tuple[BinanceFundingProxyJob, _FakeSessionFactory]:
    factory = factory or _FakeSessionFactory()
    job = BinanceFundingProxyJob(
        client=client,
        session_factory=factory,  # type: ignore[arg-type]
        symbols=symbols,
        clock=lambda: NOW,
    )
    return job, factory


class TestBinanceFundingProxyJob:
    async def test_persists_rows_with_proxy_source_and_8h_interval(self) -> None:
        client = _FakeFundingClient(
            {
                "BTCUSDT": [_entry(), _entry(funding_time=FUNDING_TIME_MS + 8 * 3600 * 1000)],
                "ETHUSDT": [_entry(symbol="ETHUSDT", rate="0.00005")],
            }
        )
        job, factory = _job(client)
        result = await job.run_capture_once(TODAY)
        assert result.symbols_attempted == 2
        assert result.symbols_failed == 0
        assert result.rows_seen == 3
        assert result.rows_new == 3
        assert len(factory.executed) == 3
        for stmt, params in factory.executed:
            assert "INSERT INTO funding_rates" in stmt
            assert "ON CONFLICT (product_id, observed_at_utc) DO NOTHING" in stmt
            assert params["source"] == FUNDING_SOURCE_BINANCE_PROXY
            assert params["interval_hours"] == BINANCE_FUNDING_INTERVAL_HOURS
            assert isinstance(params["rate"], Decimal)
            assert params["observed_at"].tzinfo is not None
        assert {p["product_id"] for _, p in factory.executed} == {"BTCUSDT", "ETHUSDT"}

    async def test_existing_rows_counted_as_not_new(self) -> None:
        client = _FakeFundingClient({"BTCUSDT": [_entry()]})
        job, factory = _job(client, symbols=("BTCUSDT",))
        factory.existing.add(("BTCUSDT", datetime.fromtimestamp(FUNDING_TIME_MS / 1000, tz=UTC)))
        result = await job.run_capture_once(TODAY)
        assert result.rows_seen == 1
        assert result.rows_new == 0

    async def test_one_symbol_failure_never_blocks_the_other(self) -> None:
        client = _FakeFundingClient(
            {"ETHUSDT": [_entry(symbol="ETHUSDT")]},
            raises_for={"BTCUSDT"},
        )
        job, factory = _job(client)
        result = await job.run_capture_once(TODAY)
        assert result.symbols_failed == 1
        assert result.rows_new == 1
        assert {p["product_id"] for _, p in factory.executed} == {"ETHUSDT"}

    async def test_insert_failure_counts_symbol_failed_and_never_raises(self) -> None:
        client = _FakeFundingClient({"BTCUSDT": [_entry()]})
        job, factory = _job(client, symbols=("BTCUSDT",))
        factory.execute_raises = True
        result = await job.run_capture_once(TODAY)  # must not raise
        assert result.symbols_failed == 1
        assert result.rows_new == 0

    async def test_cross_symbol_rows_filtered(self) -> None:
        # A response carrying rows for a different symbol must not be
        # persisted under the requested symbol's fetch.
        client = _FakeFundingClient({"BTCUSDT": [_entry(symbol="ETHUSDT")]})
        job, factory = _job(client, symbols=("BTCUSDT",))
        result = await job.run_capture_once(TODAY)
        assert result.rows_seen == 0
        assert factory.executed == []

    async def test_lookback_window_anchors_to_clock(self) -> None:
        client = _FakeFundingClient({"BTCUSDT": []})
        job, _factory = _job(client, symbols=("BTCUSDT",))
        await job.run_capture_once(TODAY)
        (_, start_time_ms) = client.calls[0]
        expected_ms = int((NOW.timestamp() - 35 * 86400) * 1000)
        assert start_time_ms == expected_ms

    async def test_callback_adapter_returns_none(self) -> None:
        client = _FakeFundingClient({})
        job, _factory = _job(client, symbols=())
        callback = make_funding_proxy_callback(job)
        assert await callback(TODAY) is None
