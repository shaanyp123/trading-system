"""Unit tests for scripts/operator_tools/reconcile_statement.py (pure half).

The tool is read-only; the DB loader is exercised in integration. Here:

* line classification (funding / subscription / capital / reward / fee /
  trade / unattributed) — modeled on the 2026-07-14 → 2026-07-18 venue
  lines from the decisions-log P&L verification
* amount parsing (plain / $ / commas / accounting-negative / garbage)
* A1 tolerance math (±$1 or ±2%, whichever larger)
* trade matching (nearest amount, same-day tiebreak, absent line = FAIL)
* report assembly incl. the Jul-17 scenario: an unattributed
  subscription-shaped debit must surface loudly
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from scripts.operator_tools.reconcile_statement import (
    DbTrade,
    ParsedArgs,
    StatementLine,
    a1_tolerance,
    build_report,
    classify_line,
    match_trades,
    parse_statement_rows,
)


def _args(**overrides: object) -> ParsedArgs:
    defaults: dict[str, object] = {
        "statement_path": Path("/dev/null"),
        "since": None,
        "until": None,
        "col_time": "time",
        "col_type": "type",
        "col_amount": "amount",
        "col_description": "description",
    }
    defaults.update(overrides)
    return ParsedArgs(**defaults)  # type: ignore[arg-type]


def _line(
    *,
    amount: str,
    raw_type: str = "trade",
    description: str = "futures pnl",
    when: datetime | None = None,
) -> StatementLine:
    return StatementLine(
        occurred_at=when,
        raw_type=raw_type,
        description=description,
        amount_usd=Decimal(amount),
        line_class=classify_line(raw_type, description),
    )


def _trade(*, pnl: str, closed: datetime) -> DbTrade:
    return DbTrade(
        trade_id=uuid4(),
        market="BTC",
        direction="short",
        closed_at_utc=closed,
        realized_pnl_usd=Decimal(pnl),
    )


class TestClassification:
    def test_observed_venue_lines(self) -> None:
        # The 2026-07-14 → 2026-07-18 movements from the decisions-log.
        assert classify_line("funding", "hourly funding settlement") == "funding"
        assert classify_line("charge", "Coinbase One annual") == "subscription"
        assert classify_line("deposit", "USDC conversion") == "capital_event"
        assert classify_line("credit", "USDC reward") == "reward"
        assert classify_line("trade", "futures settlement BIP-20DEC30-CDE") == "trade_pnl"
        assert classify_line("fee", "exchange commission") == "fee"
        assert classify_line("???", "mystery venue line") == "unattributed"

    def test_funding_beats_trade_and_subscription_beats_fee(self) -> None:
        assert classify_line("trade", "funding on futures position") == "funding"
        assert classify_line("fee", "Coinbase One subscription fee") == "subscription"


class TestParsing:
    def test_amount_styles_and_rejects(self) -> None:
        rows = [
            {
                "time": "2026-07-17T09:21:04Z",
                "type": "charge",
                "description": "Coinbase One",
                "amount": "(52.13)",
            },
            {"time": "", "type": "trade", "description": "futures pnl", "amount": "$-53.52"},
            {"time": "", "type": "trade", "description": "pnl", "amount": "1,234.56"},
            {"time": "", "type": "junk", "description": "no amount", "amount": "abc"},
        ]
        lines, rejects = parse_statement_rows(rows, args=_args())
        assert [line.amount_usd for line in lines] == [
            Decimal("-52.13"),
            Decimal("-53.52"),
            Decimal("1234.56"),
        ]
        assert lines[0].line_class == "subscription"
        assert len(rejects) == 1

    def test_since_until_window_filters_dated_lines(self) -> None:
        rows = [
            {"time": "2026-07-01T00:00:00Z", "type": "trade", "description": "pnl", "amount": "1"},
            {"time": "2026-07-15T00:00:00Z", "type": "trade", "description": "pnl", "amount": "2"},
        ]
        lines, _ = parse_statement_rows(
            rows, args=_args(since=datetime(2026, 7, 9, tzinfo=UTC).date())
        )
        assert [line.amount_usd for line in lines] == [Decimal("2")]


class TestTolerance:
    def test_abs_floor_and_relative(self) -> None:
        assert a1_tolerance(Decimal("10")) == Decimal("1")  # $1 floor
        assert a1_tolerance(Decimal("-100")) == Decimal("2.00")  # 2% of 100
        assert a1_tolerance(Decimal("0")) == Decimal("1")


class TestMatching:
    def test_jul16_close_matches_within_tolerance(self) -> None:
        closed = datetime(2026, 7, 16, 0, 5, 20, tzinfo=UTC)
        trades = [_trade(pnl="-53.5185", closed=closed)]
        lines = [_line(amount="-53.52", when=closed)]
        matches = match_trades(trades, lines)
        assert len(matches) == 1
        assert matches[0].passed is True
        assert matches[0].difference == Decimal("-0.0015")

    def test_absent_line_fails_not_skips(self) -> None:
        trades = [_trade(pnl="-53.52", closed=datetime(2026, 7, 16, tzinfo=UTC))]
        matches = match_trades(trades, [])
        assert matches[0].passed is False
        assert matches[0].statement_amount is None

    def test_nearest_amount_wins_same_day_breaks_ties(self) -> None:
        closed = datetime(2026, 7, 16, tzinfo=UTC)
        trades = [_trade(pnl="-53.52", closed=closed)]
        lines = [
            _line(amount="-40.00", when=closed),
            _line(amount="-53.52", when=datetime(2026, 7, 12, tzinfo=UTC)),
        ]
        matches = match_trades(trades, lines)
        # Exact amount beats same-day-but-wrong amount.
        assert matches[0].statement_amount == Decimal("-53.52")
        assert matches[0].passed is True

    def test_each_line_consumed_once(self) -> None:
        closed = datetime(2026, 7, 16, tzinfo=UTC)
        trades = [
            _trade(pnl="-53.52", closed=closed),
            _trade(pnl="-53.52", closed=closed),
        ]
        lines = [_line(amount="-53.52", when=closed)]
        matches = match_trades(trades, lines)
        assert [m.passed for m in matches] == [True, False]


class TestReport:
    def test_jul17_shape_unattributed_debit_surfaces(self) -> None:
        """A subscription-shaped debit that the classifier does NOT
        recognize must land in UNATTRIBUTED and flip the verdict —
        exactly the line that took three sessions to attribute live."""
        closed = datetime(2026, 7, 16, 0, 5, 20, tzinfo=UTC)
        trades = [_trade(pnl="-53.5185", closed=closed)]
        lines = [
            _line(amount="-53.52", when=closed),
            _line(amount="3.98", raw_type="funding", description="funding credits"),
            _line(amount="802.10", raw_type="deposit", description="USDC conversion"),
            _line(amount="-52.13", raw_type="???", description="mystery debit"),
        ]
        report = build_report(trades, lines)
        assert report.matches[0].passed is True
        assert len(report.unattributed) == 1
        assert report.unattributed[0].amount_usd == Decimal("-52.13")
        assert report.all_passed is False
        assert report.class_totals["capital_event"] == Decimal("802.10")

    def test_clean_report(self) -> None:
        closed = datetime(2026, 7, 16, 0, 5, 20, tzinfo=UTC)
        trades = [_trade(pnl="-53.5185", closed=closed)]
        lines = [
            _line(amount="-53.52", when=closed),
            _line(amount="-52.13", raw_type="charge", description="Coinbase One annual"),
        ]
        report = build_report(trades, lines)
        assert report.all_passed is True
        assert report.class_totals["subscription"] == Decimal("-52.13")
        assert report.db_realized_pnl_total == Decimal("-53.5185")
