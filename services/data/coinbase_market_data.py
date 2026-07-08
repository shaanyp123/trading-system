"""services/data/coinbase_market_data.py — Coinbase market data + funding telemetry worker.

Crypto-pivot C0-B2a (delta spec §3.2, ``Docs/crypto-pivot-delta-spec.md``).
Replaces the retired ``bar_sync`` worker as the system's market-data
layer. Four responsibilities, one worker:

1. **WebSocket ticker** — subscribes the public Advanced Trade WS
   ``ticker`` channel for the spot signal products (``BTC-USD`` /
   ``ETH-USD``) plus every CDE perp-style product discovered at
   runtime, and maintains the in-memory :class:`MarkStore` the 30 s
   risk loop (delta spec §3.3) and position MTM read marks from.
   Auto-reconnects with exponential backoff; re-discovers products on
   each connect.

2. **Hourly funding logger** (§3.2 "funding logger from day one",
   REPORT F-3 rider) — once per UTC hour, fetches the FUTURE products
   list, extracts each CDE perp product's hourly funding rate, and
   INSERTs idempotent rows into ``funding_rates`` (C0-B1 migration;
   ``ON CONFLICT (product_id, observed_at_utc) DO NOTHING``). The
   nightly realized-vs-parametric comparison (gate B3) reads this
   table.

3. **Daily product_metadata snapshot** — once per UTC day at/after
   00:00 UTC, persists each discovered perp product's spec (tick size,
   contract size, margin %, full raw payload) into ``product_metadata``
   so the REPORT-rider auto-demotion check can diff today vs yesterday.
   The same daily pass samples the latest REST daily candles for the
   spot signal products (signal input per strategy §4 — daily bars at
   00:00 UTC) and retains them in memory for status surfaces; the
   signal engine (§3.3) fetches its own full history through
   :func:`fetch_daily_bars`.

4. **3-minute staleness watchdog** (strategy §7 data-outage row) —
   every tick, classifies each subscribed product's last-mark age; on
   transition past the threshold, dispatches a P2 alert (descriptor
   built in :mod:`services.data.coinbase_market_data_alerts`). The
   protective *response* (protected-hold vs flatten) belongs to the
   risk loop; this worker detects + alerts.

**No authentication anywhere in this module** — every REST call uses
the public ``/api/v3/brokerage/market`` endpoints and the WS ticker
channel is public. Coinbase API keys enter the codebase only with the
§3.1 execution adapter (C0-B2b). If the public products payload turns
out not to carry CDE funding (strategy §11 open question 4), the
funding-miss alert surfaces it immediately and the fetch seam
(:class:`CoinbaseRestLike`) swaps to an authenticated client without
touching worker logic.

[A13] (revised) — CDE product IDs are discovered at runtime via
:func:`parse_perp_product`; never hardcoded (they encode expiry). The
spot signal product IDs (``BTC-USD``/``ETH-USD``) are locked constants
per strategy §4 — they are stable spot symbols, not CDE derivative IDs.

[A05] — all prices/rates are ``Decimal``, coerced at the wire boundary
via ``Decimal(str(x))``. [A06] — tz-aware UTC everywhere. [A25] —
structlog only.

``services/data/**`` is NOT on the [A02] forbidden whitelist — this
module rides the normal review flow. Its DB tables landed under
risk-review in the C0-B1 migration.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.data.coinbase_market_data_alerts import (
    CoinbaseMarketDataAlertDescriptor,
    build_funding_snapshot_miss_alert,
    build_marks_stale_alert,
)

log = structlog.get_logger()


#: Spot products used as the daily-bar signal input (strategy §4: the
#: funding mechanism keeps the perp within bps of spot, so spot bars
#: are the locked signal series; executions reference live perp
#: quotes). These are stable Coinbase spot symbols — NOT CDE derivative
#: IDs — so pinning them does not violate [A13].
SPOT_SIGNAL_PRODUCT_IDS: Final[tuple[str, ...]] = ("BTC-USD", "ETH-USD")

#: Public Advanced Trade REST + WS endpoints (no auth).
DEFAULT_REST_BASE_URL: Final[str] = "https://api.coinbase.com"
DEFAULT_WS_URL: Final[str] = "wss://advanced-trade-ws.coinbase.com"

#: The public candles endpoint caps at 350 candles per request; stay
#: under it with margin.
CANDLES_PER_REQUEST: Final[int] = 300

#: CDE funding settles hourly (strategy §1.4 — 20 three-minute samples
#: averaged, scaled 1/24, smoothed 75/25). Persisted into
#: ``funding_rates.interval_hours`` so annualization stays derivable
#: if the venue ever changes cadence.
CDE_FUNDING_INTERVAL_HOURS: Final[Decimal] = Decimal("1")

#: Suffix all CDE-listed derivative products carry (e.g.
#: ``BIP-20DEC30-CDE``). A structural venue marker, not a product ID.
CDE_PRODUCT_ID_SUFFIX: Final[str] = "-CDE"


# --------------------------------------------------------------------------
# DTOs + pure parsers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One daily OHLCV bar sampled at 00:00 UTC (bar-start convention)."""

    product_id: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class Mark:
    """Latest observed price for one product, from the WS ticker."""

    product_id: str
    price: Decimal
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class PerpProductSnapshot:
    """One CDE perp-style product's spec + funding at observation time.

    ``raw`` is the full venue payload — persisted to
    ``product_metadata.raw`` JSONB for forensic diffing beyond the
    first-class columns (REPORT rider: auto-demotion on fee/margin
    change detection).
    """

    product_id: str
    tick_size: Decimal | None
    contract_size: Decimal | None
    initial_margin_requirement: Decimal | None
    maintenance_margin_requirement: Decimal | None
    trading_disabled: bool | None
    funding_rate_per_interval: Decimal | None
    mark_price: Decimal | None
    raw: dict[str, Any]


