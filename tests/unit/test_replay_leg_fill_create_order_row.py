"""Unit tests for ``replay_leg_fill --create-order-row`` (2026-07-16 variant).

The stranded-close repair where the leg crashed BEFORE ``insert_order_row``:
the venue filled, only the signals row exists, and the replay needs the
orders row created first. [A22]: no real DB / audit writes — every I/O seam
is monkeypatched at the module boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from scripts.operator_tools import replay_leg_fill as rlf


class _Row(SimpleNamespace):
    pass


def _args(**overrides: Any) -> Any:
    base = [
        "--client-order-id",
        "cde-20260716-btc-0-close0-deadbeef",
        "--fill-quantity",
        "2",
        "--fill-price",
        "64716.63",
    ]
    extra = overrides.pop("argv", [])
    ns = rlf._build_parser().parse_args(base + extra)
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _signal_row(**overrides: Any) -> _Row:
    row = _Row(
        id=uuid4(),
        account_id=uuid4(),
        market="BTC",
        contract_id=uuid4(),
        direction="flat",
        signal_type="exit",
        target_contracts=2,
        strategy_hash="a" * 40,
        parameter_set_hash="b" * 64,
        status="pending",
        emitted_at_utc=None,
    )
    row.__dict__.update(overrides)
    return row


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------


class TestArgparse:
    def test_new_flags_parse(self) -> None:
        ns = _args(
            argv=[
                "--create-order-row",
                "--signal-id",
                "0" * 32,
                "--order-direction",
                "buy",
            ]
        )
        assert ns.create_order_row is True
        assert ns.order_direction == "buy"

    def test_order_direction_choices_enforced(self) -> None:
        with pytest.raises(SystemExit):
            _args(argv=["--order-direction", "flat"])

    def test_defaults_off(self) -> None:
        ns = _args()
        assert ns.create_order_row is False
        assert ns.signal_id is None
        assert ns.order_direction is None


# ---------------------------------------------------------------------------
# _create_missing_order_row validations (execute=False → read-only)
# ---------------------------------------------------------------------------


class TestCreateMissingOrderRowValidations:
    @pytest.fixture
    def sf(self) -> object:
        return object()  # opaque; every consumer is monkeypatched

    async def test_missing_signal_exits_4(self, monkeypatch: pytest.MonkeyPatch, sf: Any) -> None:
        async def _none(*a: Any, **kw: Any) -> None:
            return None

        monkeypatch.setattr(rlf, "_fetch_signal_row", _none)
        args = _args(create_order_row=True, signal_id=str(uuid4()), order_direction="buy")
        rc = await rlf._create_missing_order_row(session_factory=sf, args=args, execute=False)
        assert rc == 4

    async def test_non_exit_signal_refused_without_force(
        self, monkeypatch: pytest.MonkeyPatch, sf: Any
    ) -> None:
        signal = _signal_row(signal_type="entry")

        async def _sig(*a: Any, **kw: Any) -> _Row:
            return signal

        monkeypatch.setattr(rlf, "_fetch_signal_row", _sig)
        args = _args(create_order_row=True, signal_id=str(signal.id), order_direction="buy")
        rc = await rlf._create_missing_order_row(session_factory=sf, args=args, execute=False)
        assert rc == 4

    async def test_direction_not_closing_refused(
        self, monkeypatch: pytest.MonkeyPatch, sf: Any
    ) -> None:
        signal = _signal_row()

        async def _sig(*a: Any, **kw: Any) -> _Row:
            return signal

        async def _pos(*a: Any, **kw: Any) -> _Row:
            return _Row(quantity=-2)  # short; a SELL does not close it

        monkeypatch.setattr(rlf, "_fetch_signal_row", _sig)
        monkeypatch.setattr(rlf, "_fetch_position_row", _pos)
        args = _args(create_order_row=True, signal_id=str(signal.id), order_direction="sell")
        rc = await rlf._create_missing_order_row(session_factory=sf, args=args, execute=False)
        assert rc == 4

    async def test_quantity_mismatch_refused(
        self, monkeypatch: pytest.MonkeyPatch, sf: Any
    ) -> None:
        signal = _signal_row()

        async def _sig(*a: Any, **kw: Any) -> _Row:
            return signal

        async def _pos(*a: Any, **kw: Any) -> _Row:
            return _Row(quantity=-3)  # |pos| != --fill-quantity 2

        monkeypatch.setattr(rlf, "_fetch_signal_row", _sig)
        monkeypatch.setattr(rlf, "_fetch_position_row", _pos)
        args = _args(create_order_row=True, signal_id=str(signal.id), order_direction="buy")
        rc = await rlf._create_missing_order_row(session_factory=sf, args=args, execute=False)
        assert rc == 4

    async def test_valid_short_close_dry_run_passes_without_insert(
        self, monkeypatch: pytest.MonkeyPatch, sf: Any
    ) -> None:
        signal = _signal_row()

        async def _sig(*a: Any, **kw: Any) -> _Row:
            return signal

        async def _pos(*a: Any, **kw: Any) -> _Row:
            return _Row(quantity=-2)

        monkeypatch.setattr(rlf, "_fetch_signal_row", _sig)
        monkeypatch.setattr(rlf, "_fetch_position_row", _pos)
        # execute=False must return BEFORE the StrategyWorkerStore import/
        # construction — poison the import target to prove it.
        import services.signal.strategy_worker as sw

        def _boom(*a: Any, **kw: Any) -> None:
            raise AssertionError("store must not be constructed on dry-run")

        monkeypatch.setattr(sw, "StrategyWorkerStore", _boom)
        args = _args(create_order_row=True, signal_id=str(signal.id), order_direction="buy")
        rc = await rlf._create_missing_order_row(session_factory=sf, args=args, execute=False)
        assert rc == 0


# ---------------------------------------------------------------------------
# execute path — the store receives the production-shaped insert
# ---------------------------------------------------------------------------


class TestCreateMissingOrderRowExecute:
    async def test_insert_via_store_with_repair_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signal = _signal_row()
        captured: dict[str, Any] = {}

        async def _sig(*a: Any, **kw: Any) -> _Row:
            return signal

        async def _pos(*a: Any, **kw: Any) -> _Row:
            return _Row(quantity=-2)

        class _FakeStore:
            def __init__(self, **kw: Any) -> None:
                captured["store_kwargs"] = kw

            async def insert_order_row(self, account_id: Any, **kw: Any) -> Any:
                captured["account_id"] = account_id
                captured["insert_kwargs"] = kw
                return uuid4()

        monkeypatch.setattr(rlf, "_fetch_signal_row", _sig)
        monkeypatch.setattr(rlf, "_fetch_position_row", _pos)
        import services.signal.strategy_worker as sw

        monkeypatch.setattr(sw, "StrategyWorkerStore", _FakeStore)

        args = _args(create_order_row=True, signal_id=str(signal.id), order_direction="buy")
        rc = await rlf._create_missing_order_row(session_factory=object(), args=args, execute=True)
        assert rc == 0
        assert captured["account_id"] == signal.account_id
        kw = captured["insert_kwargs"]
        assert kw["signal_id"] == signal.id
        assert kw["client_order_id"] == args.client_order_id
        assert kw["direction"] == "buy"
        assert kw["quantity"] == 2
        assert kw["order_type"] == "limit_marketable"
        assert kw["strategy_hash"] == signal.strategy_hash
        assert kw["parameter_set_hash"] == signal.parameter_set_hash
        assert kw["audit_payload"]["repair"] == "replay_leg_fill --create-order-row"


# ---------------------------------------------------------------------------
# _run wiring
# ---------------------------------------------------------------------------


class _FakeEngine:
    async def dispose(self) -> None:
        return None


class TestRunWiring:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rlf.DATABASE_URL_ENV, "postgresql+asyncpg://x/x")
        monkeypatch.setattr(rlf, "create_async_engine", lambda *a, **kw: _FakeEngine())
        monkeypatch.setattr(rlf, "async_sessionmaker", lambda *a, **kw: object())

    async def test_missing_row_without_create_mode_exits_3(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _none(*a: Any, **kw: Any) -> None:
            return None

        monkeypatch.setattr(rlf, "_fetch_order_row", _none)
        rc = await rlf._run(_args())
        assert rc == 3

    async def test_create_mode_requires_signal_and_direction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _none(*a: Any, **kw: Any) -> None:
            return None

        monkeypatch.setattr(rlf, "_fetch_order_row", _none)
        rc = await rlf._run(_args(argv=["--create-order-row"]))
        assert rc == 2

    async def test_no_dry_run_without_confirm_exits_2_before_db(self) -> None:
        rc = await rlf._run(_args(argv=["--no-dry-run"]))
        assert rc == 2

    async def test_create_mode_dry_run_previews_and_exits_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: dict[str, Any] = {}

        async def _none(*a: Any, **kw: Any) -> None:
            return None

        async def _create(*, session_factory: Any, args: Any, execute: bool) -> int:
            calls["execute"] = execute
            return 0

        monkeypatch.setattr(rlf, "_fetch_order_row", _none)
        monkeypatch.setattr(rlf, "_create_missing_order_row", _create)
        rc = await rlf._run(
            _args(
                argv=[
                    "--create-order-row",
                    "--signal-id",
                    str(uuid4()),
                    "--order-direction",
                    "buy",
                ]
            )
        )
        assert rc == 0
        assert calls["execute"] is False

    async def test_create_mode_execute_refetches_and_replays(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetches: list[int] = []
        created = _Row(id=uuid4(), market="BTC", direction="buy", quantity=2, status="pending")

        async def _fetch(*a: Any, **kw: Any) -> _Row | None:
            fetches.append(1)
            return None if len(fetches) == 1 else created

        async def _create(*, session_factory: Any, args: Any, execute: bool) -> int:
            assert execute is True
            return 0

        replayed: dict[str, Any] = {}

        async def _process(**kw: Any) -> Any:
            replayed.update(kw)
            return _Row(new_order_status="filled", trade_id=uuid4(), audit_event_uuids=())

        monkeypatch.setattr(rlf, "_fetch_order_row", _fetch)
        monkeypatch.setattr(rlf, "_create_missing_order_row", _create)
        monkeypatch.setattr(rlf, "process_fill_event", _process)
        rc = await rlf._run(
            _args(
                argv=[
                    "--create-order-row",
                    "--signal-id",
                    str(uuid4()),
                    "--order-direction",
                    "buy",
                    "--no-dry-run",
                    "--confirm",
                ]
            )
        )
        assert rc == 0
        assert len(fetches) == 2
        assert replayed["client_order_id"] == "cde-20260716-btc-0-close0-deadbeef"
        assert replayed["payload"].fill_quantity == 2
