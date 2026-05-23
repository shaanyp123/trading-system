"""Tests for ``strategies.v1_trend_following.universe_freshness``.

Pure Python; no testcontainers (A22 N/A — no DB I/O). Filesystem
fixtures use ``tmp_path`` for full isolation. Covers:

* Happy path — all markets fresh.
* Mixed — some fresh, some stale, some missing.
* Edge cases — empty dirs, malformed filenames, exact-threshold dates,
  permission errors (via missing parent).
* Module contract — ``__all__`` exports + ``V1_FUTURES_MARKET_PATHS``
  shape stability.

The threshold (``DEFAULT_STALENESS_THRESHOLD_DAYS=5``) is exercised
explicitly so a future drift in the calibration is caught by the test
that asserts the constant value.
"""

from __future__ import annotations

from datetime import date as _date
from pathlib import Path

import pytest

from strategies.v1_trend_following.universe_freshness import (
    DEFAULT_STALENESS_THRESHOLD_DAYS,
    V1_FUTURES_MARKET_PATHS,
    MarketStaleness,
    UniverseFreshnessResult,
    evaluate_universe_freshness,
)


def _write_universe_files(data_root: Path, subdir: str, dates: list[str]) -> Path:
    """Create ``<data_root>/<subdir>/<date>.csv`` files. Returns the dir.

    ``dates`` are ``YYYYMMDD`` strings (no .csv suffix); the helper
    appends ``.csv`` and writes a minimal valid universe file body so
    the file exists and has some bytes (matches the real script's
    ``write_universe_files`` output minimally).
    """
    target = data_root / subdir
    target.mkdir(parents=True, exist_ok=True)
    for date_str in dates:
        (target / f"{date_str}.csv").write_text(
            "#expiry,open,high,low,close,volume,open_interest\n"
        )
    return target


class TestLockedConstants:
    """Threshold + market paths are calibration-locked artifacts."""

    def test_threshold_value_is_5_days(self) -> None:
        # Calibrated against the 2026-05-17 incident (6-day staleness
        # caused all 7 markets to fail). Any future change to this
        # constant should be a deliberate decisions-log entry.
        assert DEFAULT_STALENESS_THRESHOLD_DAYS == 5

    def test_v1_futures_market_paths_has_all_6_micros(self) -> None:
        # 6 micros post-PR-#228 (/MCL dropped per the IBKR paper-tier
        # NYMEX entitlement gap saga). Original 7-set included "/MCL".
        assert set(V1_FUTURES_MARKET_PATHS.keys()) == {
            "/MES",
            "/MNQ",
            "/MYM",
            "/M2K",
            "/MBT",
            "/MGC",
        }

    def test_v1_futures_market_paths_partition_by_exchange(self) -> None:
        # 4 of 6 are CME (/MES, /MNQ, /M2K, /MBT); /MYM lives on CBOT
        # per LEAN's FuturesExpiryFunctions.cs registration; /MGC is
        # COMEX. /MCL (NYMEX) was dropped 2026-05-23 (PR #228) — re-add
        # `nymex_markets == {"/MCL"}` if/when /MCL is re-enabled. See
        # services/data/bar_sync.py PHASE1_UNIVERSE_METADATA for the
        # paired definitive metadata.
        cme_markets = {m for m, p in V1_FUTURES_MARKET_PATHS.items() if p.startswith("cme/")}
        cbot_markets = {m for m, p in V1_FUTURES_MARKET_PATHS.items() if p.startswith("cbot/")}
        comex_markets = {m for m, p in V1_FUTURES_MARKET_PATHS.items() if p.startswith("comex/")}
        nymex_markets = {m for m, p in V1_FUTURES_MARKET_PATHS.items() if p.startswith("nymex/")}
        assert cme_markets == {"/MES", "/MNQ", "/M2K", "/MBT"}
        assert cbot_markets == {"/MYM"}
        assert comex_markets == {"/MGC"}
        assert nymex_markets == set()  # /MCL dropped PR #228

    def test_v1_futures_market_paths_format(self) -> None:
        # Each value: "<market>/universes/<lower>" — matches the seed
        # script's write_universe_files output.
        for market, path in V1_FUTURES_MARKET_PATHS.items():
            parts = path.split("/")
            assert len(parts) == 3
            assert parts[1] == "universes"
            assert parts[2] == market.lstrip("/").lower()


class TestEmptyInputs:
    """Edge cases at the boundary of "nothing to check"."""

    def test_empty_markets_dict_returns_empty_result(self, tmp_path: Path) -> None:
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={},
        )
        assert result.fresh_markets == ()
        assert result.stale_markets == ()
        assert result.missing_markets == ()

    def test_nonexistent_data_root_marks_all_missing(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist"
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(nonexistent),
            markets={"/MES": "cme/universes/mes", "/MNQ": "cme/universes/mnq"},
        )
        assert set(result.missing_markets) == {"/MES", "/MNQ"}
        assert result.fresh_markets == ()
        assert result.stale_markets == ()