def _coerce_decimal(value: object) -> Decimal | None:
    """Boundary coercion to Decimal ([A05]); None on missing/blank/garbage."""
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def parse_perp_product(raw: Mapping[str, Any]) -> PerpProductSnapshot | None:
    """Classify + parse one products-endpoint entry as a CDE perp product.

    Returns ``None`` for anything that isn't a CDE perpetual-style
    future. Filters are structural (venue suffix / venue field /
    perpetual marker), never an ID whitelist ([A13] revised — product
    IDs encode expiry and new series appear).
    """
    product_id = str(raw.get("product_id") or "")
    if not product_id:
        return None
    if str(raw.get("product_type") or "").upper() != "FUTURE":
        return None
    venue = str(raw.get("product_venue") or "").upper()
    if venue != "CDE" and not product_id.upper().endswith(CDE_PRODUCT_ID_SUFFIX):
        return None
    details = raw.get("future_product_details")
    details_map: Mapping[str, Any] = details if isinstance(details, Mapping) else {}
    perp_details = details_map.get("perpetual_details")
    perp_map: Mapping[str, Any] = perp_details if isinstance(perp_details, Mapping) else {}
    # Perpetual marker: an explicit perpetual_details block, or the
    # contract_expiry_type saying PERPETUAL. Dated CDE futures (also
    # product_type=FUTURE, same venue) are excluded here.
    expiry_type = str(details_map.get("contract_expiry_type") or "").upper()
    if not perp_map and expiry_type != "PERPETUAL":
        return None

    trading_disabled_raw = raw.get("trading_disabled")
    is_disabled_raw = raw.get("is_disabled")
    trading_disabled: bool | None
    if isinstance(trading_disabled_raw, bool) or isinstance(is_disabled_raw, bool):
        trading_disabled = bool(trading_disabled_raw) or bool(is_disabled_raw)
    else:
        trading_disabled = None

    return PerpProductSnapshot(
        product_id=product_id,
        tick_size=_coerce_decimal(raw.get("price_increment") or raw.get("quote_increment")),
        contract_size=_coerce_decimal(details_map.get("contract_size")),
        initial_margin_requirement=_coerce_decimal(
            details_map.get("intraday_initial_margin_rate")
            or details_map.get("initial_margin_rate")
        ),
        maintenance_margin_requirement=_coerce_decimal(details_map.get("maintenance_margin_rate")),
        trading_disabled=trading_disabled,
        funding_rate_per_interval=_coerce_decimal(perp_map.get("funding_rate")),
        mark_price=_coerce_decimal(perp_map.get("mark_price") or raw.get("price")),
        raw=dict(raw),
    )


def discover_perp_products(products: list[Mapping[str, Any]]) -> list[PerpProductSnapshot]:
    """Filter a products-endpoint payload down to CDE perp products."""
    out: list[PerpProductSnapshot] = []
    for raw in products:
        snap = parse_perp_product(raw)
        if snap is not None:
            out.append(snap)
    return out


