"""Unit + integration tests for ``services.data.map_file_synthesis``.

Coverage:

* TestModuleConstants — locked DataMappingMode int + persistence
  default + sentinel dates
* TestUniverseSession / TestFuturesRollTransition / TestMapFileSynthesisResult
  — frozen dataclass shape
* TestParseSessionDateFromFilename — private helper handling YYYYMMDD
  filenames + malformed
* TestParseExpiryFromUniverseBody — header skip + YYYYMM validation
* TestParseUniverseFile — happy + missing-file + malformed body
* TestIterUniverseSessions — sorted output + invalid files skipped
* TestDetectRealRolls — empty, single-expiry, clean-rolls, noisy-then-clean,
  the operator's /MES-like flip-flop pattern, persistence_days=1 boundary,
  persistence_days=0 raises
* TestBuildFuturesMapFileWithRolls — empty rolls, single roll, multiple
  rolls, mode override, lowercase normalization
* TestSynthesizeFuturesMapFile — integration via tmp_path
* TestIdempotency — synthesis twice → byte-identical + mtime preserved on
  no-content-change cycle
* TestModuleContract — public ``__all__``

Pure-Python tests; no testcontainers; no IBKR I/O. A22 N/A.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from services.data import map_file_synthesis as mfs
from services.data.map_file_synthesis import (
    DATA_MAPPING_MODE_OPEN_INTEREST,
    DEFAULT_PERSISTENCE_DAYS,
    END_SENTINEL_DATE,
    INCEPTION_SENTINEL_DATE,
    FuturesRollTransition,
    MapFileSynthesisResult,
    UniverseSession,
    build_futures_map_file_with_rolls,
    detect_real_rolls,
    iter_universe_sessions,
    parse_universe_file,
    synthesize_futures_map_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_universe_file(
    *,
    universe_dir: Path,
    session_date: date,
    expiry: str,
    extra_fields: str = "100,101,99,100.5,12345,67890",
) -> Path:
    """Write a synthetic universe file matching bar_sync's on-disk shape."""
    universe_dir.mkdir(parents=True, exist_ok=True)
    body = f"#expiry,open,high,low,close,volume,open_interest\n{expiry},{extra_fields}\n"
    path = universe_dir / f"{session_date:%Y%m%d}.csv"
    path.write_text(body, encoding="utf-8")
    return path


def _make_sessions(
    pattern: list[tuple[str, int]], *, start_date: date = date(2024, 1, 1)
) -> list[UniverseSession]:
    """Build a session list from (expiry, count) pairs.

    ``[("202403", 30), ("202406", 50)]`` → 30 sessions of expiry 202403
    followed by 50 sessions of expiry 202406, all in business-day order
    starting at ``start_date``.

    Session dates use stdlib weekday math — Saturdays + Sundays are
    skipped to mimic trading sessions. This makes the test data
    realistic-looking without depending on a real calendar.
    """
    sessions: list[UniverseSession] = []
    current = start_date
    for expiry, count in pattern:
        emitted = 0
        while emitted < count:
            if current.weekday() < 5:  # Mon-Fri
                sessions.append(UniverseSession(session_date=current, expiry=expiry))
                emitted += 1
            # advance one calendar day
            current = date.fromordinal(current.toordinal() + 1)
    return sessions


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_data_mapping_mode_open_interest_is_2(self) -> None:
        # Verified against QuantConnect/Lean/Common/Global.cs enum order:
        # LastTradingDay=0, FirstDayMonth=1, OpenInterest=2, OpenInterestAnnual=3.
        assert DATA_MAPPING_MODE_OPEN_INTEREST == 2

    def test_default_persistence_days_is_15(self) -> None:
        # Operator-recommended in the 2026-05-22 brief; locked here for
        # regression purposes.
        assert DEFAULT_PERSISTENCE_DAYS == 15

    def test_inception_sentinel_date_is_18991230(self) -> None:
        assert INCEPTION_SENTINEL_DATE == date(1899, 12, 30)

    def test_end_sentinel_date_is_20501231(self) -> None:
        assert END_SENTINEL_DATE == date(2050, 12, 31)


# ---------------------------------------------------------------------------
# Frozen dataclass shape
# ---------------------------------------------------------------------------


