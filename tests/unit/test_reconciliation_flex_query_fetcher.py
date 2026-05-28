"""Unit tests for services.reconciliation.flex_query_fetcher (Pivot-PR-C).

Uses respx to mock IBKR's FlexQuery HTTP endpoints. Covers:
- parse_flex_xml shape (account / positions / cash)
- malformed XML
- two-step SendRequest → poll → GetStatement flow
- error codes (auth, query-not-found, retryable, terminal)

A22 N/A (no audit_log writes). A06 enforced.
"""

from __future__ import annotations

from datetime import UTC, date
from decimal import Decimal

import httpx
import pytest
import respx

from services.reconciliation.flex_query_fetcher import (
    FlexQueryFetchError,
    IbkrFlexQueryClient,
    parse_flex_xml,
)

_SAMPLE_FLEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="recon" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U25655583" fromDate="2026-05-12" toDate="2026-05-12">
      <AccountInformation accountId="U25655583" currency="USD" type="MARGIN"/>
      <EquitySummaryByReportDateInBase accountId="U25655583" reportDate="2026-05-12"
        cash="12000.00" stock="0.00" bond="0.00" futuresPNL="123.45" total="12123.45"/>
      <OpenPositions>
        <OpenPosition accountId="U25655583" symbol="MES" assetCategory="FUT"
          position="1" costBasisPrice="5234.50" markPrice="5240.00"
          positionValue="26200.00" fifoPnlUnrealized="27.50"/>
        <OpenPosition accountId="U25655583" symbol="TLT" assetCategory="STK"
          position="50" costBasisPrice="95.20" markPrice="95.80"
          positionValue="4790.00" fifoPnlUnrealized="30.00"/>
      </OpenPositions>
      <CashReport>
        <CashReportCurrency currency="USD" endingCash="12000.00" accruedInterest="2.50"/>
      </CashReport>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""


_SUCCESS_SENDREQUEST_XML = """<?xml version="1.0"?>
<FlexStatementResponse>
  <Status>Success</Status>
  <ReferenceCode>1234567890</ReferenceCode>
  <Url>https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
</FlexStatementResponse>"""


_AUTH_FAIL_XML = """<?xml version="1.0"?>
<FlexStatementResponse>
  <Status>Fail</Status>
  <ErrorCode>1003</ErrorCode>
  <ErrorMessage>Statement could not be generated. Please check the query id and token.</ErrorMessage>
</FlexStatementResponse>"""


_RETRYABLE_NOT_READY_XML = """<?xml version="1.0"?>
<FlexQueryResponse>
  <Status>Warn</Status>
  <ErrorCode>1019</ErrorCode>
  <ErrorMessage>Statement generation in progress.</ErrorMessage>
</FlexQueryResponse>"""


class TestParseFlexXml:
    def test_basic_shape(self) -> None:
        snapshot = parse_flex_xml(_SAMPLE_FLEX_XML)
        assert snapshot.account_summary.account_id == "U25655583"
        assert snapshot.account_summary.report_date == date(2026, 5, 12)
        assert snapshot.account_summary.net_liquidation_usd == Decimal("12123.45")
        assert snapshot.account_summary.futures_pnl_usd == Decimal("123.45")
        assert len(snapshot.positions) == 2
        assert len(snapshot.cash_balances) == 1
        assert snapshot.pulled_at_utc.tzinfo == UTC

    def test_positions_parsed_with_decimal_precision(self) -> None:
        snapshot = parse_flex_xml(_SAMPLE_FLEX_XML)
        mes_pos = next(p for p in snapshot.positions if p.symbol == "MES")
        assert mes_pos.quantity == Decimal("1")
        assert mes_pos.avg_cost_usd == Decimal("5234.50")
        assert mes_pos.market_price_usd == Decimal("5240.00")
        assert mes_pos.unrealized_pnl_usd == Decimal("27.50")

    def test_cash_balance(self) -> None:
        snapshot = parse_flex_xml(_SAMPLE_FLEX_XML)
        usd = next(c for c in snapshot.cash_balances if c.currency == "USD")
        assert usd.balance == Decimal("12000.00")
        assert usd.accrued_interest == Decimal("2.50")

    def test_malformed_xml_raises(self) -> None:
        with pytest.raises(FlexQueryFetchError) as exc_info:
            parse_flex_xml("<not-well-formed")
        assert exc_info.value.error_code == "MALFORMED_XML"

    def test_missing_account_information_raises(self) -> None:
        xml = """<?xml version="1.0"?>
        <FlexQueryResponse>
          <FlexStatements>
            <FlexStatement accountId="U1" fromDate="2026-05-12" toDate="2026-05-12">
              <EquitySummaryByReportDateInBase reportDate="2026-05-12" total="100"/>
            </FlexStatement>
          </FlexStatements>
        </FlexQueryResponse>"""
        with pytest.raises(FlexQueryFetchError, match="AccountInformation"):
            parse_flex_xml(xml)

    def test_ibkr_error_response_raises(self) -> None:
        with pytest.raises(FlexQueryFetchError) as exc_info:
            parse_flex_xml(_AUTH_FAIL_XML)
        assert exc_info.value.error_code == "IBKR_RETURNED_ERROR"
        assert exc_info.value.details["ibkr_error_code"] == "1003"