def parse_daily_candle(product_id: str, raw: Mapping[str, Any]) -> DailyBar | None:
    """Parse one public-candles entry (bar-start unix seconds + OHLCV strings)."""
    try:
        start_unix = int(str(raw["start"]))
        o = Decimal(str(raw["open"]))
        h = Decimal(str(raw["high"]))
        lo = Decimal(str(raw["low"]))
        c = Decimal(str(raw["close"]))
        v = Decimal(str(raw["volume"]))
    except (KeyError, ValueError, InvalidOperation):
        return None
    session = datetime.fromtimestamp(start_unix, tz=UTC).date()
    return DailyBar(
        product_id=product_id,
        session_date=session,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=v,
    )


def extract_ticker_updates(message: Mapping[str, Any]) -> list[tuple[str, Decimal]]:
    """Pull (product_id, price) pairs out of one WS ticker message.

    Tolerant of schema drift: anything that doesn't look like a ticker
    event yields no updates (heartbeats / subscriptions acks included).
    """
    if str(message.get("channel") or "") != "ticker":
        return []
    updates: list[tuple[str, Decimal]] = []
    events = message.get("events")
    if not isinstance(events, list):
        return []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        tickers = event.get("tickers")
        if not isinstance(tickers, list):
            continue
        for ticker in tickers:
            if not isinstance(ticker, Mapping):
                continue
            product_id = str(ticker.get("product_id") or "")
            price = _coerce_decimal(ticker.get("price"))
            if product_id and price is not None and price > 0:
                updates.append((product_id, price))
    return updates


# --------------------------------------------------------------------------
# Pure scheduling policy
# --------------------------------------------------------------------------


def current_utc_hour(now_utc: datetime) -> datetime:
    """Truncate a tz-aware datetime to the top of its UTC hour."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")
    return now_utc.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def should_fire_hourly(*, now_utc: datetime, last_fired_hour_utc: datetime | None) -> bool:
    """True when the hourly funding snapshot hasn't run in this UTC hour."""
    return last_fired_hour_utc is None or current_utc_hour(now_utc) > last_fired_hour_utc