class TestFreshClassification:
    """Markets within threshold → fresh."""

    def test_zero_days_stale_is_fresh(self, tmp_path: Path) -> None:
        # Today's file = 0 days stale = always fresh.
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260517"])
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.fresh_markets == ("/MES",)
        assert result.stale_markets == ()
        assert result.missing_markets == ()

    def test_exactly_at_threshold_is_fresh(self, tmp_path: Path) -> None:
        # 5 days stale with threshold=5 → days_stale > threshold is
        # False → classified fresh. Threshold is exclusive.
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260512"])
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.fresh_markets == ("/MES",)
        assert result.stale_markets == ()

    def test_one_day_stale_with_default_threshold_is_fresh(self, tmp_path: Path) -> None:
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260516"])
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.fresh_markets == ("/MES",)

    def test_multiple_files_picks_most_recent(self, tmp_path: Path) -> None:
        # Universe dirs accumulate historical files; the latest is what
        # matters for freshness. Test the lex-sort over YYYYMMDD picks
        # the latest correctly.
        _write_universe_files(
            tmp_path,
            "cme/universes/mes",
            ["20240601", "20251231", "20260101", "20260517"],
        )
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.fresh_markets == ("/MES",)


class TestStaleClassification:
    """Markets past threshold → stale (with diagnostic context)."""

    def test_six_days_stale_with_default_threshold_is_stale(self, tmp_path: Path) -> None:
        # Replicates the 2026-05-17 incident: universe last=20260511,
        # today=20260517 → 6 days stale → past threshold (5) → stale.
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260511"])
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.fresh_markets == ()
        assert len(result.stale_markets) == 1
        stale = result.stale_markets[0]
        assert stale.market == "/MES"
        assert stale.last_file == "20260511.csv"
        assert stale.days_stale == 6

    def test_stale_diagnostic_context_preserved(self, tmp_path: Path) -> None:
        _write_universe_files(tmp_path, "cme/universes/mnq", ["20260501"])
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MNQ": "cme/universes/mnq"},
        )
        stale = result.stale_markets[0]
        assert isinstance(stale, MarketStaleness)
        assert stale.market == "/MNQ"
        assert stale.last_file == "20260501.csv"
        assert stale.days_stale == 16

    def test_custom_threshold_can_be_tighter(self, tmp_path: Path) -> None:
        # 2 days stale; default threshold 5 = fresh; custom threshold 1
        # = stale.
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260515"])
        default = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        tighter = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
            threshold_days=1,
        )
        assert default.fresh_markets == ("/MES",)
        assert default.stale_markets == ()
        assert tighter.fresh_markets == ()
        assert tighter.stale_markets[0].market == "/MES"
        assert tighter.stale_markets[0].days_stale == 2

    def test_custom_threshold_can_be_looser(self, tmp_path: Path) -> None:
        # 6 days stale; default = stale; custom threshold 30 = fresh.
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260511"])
        default = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        looser = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
            threshold_days=30,
        )
        assert len(default.stale_markets) == 1
        assert looser.fresh_markets == ("/MES",)


class TestMissingClassification:
    """Markets with directory issues → missing (severe)."""

    def test_missing_directory_classified_missing(self, tmp_path: Path) -> None:
        # /MES dir doesn't exist under data_root.
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.missing_markets == ("/MES",)
        assert result.fresh_markets == ()
        assert result.stale_markets == ()

    def test_empty_dir_classified_missing(self, tmp_path: Path) -> None:
        # Dir exists but has no .csv files.
        (tmp_path / "cme/universes/mes").mkdir(parents=True)
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.missing_markets == ("/MES",)

    def test_only_non_csv_files_classified_missing(self, tmp_path: Path) -> None:
        # Dir has files but none match the YYYYMMDD.csv pattern.
        d = tmp_path / "cme/universes/mes"
        d.mkdir(parents=True)
        (d / "README.txt").write_text("not a universe file")
        (d / "20260517.json").write_text("{}")
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.missing_markets == ("/MES",)

    def test_malformed_date_filename_skipped(self, tmp_path: Path) -> None:
        # File matches YYYYMMDD.csv length (12 chars) but isn't a valid
        # date. With ONLY a malformed file present, classified missing.
        d = tmp_path / "cme/universes/mes"
        d.mkdir(parents=True)
        (d / "99999999.csv").write_text("#expiry,open,high,low,close,volume,open_interest\n")
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        # max() picks "99999999.csv"; date parse raises ValueError on
        # month=99 → caught → market classified missing.
        assert result.missing_markets == ("/MES",)

    def test_path_is_a_file_not_dir_classified_missing(self, tmp_path: Path) -> None:
        # Edge: someone replaced the universe dir with a regular file.
        (tmp_path / "cme/universes").mkdir(parents=True)
        (tmp_path / "cme/universes/mes").write_text("oops, this is a file")
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={"/MES": "cme/universes/mes"},
        )
        assert result.missing_markets == ("/MES",)