class TestUniverseSession:
    def test_immutable(self) -> None:
        s = UniverseSession(session_date=date(2026, 5, 19), expiry="202606")
        with pytest.raises((AttributeError, TypeError)):
            s.expiry = "202609"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = UniverseSession(session_date=date(2026, 5, 19), expiry="202606")
        b = UniverseSession(session_date=date(2026, 5, 19), expiry="202606")
        assert a == b


class TestFuturesRollTransition:
    def test_immutable(self) -> None:
        t = FuturesRollTransition(
            boundary_date=date(2026, 5, 19),
            from_expiry="202603",
            to_expiry="202606",
            persisted_sessions=42,
        )
        with pytest.raises((AttributeError, TypeError)):
            t.persisted_sessions = 0  # type: ignore[misc]


class TestMapFileSynthesisResult:
    def test_immutable(self) -> None:
        r = MapFileSynthesisResult(
            ticker="mes",
            market_dir="cme",
            market_code="CME",
            rolls=(),
            map_file_path=Path("/tmp/x"),
            content_changed=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            r.content_changed = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Private parsing helpers
# ---------------------------------------------------------------------------


class TestParseSessionDateFromFilename:
    def test_valid_yyyymmdd(self) -> None:
        assert mfs._parse_session_date_from_filename("20260519.csv") == date(2026, 5, 19)

    def test_non_csv_extension(self) -> None:
        assert mfs._parse_session_date_from_filename("20260519.txt") is None

    def test_short_stem(self) -> None:
        assert mfs._parse_session_date_from_filename("2026.csv") is None

    def test_non_digit_stem(self) -> None:
        assert mfs._parse_session_date_from_filename("20260S19.csv") is None

    def test_invalid_date_components(self) -> None:
        # Feb 30 doesn't exist.
        assert mfs._parse_session_date_from_filename("20260230.csv") is None


class TestParseExpiryFromUniverseBody:
    def test_header_plus_data_row(self) -> None:
        body = "#expiry,open,high,low,close,volume,open_interest\n202606,7467.5,7524,7466.5,7484,1148753,292401\n"
        assert mfs._parse_expiry_from_universe_body(body) == "202606"

    def test_multiple_data_rows_first_wins(self) -> None:
        body = "#header\n202606,1\n202609,2\n"
        assert mfs._parse_expiry_from_universe_body(body) == "202606"

    def test_empty_body_returns_none(self) -> None:
        assert mfs._parse_expiry_from_universe_body("") is None

    def test_header_only_returns_none(self) -> None:
        assert mfs._parse_expiry_from_universe_body("#expiry,o,h,l,c,v,oi\n") is None

    def test_malformed_expiry_returns_none(self) -> None:
        # 5-digit (not 6)
        body = "#hdr\n20260,1\n"
        assert mfs._parse_expiry_from_universe_body(body) is None

    def test_non_digit_expiry_returns_none(self) -> None:
        body = "#hdr\n2026XX,1\n"
        assert mfs._parse_expiry_from_universe_body(body) is None

    def test_invalid_month_returns_none(self) -> None:
        body = "#hdr\n202613,1\n"
        assert mfs._parse_expiry_from_universe_body(body) is None

    def test_invalid_year_returns_none(self) -> None:
        body = "#hdr\n189906,1\n"  # year < 1900
        assert mfs._parse_expiry_from_universe_body(body) is None

    def test_blank_lines_between_header_and_data(self) -> None:
        body = "#hdr\n\n202606,1\n"
        assert mfs._parse_expiry_from_universe_body(body) == "202606"


# ---------------------------------------------------------------------------
# parse_universe_file (filesystem IO)
# ---------------------------------------------------------------------------


class TestParseUniverseFile:
    def test_happy_path(self, tmp_path: Path) -> None:
        path = _write_universe_file(
            universe_dir=tmp_path,
            session_date=date(2026, 5, 19),
            expiry="202606",
        )
        assert parse_universe_file(path) == "202606"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_universe_file(tmp_path / "nonexistent.csv") is None

    def test_unreadable_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "garbled.csv"
        # Random bytes that don't decode as utf-8.
        path.write_bytes(b"\xff\xfe\x00\x00not-utf-8")
        # Either UnicodeDecodeError or our defensive None — both acceptable.
        assert parse_universe_file(path) is None


# ---------------------------------------------------------------------------
# iter_universe_sessions
# ---------------------------------------------------------------------------


class TestIterUniverseSessions:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        sessions = list(iter_universe_sessions(tmp_path / "no-such-dir"))
        assert sessions == []

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        assert list(iter_universe_sessions(d)) == []

    def test_sorted_by_session_date(self, tmp_path: Path) -> None:
        # Write in non-monotonic order; iterator must sort ascending.
        for d in (date(2026, 5, 19), date(2026, 5, 17), date(2026, 5, 18)):
            _write_universe_file(universe_dir=tmp_path, session_date=d, expiry="202606")
        sessions = list(iter_universe_sessions(tmp_path))
        assert [s.session_date for s in sessions] == [
            date(2026, 5, 17),
            date(2026, 5, 18),
            date(2026, 5, 19),
        ]

    def test_skips_non_csv_files(self, tmp_path: Path) -> None:
        _write_universe_file(universe_dir=tmp_path, session_date=date(2026, 5, 19), expiry="202606")
        (tmp_path / "README.md").write_text("hi")
        (tmp_path / "junk.txt").write_text("more junk")
        sessions = list(iter_universe_sessions(tmp_path))
        assert len(sessions) == 1
        assert sessions[0].session_date == date(2026, 5, 19)

    def test_skips_malformed_filename(self, tmp_path: Path) -> None:
        _write_universe_file(universe_dir=tmp_path, session_date=date(2026, 5, 19), expiry="202606")
        # Wrong filename shape — should be silently skipped.
        (tmp_path / "not-a-date.csv").write_text("#hdr\n202606,1\n")
        sessions = list(iter_universe_sessions(tmp_path))
        assert len(sessions) == 1

    def test_skips_malformed_body(self, tmp_path: Path) -> None:
        _write_universe_file(universe_dir=tmp_path, session_date=date(2026, 5, 19), expiry="202606")
        # Filename valid but body unparseable.
        (tmp_path / "20260520.csv").write_text("garbage-no-expiry-here")
        sessions = list(iter_universe_sessions(tmp_path))
        assert len(sessions) == 1
        assert sessions[0].session_date == date(2026, 5, 19)


# ---------------------------------------------------------------------------
# Roll detection — the noise filter
# ---------------------------------------------------------------------------


class TestDetectRealRolls:
    def test_empty_sessions(self) -> None:
        assert detect_real_rolls([]) == []

    def test_single_session(self) -> None:
        sessions = [UniverseSession(session_date=date(2026, 5, 19), expiry="202606")]
        assert detect_real_rolls(sessions) == []

    def test_single_stable_expiry_no_rolls(self) -> None:
        # 50 sessions all with the same expiry — no transitions exist.
        sessions = _make_sessions([("202606", 50)])
        assert detect_real_rolls(sessions) == []

    def test_two_clean_runs_one_roll(self) -> None:
        # 30 sessions of E1 → 30 sessions of E2 with persistence_days=15.
        sessions = _make_sessions([("202603", 30), ("202606", 30)])
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert len(rolls) == 1
        assert rolls[0].from_expiry == "202603"
        assert rolls[0].to_expiry == "202606"
        # Boundary date is the FIRST session of the new stable run.
        assert rolls[0].boundary_date == sessions[30].session_date

    def test_three_clean_runs_two_rolls(self) -> None:
        sessions = _make_sessions(
            [("202603", 25), ("202606", 25), ("202609", 25)],
        )
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert len(rolls) == 2
        assert (rolls[0].from_expiry, rolls[0].to_expiry) == ("202603", "202606")
        assert (rolls[1].from_expiry, rolls[1].to_expiry) == ("202606", "202609")

    def test_single_noisy_transition_filtered(self) -> None:
        # 30 stable E1 → 5 noise E2 → 30 stable E1. The 5-session E2 run
        # is shorter than persistence=15 → filtered as noise → no rolls.
        # CRITICAL: the two surviving stable E1 runs MUST NOT be coalesced
        # into a same-expiry transition (E1→E1). Regression for a bug
        # caught in the first test pass where the algorithm emitted a
        # zero-information FuturesRollTransition(from='E1', to='E1').
        sessions = _make_sessions(
            [("202603", 30), ("202606", 5), ("202603", 30)],
        )
        assert detect_real_rolls(sessions, persistence_days=15) == []

    def test_same_expiry_stable_runs_never_emit_transition(self) -> None:
        # Stable runs of the SAME expiry separated by short noise patches
        # never produce a roll (the same-expiry skip handles this).
        sessions = _make_sessions(
            [("E1", 30), ("E2", 5), ("E1", 30), ("E3", 5), ("E1", 30)],
        )
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert rolls == []

    def test_mes_flip_flop_pattern_collapses_to_one_roll(self) -> None:
        # Recreates the operator's /MES "2025-09-12 → 2025-10-27 oscillates
        # between 202606 and 202512 ~14 times in 6 weeks" pattern. Stable
        # 202603 prefix → flip-flop period → stable 202606 suffix.
        flip_flop_pattern: list[tuple[str, int]] = []
        for _ in range(14):
            flip_flop_pattern.extend([("202606", 2), ("202603", 2)])
        sessions = _make_sessions(
            [("202603", 30), *flip_flop_pattern, ("202606", 30)],
        )
        rolls = detect_real_rolls(sessions, persistence_days=15)
        # Every flip-flop run is <15 → noise. Only one genuine roll.
        assert len(rolls) == 1
        assert rolls[0].from_expiry == "202603"
        assert rolls[0].to_expiry == "202606"

    def test_persistence_days_one_passes_every_transition(self) -> None:
        sessions = _make_sessions([("E1", 1), ("E2", 1), ("E3", 1)])
        rolls = detect_real_rolls(sessions, persistence_days=1)
        assert len(rolls) == 2

    def test_persistence_days_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="persistence_days"):
            detect_real_rolls([], persistence_days=0)

    def test_persistence_days_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="persistence_days"):
            detect_real_rolls([], persistence_days=-1)

    def test_first_run_shorter_than_persistence_dropped(self) -> None:
        # 5 sessions of E1 (noise) → 30 sessions of E2 (stable).
        # No genuine rolls — the E2 prefix is the first "stable" run.
        sessions = _make_sessions([("E1", 5), ("E2", 30)])
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert rolls == []

    def test_persisted_sessions_recorded(self) -> None:
        sessions = _make_sessions([("E1", 20), ("E2", 40)])
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert len(rolls) == 1
        assert rolls[0].persisted_sessions == 40

    def test_iterable_input_accepted(self) -> None:
        # Generator input should work (not just list).
        def gen() -> object:
            yield UniverseSession(session_date=date(2026, 1, 1), expiry="E1")
            yield UniverseSession(session_date=date(2026, 1, 2), expiry="E1")

        assert detect_real_rolls(gen(), persistence_days=1) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Map_file content rendering