def should_fire_daily(*, now_utc: datetime, last_fired_date_utc: date | None) -> bool:
    """True when the daily snapshot hasn't run on this UTC calendar date."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")
    return last_fired_date_utc is None or now_utc.astimezone(UTC).date() > last_fired_date_utc


# --------------------------------------------------------------------------
# Mark store
# --------------------------------------------------------------------------


class MarkStore:
    """In-memory latest-mark store fed by the WS ticker loop.

    Single-event-loop access only (no locking). Consumers: the 30 s
    risk loop (client stops / liquidation buffer, §3.3), position MTM,
    and the staleness watchdog. In-memory by design — lost on restart
    and honestly reported as absent until the WS reconnects (mirrors
    the heartbeat-registry precedent).
    """

    def __init__(self) -> None:
        self._marks: dict[str, Mark] = {}

    def record(self, product_id: str, price: Decimal, *, observed_at_utc: datetime) -> None:
        if observed_at_utc.tzinfo is None:
            raise ValueError("observed_at_utc must be tz-aware (dev-guide §3 / A06); got naive")
        self._marks[product_id] = Mark(
            product_id=product_id,
            price=price,
            observed_at_utc=observed_at_utc,
        )

    def latest(self, product_id: str) -> Mark | None:
        return self._marks.get(product_id)

    def snapshot(self) -> dict[str, Mark]:
        return dict(self._marks)

    def ages_seconds(self, *, now_utc: datetime) -> dict[str, float]:
        """Seconds since last tick, per tracked product."""
        return {
            pid: max((now_utc - mark.observed_at_utc).total_seconds(), 0.0)
            for pid, mark in self._marks.items()
        }


# --------------------------------------------------------------------------
# REST client (public endpoints, httpx)
# --------------------------------------------------------------------------


class CoinbaseRestError(Exception):
    """Raised when a public REST call fails (transport or non-2xx)."""


class HttpxCoinbaseRestClient:
    """Thin async client over the PUBLIC Advanced Trade market endpoints.

    * ``GET /api/v3/brokerage/market/products?product_type=FUTURE``
    * ``GET /api/v3/brokerage/market/products/{id}/candles``

    A fresh ``httpx.AsyncClient`` per call — at this worker's cadence
    (hourly/daily) connection reuse buys nothing and per-call clients
    avoid lifecycle management in the long-lived worker.
    """

    def __init__(self, *, base_url: str = DEFAULT_REST_BASE_URL, timeout_s: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s)) as client:
                resp = await client.get(url, params=dict(params))
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise CoinbaseRestError(f"GET {path} failed: {exc!r}") from exc
        if not isinstance(body, dict):
            raise CoinbaseRestError(f"GET {path} returned non-object JSON: {type(body).__name__}")
        return body

    async def get_future_products(self) -> list[dict[str, Any]]:
        """All FUTURE products (perp classification happens in the parser)."""
        body = await self._get(
            "/api/v3/brokerage/market/products",
            {"product_type": "FUTURE", "get_all_products": "true"},
        )
        products = body.get("products")
        if not isinstance(products, list):
            raise CoinbaseRestError("products payload missing 'products' list")
        return [p for p in products if isinstance(p, dict)]

    async def get_daily_candles(
        self, product_id: str, *, start_unix: int, end_unix: int
    ) -> list[dict[str, Any]]:
        """One page of ONE_DAY candles for ``product_id`` (max ~350)."""
        body = await self._get(
            f"/api/v3/brokerage/market/products/{product_id}/candles",
            {
                "start": str(start_unix),
                "end": str(end_unix),
                "granularity": "ONE_DAY",
            },
        )
        candles = body.get("candles")
        if not isinstance(candles, list):
            raise CoinbaseRestError(f"candles payload for {product_id} missing 'candles' list")
        return [c for c in candles if isinstance(c, dict)]


#: Structural typing seam for tests + a future authenticated client.
#: (Kept as a simple alias rather than a Protocol: the two methods are
#: the whole surface and mypy structural-checks the fakes at call sites.)
CoinbaseRestLike = HttpxCoinbaseRestClient


async def fetch_daily_bars(
    rest: Any,
    product_id: str,
    *,
    days: int,
    now_utc: datetime,
) -> list[DailyBar]:
    """Fetch the last ``days`` daily bars for one product, ascending.

    Paginates the public candles endpoint in ``CANDLES_PER_REQUEST``-day
    windows (the endpoint caps ~350/request), dedupes on session_date,
    and returns bars sorted ascending. The signal engine (§3.3) uses
    this for its 00:05 UTC decision input; the worker's daily pass uses
    it with a small ``days`` to keep the latest bars in memory.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")
    if days <= 0:
        return []
    end = current_utc_hour(now_utc).replace(hour=0)  # today 00:00 UTC (bar-start of today)
    remaining = days
    window_end = end
    by_date: dict[date, DailyBar] = {}
    while remaining > 0:
        window_days = min(remaining, CANDLES_PER_REQUEST)
        window_start = window_end - timedelta(days=window_days)
        raw_candles = await rest.get_daily_candles(
            product_id,
            start_unix=int(window_start.timestamp()),
            end_unix=int(window_end.timestamp()),
        )
        for raw in raw_candles:
            bar = parse_daily_candle(product_id, raw)
            if bar is not None:
                by_date[bar.session_date] = bar
        remaining -= window_days
        window_end = window_start
    return [by_date[d] for d in sorted(by_date)]


# --------------------------------------------------------------------------
# Config + worker
# --------------------------------------------------------------------------

#: Async factory returning a connected WS context manager for ``url``.
#: Default uses ``websockets.connect``; tests inject fakes.
WsConnectFactory = Callable[[str], AbstractAsyncContextManager[Any]]

#: Alert seam: the api lifespan wires an INSERT-alerts-row + Discord
#: dispatch closure here; ``None`` degrades to log-and-drop (canonical
#: bar_sync seam semantics).
MarketDataAlertDispatchHook = Callable[[CoinbaseMarketDataAlertDescriptor], Awaitable[None]]


def _default_ws_connect(url: str) -> AbstractAsyncContextManager[Any]:
    # Lazy import so the module imports on hosts without the websockets
    # package (same pattern as bar_sync's lazy ib_async import).
    import websockets

    return websockets.connect(url, max_queue=512, open_timeout=30, close_timeout=5)


@dataclass(frozen=True, slots=True)
class CoinbaseMarketDataConfig:
    """All tunables; tests override via constructor, prod via APISettings."""

    rest_base_url: str = DEFAULT_REST_BASE_URL
    ws_url: str = DEFAULT_WS_URL
    spot_product_ids: tuple[str, ...] = SPOT_SIGNAL_PRODUCT_IDS
    #: Periodic-jobs tick cadence (staleness check + due-checks).
    tick_interval_s: float = 30.0
    #: Strategy §7: market data stale > 3 min triggers the outage policy.
    stale_threshold_s: float = 180.0
    #: Re-alert cooldown while continuously stale (one P2 per window).
    stale_realert_cooldown_s: float = 900.0
    #: Grace after worker start before staleness can alert (lets the WS
    #: connect + first ticks land without a boot-time false alarm).
    startup_grace_s: float = 180.0
    #: Days of daily bars retained in memory by the daily pass.
    daily_bars_prefetch_days: int = 5
    http_timeout_s: float = 30.0
    ws_reconnect_max_backoff_s: float = 60.0


