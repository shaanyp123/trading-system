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
    DbCashSweep,
    DbTrade,
    ParsedArgs,
    StatementLine,
    a1_tolerance,
    build_report,
    classify_line,
    match_trades,
    parse_statement_rows,
    reclassify_sweep_lines,
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


def _sweep(
    *,
    sweep_id: int = 1,
    direction: str = "to_yield",
    amount: str = "200.00",
    completed: datetime = datetime(2026, 7, 25, 0, 25, 30, tzinfo=UTC),
) -> DbCashSweep:
    return DbCashSweep(
        sweep_id=sweep_id,
        direction=direction,
        amount_usd=Decimal(amount),
        completed_at_utc=completed,
    )


class TestSweepClassificationKeyword:
    def test_futures_sweep_line_is_capital_shaped_not_trade(self) -> None:
        # Without the ``sweep`` needle this line would hit ``futures`` and
        # pollute the A1 trade bucket — the exact pollution this exists
        # to stop.
        assert classify_line("transfer", "futures sweep") == "capital_event"
        assert classify_line("sweep", "scheduled sweep to CFM") == "capital_event"


class TestSweepReclassification:
    def test_zero_sweeps_is_bit_identical(self) -> None:
        """Dormant cash manager (today's reality): no reclassification,
        no unmatched flags, classification untouched."""
        when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        lines = [
            _line(amount="200.00", raw_type="deposit", description="USDC conversion", when=when)
        ]
        out, matches, unmatched = reclassify_sweep_lines(lines, [])
        assert out == lines
        assert matches == ()
        assert unmatched == ()

    def test_conversion_line_matching_sweep_reclassifies(self) -> None:
        when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        lines = [
            _line(amount="-200.00", raw_type="conversion", description="USD to USDC", when=when)
        ]
        out, matches, unmatched = reclassify_sweep_lines(lines, [_sweep()])
        assert out[0].line_class == "sweep"
        assert len(matches) == 1
        assert matches[0].sweep.sweep_id == 1
        assert unmatched == ()

    def test_amount_mismatch_stays_capital_event(self) -> None:
        when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        lines = [
            _line(amount="-150.00", raw_type="conversion", description="USD to USDC", when=when)
        ]
        out, matches, unmatched = reclassify_sweep_lines(lines, [_sweep()])
        assert out[0].line_class == "capital_event"
        assert matches == ()
        # The sweep is inside the statement's coverage but unexplained.
        assert [s.sweep_id for s in unmatched] == [1]

    def test_deposit_keyword_is_not_eligible(self) -> None:
        """A genuine capital movement equal to a sweep amount must stay
        in the capital bucket — deposit/withdrawal lines are never
        reclassified on an amount coincidence."""
        when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        lines = [_line(amount="200.00", raw_type="deposit", description="wire in", when=when)]
        out, matches, _ = reclassify_sweep_lines(lines, [_sweep()])
        assert out[0].line_class == "capital_event"
        assert matches == ()

    def test_dateless_line_stays_capital_event(self) -> None:
        lines = [_line(amount="200.00", raw_type="conversion", description="USDC", when=None)]
        out, matches, unmatched = reclassify_sweep_lines(lines, [_sweep()])
        assert out[0].line_class == "capital_event"
        assert matches == ()
        # No dated lines at all ⇒ no coverage anchor ⇒ nothing flagged.
        assert unmatched == ()

    def test_day_slack_boundary(self) -> None:
        completed = datetime(2026, 7, 25, 0, 25, tzinfo=UTC)
        one_day_off = datetime(2026, 7, 26, 23, 0, tzinfo=UTC)
        two_days_off = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
        inside = [
            _line(amount="200.00", raw_type="conversion", description="USDC", when=one_day_off)
        ]
        out, matches, _ = reclassify_sweep_lines(inside, [_sweep(completed=completed)])
        assert out[0].line_class == "sweep"
        assert len(matches) == 1
        outside = [
            _line(amount="200.00", raw_type="conversion", description="USDC", when=two_days_off)
        ]
        out, matches, _ = reclassify_sweep_lines(outside, [_sweep(completed=completed)])
        assert out[0].line_class == "capital_event"
        assert matches == ()

    def test_to_margin_two_leg_claims_both_lines(self) -> None:
        """A reclaim produces a convert line AND a futures-sweep line for
        the same amount — one sweep row legitimately claims both."""
        when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        sweep = _sweep(direction="to_margin", amount="300.00", completed=when)
        lines = [
            _line(amount="300.00", raw_type="conversion", description="USDC to USD", when=when),
            _line(amount="-300.00", raw_type="transfer", description="futures sweep", when=when),
        ]
        out, matches, unmatched = reclassify_sweep_lines(lines, [sweep])
        assert [line.line_class for line in out] == ["sweep", "sweep"]
        assert len(matches) == 2
        assert unmatched == ()

    def test_nearest_completion_date_wins(self) -> None:
        when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        same_day = _sweep(sweep_id=7, completed=datetime(2026, 7, 25, 0, 25, tzinfo=UTC))
        day_off = _sweep(sweep_id=3, completed=datetime(2026, 7, 24, 0, 25, tzinfo=UTC))
        lines = [_line(amount="200.00", raw_type="conversion", description="USDC", when=when)]
        _, matches, _ = reclassify_sweep_lines(lines, [day_off, same_day])
        assert matches[0].sweep.sweep_id == 7

    def test_unmatched_sweep_outside_statement_coverage_not_flagged(self) -> None:
        when = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        far_sweep = _sweep(completed=datetime(2026, 7, 1, 0, 25, tzinfo=UTC))
        lines = [_line(amount="-53.52", when=when)]  # trade line anchors coverage
        _, matches, unmatched = reclassify_sweep_lines(lines, [far_sweep])
        assert matches == ()
        assert unmatched == ()