# ---------------------------------------------------------------------------


class TestBuildFuturesMapFileWithRolls:
    def test_empty_rolls_writes_2_sentinels(self) -> None:
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=[],
        )
        assert content == "18991230,mes,CME\n20501231,mes,CME,2\n"

    def test_single_roll_emits_3_rows(self) -> None:
        rolls = [
            FuturesRollTransition(
                boundary_date=date(2026, 6, 20),
                from_expiry="202603",
                to_expiry="202606",
                persisted_sessions=45,
            )
        ]
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=rolls,
        )
        expected = "18991230,mes,CME\n20260620,mes,CME,2\n20501231,mes,CME,2\n"
        assert content == expected

    def test_multiple_rolls_in_order(self) -> None:
        rolls = [
            FuturesRollTransition(
                boundary_date=date(2026, 3, 20),
                from_expiry="202512",
                to_expiry="202603",
                persisted_sessions=50,
            ),
            FuturesRollTransition(
                boundary_date=date(2026, 6, 20),
                from_expiry="202603",
                to_expiry="202606",
                persisted_sessions=50,
            ),
        ]
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=rolls,
        )
        lines = content.splitlines()
        assert lines[0] == "18991230,mes,CME"
        assert lines[1] == "20260320,mes,CME,2"
        assert lines[2] == "20260620,mes,CME,2"
        assert lines[3] == "20501231,mes,CME,2"
        assert len(lines) == 4

    def test_ticker_lowercased(self) -> None:
        content = build_futures_map_file_with_rolls(
            ticker="mEs",
            market_code="CME",
            rolls=[],
        )
        assert ",mes," in content
        assert "MES" not in content

    def test_market_code_preserved_uppercase(self) -> None:
        content = build_futures_map_file_with_rolls(
            ticker="MGC",
            market_code="COMEX",
            rolls=[],
        )
        assert "COMEX" in content

    def test_mode_int_override(self) -> None:
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=[
                FuturesRollTransition(
                    boundary_date=date(2026, 6, 20),
                    from_expiry="202603",
                    to_expiry="202606",
                    persisted_sessions=20,
                )
            ],
            data_mapping_mode_int=1,  # FirstDayMonth instead of OpenInterest
        )
        # Roll row + end sentinel both carry the override.
        assert "20260620,mes,CME,1" in content
        assert "20501231,mes,CME,1" in content
        # No mode-2 rows in this output.
        assert ",CME,2" not in content

    def test_trailing_newline(self) -> None:
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=[],
        )
        assert content.endswith("\n")
        assert not content.endswith("\n\n")  # single trailing newline