class TestParseFlexXmlUnderlyingSymbol:
    """Tests for the 2026-05-27 fix: parse the ``underlyingSymbol``
    attribute on ``<OpenPosition>`` so the broker view normalizes
    contract-month-form FUT rows (``"M2KM6"``) onto the root ticker
    (``"M2K"``) the backend's ``positions_current.market`` uses.

    See the false-positive break that fired EOD 2026-05-27: broker view
    showed market ``"/M2K"`` qty 0, backend showed ``/M2K`` qty 1 (correct
    per IBKR live broker state). Root cause hypothesis: FlexQuery template
    emitted FUT rows with ``symbol="M2KM6"`` + ``underlyingSymbol="M2K"``,
    pre-fix parser dropped underlyingSymbol on the floor + the view
    builder used ``f"/{symbol}"`` = ``"/M2KM6"`` which doesn't match
    backend's ``"/M2K"``.
    """

    def test_underlying_symbol_parsed_when_present(self) -> None:
        # M2KM6 = micro Russell 2000 June 2026; underlyingSymbol is the
        # root ticker M2K. This is the production-shape XML that the
        # 2026-05-27 EOD recon was processing.
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U25655583" fromDate="2026-05-27" toDate="2026-05-27">
      <AccountInformation accountId="U25655583" currency="USD" type="MARGIN"/>
      <EquitySummaryByReportDateInBase accountId="U25655583" reportDate="2026-05-27"
        cash="15000.00" stock="0.00" bond="0.00" futuresPNL="0.00" total="15000.00"/>
      <OpenPositions>
        <OpenPosition accountId="U25655583" symbol="M2KM6" underlyingSymbol="M2K"
          assetCategory="FUT" position="1" costBasisPrice="14625.62"
          markPrice="14700.00" positionValue="14700.00" fifoPnlUnrealized="74.38"/>
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        snapshot = parse_flex_xml(xml)
        assert len(snapshot.positions) == 1
        pos = snapshot.positions[0]
        assert pos.symbol == "M2KM6"
        assert pos.underlying_symbol == "M2K"
        assert pos.sec_type == "FUT"

    def test_underlying_symbol_none_when_attribute_absent(self) -> None:
        # Backwards-compat: the existing sample XML in this test file
        # never carried underlyingSymbol, so the parser must default to
        # None when the attribute is missing entirely. This locks the
        # backwards-compat contract for older FlexQuery templates.
        snapshot = parse_flex_xml(_SAMPLE_FLEX_XML)
        for pos in snapshot.positions:
            # Neither MES nor TLT in the sample carries the attribute.
            assert pos.underlying_symbol is None

    def test_underlying_symbol_none_when_attribute_empty(self) -> None:
        # Defensive: an empty-string underlyingSymbol attribute (which
        # IBKR doesn't currently emit, but which a future template change
        # could produce) is treated as absent. The downstream view
        # builder falls back to ``symbol``.
        xml = """<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements>
    <FlexStatement accountId="U1" fromDate="2026-05-27" toDate="2026-05-27">
      <AccountInformation accountId="U1" currency="USD" type="MARGIN"/>
      <EquitySummaryByReportDateInBase accountId="U1" reportDate="2026-05-27"
        cash="100" stock="0" bond="0" futuresPNL="0" total="100"/>
      <OpenPositions>
        <OpenPosition accountId="U1" symbol="MES" underlyingSymbol=""
          assetCategory="FUT" position="1" costBasisPrice="5230"/>
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        snapshot = parse_flex_xml(xml)
        assert snapshot.positions[0].underlying_symbol is None

    def test_underlying_symbol_strip_whitespace(self) -> None:
        # Defensive: any surrounding whitespace is stripped so a template
        # that emits ``" M2K "`` doesn't break the normalization.
        xml = """<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements>
    <FlexStatement accountId="U1" fromDate="2026-05-27" toDate="2026-05-27">
      <AccountInformation accountId="U1" currency="USD" type="MARGIN"/>
      <EquitySummaryByReportDateInBase accountId="U1" reportDate="2026-05-27"
        cash="100" stock="0" bond="0" futuresPNL="0" total="100"/>
      <OpenPositions>
        <OpenPosition accountId="U1" symbol="M2KM6" underlyingSymbol="  M2K  "
          assetCategory="FUT" position="1" costBasisPrice="14625.62"/>
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        snapshot = parse_flex_xml(xml)
        assert snapshot.positions[0].underlying_symbol == "M2K"


