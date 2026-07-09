"""Unit tests for ``GET /api/today/exit-proximity`` (crypto-pivot §3.9).

The exit-side "Stops" projection: per-open-position distance to the
client 2xATR stop and the native 3xATR venue backstop, composed from
``positions_current`` (+ ``contracts``), worker ``engine_state``, the
latest native-stop ``orders`` row and the live mark store.

Route-level coverage via the conftest ASGI app + stubbed repos (same
pattern as ``test_api_cycle_route.py``):

  * empty DB — no active account → ``{"positions": []}``, 200
  * account with no positions → empty list, 200
  * headroom signs for long AND short; **Decimal-as-string** ([A05])
  * missing mark → null headroom fractions
  * unprotected (no native stop resting) rows sort first
  * stopped_today from engine_state's stopped_on_date
  * worker-never-ran → marks_stale=True (conservative), no stop fields

Plus pure-function coverage of :func:`stop_headroom_frac` and
:func:`sort_stop_views`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from services.api import marks
from services.api.repos.cycle import WorkerStatusRow
from services.api.routes import today as today_route
from services.api.schemas.exit_proximity import (
    build_position_stop_view,
    sort_stop_views,
    stop_headroom_frac,
)

NOW_DATE = datetime.now(tz=UTC).date()

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubPhase1Repo:
    account_id: UUID | None = None
    positions: list[dict[str, object]] = field(default_factory=list)
    stop_orders: dict[str, dict[str, object]] = field(default_factory=dict)

    async def fetch_active_account_id(self) -> UUID | None:
        return self.account_id

    async def fetch_positions_current_with_contract(
        self, account_id: UUID
    ) -> list[dict[str, object]]:
        return self.positions

    async def fetch_latest_stop_order(self, client_order_id: str) -> dict[str, object] | None:
        return self.stop_orders.get(client_order_id)


@dataclass
class _StubCycleRepo:
    status: WorkerStatusRow | None = None

    async def fetch_latest_decision(self, account_id: UUID) -> None:
        return None

    async def fetch_worker_status(self, account_id: UUID) -> WorkerStatusRow | None:
        return self.status


@dataclass(frozen=True)
class _FakeMark:
    price: Decimal
    observed_at_utc: datetime


class _FakeMarkStore:
    def __init__(self, prices: dict[str, Decimal]) -> None:
        self._prices = prices

    def latest(self, product_id: str) -> _FakeMark | None:
        price = self._prices.get(product_id)
        if price is None:
            return None
        return _FakeMark(price=price, observed_at_utc=datetime.now(tz=UTC))


@pytest.fixture
def mark_store_teardown() -> Any:
    yield
    marks.clear_mark_store()


def _position_row(
    market: str,
    qty: int,
    avg_cost: str,
    market_root: str | None,
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "market": market,
        "contract_id": uuid4(),
        "quantity": qty,
        "avg_cost": Decimal(avg_cost),
        "margin_held": Decimal("0"),
        "unrealized_pnl": None,
        "last_mark_ts": None,
        "managed_by_version": "b" * 40,
        "market_root": market_root,
        "multiplier": Decimal("0.01"),
    }


def _worker_status(engine_state: dict[str, Any], *, marks_stale: bool = False) -> WorkerStatusRow:
    now = datetime.now(tz=UTC)
    return WorkerStatusRow(
        risk_loop_heartbeat_utc=now,
        risk_loop_tick_count=10,
        marks_stale=marks_stale,
        last_decision_date=date(2026, 7, 9),
        updated_at_utc=now,
        engine_state=engine_state,
    )


def _btc_runtime(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contracts": 2,
        "applied_dir": 1,
        "pending_dir": 0,
        "pending_count": 0,
        "lockout_until_ord": -1,
        "lockout_dir": 0,
        "vol_blocked": False,
        "stopped_on_date": None,
        "entry_vwap": "63950.00",
        "client_stop_level": "58100.00",
        "atrp_at_stop_set": "0.045",
        "native_stop_order_id": "venue-1",
        "native_stop_client_order_id": "coid-btc-1",
        "stop_rearm_version": 1,
    }
    base.update(overrides)
    return base


def _wire(
    override_dep: Any,
    *,
    account_id: UUID | None,
    positions: list[dict[str, object]] | None = None,
    stop_orders: dict[str, dict[str, object]] | None = None,
    status: WorkerStatusRow | None = None,
) -> None:
    override_dep(
        today_route._get_repo,
        lambda: _StubPhase1Repo(
            account_id=account_id,
            positions=positions or [],
            stop_orders=stop_orders or {},
        ),
    )
    override_dep(today_route._get_cycle_repo, lambda: _StubCycleRepo(status=status))


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


class TestExitProximityRoute:
    async def test_no_active_account_returns_empty_positions(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, account_id=None)
        resp = await api_client.get("/api/today/exit-proximity")
        assert resp.status_code == 200
        body = resp.json()
        assert body["positions"] == []
        assert body["server_now"] is not None

    async def test_account_but_no_positions_returns_empty(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        _wire(override_dep, account_id=uuid4())
        resp = await api_client.get("/api/today/exit-proximity")
        assert resp.status_code == 200
        assert resp.json()["positions"] == []

    async def test_long_headroom_signs_and_decimal_strings(
        self, api_client: AsyncClient, override_dep: Any, mark_store_teardown: Any
    ) -> None:
        """[A05] — every money/fraction value serializes as a JSON string."""
        _wire(
            override_dep,
            account_id=uuid4(),
            positions=[_position_row("BIP-20DEC30-CDE", 2, "63950.00", "BTC")],
            stop_orders={"coid-btc-1": {"stop_price": Decimal("50000.00"), "status": "working"}},
            status=_worker_status({"BTC": _btc_runtime()}),
        )
        marks.set_mark_store(_FakeMarkStore({"BIP-20DEC30-CDE": Decimal("64000.00")}))
        resp = await api_client.get("/api/today/exit-proximity")
        assert resp.status_code == 200
        (row,) = resp.json()["positions"]
        assert row["product_id"] == "BIP-20DEC30-CDE"
        assert row["asset"] == "BTC"
        assert row["direction"] == "long"
        assert row["contracts"] == 2
        assert row["entry_vwap"] == "63950.00000000"
        assert row["mark"] == "64000.00"
        assert isinstance(row["mark"], str)
        assert row["marks_stale"] is False
        assert row["client_stop_price"] == "58100.00"
        # (64000 - 58100) / 64000 = 0.0921875
        assert row["client_stop_headroom_frac"] == "0.092188"
        assert isinstance(row["client_stop_headroom_frac"], str)
        assert row["native_stop_price"] == "50000.00"
        # (64000 - 50000) / 64000 = 0.21875
        assert row["native_stop_headroom_frac"] == "0.218750"
        assert row["native_stop_resting"] is True
        assert row["stopped_today"] is False

    async def test_short_headroom_sign_mirrors(
        self, api_client: AsyncClient, override_dep: Any, mark_store_teardown: Any
    ) -> None:
        runtime = _btc_runtime(
            contracts=-1,
            applied_dir=-1,
            client_stop_level="110.00",
            native_stop_client_order_id=None,
            native_stop_order_id="venue-2",
        )
        _wire(
            override_dep,
            account_id=uuid4(),
            positions=[_position_row("ETP-20DEC30-CDE", -1, "105.00", "ETH")],
            status=_worker_status({"ETH": runtime}),
        )
        marks.set_mark_store(_FakeMarkStore({"ETP-20DEC30-CDE": Decimal("100.00")}))
        resp = await api_client.get("/api/today/exit-proximity")
        (row,) = resp.json()["positions"]
        assert row["direction"] == "short"
        # short: (stop - mark) / mark = (110 - 100) / 100 = 0.10
        assert row["client_stop_headroom_frac"] == "0.100000"
        assert row["native_stop_price"] is None  # no client_order_id to resolve

    async def test_missing_mark_yields_null_headrooms(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        # No mark store registered at all — the honest no-live-marks state.
        marks.clear_mark_store()
        _wire(
            override_dep,
            account_id=uuid4(),
            positions=[_position_row("BIP-20DEC30-CDE", 2, "63950.00", "BTC")],
            status=_worker_status({"BTC": _btc_runtime()}),
        )
        resp = await api_client.get("/api/today/exit-proximity")
        (row,) = resp.json()["positions"]
        assert row["mark"] is None
        assert row["client_stop_headroom_frac"] is None
        assert row["native_stop_headroom_frac"] is None

    async def test_unprotected_rows_sort_first(
        self, api_client: AsyncClient, override_dep: Any, mark_store_teardown: Any
    ) -> None:
        btc = _btc_runtime()  # protected, wide headroom
        eth = _btc_runtime(
            contracts=4,
            client_stop_level="95.00",
            native_stop_order_id=None,  # NOT resting at the venue
            native_stop_client_order_id=None,
        )
        _wire(
            override_dep,
            account_id=uuid4(),
            positions=[
                _position_row("BIP-20DEC30-CDE", 2, "63950.00", "BTC"),
                _position_row("ETP-20DEC30-CDE", 4, "98.00", "ETH"),
            ],
            stop_orders={"coid-btc-1": {"stop_price": Decimal("50000.00"), "status": "working"}},
            status=_worker_status({"BTC": btc, "ETH": eth}),
        )
        marks.set_mark_store(
            _FakeMarkStore(
                {
                    "BIP-20DEC30-CDE": Decimal("64000.00"),
                    "ETP-20DEC30-CDE": Decimal("100.00"),
                }
            )
        )
        resp = await api_client.get("/api/today/exit-proximity")
        rows = resp.json()["positions"]
        assert [r["product_id"] for r in rows] == ["ETP-20DEC30-CDE", "BIP-20DEC30-CDE"]
        assert rows[0]["native_stop_resting"] is False

    async def test_stopped_today_from_engine_state(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        runtime = _btc_runtime(stopped_on_date=NOW_DATE.isoformat())
        _wire(
            override_dep,
            account_id=uuid4(),
            positions=[_position_row("BIP-20DEC30-CDE", 2, "63950.00", "BTC")],
            status=_worker_status({"BTC": runtime}),
        )
        resp = await api_client.get("/api/today/exit-proximity")
        (row,) = resp.json()["positions"]
        assert row["stopped_today"] is True

    async def test_worker_never_ran_is_conservative(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """No worker status row → marks_stale=True, no stops, unprotected."""
        _wire(
            override_dep,
            account_id=uuid4(),
            positions=[_position_row("BIP-20DEC30-CDE", 2, "63950.00", "BTC")],
            status=None,
        )
        resp = await api_client.get("/api/today/exit-proximity")
        assert resp.status_code == 200
        (row,) = resp.json()["positions"]
        assert row["marks_stale"] is True
        assert row["client_stop_price"] is None
        assert row["native_stop_resting"] is False
        assert row["stopped_today"] is False

    async def test_flat_netted_market_is_dropped(
        self, api_client: AsyncClient, override_dep: Any
    ) -> None:
        """A roll boundary can net a market to zero — nothing at stop risk."""
        _wire(
            override_dep,
            account_id=uuid4(),
            positions=[
                _position_row("BIP-20DEC30-CDE", 2, "63000.00", "BTC"),
                _position_row("BIP-20DEC30-CDE", -2, "64000.00", "BTC"),
            ],
            status=_worker_status({"BTC": _btc_runtime()}),
        )
        resp = await api_client.get("/api/today/exit-proximity")
        assert resp.json()["positions"] == []


# ---------------------------------------------------------------------------
# Pure mappers
# ---------------------------------------------------------------------------


class TestStopHeadroomFrac:
    def test_long_positive_when_mark_above_stop(self) -> None:
        assert stop_headroom_frac(
            mark=Decimal("100"), stop=Decimal("90"), direction="long"
        ) == Decimal("0.100000")

    def test_long_negative_when_mark_below_stop(self) -> None:
        assert stop_headroom_frac(
            mark=Decimal("100"), stop=Decimal("110"), direction="long"
        ) == Decimal("-0.100000")

    def test_short_positive_when_stop_above_mark(self) -> None:
        assert stop_headroom_frac(
            mark=Decimal("100"), stop=Decimal("110"), direction="short"
        ) == Decimal("0.100000")

    def test_missing_inputs_yield_none(self) -> None:
        assert stop_headroom_frac(mark=None, stop=Decimal("90"), direction="long") is None
        assert stop_headroom_frac(mark=Decimal("100"), stop=None, direction="long") is None

    def test_non_positive_mark_yields_none(self) -> None:
        assert stop_headroom_frac(mark=Decimal("0"), stop=Decimal("90"), direction="long") is None


class TestSortStopViews:
    def _view(
        self,
        product_id: str,
        *,
        mark: str | None,
        client_stop: str | None,
        resting: bool = True,
        stopped_today: bool = False,
    ) -> Any:
        return build_position_stop_view(
            product_id=product_id,
            asset=None,
            contracts=1,
            entry_vwap=None,
            mark=Decimal(mark) if mark is not None else None,
            marks_stale=False,
            client_stop_price=Decimal(client_stop) if client_stop is not None else None,
            native_stop_price=None,
            native_stop_resting=resting,
            stopped_today=stopped_today,
        )

    def test_closest_to_stop_first(self) -> None:
        wide = self._view("A", mark="100", client_stop="80")  # 0.20
        tight = self._view("B", mark="100", client_stop="98")  # 0.02
        assert [v.product_id for v in sort_stop_views([wide, tight])] == ["B", "A"]

    def test_stopped_and_unprotected_lead(self) -> None:
        normal = self._view("A", mark="100", client_stop="98")
        stopped = self._view("B", mark="100", client_stop="80", stopped_today=True)
        unprotected = self._view("C", mark="100", client_stop="80", resting=False)
        ordered = [v.product_id for v in sort_stop_views([normal, stopped, unprotected])]
        assert ordered[:2] == ["B", "C"]  # same attention group; tie broken by headroom/product
        assert ordered[2] == "A"

    def test_null_headroom_sorts_last_within_group(self) -> None:
        no_mark = self._view("A", mark=None, client_stop="98")
        tight = self._view("B", mark="100", client_stop="98")
        assert [v.product_id for v in sort_stop_views([no_mark, tight])] == ["B", "A"]
