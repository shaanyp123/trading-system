"""Unit tests for ``scripts/operator_tools/bootstrap_live_account.py``.

A22 N/A: the script's DB-touching code paths require a real Postgres
testcontainer to exercise meaningfully; unit tests cover the argparse +
JSON-validation surface + the dry-run + bad-args + bad-JSON exit paths.

Coverage matrix:

* Argparse happy path with all required + defaulted args.
* --env live-small WITHOUT --allow-non-paper rejected.
* --no-dry-run WITHOUT --confirm rejected.
* --no-dry-run AND --confirm with --env paper accepted.
* load_parameter_set_json — happy path returns ParameterSetPayload.
* load_parameter_set_json — missing keys / wrong types / bad hash length
  rejected with ValueError.
* main() with --dry-run completes with EXIT_OK + no DB connection.
* main() with bad parameter-set JSON returns EXIT_BAD_PARAM_SET_JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

import scripts.operator_tools.bootstrap_live_account as boot
from scripts.operator_tools.bootstrap_live_account import (
    DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID,
    EXIT_ACCOUNT_MISMATCH,
    EXIT_BAD_ARGS,
    EXIT_BAD_PARAM_SET_JSON,
    EXIT_OK,
    ParameterSetPayload,
    ParsedArgs,
    _amain,
    _insert_parameter_set_idempotent,
    build_baseline_parameter_set_payload,
    load_parameter_set_json,
    main,
    parse_args,
)
from services.version.composite_hash import compute_parameter_set_hash
from strategies.v1_trend_following.parameters import default_v1_parameters

_HEX = set("0123456789abcdef")


class TestParseArgs:
    def test_paper_dry_run_defaults(self) -> None:
        result = parse_args(["--env", "paper"])
        assert isinstance(result, ParsedArgs)
        assert result.external_account_id == DEFAULT_LIVE_EXTERNAL_ACCOUNT_ID
        assert result.env == "paper"
        assert result.dry_run is True
        assert result.confirm is False
        assert result.allow_non_paper is False
        assert result.parameter_set_json is None
        assert result.mint_from_defaults is False

    def test_live_small_without_allow_non_paper_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(["--env", "live-small"])

    def test_live_scale_without_allow_non_paper_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(["--env", "live-scale"])

    def test_live_small_with_allow_non_paper_accepted(self) -> None:
        result = parse_args(["--env", "live-small", "--allow-non-paper"])
        assert result.env == "live-small"
        assert result.allow_non_paper is True

    def test_no_dry_run_without_confirm_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --confirm"):
            parse_args(["--env", "paper", "--no-dry-run"])

    def test_no_dry_run_with_confirm_accepted(self) -> None:
        result = parse_args(["--env", "paper", "--no-dry-run", "--confirm"])
        assert result.dry_run is False
        assert result.confirm is True

    def test_external_account_id_override(self) -> None:
        result = parse_args(
            [
                "--env",
                "live-small",
                "--allow-non-paper",
                "--external-account-id",
                "U00000000",
            ]
        )
        assert result.external_account_id == "U00000000"

    def test_parameter_set_json_path_propagates(self, tmp_path: Path) -> None:
        path = tmp_path / "ps.json"
        path.write_text('{"x": 1}')
        result = parse_args(["--env", "paper", "--parameter-set-json", str(path)])
        assert result.parameter_set_json == path

    def test_mint_from_defaults_flag(self) -> None:
        result = parse_args(["--env", "paper", "--mint-from-defaults"])
        assert result.mint_from_defaults is True
        assert result.parameter_set_json is None

    def test_mint_and_parameter_set_json_mutually_exclusive(self, tmp_path: Path) -> None:
        # Valid-shaped stub (64-hex) so the test exercises the mutual-exclusion
        # gate on its own merits, not as a side effect of file-validation order.
        path = tmp_path / "ps.json"
        path.write_text(json.dumps({"parameter_set_hash": "a" * 64, "parameters": {}}))
        with pytest.raises(ValueError, match="mutually exclusive"):
            parse_args(
                [
                    "--env",
                    "paper",
                    "--mint-from-defaults",
                    "--parameter-set-json",
                    str(path),
                ]
            )


class TestLoadParameterSetJson:
    def _write(self, tmp_path: Path, content: object) -> Path:
        path = tmp_path / "ps.json"
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_text(json.dumps(content))
        return path

    def test_happy_path(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            {"parameter_set_hash": "a" * 64, "parameters": {"X": 0.15}},
        )
        result = load_parameter_set_json(path)
        assert isinstance(result, ParameterSetPayload)
        assert result.parameter_set_hash == "a" * 64
        assert result.parameters == {"X": 0.15}

    def test_top_level_not_object_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, ["not", "an", "object"])
        with pytest.raises(ValueError, match="top-level must be a JSON object"):
            load_parameter_set_json(path)

    def test_missing_hash_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameters": {"X": 1}})
        with pytest.raises(ValueError, match="missing 'parameter_set_hash'"):
            load_parameter_set_json(path)

    def test_missing_parameters_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": "a" * 64})
        with pytest.raises(ValueError, match="missing 'parameters'"):
            load_parameter_set_json(path)

    def test_hash_wrong_length_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": "abc", "parameters": {}})
        with pytest.raises(ValueError, match="must be 64 hex chars"):
            load_parameter_set_json(path)

    def test_hash_wrong_type_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": 12345, "parameters": {}})
        with pytest.raises(ValueError, match="'parameter_set_hash' must be a string"):
            load_parameter_set_json(path)

    def test_parameters_wrong_type_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"parameter_set_hash": "a" * 64, "parameters": "not-a-dict"})
        with pytest.raises(ValueError, match="'parameters' must be a JSON object"):
            load_parameter_set_json(path)

    def test_malformed_json_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "ps.json"
        path.write_text("not valid json {{{")
        with pytest.raises(ValueError, match="failed to read/parse"):
            load_parameter_set_json(path)

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        with pytest.raises(ValueError, match="failed to read/parse"):
            load_parameter_set_json(path)


class TestMainExits:
    def test_dry_run_returns_ok_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--dry-run default returns 0 + no DB connection attempted.

        Sentinel: DATABASE_URL env unset → if the code reached the DB
        init path it would return EXIT_DB_INIT_FAILED (=5). EXIT_OK
        proves the dry-run short-circuit fires before DB init.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        result = main(["--env", "paper"])
        assert result == EXIT_OK

    def test_mint_from_defaults_dry_run_returns_ok_without_db(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--mint-from-defaults respects the dry-run default: no DB write.

        DATABASE_URL unset → reaching the DB-init path would return
        EXIT_DB_INIT_FAILED (=5). EXIT_OK proves minting happens (hash computed
        + logged) but the dry-run short-circuit fires before any DB write.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        result = main(["--env", "paper", "--mint-from-defaults"])
        assert result == EXIT_OK

    def test_bad_param_set_json_returns_exit_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed parameter-set JSON returns EXIT_BAD_PARAM_SET_JSON."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        path = tmp_path / "ps.json"
        path.write_text("not valid json {{{")
        result = main(
            [
                "--env",
                "paper",
                "--parameter-set-json",
                str(path),
            ]
        )
        assert result == EXIT_BAD_PARAM_SET_JSON

    def test_invalid_args_returns_exit_6(self) -> None:
        """--env live-small WITHOUT --allow-non-paper returns EXIT_BAD_ARGS."""
        result = main(["--env", "live-small"])
        assert result == EXIT_BAD_ARGS


# ---------------------------------------------------------------------------
# --mint-from-defaults: baseline payload minting
# ---------------------------------------------------------------------------

_CANONICAL_KEYS = {
    "LOOKBACK_DAYS_DONCHIAN",
    "MA_FAST_DAYS",
    "MA_SLOW_DAYS",
    "EFFICIENCY_RATIO_THRESHOLD",
    "STOP_DISTANCE_ATR_MULT",
    "ATR_LOOKBACK_DAYS",
    "MIN_HOLDING_DAYS",
    "VOL_TARGET_PCT_ANNUAL",
    "INSTRUMENT_VOL_LOOKBACK_DAYS",
    "ROLL_DAYS_BEFORE_EXPIRY",
    "STRATEGY_DECOMMISSIONED",
    "EXIT_AUTO_APPROVE",
}


class TestBuildBaselineParameterSetPayload:
    def test_returns_validated_payload_with_64_hex_hash(self) -> None:
        payload = build_baseline_parameter_set_payload()
        assert isinstance(payload, ParameterSetPayload)
        assert len(payload.parameter_set_hash) == 64
        assert set(payload.parameter_set_hash) <= _HEX

    def test_stored_parameters_carry_all_twelve_canonical_keys(self) -> None:
        payload = build_baseline_parameter_set_payload()
        assert set(payload.parameters) == _CANONICAL_KEYS
        assert len(payload.parameters) == 12

    def test_both_flags_stored_false(self) -> None:
        """Stored as the to_canonical_dict() string shape; round-trips to False
        via the trigger_v1_cycle _coerce_bool_param reader
        (str("False").lower() != "true")."""
        payload = build_baseline_parameter_set_payload()
        assert payload.parameters["STRATEGY_DECOMMISSIONED"] == "False"
        assert payload.parameters["EXIT_AUTO_APPROVE"] == "False"

    def test_hash_matches_composite_hash_of_canonical_defaults(self) -> None:
        payload = build_baseline_parameter_set_payload()
        expected = compute_parameter_set_hash(default_v1_parameters().to_canonical_dict())
        assert payload.parameter_set_hash == expected

    def test_hash_unchanged_when_strategy_decommissioned_flips(self) -> None:
        """The load-bearing invariant: the seed row's PK survives the §10.3
        in-place decommission UPDATE because the hash excludes the flag."""
        payload = build_baseline_parameter_set_payload()
        flipped = replace(default_v1_parameters(), strategy_decommissioned=True).to_canonical_dict()
        assert flipped["STRATEGY_DECOMMISSIONED"] == "True"
        assert compute_parameter_set_hash(flipped) == payload.parameter_set_hash


# ---------------------------------------------------------------------------
# Idempotent INSERT contract (stub session — no real DB; A22 N/A)
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _StubTxn:
    async def __aenter__(self) -> _StubTxn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubSession:
    def __init__(self, fetch_result: object, captured: list[tuple[str, object]]) -> None:
        self._fetch_result = fetch_result
        self._captured = captured

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _StubTxn:
        return _StubTxn()

    async def execute(self, statement: object, params: object = None) -> _StubResult:
        self._captured.append((str(statement), params))
        return _StubResult(self._fetch_result)


def _make_session_factory(
    fetch_result: object, captured: list[tuple[str, object]]
) -> Callable[[], _StubSession]:
    def factory() -> _StubSession:
        return _StubSession(fetch_result, captured)

    return factory


class TestInsertParameterSetIdempotent:
    async def test_returns_created_true_when_row_inserted(self) -> None:
        captured: list[tuple[str, object]] = []
        factory = _make_session_factory(("deadbeef",), captured)
        created = await _insert_parameter_set_idempotent(
            factory, payload=build_baseline_parameter_set_payload()
        )
        assert created is True

    async def test_returns_created_false_on_conflict(self) -> None:
        """ON CONFLICT DO NOTHING → RETURNING empty → created False (idempotent)."""
        captured: list[tuple[str, object]] = []
        factory = _make_session_factory(None, captured)
        created = await _insert_parameter_set_idempotent(
            factory, payload=build_baseline_parameter_set_payload()
        )
        assert created is False

    async def test_uses_on_conflict_do_nothing_and_binds_minted_row(self) -> None:
        captured: list[tuple[str, object]] = []
        factory = _make_session_factory(("deadbeef",), captured)
        payload = build_baseline_parameter_set_payload()
        await _insert_parameter_set_idempotent(factory, payload=payload)

        sql, params = captured[0]
        assert "ON CONFLICT (parameter_set_hash) DO NOTHING" in sql
        assert isinstance(params, dict)
        assert params["psh"] == payload.parameter_set_hash
        stored = json.loads(params["params"])
        assert stored["STRATEGY_DECOMMISSIONED"] == "False"
        assert stored["EXIT_AUTO_APPROVE"] == "False"


# ---------------------------------------------------------------------------
# --seed-params-only (2026-05-30 dup-account fix)
# ---------------------------------------------------------------------------


class TestSeedParamsOnlyArgs:
    def test_flag_defaults_false(self) -> None:
        result = parse_args(["--env", "paper"])
        assert result.seed_params_only is False

    def test_seed_params_only_with_mint_accepted(self) -> None:
        result = parse_args(["--env", "paper", "--seed-params-only", "--mint-from-defaults"])
        assert result.seed_params_only is True
        assert result.mint_from_defaults is True

    def test_seed_params_only_with_json_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "ps.json"
        path.write_text(json.dumps({"parameter_set_hash": "a" * 64, "parameters": {"X": 1}}))
        result = parse_args(
            ["--env", "paper", "--seed-params-only", "--parameter-set-json", str(path)]
        )
        assert result.seed_params_only is True
        assert result.parameter_set_json == path

    def test_seed_params_only_without_source_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires a parameter-set source"):
            parse_args(["--env", "paper", "--seed-params-only"])

    def test_seed_params_only_dry_run_returns_ok_without_db(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry-run short-circuits before DB init (DATABASE_URL unset → would be 5)."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        result = main(["--env", "paper", "--seed-params-only", "--mint-from-defaults"])
        assert result == EXIT_OK


# ---------------------------------------------------------------------------
# Wet-path orchestration: which inserts run? (monkeypatched engine + helpers)
# ---------------------------------------------------------------------------


def _patch_wet_db(monkeypatch: pytest.MonkeyPatch, *, existing_owners: list) -> dict:
    """Stub the engine/sessionmaker + the three insert helpers + owner-fetch.

    Returns a ``calls`` dict recording which orchestration steps ran, so a test
    can assert seed-params-only SKIPS account/risk_state while the param insert
    still fires.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://stub/stub")
    calls: dict[str, int] = {"account": 0, "risk_state": 0, "param": 0, "owners_fetch": 0}

    class _StubEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(boot, "create_async_engine", lambda *a, **k: _StubEngine())
    monkeypatch.setattr(boot, "async_sessionmaker", lambda *a, **k: lambda: None)

    async def _fake_fetch_owners(_factory: object) -> list:
        calls["owners_fetch"] += 1
        return list(existing_owners)

    async def _fake_insert_account(_factory: object, *, external_account_id: str):
        calls["account"] += 1
        return uuid4(), True

    async def _fake_insert_risk_state(_factory: object, *, account_id: object) -> bool:
        calls["risk_state"] += 1
        return True

    async def _fake_insert_param(_factory: object, *, payload: object) -> bool:
        calls["param"] += 1
        return True

    monkeypatch.setattr(boot, "_fetch_active_owner_accounts", _fake_fetch_owners)
    monkeypatch.setattr(boot, "_insert_account_idempotent", _fake_insert_account)
    monkeypatch.setattr(boot, "_insert_risk_state_idempotent", _fake_insert_risk_state)
    monkeypatch.setattr(boot, "_insert_parameter_set_idempotent", _fake_insert_param)
    return calls


