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
* TestEncodeBase36 — known reference values from LEAN's
  ``Common/Extensions.cs::EncodeBase36``
* TestOadate — known .NET reference values
* TestComputeFutureSidHash — 8+ parametrized cases reproducing real
  contracts from LEAN's bundled ``Data/future/cme/map_files/es.csv``
* TestExpiryHelpers — primitives (third_friday, last_friday,
  nth_last_business_day, add_business_days, add_business_days_if_holiday)
* TestExpiryRules — per-ticker known expiries
  (e.g. MES Mar 2026 → 2026-03-20; MCL Dec 2025 → 2025-11-19;
  MES Jun 2026 → 2026-06-18 because Juneteenth)
* TestBuildFuturesMapFileWithRolls — empty rolls, single roll, multiple
  rolls, mode override, lowercase normalization, SID emission default,
  bare-permtick fallback, end-sentinel SID propagation
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
from structlog.testing import capture_logs

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
    compute_future_expiry,
    compute_future_sid_hash,
    detect_real_rolls,
    encode_base36,
    iter_universe_sessions,
    oadate,
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

    # --- Live-edge rule (Step 2b): the most-recent run is ALWAYS honored even
    # below persistence_days, so the continuous never strands on an expired
    # contract. Regression for the 2026-06 /MBT staleness.

    def test_live_edge_fresh_roll_honored_below_persistence(self) -> None:
        # 30 stable sessions of E1 → 4 fresh sessions of E2 (< persistence=15).
        # PRE-FIX: the 4-session run was dropped → 0 rolls → continuous pinned
        # to E1 (which has since expired). POST-FIX: the live edge is honored.
        sessions = _make_sessions([("202605", 30), ("202606", 4)])
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert len(rolls) == 1
        assert rolls[0].from_expiry == "202605"
        assert rolls[0].to_expiry == "202606"
        # Boundary date is the FIRST session of the fresh (live-edge) run.
        assert rolls[0].boundary_date == sessions[30].session_date
        # The short run's length is recorded as-is (observability).
        assert rolls[0].persisted_sessions == 4

    def test_mbt_monthly_roll_regression(self) -> None:
        # The exact shape of the 2026-06 /MBT bug: a long stable prior run
        # (202605, 31 sessions) followed by the fresh monthly roll (202606,
        # 8 sessions < 15). The fresh roll MUST appear so LEAN resolves to the
        # live June contract instead of serving the expired May contract's bar.
        sessions = _make_sessions([("202604", 22), ("202605", 31), ("202606", 8)])
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert len(rolls) == 2
        assert (rolls[0].from_expiry, rolls[0].to_expiry) == ("202604", "202605")
        assert (rolls[1].from_expiry, rolls[1].to_expiry) == ("202605", "202606")

    def test_interior_noise_dropped_but_live_edge_honored(self) -> None:
        # Interior noise (202605, 3 sessions) is still filtered; only the
        # live-edge run (202606, 4 sessions) survives the sub-threshold rule.
        # Result: a single roll straight from the prior stable run to the
        # live edge, skipping the interior blip.
        sessions = _make_sessions([("202604", 20), ("202605", 3), ("202606", 4)])
        rolls = detect_real_rolls(sessions, persistence_days=15)
        assert len(rolls) == 1
        assert (rolls[0].from_expiry, rolls[0].to_expiry) == ("202604", "202606")

    def test_live_edge_flip_back_emits_no_spurious_roll(self) -> None:
        # The live edge flips back to the prior stable contract for 1 session
        # (E1[30] → E2[3 noise] → E1[1]). Honoring the live edge must NOT
        # manufacture a roll: the appended same-expiry run is skipped by the
        # same-expiry guard, so the continuous stays correctly on E1.
        sessions = _make_sessions([("202605", 30), ("202606", 3), ("202605", 1)])
        assert detect_real_rolls(sessions, persistence_days=15) == []

    def test_live_edge_single_short_run_no_roll(self) -> None:
        # A single sub-threshold run (brand-new ticker) is force-kept but
        # cannot form a transition on its own — no roll, no crash.
        sessions = _make_sessions([("202606", 4)])
        assert detect_real_rolls(sessions, persistence_days=15) == []