class TestIbkrFlexQueryClient:
    def test_constructor_rejects_invalid_id(self) -> None:
        with pytest.raises(ValueError, match="flex_query_id"):
            IbkrFlexQueryClient(flex_query_id=0, token="token")

    def test_constructor_rejects_empty_token(self) -> None:
        with pytest.raises(ValueError, match="token"):
            IbkrFlexQueryClient(flex_query_id=1, token="")

    @respx.mock
    async def test_happy_path_two_step_fetch(self) -> None:
        # Step 1: SendRequest → Success + ref code
        respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
        ).respond(200, text=_SUCCESS_SENDREQUEST_XML)
        # Step 2: GetStatement → actual XML
        respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
        ).respond(200, text=_SAMPLE_FLEX_XML)

        client = IbkrFlexQueryClient(flex_query_id=12345, token="test-token")
        snapshot = await client.fetch_snapshot()
        assert snapshot.account_summary.net_liquidation_usd == Decimal("12123.45")
        assert len(snapshot.positions) == 2

    @respx.mock
    async def test_auth_invalid_returns_AUTH_INVALID(self) -> None:
        respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
        ).respond(401, text="<error>auth failed</error>")
        client = IbkrFlexQueryClient(flex_query_id=12345, token="bad-token")
        with pytest.raises(FlexQueryFetchError) as exc_info:
            await client.fetch_snapshot()
        assert exc_info.value.error_code == "AUTH_INVALID"

    @respx.mock
    async def test_query_not_found_returns_QUERY_NOT_FOUND(self) -> None:
        respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
        ).respond(200, text=_AUTH_FAIL_XML)  # 1003 error code → QUERY_NOT_FOUND
        client = IbkrFlexQueryClient(flex_query_id=12345, token="test-token")
        with pytest.raises(FlexQueryFetchError) as exc_info:
            await client.fetch_snapshot()
        assert exc_info.value.error_code == "QUERY_NOT_FOUND"

    @respx.mock
    async def test_retryable_error_retries_until_ready(self) -> None:
        respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
        ).respond(200, text=_SUCCESS_SENDREQUEST_XML)
        # First 2 polls return "not ready" (1019), then statement is ready
        get_statement_route = respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
        )
        get_statement_route.side_effect = [
            httpx.Response(200, text=_RETRYABLE_NOT_READY_XML),
            httpx.Response(200, text=_RETRYABLE_NOT_READY_XML),
            httpx.Response(200, text=_SAMPLE_FLEX_XML),
        ]
        client = IbkrFlexQueryClient(
            flex_query_id=12345,
            token="test-token",
            poll_interval_seconds=0.001,  # fast test
            poll_max_attempts=5,
        )
        snapshot = await client.fetch_snapshot()
        assert snapshot.account_summary.account_id == "U25655583"
        # All 3 GetStatement calls were made
        assert get_statement_route.call_count == 3

    @respx.mock
    async def test_max_attempts_exhausted_raises(self) -> None:
        respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
        ).respond(200, text=_SUCCESS_SENDREQUEST_XML)
        respx.get(
            "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
        ).respond(200, text=_RETRYABLE_NOT_READY_XML)
        client = IbkrFlexQueryClient(
            flex_query_id=12345,
            token="test-token",
            poll_interval_seconds=0.001,
            poll_max_attempts=3,
        )
        with pytest.raises(FlexQueryFetchError) as exc_info:
            await client.fetch_snapshot()
        assert exc_info.value.error_code == "MAX_ATTEMPTS_EXHAUSTED"


class TestModuleContract:
    def test_public_surface(self) -> None:
        from services.reconciliation import flex_query_fetcher

        for name in (
            "FlexAccountSummary",
            "FlexCashBalance",
            "FlexPosition",
            "FlexQueryFetchError",
            "IbkrFlexQueryClient",
            "ReconciliationSnapshot",
            "parse_flex_xml",
        ):
            assert hasattr(flex_query_fetcher, name), f"missing export: {name}"