def _wet_args(**overrides: object) -> ParsedArgs:
    base = {
        "external_account_id": "U25655583",
        "env": "paper",
        "parameter_set_json": None,
        "mint_from_defaults": True,
        "seed_params_only": False,
        "dry_run": False,
        "confirm": True,
        "allow_non_paper": False,
    }
    base.update(overrides)
    return ParsedArgs(**base)  # type: ignore[arg-type]


class TestWetOrchestration:
    async def test_seed_params_only_skips_account_and_risk_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_wet_db(monkeypatch, existing_owners=[(uuid4(), "operator")])
        rc = await _amain(_wet_args(seed_params_only=True))
        assert rc == EXIT_OK
        # ONLY the param insert ran; account + risk_state SKIPPED; no owner guard.
        assert calls["param"] == 1
        assert calls["account"] == 0
        assert calls["risk_state"] == 0
        assert calls["owners_fetch"] == 0

    async def test_full_path_refuses_on_existing_account_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Live paper account is 'operator'; requesting U25655583 → mismatch.
        calls = _patch_wet_db(monkeypatch, existing_owners=[(uuid4(), "operator")])
        rc = await _amain(_wet_args(external_account_id="U25655583", seed_params_only=False))
        assert rc == EXIT_ACCOUNT_MISMATCH
        # Guard fired; NO inserts happened (this was the 2026-05-30 footgun).
        assert calls["owners_fetch"] == 1
        assert calls["account"] == 0
        assert calls["risk_state"] == 0
        assert calls["param"] == 0

    async def test_full_path_proceeds_when_no_existing_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fresh DB (live cutover): no owner accounts yet → full bootstrap runs.
        calls = _patch_wet_db(monkeypatch, existing_owners=[])
        rc = await _amain(_wet_args(seed_params_only=False))
        assert rc == EXIT_OK
        assert calls["account"] == 1
        assert calls["risk_state"] == 1
        assert calls["param"] == 1

    async def test_full_path_proceeds_when_external_id_matches_existing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Idempotent re-run of a live bootstrap: existing owner has the SAME id.
        calls = _patch_wet_db(monkeypatch, existing_owners=[(uuid4(), "U25655583")])
        rc = await _amain(_wet_args(external_account_id="U25655583", seed_params_only=False))
        assert rc == EXIT_OK
        assert calls["account"] == 1
        assert calls["risk_state"] == 1
        assert calls["param"] == 1