# ---------------------------------------------------------------------------
# encode_base36 + oadate — primitive helpers
# ---------------------------------------------------------------------------


class TestEncodeBase36:
    def test_zero_returns_empty_string(self) -> None:
        # Mirrors LEAN's Extensions.cs::EncodeBase36 ("" for n==0).
        assert encode_base36(0) == ""

    def test_basic_digits(self) -> None:
        for n, expected in [
            (1, "1"),
            (9, "9"),
            (10, "A"),
            (35, "Z"),
            (36, "10"),
            (37, "11"),
            (71, "1Z"),
            (72, "20"),
        ]:
            assert encode_base36(n) == expected, (
                f"{n!r} → {encode_base36(n)!r}, expected {expected!r}"
            )

    def test_es_dec_2009_otherdata_reproduces_uik2f7cj4v0h(self) -> None:
        # The full integer for ES Dec 2009 (expiry 2009-12-18, market=cme,
        # secType=Future): 4_016_500_000_000_002_305. base36 → UIK2F7CJ4V0H.
        assert encode_base36(4_016_500_000_000_002_305) == "UIK2F7CJ4V0H"

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            encode_base36(-1)


class TestOadate:
    @pytest.mark.parametrize(
        "d, expected",
        [
            # .NET reference values (cross-check via DateTime.ToOADate())
            (date(1900, 3, 1), 61),
            (date(2009, 12, 18), 40165),  # ES Dec 2009 contract
            (date(2025, 3, 21), 45737),
            (date(2026, 5, 22), 46164),
        ],
    )
    def test_known_reference_values(self, d: date, expected: int) -> None:
        assert oadate(d) == expected

    def test_pre_1900_03_01_raises(self) -> None:
        with pytest.raises(ValueError, match="1900-03-01"):
            oadate(date(1899, 12, 30))

    def test_1900_03_01_boundary_ok(self) -> None:
        # The exact boundary should succeed.
        assert oadate(date(1900, 3, 1)) == 61


# ---------------------------------------------------------------------------
# compute_future_sid_hash — 12 reference SIDs from LEAN's bundled es.csv
# ---------------------------------------------------------------------------


class TestComputeFutureSidHash:
    @pytest.mark.parametrize(
        "expiry, market_dir, expected_sid",
        [
            # All from QuantConnect/Lean/Data/future/cme/map_files/es.csv
            # (third Friday of contract month; verified by walking gc.csv
            # reverse-engineering then forward-validating).
            (date(2009, 12, 18), "cme", "uik2f7cj4v0h"),  # ES Dec 2009
            (date(2010, 3, 19), "cme", "ul1o3pmw3im9"),  # ES Mar 2010
            (date(2010, 6, 18), "cme", "unj9s7x92681"),  # ES Jun 2010
            (date(2010, 9, 17), "cme", "uq0vgq7m0ttt"),  # ES Sep 2010
            (date(2010, 12, 17), "cme", "usih58hyzhfl"),  # ES Dec 2010
            (date(2015, 6, 19), "cme", "w1i7j18mukg1"),  # ES Jun 2015
            (date(2020, 12, 18), "cme", "xkgcmv4qk9vl"),  # ES Dec 2020
            (date(2023, 6, 16), "cme", "y9cdfy0c6txd"),  # ES Jun 2023
        ],
    )
    def test_es_bundled_reference(self, expiry: date, market_dir: str, expected_sid: str) -> None:
        assert compute_future_sid_hash(expiry_date=expiry, market_dir=market_dir) == expected_sid

    def test_unknown_market_dir_raises(self) -> None:
        with pytest.raises(ValueError, match="not in known LEAN markets"):
            compute_future_sid_hash(expiry_date=date(2026, 3, 20), market_dir="bogus")

    def test_market_dir_case_insensitive(self) -> None:
        # Uppercase "CME" should still resolve via lowercase lookup.
        a = compute_future_sid_hash(expiry_date=date(2009, 12, 18), market_dir="CME")
        b = compute_future_sid_hash(expiry_date=date(2009, 12, 18), market_dir="cme")
        assert a == b == "uik2f7cj4v0h"

    def test_market_id_affects_sid(self) -> None:
        # Same expiry, different market → different SID. Sanity-checks
        # the market_id is actually in the bit-packed integer.
        cme_sid = compute_future_sid_hash(expiry_date=date(2026, 3, 20), market_dir="cme")
        cbot_sid = compute_future_sid_hash(expiry_date=date(2026, 3, 20), market_dir="cbot")
        comex_sid = compute_future_sid_hash(expiry_date=date(2026, 3, 20), market_dir="comex")
        assert cme_sid != cbot_sid != comex_sid
        assert cme_sid != comex_sid

    def test_returned_value_is_lowercase(self) -> None:
        sid = compute_future_sid_hash(expiry_date=date(2009, 12, 18), market_dir="cme")
        assert sid == sid.lower()


