"""Unit tests for ``services/data/usdc_rewards.py`` (USDC-interest capture).

Coverage follows the established transport/worker test style (pure
parsers with dict fixtures + Protocol-level fakes; the SDK bridge
itself is exercised by the operator's live probes, not mocked HTTP):

  * v2 ledger + accounts parsers — live-shaped payloads, money-object
    unwrapping, Decimal anchoring, malformed rows degrade to None
  * pagination envelope extractors — cursor plumbing for both APIs
  * the DELIBERATELY LOOSE persistence filter — buy/sell/send excluded,
    expected-reward types persisted, UNKNOWN types persisted too (the
    observe-then-lock loop) and classified for the warning log
  * the daily job — idempotent INSERT parameters, per-half error
    isolation (ledger failure never blocks the balance snapshot),
    None-vs-zero balance semantics, first-write-of-the-day counting
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from services.data.usdc_rewards import (
    CashAccountBalance,
    CashCaptureError,
    SdkCoinbaseCashClient,
    UsdcRewardsCaptureJob,
    V2LedgerTransaction,
    classify_transaction_type,
    extract_accounts_page,
    extract_v2_page,
    make_capture_callback,
    parse_cash_account,
    parse_v2_transaction,
    should_persist_transaction,
)

NOW = datetime(2026, 7, 10, 0, 20, tzinfo=UTC)
TODAY = date(2026, 7, 10)


# ---------------------------------------------------------------------------
# Fixture payloads (v2 ledger shape per the 2026-07-09 live probe)
# ---------------------------------------------------------------------------


def _v2_tx(
    *,
    tx_id: str = "tx-001",
    tx_type: str = "interest",
    amount: str = "0.48",
    currency: str = "USDC",
    created_at: str = "2026-07-10T00:00:00Z",
    status: str = "completed",
) -> dict[str, Any]:
    return {
        "id": tx_id,
        "type": tx_type,
        "status": status,
        "amount": {"amount": amount, "currency": currency},
        "native_amount": {"amount": amount, "currency": "USD"},
        "description": None,
        "created_at": created_at,
        "resource": "transaction",
    }


def _account(
    *,
    currency: str = "USD",
    value: str = "1397.01",
    name: str = "Cash (USD)",
) -> dict[str, Any]:
    return {
        "uuid": "acct-1",
        "name": name,
        "currency": currency,
        "available_balance": {"value": value, "currency": currency},
        "type": "ACCOUNT_TYPE_FIAT",
        "active": True,
    }


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


class TestParseV2Transaction:
    def test_accepts_live_shape(self) -> None:
        parsed = parse_v2_transaction(_v2_tx())
        assert parsed is not None
        assert parsed.venue_transaction_id == "tx-001"
        assert parsed.transaction_type == "interest"
        assert parsed.status == "completed"
        assert parsed.amount == Decimal("0.48")
        assert parsed.currency == "USDC"
        assert parsed.native_amount == Decimal("0.48")
        assert parsed.native_currency == "USD"
        assert parsed.venue_created_at_utc == datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
        assert parsed.raw["resource"] == "transaction"

    def test_signed_debit_amount_preserved(self) -> None:
        parsed = parse_v2_transaction(_v2_tx(amount="-1500.00", tx_type="sell"))
        assert parsed is not None
        assert parsed.amount == Decimal("-1500.00")

    def test_missing_id_returns_none(self) -> None:
        payload = _v2_tx()
        del payload["id"]
        assert parse_v2_transaction(payload) is None

    def test_missing_type_returns_none(self) -> None:
        payload = _v2_tx()
        payload["type"] = ""
        assert parse_v2_transaction(payload) is None

    def test_unparseable_amount_returns_none(self) -> None:
        payload = _v2_tx()
        payload["amount"] = {"amount": "not-a-number", "currency": "USDC"}
        assert parse_v2_transaction(payload) is None

    def test_missing_created_at_returns_none(self) -> None:
        payload = _v2_tx()
        payload["created_at"] = None
        assert parse_v2_transaction(payload) is None

    def test_optional_fields_degrade_to_none(self) -> None:
        payload = _v2_tx()
        payload["native_amount"] = None
        payload["status"] = ""
        parsed = parse_v2_transaction(payload)
        assert parsed is not None
        assert parsed.native_amount is None
        assert parsed.native_currency is None
        assert parsed.status is None


class TestParseCashAccount:
    def test_accepts_live_shape(self) -> None:
        parsed = parse_cash_account(_account())
        assert parsed is not None
        assert parsed.currency == "USD"
        assert parsed.available == Decimal("1397.01")
        assert parsed.name == "Cash (USD)"

    def test_missing_balance_returns_none(self) -> None:
        payload = _account()
        payload["available_balance"] = None
        assert parse_cash_account(payload) is None

    def test_missing_currency_returns_none(self) -> None:
        payload = _account()
        payload["currency"] = ""
        assert parse_cash_account(payload) is None


class TestPageExtractors:
    def test_v2_page_with_cursor(self) -> None:
        rows, cursor = extract_v2_page(
            {
                "data": [_v2_tx(), "garbage", _v2_tx(tx_id="tx-002")],
                "pagination": {"next_starting_after": "tx-002"},
            }
        )
        assert len(rows) == 2  # non-dict rows dropped
        assert cursor == "tx-002"

    def test_v2_last_page_has_no_cursor(self) -> None:
        rows, cursor = extract_v2_page(
            {"data": [_v2_tx()], "pagination": {"next_starting_after": None}}
        )
        assert len(rows) == 1
        assert cursor is None

    def test_v2_missing_envelope_degrades_empty(self) -> None:
        assert extract_v2_page({}) == ([], None)

    def test_accounts_page_with_cursor(self) -> None:
        rows, cursor = extract_accounts_page(
            {"accounts": [_account()], "has_next": True, "cursor": "abc"}
        )
        assert len(rows) == 1
        assert cursor == "abc"

    def test_accounts_last_page(self) -> None:
        rows, cursor = extract_accounts_page(
            {"accounts": [_account()], "has_next": False, "cursor": ""}
        )
        assert len(rows) == 1
        assert cursor is None


# ---------------------------------------------------------------------------
# Persistence filter (the observe-then-lock policy)
# ---------------------------------------------------------------------------


def _tx(tx_type: str, amount: str = "0.48") -> V2LedgerTransaction:
    parsed = parse_v2_transaction(_v2_tx(tx_type=tx_type, amount=amount))
    assert parsed is not None
    return parsed


class TestPersistenceFilter:
    def test_buy_sell_send_excluded(self) -> None:
        for tx_type in ("buy", "sell", "send", "BUY", "Sell"):
            assert should_persist_transaction(_tx(tx_type)) is False, tx_type

    def test_expected_reward_type_persisted(self) -> None:
        assert should_persist_transaction(_tx("interest")) is True
        assert should_persist_transaction(_tx("staking_reward")) is True

    def test_unknown_type_persisted_not_dropped(self) -> None:
        # The whole point of the loose filter: the venue's real reward
        # type is unknown until the first Friday payout — an unfamiliar
        # type MUST be captured, never filtered.
        assert should_persist_transaction(_tx("some_new_venue_type")) is True

    def test_unknown_debit_persisted_for_forensics(self) -> None:
        assert should_persist_transaction(_tx("mystery_fee", amount="-0.10")) is True

    def test_classification_buckets(self) -> None:
        assert classify_transaction_type("buy") == "excluded"
        assert classify_transaction_type("interest") == "expected_reward"
        assert classify_transaction_type("some_new_venue_type") == "unknown"


# ---------------------------------------------------------------------------
# Fakes for the job
# ---------------------------------------------------------------------------


class _FakeCashClient:
    """Scripted CoinbaseCashClient (Protocol-level fake)."""

    def __init__(
        self,
        *,
        transactions: list[V2LedgerTransaction] | None = None,
        balances: list[CashAccountBalance] | None = None,
        ledger_raises: bool = False,
        balances_raises: bool = False,
    ) -> None:
        self.transactions = transactions or []
        self.balances = balances or []
        self.ledger_raises = ledger_raises
        self.balances_raises = balances_raises
        self.ledger_calls: list[str] = []

    async def list_account_balances(self) -> list[CashAccountBalance]:
        if self.balances_raises:
            raise RuntimeError("venue down")
        return self.balances

    async def list_v2_transactions(self, account_currency: str) -> list[V2LedgerTransaction]:
        self.ledger_calls.append(account_currency)
        if self.ledger_raises:
            raise RuntimeError("venue down")
        return self.transactions


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
        # Scripted conflict behavior: params whose idempotency key is in
        # `existing` count as ON CONFLICT DO NOTHING skips (rowcount 0).
        key = None
        if isinstance(params, dict):
            key = params.get("venue_transaction_id", params.get("snapshot_date_utc"))
        rowcount = 0 if key in self._factory.existing else 1
        return _FakeResult(rowcount)


class _FakeSessionFactory:
    """Stands in for async_sessionmaker; records every execute()."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.existing: set[Any] = set()
        self.execute_raises = False

    def __call__(self) -> _FakeSession:
        return _FakeSession(self)