# ---------------------------------------------------------------------------
# Top-level synthesizer integration
# ---------------------------------------------------------------------------


class TestSynthesizeFuturesMapFile:
    def test_writes_map_file_at_expected_path(self, tmp_path: Path) -> None:
        # Populate universes with 30-day runs of two expiries.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        sessions = _make_sessions([("202603", 30), ("202606", 30)])
        for s in sessions:
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        result = synthesize_futures_map_file(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
        )
        assert result.map_file_path == tmp_path / "future" / "cme" / "map_files" / "mes.csv"
        assert result.map_file_path.exists()
        assert result.content_changed is True
        assert len(result.rolls) == 1

    def test_content_matches_expected_format(self, tmp_path: Path) -> None:
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        sessions = _make_sessions([("202603", 30), ("202606", 30)])
        for s in sessions:
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        result = synthesize_futures_map_file(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
        )
        content = result.map_file_path.read_text(encoding="utf-8")
        # Inception sentinel + 1 roll row + end sentinel.
        lines = content.splitlines()
        assert lines[0] == "18991230,mes,CME"
        assert lines[-1] == "20501231,mes,CME,2"
        # The middle row pins the new expiry's first session-date.
        boundary_idx = next(
            i
            for i, ln in enumerate(lines)
            if ln.startswith("2") and ln != lines[0] and ln != lines[-1]
        )
        assert ",mes,CME,2" in lines[boundary_idx]

    def test_empty_universe_dir_writes_sentinel_only(self, tmp_path: Path) -> None:
        # The map_files/ dir doesn't yet exist + the universes/ dir is
        # empty — the synthesizer should produce a sentinel-only map_file
        # rather than skip the write.
        result = synthesize_futures_map_file(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
        )
        assert result.rolls == ()
        assert result.map_file_path.exists()
        content = result.map_file_path.read_text(encoding="utf-8")
        assert content == "18991230,mes,CME\n20501231,mes,CME,2\n"

    def test_writes_to_lowercased_ticker_path(self, tmp_path: Path) -> None:
        # Caller passes uppercase ticker; on-disk path uses lowercase.
        result = synthesize_futures_map_file(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
        )
        assert "mes.csv" in str(result.map_file_path)
        assert "MES.csv" not in str(result.map_file_path)

    def test_persistence_days_override_filters_more_aggressively(self, tmp_path: Path) -> None:
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        # 30-30 split — with persistence=15 → 1 roll; with persistence=50 → 0.
        sessions = _make_sessions([("202603", 30), ("202606", 30)])
        for s in sessions:
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        lax = synthesize_futures_map_file(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            persistence_days=15,
        )
        strict = synthesize_futures_map_file(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            persistence_days=50,
        )
        assert len(lax.rolls) == 1
        assert len(strict.rolls) == 0