# ---------------------------------------------------------------------------
# Expiry helpers — primitives
# ---------------------------------------------------------------------------


class TestExpiryHelpers:
    def test_third_friday_of_month_examples(self) -> None:
        # March 2026: 1st Fri = Mar 6 → 3rd Fri = Mar 20
        assert mfs._third_friday_of_month(2026, 3) == date(2026, 3, 20)
        # June 2026: 1st Fri = Jun 5 → 3rd Fri = Jun 19 (Juneteenth!)
        assert mfs._third_friday_of_month(2026, 6) == date(2026, 6, 19)
        # Dec 2009: 1st Fri = Dec 4 → 3rd Fri = Dec 18
        assert mfs._third_friday_of_month(2009, 12) == date(2009, 12, 18)

    def test_last_friday_of_month_examples(self) -> None:
        # Jun 2026: last Friday = Jun 26
        assert mfs._last_friday_of_month(2026, 6) == date(2026, 6, 26)
        # Feb 2026: last Friday = Feb 27
        assert mfs._last_friday_of_month(2026, 2) == date(2026, 2, 27)
        # Mar 2026: last Friday = Mar 27
        assert mfs._last_friday_of_month(2026, 3) == date(2026, 3, 27)

    def test_nth_last_business_day_no_holidays(self) -> None:
        # Feb 2013: EOM = Feb 28 (Thu). Last biz = Feb 28; 2nd-last = Feb 27;
        # 3rd-last = Feb 26 (Tue). No holidays in Feb 2013.
        empty_holidays: frozenset[date] = frozenset()
        assert mfs._nth_last_business_day(2013, 2, 3, empty_holidays) == date(2013, 2, 26)

    def test_nth_last_business_day_with_holiday(self) -> None:
        # March 2013 EOM = Mar 31 (Sun). Last biz = Mar 29 (Fri).
        # But Mar 29 2013 = Good Friday → holiday → skip.
        # So last biz = Mar 28 (Thu); 2nd-last = Mar 27; 3rd-last = Mar 26.
        holidays = frozenset({date(2013, 3, 29)})
        assert mfs._nth_last_business_day(2013, 3, 3, holidays) == date(2013, 3, 26)

    def test_add_business_days_negative(self) -> None:
        # Nov 25 2025 (Tue) minus 4 biz days = Nov 19 (Wed) — used by /MCL.
        empty_holidays: frozenset[date] = frozenset()
        assert mfs._add_business_days(date(2025, 11, 25), -4, empty_holidays) == date(2025, 11, 19)

    def test_add_business_days_skips_weekend(self) -> None:
        # Mon plus 1 biz day = Tue (no weekend in between)
        empty_holidays: frozenset[date] = frozenset()
        assert mfs._add_business_days(date(2026, 6, 1), 1, empty_holidays) == date(2026, 6, 2)
        # Fri plus 1 biz day = Mon (skip Sat + Sun)
        assert mfs._add_business_days(date(2026, 6, 5), 1, empty_holidays) == date(2026, 6, 8)

    def test_add_business_days_if_holiday_skips_when_not_holiday(self) -> None:
        empty_holidays: frozenset[date] = frozenset()
        # Mar 15 2026 is Sunday but our function only triggers on holiday match —
        # since Mar 15 isn't in `holidays`, it's returned as-is.
        d = date(2026, 3, 15)
        assert mfs._add_business_days_if_holiday(d, -1, empty_holidays) == d

    def test_add_business_days_if_holiday_subtracts_when_holiday(self) -> None:
        # Jun 19 2026 is Juneteenth; subtract 1 biz day → Jun 18 (Thu).
        holidays = frozenset({date(2026, 6, 19)})
        assert mfs._add_business_days_if_holiday(date(2026, 6, 19), -1, holidays) == date(
            2026, 6, 18
        )