def _job(
    client: _FakeCashClient,
    factory: _FakeSessionFactory | None = None,
) -> tuple[UsdcRewardsCaptureJob, _FakeSessionFactory]:
    factory = factory or _FakeSessionFactory()
    job = UsdcRewardsCaptureJob(
        client=client,
        session_factory=factory,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return job, factory


def _usd_balance(value: str) -> CashAccountBalance:
    parsed = parse_cash_account(_account(currency="USD", value=value))
    assert parsed is not None
    return parsed


def _usdc_balance(value: str) -> CashAccountBalance:
    parsed = parse_cash_account(_account(currency="USDC", value=value, name="USDC Wallet"))
    assert parsed is not None
    return parsed


# ---------------------------------------------------------------------------
# The daily job
# ---------------------------------------------------------------------------


class TestRunCaptureOnce:
    async def test_persists_reward_and_skips_conversions(self) -> None:
        client = _FakeCashClient(
            transactions=[
                _tx("interest"),
                _tx("buy", amount="100.00"),
                _tx("sell", amount="-100.00"),
                _tx("send", amount="-5.00"),
            ],
            balances=[_usd_balance("1397.01"), _usdc_balance("4300.55")],
        )
        job, factory = _job(client)
        result = await job.run_capture_once(TODAY)
        assert result.ledger_rows_seen == 4
        assert result.ledger_rows_persisted == 1
        assert result.ledger_rows_new == 1
        assert result.snapshot_written is True
        assert client.ledger_calls == ["USDC"]
        ledger_inserts = [p for (stmt, p) in factory.executed if "usdc_reward_transactions" in stmt]
        assert len(ledger_inserts) == 1
        assert ledger_inserts[0]["transaction_type"] == "interest"
        assert ledger_inserts[0]["amount"] == Decimal("0.48")

    async def test_reinsert_of_known_transaction_counts_zero_new(self) -> None:
        client = _FakeCashClient(transactions=[_tx("interest")])
        job, factory = _job(client)
        factory.existing.add("tx-001")
        result = await job.run_capture_once(TODAY)
        assert result.ledger_rows_persisted == 1  # attempted
        assert result.ledger_rows_new == 0  # ON CONFLICT DO NOTHING

    async def test_snapshot_params_sum_by_currency(self) -> None:
        client = _FakeCashClient(
            balances=[
                _usd_balance("1000.00"),
                _usd_balance("397.01"),  # two USD accounts sum
                _usdc_balance("4300.55"),
            ],
        )
        job, factory = _job(client)
        await job.run_capture_once(TODAY)
        snapshot_inserts = [p for (stmt, p) in factory.executed if "cash_balance_snapshots" in stmt]
        assert len(snapshot_inserts) == 1
        params = snapshot_inserts[0]
        assert params["snapshot_date_utc"] == TODAY
        assert params["captured_at_utc"] == NOW
        assert params["spot_usd"] == Decimal("1397.01")
        assert params["cbi_usdc"] == Decimal("4300.55")

    async def test_missing_usdc_account_snapshots_none_not_zero(self) -> None:
        client = _FakeCashClient(balances=[_usd_balance("1397.01")])
        job, factory = _job(client)
        await job.run_capture_once(TODAY)
        params = next(p for (stmt, p) in factory.executed if "cash_balance_snapshots" in stmt)
        assert params["spot_usd"] == Decimal("1397.01")
        assert params["cbi_usdc"] is None

    async def test_snapshot_conflict_reports_not_written(self) -> None:
        client = _FakeCashClient(balances=[_usd_balance("1.00")])
        job, factory = _job(client)
        factory.existing.add(TODAY)
        result = await job.run_capture_once(TODAY)
        assert result.snapshot_written is False

    async def test_ledger_failure_does_not_block_snapshot(self) -> None:
        client = _FakeCashClient(
            ledger_raises=True,
            balances=[_usd_balance("1397.01")],
        )
        job, _factory = _job(client)
        result = await job.run_capture_once(TODAY)
        assert result.ledger_rows_seen == 0
        assert result.snapshot_written is True

    async def test_balances_failure_does_not_block_ledger(self) -> None:
        client = _FakeCashClient(
            transactions=[_tx("interest")],
            balances_raises=True,
        )
        job, _factory = _job(client)
        result = await job.run_capture_once(TODAY)
        assert result.ledger_rows_new == 1
        assert result.snapshot_written is False

    async def test_db_failure_never_raises(self) -> None:
        client = _FakeCashClient(
            transactions=[_tx("interest")],
            balances=[_usd_balance("1.00")],
        )
        job, factory = _job(client)
        factory.execute_raises = True
        result = await job.run_capture_once(TODAY)  # must not raise
        assert result.ledger_rows_new == 0
        assert result.snapshot_written is False

    async def test_callback_adapter_returns_none(self) -> None:
        client = _FakeCashClient()
        job, _factory = _job(client)
        callback = make_capture_callback(job)
        await callback(TODAY)  # returns None; must not raise


# ---------------------------------------------------------------------------
# SDK client — pagination + guards (scripted rest object into the lazy slot)
# ---------------------------------------------------------------------------


class _ScriptedRest:
    """Stands in for coinbase.rest.RESTClient via the lazy ``_rest`` slot."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((url_path, dict(params or {})))
        return self._pages[len(self.calls) - 1]


def _sdk_client(pages: list[dict[str, Any]]) -> tuple[SdkCoinbaseCashClient, _ScriptedRest]:
    client = SdkCoinbaseCashClient(api_key_name="k", api_private_key="p")
    rest = _ScriptedRest(pages)
    client._rest = rest  # inject into the lazy slot; _rest_client() returns it
    return client, rest


class TestSdkClientLedgerPaging:
    async def test_order_desc_and_limit_pinned_on_every_page(self) -> None:
        """FC-2: newest-first is load-bearing for the page cap."""
        client, rest = _sdk_client(
            [
                {
                    "data": [_v2_tx(tx_id="tx-1")],
                    "pagination": {"next_starting_after": "tx-1"},
                },
                {"data": [_v2_tx(tx_id="tx-2")], "pagination": {}},
            ]
        )
        transactions = await client.list_v2_transactions("USDC")
        assert [t.venue_transaction_id for t in transactions] == ["tx-1", "tx-2"]
        assert [path for (path, _p) in rest.calls] == [
            "/v2/accounts/USDC/transactions",
            "/v2/accounts/USDC/transactions",
        ]
        for _path, params in rest.calls:
            assert params["order"] == "desc"
            assert params["limit"] == 100
        # Cursor threaded on the second page only.
        assert "starting_after" not in rest.calls[0][1]
        assert rest.calls[1][1]["starting_after"] == "tx-1"

    async def test_non_alphanumeric_currency_rejected_before_any_call(self) -> None:
        client, rest = _sdk_client([])
        with pytest.raises(CashCaptureError, match="alphanumeric"):
            await client.list_v2_transactions("USDC/../orders")
        assert rest.calls == []