class TestIdempotency:
    def test_byte_identical_on_unchanged_input(self, tmp_path: Path) -> None:
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        sessions = _make_sessions([("202603", 30), ("202606", 30)])
        for s in sessions:
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        first = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        first_bytes = first.map_file_path.read_bytes()
        second = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        second_bytes = second.map_file_path.read_bytes()
        assert first_bytes == second_bytes

    def test_content_changed_flag_false_on_second_run(self, tmp_path: Path) -> None:
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        for s in _make_sessions([("202603", 30), ("202606", 30)]):
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        first = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        second = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        assert first.content_changed is True
        assert second.content_changed is False

    def test_mtime_preserved_on_no_op_cycle(self, tmp_path: Path) -> None:
        # No-op cycle (content unchanged) must NOT rewrite the file —
        # downstream tooling (e.g. systemd unit's restart trigger,
        # backup snapshotters) often watches mtime as a change signal.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        for s in _make_sessions([("202603", 30), ("202606", 30)]):
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        first_mtime = (tmp_path / "future" / "cme" / "map_files" / "mes.csv").stat().st_mtime_ns
        # Sleep just enough so a re-write would produce a different mtime.
        time.sleep(0.05)
        synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        second_mtime = (tmp_path / "future" / "cme" / "map_files" / "mes.csv").stat().st_mtime_ns
        assert first_mtime == second_mtime

    def test_atomic_replace_when_content_changes(self, tmp_path: Path) -> None:
        # First run writes sentinel-only (no universe data). Second run
        # with universe data should produce content_changed=True + new
        # map_file content with a roll row in it.
        synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        # Now populate universes and re-synthesize.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        for s in _make_sessions([("202603", 30), ("202606", 30)]):
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        result = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        assert result.content_changed is True
        content = result.map_file_path.read_text(encoding="utf-8")
        assert content.count("\n") == 3  # sentinel + 1 roll + end-sentinel
        # No temp file leaks in the map_files dir.
        leftovers = [
            p.name
            for p in (tmp_path / "future" / "cme" / "map_files").iterdir()
            if p.name != "mes.csv"
        ]
        assert leftovers == []


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_public_surface(self) -> None:
        expected = {
            "DATA_MAPPING_MODE_OPEN_INTEREST",
            "DEFAULT_PERSISTENCE_DAYS",
            "END_SENTINEL_DATE",
            "INCEPTION_SENTINEL_DATE",
            "FuturesRollTransition",
            "MapFileSynthesisResult",
            "UniverseSession",
            "build_futures_map_file_with_rolls",
            "detect_real_rolls",
            "iter_universe_sessions",
            "parse_universe_file",
            "synthesize_futures_map_file",
        }
        assert set(mfs.__all__) == expected

    def test_imports_clean_without_lean_runtime(self) -> None:
        # Module must import even when LEAN / ib-async aren't on the path.
        # This is a smoke test — the import already happened at file top;
        # we just assert the public functions are callable.
        assert callable(synthesize_futures_map_file)
        assert callable(detect_real_rolls)
        assert callable(build_futures_map_file_with_rolls)


