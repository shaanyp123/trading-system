"""Unit tests for ``scripts/operator_tools/bootstrap_slippage_calibration.py``.

A22 N/A — the DB-touching ceremony is exercised via a stub
``session_factory_override`` + a monkeypatched ``append_audit_event``; no live
Postgres, no real audit events. The high-value assertions are the
**CalibrationPlan persistence-contract ordering** (audit event BEFORE the
version INSERT; HEAD swap in the same transaction as the INSERT) and the
**Decimal-as-string** JSONB shape (A05).

Coverage matrix:

* parse_args — dry-run default, live-env gate, two-flag gate, --force flag.
* Dry-run — EXIT_OK with no DB access (DATABASE_URL unset sentinel).
* Fresh-DB seed — audit write precedes the INSERT precedes the HEAD swap;
  INSERT + swap share one transaction; version_no=1; trigger='bootstrap';
  empty coefficients; the captured audit uuid lands in the row params.
* Existing-HEAD no-op — exit 0, NO audit write, NO insert.
* --force-new-version — successor row (version_no = max+1) parented on the
  current HEAD; audit payload carries parent_version_id.
* No accounts row — EXIT_NO_ACCOUNT before any audit write.
* Decimal serialization — coefficients_jsonb emits str-Decimals / JSON-null
  r_squared; the audit payload is json-serializable (no Decimal/float leaks).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts.operator_tools.bootstrap_slippage_calibration import (
    EXIT_BAD_ARGS,
    EXIT_NO_ACCOUNT,
    EXIT_OK,
    ParsedArgs,
    _amain,
    build_bootstrap_plan,
    coefficients_jsonb,
    main,
    mint_version_id,
    parse_args,
)
from services.calibration.calibration import (
    CalibrationFitMethod,
    CalibrationTrigger,
    FillObservation,
    plan_calibration_fit,
)

_AUDIT_UUID = UUID("0190aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee")
_ACCOUNT_UUID = "0190ffff-1111-7222-8333-444444444444"
_HEAD_UUID = "01901111-2222-7333-8444-555555555555"


# ---------------------------------------------------------------------------
# Stub session + factory (records statement kind + params + txn depth)
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _Txn:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Txn:
        self._session.txn_depth += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self._session.txn_depth -= 1
        return False


class _Session:
    """Classifies executed SQL into named kinds; shared recorder across
    sessions so a test can assert cross-session ordering. Records whether
    each write ran inside an explicit ``session.begin()`` transaction."""

    def __init__(
        self,
        head_row: object,
        max_row: object,
        account_row: object,
        recorder: list[tuple[str, object]],
    ) -> None:
        self._head = head_row
        self._max = max_row
        self._account = account_row
        self._rec = recorder
        self.txn_depth = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _Txn:
        return _Txn(self)

    async def execute(self, statement: object, params: object = None) -> _Result:
        sql = str(statement)
        in_txn = self.txn_depth > 0
        if "WHERE is_head = TRUE LIMIT 1" in sql:
            self._rec.append(("select_head", params))
            return _Result(self._head)
        if "MAX(version_no)" in sql:
            self._rec.append(("select_max", params))
            return _Result(self._max)
        if "FROM accounts" in sql:
            self._rec.append(("select_account", params))
            return _Result(self._account)
        if "INSERT INTO slippage_calibration_versions" in sql:
            self._rec.append(("insert_version", {"params": params, "in_txn": in_txn}))
        elif "SET is_head = FALSE WHERE is_head = TRUE" in sql:
            self._rec.append(("clear_head", {"params": params, "in_txn": in_txn}))
        elif "SET is_head = TRUE WHERE id" in sql:
            self._rec.append(("set_head", {"params": params, "in_txn": in_txn}))
        return _Result(None)


def _make_factory(
    head_row: object,
    max_row: object,
    account_row: object,
    recorder: list[tuple[str, object]],
) -> Callable[[], _Session]:
    def factory() -> _Session:
        return _Session(head_row, max_row, account_row, recorder)

    return factory


def _make_fake_append(recorder: list[tuple[str, object]]) -> Callable[..., object]:
    async def _fake_append(
        session: object,
        event_type: object,
        payload: object,
        *,
        account_id: object,
        env: object,
        phase_at_emit: object,
        **kw: object,
    ) -> object:
        recorder.append(
            (
                "audit",
                {
                    "event_type": str(event_type),
                    "payload": payload,
                    "account_id": account_id,
                    "env": env,
                    "phase_at_emit": phase_at_emit,
                },
            )
        )
        return SimpleNamespace(event_uuid=_AUDIT_UUID, sequence_no=1)

    return _fake_append


def _wet_args(*, force: bool = False) -> ParsedArgs:
    argv = ["--env", "paper", "--no-dry-run", "--confirm"]
    if force:
        argv.append("--force-new-version")
    return parse_args(argv)


def _kinds(recorder: list[tuple[str, object]]) -> list[str]:
    return [k for k, _ in recorder]


def _entry(recorder: list[tuple[str, object]], kind: str) -> dict:  # type: ignore[type-arg]
    value = next(p for k, p in recorder if k == kind)
    assert isinstance(value, dict)
    return value


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_paper_dry_run_defaults(self) -> None:
        a = parse_args(["--env", "paper"])
        assert isinstance(a, ParsedArgs)
        assert a.env == "paper"
        assert a.dry_run is True
        assert a.confirm is False
        assert a.force_new_version is False
        assert a.notes is None

    def test_live_small_without_allow_non_paper_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --allow-non-paper"):
            parse_args(["--env", "live-small"])

    def test_live_small_with_allow_non_paper_accepted(self) -> None:
        a = parse_args(["--env", "live-small", "--allow-non-paper"])
        assert a.env == "live-small"

    def test_no_dry_run_without_confirm_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires --confirm"):
            parse_args(["--env", "paper", "--no-dry-run"])

    def test_no_dry_run_with_confirm_accepted(self) -> None:
        a = parse_args(["--env", "paper", "--no-dry-run", "--confirm"])
        assert a.dry_run is False and a.confirm is True

    def test_force_and_notes_flags(self) -> None:
        a = parse_args(["--env", "paper", "--force-new-version", "--notes", "C1 seed"])
        assert a.force_new_version is True
        assert a.notes == "C1 seed"

    def test_invalid_args_exit_code_via_main(self) -> None:
        assert main(["--env", "live-small"]) == EXIT_BAD_ARGS


# ---------------------------------------------------------------------------
# Dry-run — no DB access, no writes
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_returns_ok_without_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sentinel: DATABASE_URL unset → reaching the DB-init path would
        return EXIT_DB_INIT_FAILED (=5). EXIT_OK proves the dry-run
        short-circuit fires before any DB access."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert main(["--env", "paper"]) == EXIT_OK

    async def test_dry_run_never_touches_session_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        factory = _make_factory(None, _Row(max_no=None), _Row(id=_ACCOUNT_UUID), recorder)
        rc = await _amain(parse_args(["--env", "paper"]), session_factory_override=factory)
        assert rc == EXIT_OK
        assert recorder == []  # zero SQL, zero audit


# ---------------------------------------------------------------------------
# Fresh-DB seed — the CalibrationPlan persistence contract
# ---------------------------------------------------------------------------


class TestFreshSeed:
    async def test_audit_before_insert_before_head_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        factory = _make_factory(None, _Row(max_no=None), _Row(id=_ACCOUNT_UUID), recorder)

        rc = await _amain(_wet_args(), session_factory_override=factory)
        assert rc == EXIT_OK

        kinds = _kinds(recorder)
        # Contract step 1 (audit) strictly before step 2 (INSERT) strictly
        # before step 3 (HEAD swap: clear then set).
        assert (
            kinds.index("audit")
            < kinds.index("insert_version")
            < kinds.index("clear_head")
            < kinds.index("set_head")
        )

    async def test_insert_and_head_swap_share_one_transaction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        factory = _make_factory(None, _Row(max_no=None), _Row(id=_ACCOUNT_UUID), recorder)
        await _amain(_wet_args(), session_factory_override=factory)
        for kind in ("insert_version", "clear_head", "set_head"):
            assert _entry(recorder, kind)["in_txn"] is True, f"{kind} ran outside session.begin()"

    async def test_row_params_reference_captured_audit_uuid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        factory = _make_factory(None, _Row(max_no=None), _Row(id=_ACCOUNT_UUID), recorder)
        await _amain(_wet_args(), session_factory_override=factory)

        insert = _entry(recorder, "insert_version")["params"]
        assert isinstance(insert, dict)
        assert insert["audit_event_uuid"] == _AUDIT_UUID
        assert insert["version_no"] == 1
        assert insert["trigger"] == "bootstrap"
        assert insert["coefficients"] == "{}"
        assert insert["window_start"] is None and insert["window_end"] is None
        # calibrated_at_utc round-trips tz-aware from the audit payload ts (A06)
        assert insert["calibrated_at_utc"].tzinfo is not None
        # HEAD swap targets the freshly inserted id
        set_head = _entry(recorder, "set_head")["params"]
        assert isinstance(set_head, dict)
        assert set_head["id"] == insert["id"]

        audit = _entry(recorder, "audit")
        assert audit["event_type"] == "slippage_calibration_recalibrated"
        assert audit["env"] == "paper"
        assert audit["phase_at_emit"] == 1
        payload = audit["payload"]
        assert isinstance(payload, dict)
        assert payload["version_no"] == 1
        assert payload["trigger"] == "bootstrap"
        assert payload["fit_method"] == "zero_prior"
        assert payload["fill_count"] == 0
        assert payload["parent_version_id"] is None
        assert payload["becomes_head"] is True
        # audit payload row-id matches the INSERTed row (plan-then-apply)
        assert payload["new_version_id"] == str(insert["id"])


# ---------------------------------------------------------------------------
# Existing-HEAD no-op + --force-new-version successor
# ---------------------------------------------------------------------------


class TestExistingHead:
    async def test_existing_head_is_noop_exit_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        head = _Row(id=_HEAD_UUID, version_no=1)
        factory = _make_factory(head, _Row(max_no=1), _Row(id=_ACCOUNT_UUID), recorder)
        rc = await _amain(_wet_args(), session_factory_override=factory)
        assert rc == EXIT_OK
        kinds = _kinds(recorder)
        # HEAD lookup happened; NOTHING was written (no audit, no insert, no swap).
        assert kinds == ["select_head"]

    async def test_force_new_version_appends_successor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        head = _Row(id=_HEAD_UUID, version_no=3)
        factory = _make_factory(head, _Row(max_no=3), _Row(id=_ACCOUNT_UUID), recorder)
        rc = await _amain(_wet_args(force=True), session_factory_override=factory)
        assert rc == EXIT_OK

        insert = _entry(recorder, "insert_version")["params"]
        assert isinstance(insert, dict)
        assert insert["version_no"] == 4  # max + 1
        payload = _entry(recorder, "audit")["payload"]
        assert isinstance(payload, dict)
        assert payload["parent_version_id"] == str(UUID(_HEAD_UUID))
        assert payload["version_no"] == 4
        # Successor still swaps HEAD to itself.
        assert "clear_head" in _kinds(recorder) and "set_head" in _kinds(recorder)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestGuards:
    async def test_no_account_errors_before_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr("services.audit.writer.append_audit_event", _make_fake_append(recorder))
        factory = _make_factory(None, _Row(max_no=None), None, recorder)
        rc = await _amain(_wet_args(), session_factory_override=factory)
        assert rc == EXIT_NO_ACCOUNT
        kinds = _kinds(recorder)
        assert "audit" not in kinds and "insert_version" not in kinds

    def test_wet_without_database_url_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert main(["--env", "paper", "--no-dry-run", "--confirm"]) == 5  # EXIT_DB_INIT_FAILED


# ---------------------------------------------------------------------------
# Decimal serialization (A05) + plan shape
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_bootstrap_plan_coefficients_jsonb_empty(self) -> None:
        plan = build_bootstrap_plan(
            new_version_id=mint_version_id(),
            calibrated_at_utc=datetime.now(tz=UTC),
        )
        assert coefficients_jsonb(plan) == "{}"
        assert plan.fit_method is CalibrationFitMethod.ZERO_PRIOR
        assert plan.trigger is CalibrationTrigger.BOOTSTRAP

    def test_decimals_serialize_as_strings_in_jsonb(self) -> None:
        """A05: a plan with per-market coefficients emits str-Decimals and
        JSON-null r_squared — no float/Decimal objects reach the JSONB text."""
        fill = FillObservation(
            market="BIP-20DEC30-CDE",
            order_size_contracts=Decimal("2"),
            adv_30d_contracts=Decimal("1000"),
            expected_price=Decimal("65000.5"),
            realized_price=Decimal("65001.5"),
            direction="buy",
            fill_at_utc=datetime.now(tz=UTC),
        )
        plan = plan_calibration_fit(
            new_version_id=mint_version_id(),
            trigger=CalibrationTrigger.BOOTSTRAP,
            calibrated_at_utc=datetime.now(tz=UTC),
            fills=(fill,),
        )
        decoded = json.loads(coefficients_jsonb(plan))
        coef = decoded["BIP-20DEC30-CDE"]
        assert coef["alpha"] == "0" and isinstance(coef["alpha"], str)
        assert coef["beta"] == "0" and isinstance(coef["beta"], str)
        assert coef["n_obs"] == 1
        assert coef["r_squared"] is None

    def test_audit_payload_is_json_serializable(self) -> None:
        """No Decimal/float leaks anywhere in the payload the tool hands to
        append_audit_event (the writer's JCS canonicalizer rejects floats)."""
        plan = build_bootstrap_plan(
            new_version_id=mint_version_id(),
            calibrated_at_utc=datetime.now(tz=UTC),
            notes="C1 bootstrap seed",
        )
        text = json.dumps(plan.audit_event.payload)  # raises on Decimal
        assert "C1 bootstrap seed" in text

    def test_naive_datetime_rejected(self) -> None:
        """A06 propagates from the planner: naive calibrated_at_utc raises."""
        from services.calibration.calibration import CalibrationError

        with pytest.raises(CalibrationError, match="timezone-aware"):
            build_bootstrap_plan(
                new_version_id=mint_version_id(),
                calibrated_at_utc=datetime(2026, 7, 9, 0, 5, 0),  # naive on purpose
            )

    def test_mint_version_id_is_uuid(self) -> None:
        a, b = mint_version_id(), mint_version_id()
        assert isinstance(a, UUID) and isinstance(b, UUID)
        assert a != b  # fresh mint per call
        assert a.version == 7  # UUIDv7 per the codebase convention
