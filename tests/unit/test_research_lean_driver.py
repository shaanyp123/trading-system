"""LEAN driver assembly + isolation invariants (design §4.3 driver.py, §9 P2).

The subprocess is skip-gated (no LEAN here), but the command/env/mount ASSEMBLY is
pure and unit-tested — especially the anti-pollution guarantees: a research V1
backtest must never reach the prod api or mount the live data volume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research.lean import driver, images
from research.lean.driver import (
    LeanRunError,
    LeanRunSpec,
    assemble_docker_run,
    build_run_argv,
    isolation_env,
    run_backtest,
)


def _spec(tmp_path: Path, **overrides: object) -> LeanRunSpec:
    data_root = tmp_path / "lean_bars"
    data_root.mkdir(exist_ok=True)
    algo = tmp_path / "donchian_reference.py"
    algo.write_text("# reference algo\n", encoding="utf-8")
    base: dict[str, object] = {
        "algorithm_type_name": "DonchianReferenceAlgorithm",
        "algorithm_source": algo,
        "parameters": {"DONCHIAN_CHANNEL": "20", "RESEARCH_SYMBOL": "TLT"},
        "data_root": data_root,
        "symbol": "TLT",
        "multiplier": 1.0,
        "strategy_name": "donchian(20,1)",
    }
    base.update(overrides)
    return LeanRunSpec(**base)  # type: ignore[arg-type]


def test_isolation_env_neutralizes_post_hazard() -> None:
    env = isolation_env()
    assert env["LEAN_LOCAL_API_BASE_URL"] == "http://127.0.0.1:9"  # unreachable stub
    assert env["LEAN_LOCAL_BEARER_TOKEN"]  # non-empty → initialize() won't fail-close
    assert env["LEAN_LOCAL_BEARER_TOKEN"] != ""
    assert env["LEAN_LIVE_MODE"] == "false"
    # Must NOT be the prod api.
    assert "api:8000" not in env["LEAN_LOCAL_API_BASE_URL"]


def test_guard_data_root_refuses_live_volume(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="live data volume"):
        driver._guard_data_root(Path("trading_lean_data"))
    with pytest.raises(ValueError, match="live data volume"):
        driver._guard_data_root(Path("lean_data"))


def test_guard_data_root_requires_existing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        driver._guard_data_root(tmp_path / "missing")


def test_assemble_docker_run_mounts_and_env(tmp_path: Path) -> None:
    spec = _spec(tmp_path, extra_package_mounts=(tmp_path,))
    project = tmp_path / "project"
    output = tmp_path / "output"
    output.mkdir()
    assembled = assemble_docker_run(spec, project, output)

    targets = {m.target: m for m in assembled.mounts}
    assert targets["/Lean/Data"].read_only is True
    assert targets["/Lean/Algorithm/lean.json"].read_only is True
    assert targets["/Lean/Algorithm/v1_strategy.py"].read_only is True  # entrypoint hard-codes path
    assert targets["/Results"].read_only is False  # LEAN writes results here
    assert "/Lean/strategies" in targets
    assert assembled.env == isolation_env()
    # The data mount source is the COPY, never the live volume.
    data_src = targets["/Lean/Data"].source
    assert data_src.name == "lean_bars"
    assert data_src.name not in ("lean_data", "trading_lean_data")


def test_build_docker_argv_is_isolated_and_post_safe(tmp_path: Path) -> None:
    spec = _spec(tmp_path, posts_to_api=True, extra_package_mounts=(tmp_path,))
    argv = build_run_argv(
        spec,
        tmp_path / "project",
        tmp_path / "out",
        backend="docker",
        image="trading-lean-local:latest",
    )
    joined = " ".join(argv)
    # Isolation: no prod network, POST stub, dummy bearer.
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "LEAN_LOCAL_API_BASE_URL=http://127.0.0.1:9" in argv
    assert any(a.startswith("LEAN_LOCAL_BEARER_TOKEN=") for a in argv)
    # Anti-pollution: the prod api + live volume never appear anywhere in the argv.
    assert "api:8000" not in joined
    assert "trading_lean_data" not in joined
    assert "lean_data:/Lean/Data" not in joined  # named-volume mount form
    assert argv[-2:] == ["dotnet", "/Lean/Launcher/bin/Debug/QuantConnect.Lean.Launcher.dll"]


def test_build_cli_argv(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    argv = build_run_argv(spec, tmp_path / "project", tmp_path / "out", backend="lean_cli")
    assert argv[1] == "backtest"
    assert "--output" in argv


def test_run_backtest_skips_when_no_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images, "lean_cli_available", lambda: False)
    monkeypatch.setattr(images, "docker_available", lambda: False)
    with pytest.raises(images.LeanUnavailableError):
        run_backtest(_spec(tmp_path), work_dir=tmp_path / "work")


def test_run_backtest_v1_rejects_cli_backend(tmp_path: Path) -> None:
    # V1 POSTs; the POST-stub env is only wired through docker, so CLI is refused.
    with pytest.raises(LeanRunError, match="posts_to_api"):
        run_backtest(
            _spec(tmp_path, posts_to_api=True), work_dir=tmp_path / "work", backend="lean_cli"
        )