class TestSweepReport:
    def test_sweep_lines_leave_capital_bucket_and_verdict_clean(self) -> None:
        closed = datetime(2026, 7, 25, 0, 5, 20, tzinfo=UTC)
        trades = [_trade(pnl="-53.5185", closed=closed)]
        lines = [
            _line(amount="-53.52", when=closed),
            _line(
                amount="-200.00",
                raw_type="conversion",
                description="USD to USDC",
                when=datetime(2026, 7, 25, 0, 26, tzinfo=UTC),
            ),
            _line(
                amount="802.10",
                raw_type="deposit",
                description="USDC conversion",
                when=datetime(2026, 7, 25, 9, 0, tzinfo=UTC),
            ),
        ]
        report = build_report(trades, lines, [_sweep()])
        assert report.all_passed is True
        assert report.class_totals["sweep"] == Decimal("-200.00")
        # The genuine capital movement is untouched.
        assert report.class_totals["capital_event"] == Decimal("802.10")
        assert len(report.sweep_matches) == 1

    def test_unmatched_sweep_flips_verdict(self) -> None:
        closed = datetime(2026, 7, 25, 0, 5, 20, tzinfo=UTC)
        trades = [_trade(pnl="-53.5185", closed=closed)]
        lines = [_line(amount="-53.52", when=closed)]
        report = build_report(trades, lines, [_sweep()])
        assert report.matches[0].passed is True
        assert [s.sweep_id for s in report.unmatched_sweeps] == [1]
        assert report.all_passed is False

    def test_no_sweeps_arg_matches_legacy_call_shape(self) -> None:
        """build_report without the sweeps arg is the pre-PR contract —
        every existing call site and test stays valid."""
        closed = datetime(2026, 7, 25, 0, 5, 20, tzinfo=UTC)
        report = build_report(
            [_trade(pnl="-53.5185", closed=closed)],
            [_line(amount="-53.52", when=closed)],
        )
        assert report.sweep_matches == ()
        assert report.unmatched_sweeps == ()
        assert report.all_passed is True
