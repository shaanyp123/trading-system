"""Reproduce-V1 logic: entry extraction, oracle load, cross-check (design §8)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from research.eval.reproduce_v1 import (
    Entry,
    OracleEntry,
    build_v1_run_spec,
    crosscheck_entries,
    entries_from_fills,
    first_entry_per_market,
    load_oracle,
    parse_v1_decisions_from_log,
)
from research.lean.results import OrderFill
from strategies.v1_trend_following.parameters import V1_CANDIDATE_UNIVERSE


def _write_v1_log(output_dir: Path, body: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "V1TrendFollowingAlgorithm-log.txt").write_text(body, encoding="utf-8")


def _reject_lines(session: str, markets: list[str]) -> list[str]:
    return [
        f"t v1_signal_rejected session_date={session} market={m} reason=no_breakout"
        for m in markets
    ]


def _generated_line(session: str, emitted: int, rejected: int) -> str:
    return (
        f"t v1_signals_generated session_date={session} signals_emitted_count={emitted} "
        f"rejections_count={rejected} reasons={{}}"
    )


def test_parse_v1_decisions_emitted_by_elimination(tmp_path: Path) -> None:
    # The universe is the candidate universe; on 05-06 /MNQ is the one market NOT
    # rejected with emitted_count=1 ⇒ the emitted decision. On 05-07 everything is
    # rejected (count=0) ⇒ no entry.
    others = [m for m in V1_CANDIDATE_UNIVERSE if m != "/MNQ"]
    lines = _reject_lines("2026-05-06", others)
    lines.append(_generated_line("2026-05-06", 1, len(others)))
    lines += _reject_lines("2026-05-07", list(V1_CANDIDATE_UNIVERSE))
    lines.append(_generated_line("2026-05-07", 0, len(V1_CANDIDATE_UNIVERSE)))
    _write_v1_log(tmp_path, "\n".join(lines))
    entries = parse_v1_decisions_from_log(tmp_path)
    assert entries == [Entry(date(2026, 5, 6), "/MNQ", "long")]


def test_parse_v1_decisions_market_emitted_every_cycle_is_captured(tmp_path: Path) -> None:
    # /MNQ is emitted on BOTH cycles and never rejected, so it never appears in the
    # log at all — only the V1_CANDIDATE_UNIVERSE seed can place it in the universe.
    # (A rejection-derived universe alone skipped every such cycle: false
    # missing_in_backtest.)
    others = [m for m in V1_CANDIDATE_UNIVERSE if m != "/MNQ"]
    lines: list[str] = []
    for session in ("2026-05-06", "2026-05-07"):
        lines += _reject_lines(session, others)
        lines.append(_generated_line(session, 1, len(others)))
    _write_v1_log(tmp_path, "\n".join(lines))
    entries = parse_v1_decisions_from_log(tmp_path)
    assert entries == [
        Entry(date(2026, 5, 6), "/MNQ", "long"),
        Entry(date(2026, 5, 7), "/MNQ", "long"),
    ]


def test_parse_v1_decisions_skips_ambiguous_cycle(tmp_path: Path) -> None:
    # count=1 but two markets are unrejected ⇒ elimination ambiguous ⇒ skip (no fabrication).
    others = [m for m in V1_CANDIDATE_UNIVERSE if m not in ("/MNQ", "TLT")]
    lines = _reject_lines("2026-05-08", others)
    lines.append(_generated_line("2026-05-08", 1, len(others)))
    _write_v1_log(tmp_path, "\n".join(lines))
    assert parse_v1_decisions_from_log(tmp_path) == []


def test_parse_v1_decisions_no_log_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="v1_signals_generated"):
        parse_v1_decisions_from_log(tmp_path)


def test_first_entry_per_market() -> None:
    entries = [
        Entry(date(2026, 5, 8), "/MNQ", "long"),
        Entry(date(2026, 5, 6), "/MNQ", "long"),  # earlier /MNQ — should win
        Entry(date(2026, 5, 10), "TLT", "long"),
    ]
    reduced = first_entry_per_market(entries)
    assert reduced == [
        Entry(date(2026, 5, 6), "/MNQ", "long"),
        Entry(date(2026, 5, 10), "TLT", "long"),
    ]


def _fill(market: str, day: date, qty: int) -> OrderFill:
    return OrderFill(market=market, fill_date=day, quantity=qty, direction=1 if qty > 0 else -1)


def test_entries_from_fills_flat_to_long() -> None:
    fills = [_fill("/MES", date(2026, 5, 28), 1), _fill("/M2K", date(2026, 5, 27), 1)]
    entries = entries_from_fills(fills)
    # Sorted by (date, market): M2K 05-27 first, then MES 05-28.
    assert entries == [
        Entry(date(2026, 5, 27), "/M2K", "long"),
        Entry(date(2026, 5, 28), "/MES", "long"),
    ]


def test_entries_from_fills_close_is_not_an_entry() -> None:
    fills = [_fill("/MES", date(2026, 5, 1), 1), _fill("/MES", date(2026, 5, 10), -1)]
    # Open then close to flat ⇒ exactly one entry (the open), the close is not one.
    assert entries_from_fills(fills) == [Entry(date(2026, 5, 1), "/MES", "long")]


def test_entries_from_fills_flip_counts_as_new_entry() -> None:
    fills = [_fill("/MES", date(2026, 5, 1), 1), _fill("/MES", date(2026, 5, 10), -2)]
    # +1 then -2 crosses flat to net short ⇒ a new short entry.
    assert entries_from_fills(fills) == [
        Entry(date(2026, 5, 1), "/MES", "long"),
        Entry(date(2026, 5, 10), "/MES", "short"),
    ]


def test_load_oracle_filters_non_entries(tmp_path: Path) -> None:
    payload = {
        "entries": [
            {"session_date": "2026-05-27", "market": "/M2K", "direction": "long"},
            {
                "session_date": "2026-05-30",
                "market": "/MES",
                "direction": "short",
                "signal_type": "exit",
            },
        ]
    }
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    oracle = load_oracle(path)
    assert oracle == [OracleEntry(date(2026, 5, 27), "/M2K", "long")]


def test_crosscheck_full_match() -> None:
    bt = [Entry(date(2026, 5, 27), "/M2K", "long"), Entry(date(2026, 5, 28), "/MES", "long")]
    oracle = [
        OracleEntry(date(2026, 5, 27), "/M2K", "long"),
        OracleEntry(date(2026, 5, 28), "/MES", "long"),
    ]
    report = crosscheck_entries(bt, oracle)
    assert report.match_rate == 1.0
    assert not report.missing_in_backtest and not report.extra_in_backtest


def test_crosscheck_reports_missing_and_extra() -> None:
    bt = [Entry(date(2026, 5, 28), "/MES", "long"), Entry(date(2026, 5, 29), "/MNQ", "long")]
    oracle = [
        OracleEntry(date(2026, 5, 27), "/M2K", "long"),
        OracleEntry(date(2026, 5, 28), "/MES", "long"),
    ]
    report = crosscheck_entries(bt, oracle)
    assert (date(2026, 5, 27), "/M2K", "long") in report.missing_in_backtest
    assert (date(2026, 5, 29), "/MNQ", "long") in report.extra_in_backtest
    assert report.match_rate == 0.5  # 1 of 2 oracle entries matched


def test_crosscheck_warns_on_non_long_oracle_directions() -> None:
    # Log-derived backtest entries carry a fixed "long" label; a short in the
    # oracle can falsely match/mismatch on direction — the warning flags that.
    bt = [Entry(date(2026, 5, 27), "/M2K", "long")]
    oracle = [
        OracleEntry(date(2026, 5, 27), "/M2K", "long"),
        OracleEntry(date(2026, 5, 28), "/MES", "short"),
    ]
    with capture_logs() as logs:
        crosscheck_entries(bt, oracle)
    warned = [e for e in logs if e["event"] == "research_v1_oracle_non_long_directions"]
    assert len(warned) == 1
    assert warned[0]["directions"] == ["short"]


def test_crosscheck_all_long_oracle_does_not_warn() -> None:
    bt = [Entry(date(2026, 5, 27), "/M2K", "long")]
    oracle = [OracleEntry(date(2026, 5, 27), "/M2K", "long")]
    with capture_logs() as logs:
        crosscheck_entries(bt, oracle)
    assert not [e for e in logs if e["event"] == "research_v1_oracle_non_long_directions"]


def test_crosscheck_window_filter() -> None:
    bt = [Entry(date(2026, 5, 27), "/M2K", "long"), Entry(date(2026, 6, 2), "/MES", "long")]
    oracle = [
        OracleEntry(date(2026, 5, 27), "/M2K", "long"),
        OracleEntry(date(2026, 6, 2), "/MES", "long"),
    ]
    # Restrict to a single-REGIME window cut by date (prod phashes are
    # per-signal — PR D) ⇒ only 05-27 compared.
    report = crosscheck_entries(bt, oracle, window=(date(2026, 5, 26), date(2026, 5, 28)))
    assert report.oracle_count == 1
    assert report.match_rate == 1.0


def test_build_v1_run_spec_raises_post_decommission() -> None:
    # Crypto-pivot C0 (2026-07-08): the production lean/ dir (lean.json +
    # v1_strategy.py) was deleted with the LEAN stack, so building the
    # reproduce-V1 run spec now fails loudly at the production-parameter
    # read (never a silent fallback). The harness is removed alongside
    # strategies/v1_trend_following in the risk-review'd deletion PR.
    with pytest.raises(FileNotFoundError):
        build_v1_run_spec(Path("research/data/cache/lean_bars"))
