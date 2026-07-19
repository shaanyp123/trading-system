"""services/data/binance_funding_proxy.py — Binance USDT-perp funding proxy logger.

Gate B3 (strategy §10) compares realized CDE funding on held positions
against the Binance-proxy funding over the same window — but until this
module, ``funding_rates`` carried ONLY ``source='coinbase_advanced'``
rows (the hourly CDE logger in ``services/data/coinbase_market_data.py``)
so B3 sat permanently at ``insufficient_data`` (decisions-log 2026-07-18
PR-2 evidence note). This job persists the comparison series: settled
Binance USDT-perp funding rates from the PUBLIC ``GET /fapi/v1/fundingRate``
endpoint (no API key), one ``funding_rates`` row per settlement, with

  * ``product_id`` = the Binance symbol (``BTCUSDT`` / ``ETHUSDT`` —
    derived from the spot signal pairs, never a hardcoded CDE product
    ID; [A13] does not bind on non-CDE venue symbols, same rationale as
    ``SPOT_SIGNAL_PRODUCT_IDS``),
  * ``observed_at_utc`` = the settlement's ``fundingTime``,
  * ``rate_per_interval`` = the settled ``fundingRate``,
  * ``interval_hours`` = 8 (Binance settles every 8 hours),
  * ``source`` = ``'binance_proxy'``.

Gate B3's classifier (``services/api/schemas/gates.py::_b3_view``) needs
NO change: any non-``coinbase_advanced`` source with a positive trailing
mean annualized rate arms the comparison, and the repo's annualization
(``rate * 24/interval_hours * 365``) is cadence-aware by construction.

**Cadence + idempotency.** One daily fire (00:40 UTC — a quiet slot
after the 00:05 decision / 00:15 recon / 00:20 cash capture / 00:25
dormant sweep window) fetching the trailing
:data:`FUNDING_LOOKBACK_DAYS` days per symbol. That over-fetch is
deliberate: every fire re-covers gate B3's full 30-day window, so any
missed day self-heals on the next fire and the ``UNIQUE (product_id,
observed_at_utc)`` + ``ON CONFLICT DO NOTHING`` insert makes the daily
overlap free. At 8-hourly settlements the window is ~105 rows/symbol —
one request per symbol per day (limit 1000 caps far above it).

**Proxy honesty (strategy §9 ⚠️ / §1.4):** Binance funding is 8-hourly
with ±0.75% clamps vs CDE's hourly smoothed mechanism; books are ~100x
deeper; USDT-margin basis differs from USD by bps. The proxy validates
funding MAGNITUDE (B3's 2x band), nothing finer — the rows are labeled
``binance_proxy`` so no consumer can mistake them for venue truth.
[A27]: the endpoint contract, rate limits, and terms-of-use caveat are
fact-checked via ``deploy/binance_funding_proxy/README.md``.

Telemetry-only by construction: this module writes ``funding_rates``
(append-only telemetry) and is read by gate B3 only. Nothing in the
decision/risk path consumes ``binance_proxy`` rows — the S4 funding gate
reads the trailing CDE series. ``services/data/**`` is NOT on the [A02]
forbidden whitelist.

[A05] — rates are ``Decimal``, coerced at the wire boundary.
[A06] — tz-aware UTC everywhere. [A25] — structlog only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

log = structlog.get_logger()


#: Public Binance USDT-margined futures REST host (no auth for
#: ``/fapi/v1/fundingRate``). Overridable for tests via config.
DEFAULT_FAPI_BASE_URL: Final[str] = "https://fapi.binance.com"

#: ``funding_rates.source`` value for proxy rows. The gate-B3 classifier
#: treats ANY source other than ``coinbase_advanced`` as a proxy series.
FUNDING_SOURCE_BINANCE_PROXY: Final[str] = "binance_proxy"

#: Binance USDT-perp funding settles every 8 hours (00:00/08:00/16:00
#: UTC). Persisted per row so annualization stays derivable if the
#: venue ever changes cadence (same rationale as the CDE logger's
#: ``interval_hours=1``).
BINANCE_FUNDING_INTERVAL_HOURS: Final[Decimal] = Decimal("8")

#: Trailing fetch window per daily fire. Covers gate B3's 30-day
#: comparison window with margin, so one missed fire self-heals on the
#: next (idempotent inserts make the overlap free).
FUNDING_LOOKBACK_DAYS: Final[int] = 35

#: ``/fapi/v1/fundingRate`` documented max ``limit``. 35 days of
#: 8-hourly settlements is ~105 rows — one page per symbol per fire.
FUNDING_FETCH_LIMIT: Final[int] = 1000

#: Daily fire time: 00:40 UTC — after every 00:05-00:25 pipeline step,
#: colliding with nothing. Consumed by the api-lifespan scheduler
#: wiring (generic once-per-UTC-day primitive).
DEFAULT_FUNDING_PROXY_POLL_TIME_UTC: Final[time] = time(hour=0, minute=40)

#: Quote asset appended to each traded base asset to form the Binance
#: USDT-perp symbol (``BTC`` → ``BTCUSDT``).
BINANCE_QUOTE_ASSET: Final[str] = "USDT"


# ---------------------------------------------------------------------------
# DTOs + pure parsers (dict fixtures pin these in tests)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BinanceFundingObservation:
    """One settled Binance funding-rate row, wire-parsed."""

    symbol: str
    funding_time_utc: datetime
    funding_rate: Decimal
    mark_price: Decimal | None


class BinanceFundingError(Exception):
    """A public REST call failed (transport or non-2xx or bad shape).

    Single-attempt contract (mirrors the other data jobs): the next
    daily fire is the retry, and every insert is idempotent.
    """


def proxy_symbols_for_spot_products(spot_product_ids: Iterable[str]) -> tuple[str, ...]:
    """Spot signal pairs → Binance USDT-perp symbols (sorted, deduped).

    ``BTC-USD`` → ``BTCUSDT``; derived exactly like the market-data
    worker's ``spot_base_assets`` so the proxy universe tracks the
    traded universe with no hardcoded symbol list.
    """
    bases = {pid.split("-", 1)[0].strip().upper() for pid in spot_product_ids if pid.strip()}
    return tuple(sorted(f"{base}{BINANCE_QUOTE_ASSET}" for base in bases if base))


def _dec_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def parse_funding_entry(payload: dict[str, Any]) -> BinanceFundingObservation | None:
    """One ``/fapi/v1/fundingRate`` array entry → DTO; None when unusable.

    Load-bearing: ``symbol``, an integer-ms ``fundingTime``, and a
    finite ``fundingRate``. ``markPrice`` degrades to None (older rows
    and some responses omit or blank it).
    """
    symbol = payload.get("symbol")
    funding_time_ms = payload.get("fundingTime")
    rate = _dec_or_none(payload.get("fundingRate"))
    if (
        not isinstance(symbol, str)
        or not symbol
        or not isinstance(funding_time_ms, int)
        or isinstance(funding_time_ms, bool)
        or funding_time_ms <= 0
        or rate is None
    ):
        return None
    mark_raw = payload.get("markPrice")
    mark = _dec_or_none(mark_raw) if mark_raw not in (None, "") else None
    if mark is not None and mark <= 0:
        mark = None
    return BinanceFundingObservation(
        symbol=symbol,
        funding_time_utc=datetime.fromtimestamp(funding_time_ms / 1000, tz=UTC),
        funding_rate=rate,
        mark_price=mark,
    )


# ---------------------------------------------------------------------------
# REST client (public endpoint, httpx)
# ---------------------------------------------------------------------------


class HttpxBinanceFundingClient:
    """Thin async client over the public funding-rate history endpoint.

    Fresh ``httpx.AsyncClient`` per call — at one call per symbol per
    day, connection reuse buys nothing (same rationale as the Coinbase
    public REST client).
    """

    def __init__(self, *, base_url: str = DEFAULT_FAPI_BASE_URL, timeout_s: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def get_funding_history(
        self, symbol: str, *, start_time_ms: int, limit: int = FUNDING_FETCH_LIMIT
    ) -> list[dict[str, Any]]:
        url = f"{self._base_url}/fapi/v1/fundingRate"
        params = {"symbol": symbol, "startTime": str(start_time_ms), "limit": str(limit)}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s)) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise BinanceFundingError(f"GET /fapi/v1/fundingRate {symbol} failed: {exc!r}") from exc
        except ValueError as exc:
            raise BinanceFundingError(f"non-JSON funding response for {symbol}") from exc
        if not isinstance(body, list):
            raise BinanceFundingError(
                f"funding response for {symbol} is {type(body).__name__}, expected list"
            )
        return [row for row in body if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# The daily proxy-capture job
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProxyCaptureResult:
    """Counts from one capture run — for logs + tests, never re-parsed."""

    symbols_attempted: int
    symbols_failed: int
    rows_seen: int
    rows_new: int


class BinanceFundingProxyJob:
    """One daily run: fetch + idempotently persist the proxy funding series.

    Per-symbol error isolation — one symbol's fetch failure never blocks
    the other's rows, and ``run_capture_once`` never raises (the
    scheduler's next daily fire is the retry).
    """

    def __init__(
        self,
        *,
        client: Any,
        session_factory: async_sessionmaker[Any],
        symbols: tuple[str, ...],
        clock: Callable[[], datetime] | None = None,
        lookback_days: int = FUNDING_LOOKBACK_DAYS,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._symbols = symbols
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        self._lookback_days = lookback_days
        self._log = log.bind(job="binance_funding_proxy")

    async def run_capture_once(self, session_date_utc: date) -> ProxyCaptureResult:
        """One capture pass. Never raises; per-symbol failures log + count."""
        now_utc = self._clock()
        start_time_ms = int((now_utc - timedelta(days=self._lookback_days)).timestamp() * 1000)
        symbols_failed = 0
        rows_seen = 0
        rows_new = 0
        for symbol in self._symbols:
            try:
                raw_rows = await self._client.get_funding_history(
                    symbol, start_time_ms=start_time_ms
                )
                observations = [
                    obs
                    for row in raw_rows
                    if (obs := parse_funding_entry(row)) is not None and obs.symbol == symbol
                ]
                unparseable = len(raw_rows) - len(observations)
                if unparseable > 0:
                    self._log.warning(
                        "binance_funding_rows_unparseable",
                        symbol=symbol,
                        unparseable_count=unparseable,
                    )
                rows_seen += len(observations)
                rows_new += await self._persist(observations)
            except Exception:
                symbols_failed += 1
                self._log.exception("binance_funding_symbol_capture_failed", symbol=symbol)

        result = ProxyCaptureResult(
            symbols_attempted=len(self._symbols),
            symbols_failed=symbols_failed,
            rows_seen=rows_seen,
            rows_new=rows_new,
        )
        self._log.info(
            "binance_funding_proxy_capture_completed",
            session_date_utc=session_date_utc.isoformat(),
            symbols_attempted=result.symbols_attempted,
            symbols_failed=result.symbols_failed,
            rows_seen=result.rows_seen,
            rows_new=result.rows_new,
        )
        return result

    async def _persist(self, observations: list[BinanceFundingObservation]) -> int:
        """Idempotent inserts against UNIQUE(product_id, observed_at_utc)."""
        if not observations:
            return 0
        new_rows = 0
        async with self._session_factory() as session:
            async with session.begin():
                for obs in observations:
                    result = await session.execute(
                        text(
                            "INSERT INTO funding_rates ("
                            "    product_id, observed_at_utc, rate_per_interval,"
                            "    interval_hours, mark_price, source"
                            ") VALUES ("
                            "    :product_id, :observed_at, :rate,"
                            "    :interval_hours, :mark_price, :source"
                            ") ON CONFLICT (product_id, observed_at_utc) DO NOTHING"
                        ),
                        {
                            "product_id": obs.symbol,
                            "observed_at": obs.funding_time_utc,
                            "rate": obs.funding_rate,
                            "interval_hours": BINANCE_FUNDING_INTERVAL_HOURS,
                            "mark_price": obs.mark_price,
                            "source": FUNDING_SOURCE_BINANCE_PROXY,
                        },
                    )
                    rowcount = getattr(result, "rowcount", 0)
                    if isinstance(rowcount, int) and rowcount > 0:
                        new_rows += 1
        return new_rows


def make_funding_proxy_callback(
    job: BinanceFundingProxyJob,
) -> Callable[[date], Awaitable[None]]:
    """Adapt the job to the scheduler's ``CycleCallback`` (returns None)."""

    async def callback(session_date_utc: date) -> None:
        await job.run_capture_once(session_date_utc)

    return callback


__all__ = [
    "BINANCE_FUNDING_INTERVAL_HOURS",
    "BINANCE_QUOTE_ASSET",
    "DEFAULT_FAPI_BASE_URL",
    "DEFAULT_FUNDING_PROXY_POLL_TIME_UTC",
    "FUNDING_FETCH_LIMIT",
    "FUNDING_LOOKBACK_DAYS",
    "FUNDING_SOURCE_BINANCE_PROXY",
    "BinanceFundingError",
    "BinanceFundingObservation",
    "BinanceFundingProxyJob",
    "HttpxBinanceFundingClient",
    "ProxyCaptureResult",
    "make_funding_proxy_callback",
    "parse_funding_entry",
    "proxy_symbols_for_spot_products",
]