@dataclass(slots=True)
class MarketDataStatus:
    """In-memory status snapshot (read-only surface for /api/system/cycle)."""

    ws_connected: bool = False
    ws_connect_count: int = 0
    subscribed_product_ids: tuple[str, ...] = ()
    last_funding_snapshot_utc: datetime | None = None
    last_funding_rows_written: int = 0
    consecutive_funding_misses: int = 0
    last_metadata_snapshot_utc: datetime | None = None
    last_metadata_rows_written: int = 0
    latest_daily_bars: dict[str, DailyBar] = field(default_factory=dict)
    stale_since_utc: datetime | None = None


class CoinbaseMarketDataWorker:
    """Long-lived worker: WS ticker loop + periodic funding/metadata jobs.

    ``run_forever`` spawns the WS loop as a child task and runs the
    periodic tick loop in-line; ``request_stop`` winds both down.
    ``run_once`` (one periodic tick) and the individual ``run_*_once``
    jobs are exposed for tests + operator on-demand invocation.
    """

    def __init__(
        self,
        *,
        config: CoinbaseMarketDataConfig,
        session_factory: async_sessionmaker[Any],
        rest_client: Any | None = None,
        ws_connect: WsConnectFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        alert_hook: MarketDataAlertDispatchHook | None = None,
        initial_fired_funding_hour_utc: datetime | None = None,
        initial_fired_metadata_date_utc: date | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._rest: Any = (
            rest_client
            if rest_client is not None
            else HttpxCoinbaseRestClient(
                base_url=config.rest_base_url, timeout_s=config.http_timeout_s
            )
        )
        self._ws_connect: WsConnectFactory = ws_connect or _default_ws_connect
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        self._alert_hook = alert_hook
        self._stop_event = asyncio.Event()
        self._log = log.bind(worker="coinbase_market_data")

        self.mark_store = MarkStore()
        self.status = MarketDataStatus(
            subscribed_product_ids=tuple(config.spot_product_ids),
        )
        self._perp_product_ids: tuple[str, ...] = ()
        self._last_fired_funding_hour_utc: datetime | None = initial_fired_funding_hour_utc
        self._last_fired_metadata_date_utc: date | None = initial_fired_metadata_date_utc
        self._last_funding_success_utc: datetime | None = None
        self._last_stale_alert_utc: datetime | None = None
        self._started_at_utc: datetime | None = None

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self) -> None:
        """Set the stop event; both loops exit at their next boundary."""
        self._stop_event.set()

    async def run_forever(self) -> None:
        """Supervisor: WS loop as child task + periodic tick loop in-line."""
        self._started_at_utc = self._clock()
        self._log.info(
            "coinbase_market_data_started",
            ws_url=self._config.ws_url,
            rest_base_url=self._config.rest_base_url,
            spot_product_ids=list(self._config.spot_product_ids),
            tick_interval_s=self._config.tick_interval_s,
            stale_threshold_s=self._config.stale_threshold_s,
            alert_hook_wired=self._alert_hook is not None,
        )
        ws_task = asyncio.create_task(self._ws_loop(), name="coinbase_market_data.ws_loop")
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_once()
                except Exception:
                    self._log.exception("coinbase_market_data_tick_failed")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._config.tick_interval_s
                    )
                except TimeoutError:
                    pass  # normal cadence
        finally:
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ws_task
            self._log.info("coinbase_market_data_stopped")

    async def run_once(self, *, now_utc: datetime | None = None) -> None:
        """One periodic tick: staleness check + fire any due jobs."""
        if now_utc is None:
            now_utc = self._clock()
        if now_utc.tzinfo is None:
            raise ValueError("now_utc must be tz-aware (dev-guide §3 / A06); got naive")

        await self.run_staleness_check_once(now_utc=now_utc)

        if should_fire_hourly(
            now_utc=now_utc, last_fired_hour_utc=self._last_fired_funding_hour_utc
        ):
            # Mark fired BEFORE the work so a failing hour doesn't retry-storm
            # every 30 s tick (scheduler-precedent semantics); the next UTC
            # hour retries fresh and the consecutive-miss alert covers the
            # persistent-failure case.
            self._last_fired_funding_hour_utc = current_utc_hour(now_utc)
            await self.run_funding_snapshot_once(now_utc=now_utc)

        if should_fire_daily(
            now_utc=now_utc, last_fired_date_utc=self._last_fired_metadata_date_utc
        ):
            self._last_fired_metadata_date_utc = now_utc.astimezone(UTC).date()
            await self.run_daily_snapshot_once(now_utc=now_utc)

    # -- job: hourly funding snapshot ---------------------------------------

    async def run_funding_snapshot_once(self, *, now_utc: datetime | None = None) -> int:
        """Fetch products, persist one funding_rates row per perp product.

        Returns rows written (0 counts as a miss for the alert policy).
        Never raises — failures log + count toward the consecutive-miss
        alert.
        """
        if now_utc is None:
            now_utc = self._clock()
        observed_hour = current_utc_hour(now_utc)
        perps: list[PerpProductSnapshot] = []
        try:
            products = await self._rest.get_future_products()
            perps = discover_perp_products(products)
            self._set_perp_products(tuple(sorted(p.product_id for p in perps)))
        except Exception:
            self._log.exception("coinbase_funding_products_fetch_failed")

        rows_written = 0
        for perp in perps:
            if perp.funding_rate_per_interval is None:
                self._log.warning(
                    "coinbase_funding_rate_unavailable",
                    product_id=perp.product_id,
                    note=(
                        "products payload carried no parseable funding rate "
                        "for this perp product (strategy §11 open question 4)"
                    ),
                )
                continue
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO funding_rates ("
                                "    product_id, observed_at_utc, rate_per_interval,"
                                "    interval_hours, mark_price, source"
                                ") VALUES ("
                                "    :product_id, :observed_at, :rate,"
                                "    :interval_hours, :mark_price, 'coinbase_advanced'"
                                ") ON CONFLICT (product_id, observed_at_utc) DO NOTHING"
                            ),
                            {
                                "product_id": perp.product_id,
                                "observed_at": observed_hour,
                                "rate": perp.funding_rate_per_interval,
                                "interval_hours": CDE_FUNDING_INTERVAL_HOURS,
                                "mark_price": perp.mark_price,
                            },
                        )
                rows_written += 1
            except Exception:
                self._log.exception(
                    "coinbase_funding_row_insert_failed",
                    product_id=perp.product_id,
                )

        self.status.last_funding_snapshot_utc = now_utc
        self.status.last_funding_rows_written = rows_written
        if rows_written > 0:
            self.status.consecutive_funding_misses = 0
            self._last_funding_success_utc = now_utc
            self._log.info(
                "coinbase_funding_snapshot_completed",
                observed_hour_utc=observed_hour.isoformat(),
                rows_written=rows_written,
                perp_products=len(perps),
            )
        else:
            self.status.consecutive_funding_misses += 1
            self._log.warning(
                "coinbase_funding_snapshot_empty",
                observed_hour_utc=observed_hour.isoformat(),
                perp_products=len(perps),
                consecutive_misses=self.status.consecutive_funding_misses,
            )
            descriptor = build_funding_snapshot_miss_alert(
                consecutive_misses=self.status.consecutive_funding_misses,
                perp_products_seen=len(perps),
                last_success_utc=self._last_funding_success_utc,
                now_utc=now_utc,
            )
            if descriptor is not None:
                await self._dispatch_alert(descriptor)
        return rows_written

    # -- job: daily product_metadata snapshot + bar sample -------------------

    async def run_daily_snapshot_once(self, *, now_utc: datetime | None = None) -> int:
        """Persist per-perp product_metadata rows + sample spot daily bars.

        Returns metadata rows written. Never raises (per-product /
        per-bar failures log and continue).
        """
        if now_utc is None:
            now_utc = self._clock()
        captured_at = current_utc_hour(now_utc).replace(hour=0)  # today 00:00 UTC

        perps: list[PerpProductSnapshot] = []
        try:
            products = await self._rest.get_future_products()
            perps = discover_perp_products(products)
            self._set_perp_products(tuple(sorted(p.product_id for p in perps)))
        except Exception:
            self._log.exception("coinbase_metadata_products_fetch_failed")

        rows_written = 0
        for perp in perps:
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO product_metadata ("
                                "    product_id, captured_at_utc, tick_size,"
                                "    contract_size, initial_margin_requirement,"
                                "    maintenance_margin_requirement,"
                                "    trading_disabled, raw"
                                ") VALUES ("
                                "    :product_id, :captured_at, :tick_size,"
                                "    :contract_size, :imr, :mmr,"
                                "    :trading_disabled, CAST(:raw AS JSONB)"
                                ") ON CONFLICT (product_id, captured_at_utc) DO NOTHING"
                            ),
                            {
                                "product_id": perp.product_id,
                                "captured_at": captured_at,
                                "tick_size": perp.tick_size,
                                "contract_size": perp.contract_size,
                                "imr": perp.initial_margin_requirement,
                                "mmr": perp.maintenance_margin_requirement,
                                "trading_disabled": perp.trading_disabled,
                                "raw": json.dumps(perp.raw, default=str),
                            },
                        )
                rows_written += 1
            except Exception:
                self._log.exception(
                    "coinbase_metadata_row_insert_failed",
                    product_id=perp.product_id,
                )

        self.status.last_metadata_snapshot_utc = now_utc
        self.status.last_metadata_rows_written = rows_written
        self._log.info(
            "coinbase_metadata_snapshot_completed",
            captured_at_utc=captured_at.isoformat(),
            rows_written=rows_written,
            perp_products=len(perps),
        )

        # Sample the latest spot daily bars (signal input, strategy §4).
        for product_id in self._config.spot_product_ids:
            try:
                bars = await fetch_daily_bars(
                    self._rest,
                    product_id,
                    days=self._config.daily_bars_prefetch_days,
                    now_utc=now_utc,
                )
            except Exception:
                self._log.exception(
                    "coinbase_daily_bar_fetch_failed",
                    product_id=product_id,
                )
                continue
            if not bars:
                self._log.warning("coinbase_daily_bar_empty", product_id=product_id)
                continue
            latest = bars[-1]
            self.status.latest_daily_bars[product_id] = latest
            expected_latest = (now_utc.astimezone(UTC) - timedelta(days=1)).date()
            if latest.session_date < expected_latest:
                self._log.warning(
                    "coinbase_daily_bar_lagging",
                    product_id=product_id,
                    latest_session_date=latest.session_date.isoformat(),
                    expected_session_date=expected_latest.isoformat(),
                )
            else:
                self._log.info(
                    "coinbase_daily_bar_sampled",
                    product_id=product_id,
                    session_date=latest.session_date.isoformat(),
                    close=str(latest.close),
                )
        return rows_written

    # -- job: staleness watchdog ---------------------------------------------

    async def run_staleness_check_once(self, *, now_utc: datetime | None = None) -> bool:
        """Classify mark freshness; alert on stale transition. True if stale.

        Products never ticked count as stale once the startup grace has
        elapsed (a WS that never connects must not look healthy).
        """
        if now_utc is None:
            now_utc = self._clock()
        threshold = self._config.stale_threshold_s
        in_grace = (
            self._started_at_utc is not None
            and (now_utc - self._started_at_utc).total_seconds() < self._config.startup_grace_s
        )

        tracked = set(self._config.spot_product_ids) | set(self._perp_product_ids)
        ages = self.mark_store.ages_seconds(now_utc=now_utc)
        stale: dict[str, float] = {}
        for product_id in sorted(tracked):
            age = ages.get(product_id)
            if age is None:
                if not in_grace and self._started_at_utc is not None:
                    stale[product_id] = (now_utc - self._started_at_utc).total_seconds()
            elif age >= threshold:
                stale[product_id] = age

        if not stale:
            if self.status.stale_since_utc is not None:
                self._log.info(
                    "coinbase_marks_recovered",
                    stale_for_s=int((now_utc - self.status.stale_since_utc).total_seconds()),
                )
            self.status.stale_since_utc = None
            return False

        if in_grace:
            return False

        if self.status.stale_since_utc is None:
            self.status.stale_since_utc = now_utc
        cooldown_ok = (
            self._last_stale_alert_utc is None
            or (now_utc - self._last_stale_alert_utc).total_seconds()
            >= self._config.stale_realert_cooldown_s
        )
        self._log.warning(
            "coinbase_marks_stale",
            stale_products={pid: int(age) for pid, age in stale.items()},
            stale_threshold_s=int(threshold),
            alert_due=cooldown_ok,
        )
        if cooldown_ok:
            descriptor = build_marks_stale_alert(
                stale_products=stale,
                stale_threshold_s=threshold,
                now_utc=now_utc,
            )
            await self._dispatch_alert(descriptor)
            self._last_stale_alert_utc = now_utc
        return True

    # -- WS loop ---------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Connect → subscribe → consume; reconnect with capped backoff."""
        backoff_s = 1.0
        while not self._stop_event.is_set():
            try:
                await self._refresh_products_for_subscription()
                subscribe_ids = sorted(
                    set(self._config.spot_product_ids) | set(self._perp_product_ids)
                )
                async with self._ws_connect(self._config.ws_url) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "channel": "ticker",
                                "product_ids": subscribe_ids,
                            }
                        )
                    )
                    await ws.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))
                    self.status.ws_connected = True
                    self.status.ws_connect_count += 1
                    self.status.subscribed_product_ids = tuple(subscribe_ids)
                    self._log.info(
                        "coinbase_ws_connected",
                        product_ids=subscribe_ids,
                        connect_count=self.status.ws_connect_count,
                    )
                    backoff_s = 1.0
                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except TimeoutError:
                            continue  # idle socket; re-check stop_event
                        self._handle_ws_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status.ws_connected = False
                self._log.exception(
                    "coinbase_ws_disconnected",
                    reconnect_backoff_s=backoff_s,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff_s)
                except TimeoutError:
                    pass
                backoff_s = min(backoff_s * 2, self._config.ws_reconnect_max_backoff_s)
        self.status.ws_connected = False

    def _handle_ws_message(self, raw: object) -> None:
        """Parse one WS frame; record ticker marks. Malformed frames log+drop."""
        try:
            message = json.loads(raw if isinstance(raw, str | bytes) else str(raw))
        except (ValueError, TypeError):
            self._log.warning("coinbase_ws_message_unparseable")
            return
        if not isinstance(message, dict):
            return
        updates = extract_ticker_updates(message)
        if not updates:
            return
        now_utc = self._clock()
        for product_id, price in updates:
            self.mark_store.record(product_id, price, observed_at_utc=now_utc)

    async def _refresh_products_for_subscription(self) -> None:
        """Best-effort perp discovery before (re)subscribing the ticker."""
        try:
            products = await self._rest.get_future_products()
            perps = discover_perp_products(products)
            self._set_perp_products(tuple(sorted(p.product_id for p in perps)))
        except Exception:
            self._log.warning(
                "coinbase_ws_product_discovery_failed",
                note=(
                    "subscribing spot signal products only; perp tickers "
                    "join on the next reconnect or scheduled discovery"
                ),
                known_perp_products=list(self._perp_product_ids),
            )

    def _set_perp_products(self, product_ids: tuple[str, ...]) -> None:
        if product_ids and product_ids != self._perp_product_ids:
            self._log.info(
                "coinbase_perp_products_discovered",
                product_ids=list(product_ids),
                previous=list(self._perp_product_ids),
            )
            self._perp_product_ids = product_ids

    # -- alert seam --------------------------------------------------------------

    async def _dispatch_alert(self, descriptor: CoinbaseMarketDataAlertDescriptor) -> None:
        """Canonical log-and-drop when unwired, fail-open when wired."""
        if self._alert_hook is None:
            self._log.warning(
                "coinbase_market_data_alert_dropped_no_hook",
                severity=descriptor.severity,
                category=descriptor.category,
                title=descriptor.title,
                payload=descriptor.payload,
            )
            return
        try:
            await self._alert_hook(descriptor)
        except Exception:
            self._log.exception(
                "coinbase_market_data_alert_dispatch_failed",
                severity=descriptor.severity,
                category=descriptor.category,
                title=descriptor.title,
            )


__all__ = [
    "CANDLES_PER_REQUEST",
    "CDE_FUNDING_INTERVAL_HOURS",
    "CDE_PRODUCT_ID_SUFFIX",
    "DEFAULT_REST_BASE_URL",
    "DEFAULT_WS_URL",
    "SPOT_SIGNAL_PRODUCT_IDS",
    "CoinbaseMarketDataConfig",
    "CoinbaseMarketDataWorker",
    "CoinbaseRestError",
    "DailyBar",
    "HttpxCoinbaseRestClient",
    "Mark",
    "MarkStore",
    "MarketDataAlertDispatchHook",
    "MarketDataStatus",
    "PerpProductSnapshot",
    "WsConnectFactory",
    "current_utc_hour",
    "discover_perp_products",
    "extract_ticker_updates",
    "fetch_daily_bars",
    "parse_daily_candle",
    "parse_perp_product",
    "should_fire_daily",
    "should_fire_hourly",
]