# ---------------------------------------------------------------------------
# compute_future_expiry — per-ticker rules
# ---------------------------------------------------------------------------


class TestComputeFutureExpiry:
    @pytest.mark.parametrize(
        "ticker, contract_month, expected",
        [
            # /MES, /MNQ, /MYM, /M2K — quarterly, third Friday.
            # Mar 2026: 3rd Friday Mar 20 (no holiday).
            ("MES", "202603", date(2026, 3, 20)),
            ("MNQ", "202603", date(2026, 3, 20)),
            ("MYM", "202603", date(2026, 3, 20)),
            ("M2K", "202603", date(2026, 3, 20)),
            # Jun 2026: 3rd Friday Jun 19 = Juneteenth → expiry Jun 18 Thu.
            ("MES", "202606", date(2026, 6, 18)),
            ("MNQ", "202606", date(2026, 6, 18)),
            ("MYM", "202606", date(2026, 6, 18)),
            ("M2K", "202606", date(2026, 6, 18)),
            # Sep 2025: 3rd Friday Sep 19.
            ("MES", "202509", date(2025, 9, 19)),
            # Dec 2009: 3rd Friday Dec 18 (validates against the bundled es.csv reference).
            ("MES", "200912", date(2009, 12, 18)),
        ],
    )
    def test_quarterly_index_micros(self, ticker: str, contract_month: str, expected: date) -> None:
        assert compute_future_expiry(ticker=ticker, contract_month=contract_month) == expected

    @pytest.mark.parametrize(
        "contract_month, expected",
        [
            # /MGC — bi-monthly, 3rd-last biz day of contract month
            # (with Good Friday adjustment for Apr 2026).
            #
            # Feb 2026: EOM = Feb 28 (Sat). Last biz = Feb 27 (Fri).
            # 2nd-last = Feb 26 (Thu). 3rd-last = Feb 25 (Wed).
            ("202602", date(2026, 2, 25)),
            # Apr 2026: EOM = Apr 30 (Thu). Last biz = Apr 30 (Thu).
            # 2nd-last = Apr 29 (Wed). 3rd-last = Apr 28 (Tue).
            # Apr 3 2026 is Good Friday but that's not within the last
            # 3 biz days of April. Result: Apr 28 (no adjustment).
            ("202604", date(2026, 4, 28)),
        ],
    )
    def test_mgc_bimonthly(self, contract_month: str, expected: date) -> None:
        assert compute_future_expiry(ticker="MGC", contract_month=contract_month) == expected

    @pytest.mark.parametrize(
        "contract_month, expected",
        [
            # /MCL — monthly, 4 biz days before the 25th of MONTH BEFORE.
            #
            # Dec 2025 contract: prev_month = Nov 2025. 25th = Tue Nov 25.
            # -4 biz days: Nov 25 → walk: Nov 24 (Mon)=1, Nov 21 (Fri)=2,
            # Nov 20 (Thu)=3, Nov 19 (Wed)=4 → return Nov 19.
            ("202512", date(2025, 11, 19)),
            # Jul 2026 contract: prev = Jun 2026. 25th = Thu Jun 25.
            # -4 biz days: Jun 24=1, Jun 23=2, Jun 22=3, Jun 19=4
            # (Jun 19 is Juneteenth — a /MCL holiday). The 25th itself isn't a
            # holiday, so businessDays stays at -4. Skipping holiday Jun 19:
            # Jun 24=1, 23=2, 22=3, 19 SKIP (holiday), 18=4 → Jun 18.
            ("202607", date(2026, 6, 18)),
        ],
    )
    def test_mcl_monthly(self, contract_month: str, expected: date) -> None:
        assert compute_future_expiry(ticker="MCL", contract_month=contract_month) == expected

    @pytest.mark.parametrize(
        "contract_month, expected",
        [
            # /MBT — monthly, last Friday of contract month.
            # Jun 2026 last Fri = Jun 26.
            ("202606", date(2026, 6, 26)),
            # Jul 2026 last Fri = Jul 31.
            ("202607", date(2026, 7, 31)),
            # Sep 2026 last Fri = Sep 25 (Sep 4 is not the last Fri).
            ("202609", date(2026, 9, 25)),
        ],
    )
    def test_mbt_monthly(self, contract_month: str, expected: date) -> None:
        assert compute_future_expiry(ticker="MBT", contract_month=contract_month) == expected

    def test_unknown_ticker_raises(self) -> None:
        with pytest.raises(ValueError, match="no expiry rule"):
            compute_future_expiry(ticker="BOGUS", contract_month="202606")

    def test_malformed_contract_month_raises(self) -> None:
        with pytest.raises(ValueError, match="6-digit YYYYMM"):
            compute_future_expiry(ticker="MES", contract_month="2026")

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            compute_future_expiry(ticker="MES", contract_month="202613")

    def test_ticker_case_insensitive(self) -> None:
        a = compute_future_expiry(ticker="MES", contract_month="202603")
        b = compute_future_expiry(ticker="mes", contract_month="202603")
        assert a == b == date(2026, 3, 20)