class TestMixedClassification:
    """Realistic scenario — some fresh, some stale, some missing."""

    def test_mixed_classification_all_three_buckets(self, tmp_path: Path) -> None:
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260517"])  # 0 days stale → fresh
        _write_universe_files(tmp_path, "cme/universes/mnq", ["20260511"])  # 6 days stale → stale
        # /MYM dir doesn't exist → missing
        _write_universe_files(tmp_path, "cme/universes/m2k", ["20260516"])  # 1 day → fresh
        _write_universe_files(tmp_path, "cme/universes/mbt", ["20260501"])  # 16 days → stale
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets={
                "/MES": "cme/universes/mes",
                "/MNQ": "cme/universes/mnq",
                "/MYM": "cme/universes/mym",
                "/M2K": "cme/universes/m2k",
                "/MBT": "cme/universes/mbt",
            },
        )
        assert set(result.fresh_markets) == {"/MES", "/M2K"}
        assert {s.market for s in result.stale_markets} == {"/MNQ", "/MBT"}
        assert set(result.missing_markets) == {"/MYM"}

    def test_disjoint_partition(self, tmp_path: Path) -> None:
        # Every input market must end up in exactly one bucket.
        _write_universe_files(tmp_path, "cme/universes/mes", ["20260517"])
        _write_universe_files(tmp_path, "cme/universes/mnq", ["20260501"])
        # /MYM missing
        markets = {
            "/MES": "cme/universes/mes",
            "/MNQ": "cme/universes/mnq",
            "/MYM": "cme/universes/mym",
        }
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets=markets,
        )
        all_classified = (
            set(result.fresh_markets)
            | {s.market for s in result.stale_markets}
            | set(result.missing_markets)
        )
        assert all_classified == set(markets.keys())
        # No market appears in more than one bucket.
        total = len(result.fresh_markets) + len(result.stale_markets) + len(result.missing_markets)
        assert total == len(markets)


class TestPostSeedScenario:
    """Verify the post-2026-05-17-fix state classifies cleanly."""

    def test_post_seed_2026_05_17_classifies_fresh(self, tmp_path: Path) -> None:
        # After the 2026-05-17 evening re-seed, universe extent =
        # 20260515.csv across all 7 micros. Today = 20260517 (Sun).
        # 2 calendar days stale; threshold 5 → fresh.
        for subdir in V1_FUTURES_MARKET_PATHS.values():
            _write_universe_files(tmp_path, subdir, ["20260515"])
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets=V1_FUTURES_MARKET_PATHS,
        )
        assert set(result.fresh_markets) == set(V1_FUTURES_MARKET_PATHS.keys())
        assert result.stale_markets == ()
        assert result.missing_markets == ()

    def test_pre_fix_2026_05_17_classifies_stale_for_all_7(self, tmp_path: Path) -> None:
        # Pre-fix state: universe extent = 20260511.csv. Today =
        # 20260517 → 6 days stale → all 7 fire stale.
        for subdir in V1_FUTURES_MARKET_PATHS.values():
            _write_universe_files(tmp_path, subdir, ["20260511"])
        result = evaluate_universe_freshness(
            today=_date(2026, 5, 17),
            data_root=str(tmp_path),
            markets=V1_FUTURES_MARKET_PATHS,
        )
        assert result.fresh_markets == ()
        assert {s.market for s in result.stale_markets} == set(V1_FUTURES_MARKET_PATHS.keys())
        assert all(s.days_stale == 6 for s in result.stale_markets)


class TestModuleContract:
    """``__all__`` surface + dataclass immutability."""

    def test_all_export_names(self) -> None:
        from strategies.v1_trend_following import universe_freshness as mod

        assert set(mod.__all__) == {
            "DEFAULT_STALENESS_THRESHOLD_DAYS",
            "MarketStaleness",
            "UniverseFreshnessResult",
            "V1_FUTURES_MARKET_PATHS",
            "evaluate_universe_freshness",
        }

    def test_market_staleness_is_frozen(self) -> None:
        s = MarketStaleness(market="/MES", last_file="20260511.csv", days_stale=6)
        with pytest.raises((AttributeError, Exception)):
            s.market = "/MNQ"  # type: ignore[misc]

    def test_universe_freshness_result_is_frozen(self) -> None:
        r = UniverseFreshnessResult()
        with pytest.raises((AttributeError, Exception)):
            r.fresh_markets = ("/MES",)  # type: ignore[misc]

    def test_universe_freshness_result_defaults_empty(self) -> None:
        r = UniverseFreshnessResult()
        assert r.fresh_markets == ()
        assert r.stale_markets == ()
        assert r.missing_markets == ()