# ---------------------------------------------------------------------------
# Hermetic env defense — os.scandir behavior on platforms with case-
# insensitive filesystems (macOS default APFS). Skipped on Linux which
# is case-sensitive by default. Not strictly required, just for safety.
# ---------------------------------------------------------------------------


class TestCrossPlatformFilesystem:
    def test_works_when_ticker_dir_is_lowercase(self, tmp_path: Path) -> None:
        # bar_sync's universe writer creates lowercase dirs; the
        # synthesizer must read from that exact case.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        universe_dir.mkdir(parents=True, exist_ok=True)
        _write_universe_file(
            universe_dir=universe_dir, session_date=date(2026, 5, 19), expiry="202606"
        )
        result = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        # Single session can't produce a roll.
        assert result.rolls == ()
        assert result.map_file_path.exists()

    def test_handles_dot_underscore_macos_files(self, tmp_path: Path) -> None:
        # macOS / SMB shares sometimes drop `._<name>.csv` shadow files
        # next to real ones. Filename parser must skip them silently.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        _write_universe_file(
            universe_dir=universe_dir, session_date=date(2026, 5, 19), expiry="202606"
        )
        (universe_dir / "._20260519.csv").write_bytes(b"\x00")
        result = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MES", market_dir="cme", market_code="CME"
        )
        assert result.map_file_path.exists()
        # The macOS shadow file did not crash the iteration.
        # Note: os.scandir may return the shadow file but the filename
        # parser rejects `._YYYYMMDD.csv` (length != 8 chars stem).