# ---------------------------------------------------------------------------
# front_month_for_session_date — the "what was front-month on date X" helper
# ---------------------------------------------------------------------------


class TestFrontMonthForSessionDate:
    """Locks the 2026-05-24 forward-fix for bar_sync's per-bar universe-write
    bug. Pre-fix, bar_sync wrote TODAY's front-month into every historical
    session's universe file, corrupting the synthesizer's roll-detection
    input. Post-fix, this helper computes the actual historical front-month
    per session_date deterministically from each ticker's expiry rules.
    """

    @pytest.mark.parametrize(
        "session_date, expected",
        [
            # Pre-Sep-2025 expiry — 202509 (Sep 19) >= Sep 1
            (date(2025, 9, 1), "202509"),
            # On expiry day — 202509 (Sep 19) >= Sep 19
            (date(2025, 9, 19), "202509"),
            # Day after expiry — rolls to 202512
            (date(2025, 9, 22), "202512"),
            # Pre-Dec expiry — 202512 still front
            (date(2025, 12, 19), "202512"),
            # Post-Dec expiry — 202603
            (date(2025, 12, 22), "202603"),
            # On Mar 2026 expiry day — 202603 still front
            (date(2026, 3, 20), "202603"),
            # Post-Mar expiry — 202606
            (date(2026, 3, 23), "202606"),
            # /MES Jun 2026 expiry is 2026-06-18 (Juneteenth adjustment)
            (date(2026, 6, 18), "202606"),
            # Day after Juneteenth-adjusted expiry — 202609
            (date(2026, 6, 22), "202609"),
            # Mid-cycle (Q3 2025) — 202509 still front
            (date(2025, 7, 15), "202509"),
            # Today's actual front-month for the 2026-05-24 cycle
            (date(2026, 5, 24), "202606"),
        ],
    )
    def test_mes_quarterly(self, session_date: date, expected: str) -> None:
        assert mfs.front_month_for_session_date(ticker="MES", session_date=session_date) == expected

    def test_other_quarterly_micros_match_mes(self) -> None:
        # /MNQ, /MYM, /M2K all use the same HMUZ cycle + third-Friday
        # expiry rule as /MES, so they should produce the same front-month
        # for any given session_date.
        sd = date(2025, 12, 22)
        mes = mfs.front_month_for_session_date(ticker="MES", session_date=sd)
        for t in ("MNQ", "MYM", "M2K"):
            assert mfs.front_month_for_session_date(ticker=t, session_date=sd) == mes

    @pytest.mark.parametrize(
        "session_date, expected",
        [
            # /MGC bi-monthly Feb/Apr/Jun/Aug/Oct/Dec. Oct 2025 expiry is
            # 3rd-last business day of Oct = Oct 29 2025 (Wed).
            (date(2025, 9, 15), "202510"),
            # On Oct expiry day — still 202510
            (date(2025, 10, 29), "202510"),
            # After Oct expiry — rolls to 202512
            (date(2025, 10, 30), "202512"),
            # Today
            (date(2026, 5, 24), "202606"),
        ],
    )
    def test_mgc_bimonthly(self, session_date: date, expected: str) -> None:
        assert mfs.front_month_for_session_date(ticker="MGC", session_date=session_date) == expected

    @pytest.mark.parametrize(
        "session_date, expected",
        [
            # /MBT monthly, last Friday of contract month.
            # Sep 2025 last Friday = Sep 26 (with -1 biz day if holiday;
            # Sep 26 2025 is Fri, not a holiday) → expiry Sep 26.
            (date(2025, 9, 15), "202509"),
            # Today's front-month
            (date(2026, 5, 24), "202605"),
            # Past May 2026 expiry, rolls to Jun
            (date(2026, 6, 1), "202606"),
        ],
    )
    def test_mbt_monthly(self, session_date: date, expected: str) -> None:
        assert mfs.front_month_for_session_date(ticker="MBT", session_date=session_date) == expected

    def test_unknown_ticker_raises(self) -> None:
        with pytest.raises(ValueError, match="no expiry rule"):
            mfs.front_month_for_session_date(ticker="BOGUS", session_date=date(2026, 5, 24))

    def test_ticker_case_insensitive(self) -> None:
        a = mfs.front_month_for_session_date(ticker="MES", session_date=date(2026, 5, 24))
        b = mfs.front_month_for_session_date(ticker="mes", session_date=date(2026, 5, 24))
        assert a == b == "202606"


# ---------------------------------------------------------------------------
# Map_file content rendering
# ---------------------------------------------------------------------------


class TestBuildFuturesMapFileWithRolls:
    def test_empty_rolls_writes_2_sentinels_bare(self) -> None:
        # With no rolls there's no SID to propagate → end sentinel falls
        # back to bare permtick. Inception sentinel is always bare.
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=[],
        )
        assert content == "18991230,mes,CME\n20501231,mes,CME,2\n"

    def test_single_roll_emits_3_rows_with_sid(self) -> None:
        rolls = [
            FuturesRollTransition(
                boundary_date=date(2026, 3, 20),
                from_expiry="202512",
                to_expiry="202603",
                persisted_sessions=45,
            )
        ]
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=rolls,
        )
        lines = content.splitlines()
        # Inception sentinel: bare permtick (no SID).
        assert lines[0] == "18991230,mes,CME"
        # Roll row: SID-hash MappedSymbol.
        # MES 202603 → expiry Mar 20 2026 → SID computed from (Mar 20, cme).
        expected_sid = compute_future_sid_hash(expiry_date=date(2026, 3, 20), market_dir="cme")
        assert lines[1] == f"20260320,mes {expected_sid},CME,2"
        # End sentinel: carries last roll's SID forward.
        assert lines[2] == f"20501231,mes {expected_sid},CME,2"

    def test_multiple_rolls_in_order_with_distinct_sids(self) -> None:
        rolls = [
            FuturesRollTransition(
                boundary_date=date(2026, 3, 20),
                from_expiry="202512",
                to_expiry="202603",
                persisted_sessions=50,
            ),
            FuturesRollTransition(
                boundary_date=date(2026, 6, 15),
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
        sid_mar = compute_future_sid_hash(expiry_date=date(2026, 3, 20), market_dir="cme")
        sid_jun = compute_future_sid_hash(expiry_date=date(2026, 6, 18), market_dir="cme")
        # MES Jun 2026 expiry is Jun 18 (Juneteenth adjustment).
        assert sid_mar != sid_jun
        assert lines[1] == f"20260320,mes {sid_mar},CME,2"
        assert lines[2] == f"20260615,mes {sid_jun},CME,2"
        # End sentinel carries the LAST roll's SID.
        assert lines[3] == f"20501231,mes {sid_jun},CME,2"
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
            market_dir="comex",
        )
        assert "COMEX" in content

    def test_mode_int_override(self) -> None:
        sid = compute_future_sid_hash(expiry_date=date(2026, 3, 20), market_dir="cme")
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=[
                FuturesRollTransition(
                    boundary_date=date(2026, 3, 20),
                    from_expiry="202512",
                    to_expiry="202603",
                    persisted_sessions=20,
                )
            ],
            data_mapping_mode_int=1,  # FirstDayMonth instead of OpenInterest
        )
        # Roll row + end sentinel both carry the override.
        assert f"20260320,mes {sid},CME,1" in content
        assert f"20501231,mes {sid},CME,1" in content
        # No mode-2 rows in this output.
        assert ",CME,2" not in content

    def test_bare_permtick_fallback_via_emit_sid_hash_false(self) -> None:
        # PR #222's pre-fix bare-permtick form, available for testing
        # + safety fallback. Per-roll rows have NO sid-hash suffix.
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=[
                FuturesRollTransition(
                    boundary_date=date(2026, 3, 20),
                    from_expiry="202512",
                    to_expiry="202603",
                    persisted_sessions=50,
                )
            ],
            emit_sid_hash=False,
        )
        assert " " not in content.splitlines()[1].split(",")[1]  # no SID hash on row 1
        assert content == "18991230,mes,CME\n20260320,mes,CME,2\n20501231,mes,CME,2\n"

    def test_trailing_newline(self) -> None:
        content = build_futures_map_file_with_rolls(
            ticker="MES",
            market_code="CME",
            rolls=[],
        )
        assert content.endswith("\n")
        assert not content.endswith("\n\n")  # single trailing newline

    def test_market_dir_override(self) -> None:
        # /MYM canonical example: market_dir might differ from market_code.
        # We pass market_dir="cbot" to compute SID against CBOT market_id=8
        # while still writing CME in the third CSV column.
        content_cme = build_futures_map_file_with_rolls(
            ticker="MYM",
            market_code="CME",
            rolls=[
                FuturesRollTransition(
                    boundary_date=date(2026, 3, 20),
                    from_expiry="202512",
                    to_expiry="202603",
                    persisted_sessions=50,
                )
            ],
            market_dir="cme",
        )
        content_cbot = build_futures_map_file_with_rolls(
            ticker="MYM",
            market_code="CME",
            rolls=[
                FuturesRollTransition(
                    boundary_date=date(2026, 3, 20),
                    from_expiry="202512",
                    to_expiry="202603",
                    persisted_sessions=50,
                )
            ],
            market_dir="cbot",
        )
        # CME vs CBOT yields different SIDs.
        assert content_cme != content_cbot
        # Both have CME in the exchange column (the 3rd CSV column).
        for ln in content_cme.splitlines() + content_cbot.splitlines():
            if ln and not ln.startswith("18991230,mes"):
                assert ",CME" in ln

    def test_unknown_ticker_with_sid_enabled_raises(self) -> None:
        # Default emit_sid_hash=True needs to look up expiry rules — any
        # unknown ticker triggers ValueError from compute_future_expiry.
        with pytest.raises(ValueError, match="no expiry rule"):
            build_futures_map_file_with_rolls(
                ticker="BOGUS",
                market_code="CME",
                rolls=[
                    FuturesRollTransition(
                        boundary_date=date(2026, 3, 20),
                        from_expiry="202512",
                        to_expiry="202603",
                        persisted_sessions=50,
                    )
                ],
            )


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
        lines = content.splitlines()
        # 3 rows: inception + 1 roll + end sentinel.
        assert len(lines) == 3
        # Inception sentinel: bare permtick (no SID).
        assert lines[0] == "18991230,mes,CME"
        # Roll row: SID-hash MappedSymbol.
        # The "to_expiry" is 202606 (June 2026) → MES Jun 2026 → expiry
        # Jun 18 2026 (Juneteenth adjustment) → SID from (Jun 18, cme).
        sid = compute_future_sid_hash(expiry_date=date(2026, 6, 18), market_dir="cme")
        # Boundary date is the first session of the 2nd run (session index 30 in 30-30 split).
        boundary = lines[1].split(",")[0]
        assert lines[1] == f"{boundary},mes {sid},CME,2"
        # End sentinel: carries last roll's SID.
        assert lines[-1] == f"20501231,mes {sid},CME,2"

    def test_empty_universe_dir_writes_sentinel_only(self, tmp_path: Path) -> None:
        # The map_files/ dir doesn't yet exist + the universes/ dir is
        # empty — the synthesizer should produce a sentinel-only map_file
        # rather than skip the write. End sentinel falls back to bare
        # permtick (no SID to propagate).
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

    def test_live_edge_roll_present_in_map_file(self, tmp_path: Path) -> None:
        # End-to-end regression for the 2026-06 /MBT staleness: a long stable
        # prior contract (202605) + a fresh sub-threshold live-edge roll
        # (202606, 4 sessions). The synthesized map_file's END SENTINEL — what
        # LEAN's continuous resolver carries forward to "now" — must map to the
        # LIVE June contract, not the (now-expired) May contract.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mbt"
        sessions = _make_sessions([("202605", 30), ("202606", 4)], start_date=date(2026, 5, 1))
        for s in sessions:
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        result = synthesize_futures_map_file(
            data_root=tmp_path, ticker="MBT", market_dir="cme", market_code="CME"
        )
        assert len(result.rolls) == 1
        assert result.rolls[-1].to_expiry == "202606"
        sid_live = compute_future_sid_hash(
            expiry_date=compute_future_expiry(ticker="MBT", contract_month="202606"),
            market_dir="cme",
        )
        sid_expired = compute_future_sid_hash(
            expiry_date=compute_future_expiry(ticker="MBT", contract_month="202605"),
            market_dir="cme",
        )
        assert sid_live != sid_expired
        content = result.map_file_path.read_text(encoding="utf-8")
        # End sentinel carries the LIVE contract's SID, not the expired one.
        assert content.splitlines()[-1] == f"20501231,mbt {sid_live},CME,2"
        assert sid_expired not in content.splitlines()[-1]

    def test_fresh_front_month_no_warning(self, tmp_path: Path) -> None:
        # Healthy case: the latest session's front month has not yet expired →
        # the stale-front-month guard stays silent.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mbt"
        sessions = _make_sessions([("202605", 30), ("202606", 4)], start_date=date(2026, 5, 1))
        for s in sessions:
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        with capture_logs() as logs:
            synthesize_futures_map_file(
                data_root=tmp_path, ticker="MBT", market_dir="cme", market_code="CME"
            )
        assert not any(e.get("event") == "futures_map_file_front_month_expired" for e in logs)

    def test_stale_front_month_emits_warning(self, tmp_path: Path) -> None:
        # Upstream staleness the live-edge rule cannot fix: bar_sync recorded an
        # ALREADY-EXPIRED contract as the latest front month. /MBT 202601
        # expires 2026-01-30 (last Friday of Jan); a session dated 2026-03-02
        # carrying 202601 is past-expiry → the guard must warn so the condition
        # is observable per-cycle instead of silently mispricing for weeks.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mbt"
        for d in (date(2026, 2, 26), date(2026, 2, 27), date(2026, 3, 2)):
            _write_universe_file(universe_dir=universe_dir, session_date=d, expiry="202601")
        with capture_logs() as logs:
            result = synthesize_futures_map_file(
                data_root=tmp_path, ticker="MBT", market_dir="cme", market_code="CME"
            )
        warnings = [e for e in logs if e.get("event") == "futures_map_file_front_month_expired"]
        assert len(warnings) == 1
        assert warnings[0]["front_month_expiry"] == "202601"
        assert warnings[0]["front_month_last_trading_date"] == "2026-01-30"
        # Guard is warn-only — the map_file is still written.
        assert result.map_file_path.exists()

    def test_emit_sid_hash_false_falls_back_to_bare_permtick(self, tmp_path: Path) -> None:
        # PR #222's pre-fix output via the explicit knob.
        universe_dir = tmp_path / "future" / "cme" / "universes" / "mes"
        for s in _make_sessions([("202603", 30), ("202606", 30)]):
            _write_universe_file(
                universe_dir=universe_dir, session_date=s.session_date, expiry=s.expiry
            )
        result = synthesize_futures_map_file(
            data_root=tmp_path,
            ticker="MES",
            market_dir="cme",
            market_code="CME",
            emit_sid_hash=False,
        )
        content = result.map_file_path.read_text(encoding="utf-8")
        # No SID hashes anywhere; just bare permticks.
        assert " " not in content  # no space-separated SID rows


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
            "compute_future_expiry",
            "compute_future_sid_hash",
            "detect_real_rolls",
            "encode_base36",
            "front_month_for_session_date",
            "iter_universe_sessions",
            "oadate",
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
        assert callable(compute_future_sid_hash)
        assert callable(compute_future_expiry)


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
