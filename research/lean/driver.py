"""Launch a daily LEAN backtest + normalize its output (design §4.3, P2).

The driver renders a throwaway per-run config (:mod:`research.lean.config_render`),
assembles an ISOLATED invocation, runs LEAN (skip-gated on availability —
:mod:`research.lean.images`), and parses the output into the SHARED
:class:`research.eval.results.BacktestResult` (:mod:`research.lean.results`).

**Primary backend: raw ``docker run`` against the ``lean_local`` image.** That
image's entrypoint (``infrastructure/lean_local/entrypoint.sh``) deep-merges the
rendered ``lean.json`` onto the upstream Launcher config (so handler bindings are
correct — skipping the merge is the Pivot-PR-F crash) and reads the POST config
from env. It is the production-faithful path AND the only one where we control the
container env, which matters because:

⚠️ **POST hazard (design §9 P2).** ``lean/v1_strategy.py`` POSTs ``signal_emitted``
+ ``lean_cycle_heartbeat`` to ``LEAN_LOCAL_API_BASE_URL`` (default
``http://api:8000``) on EVERY daily cycle, in backtest AND live mode, and
``initialize()`` fail-closes if ``LEAN_LOCAL_BEARER_TOKEN`` is empty. A research V1
backtest MUST NOT reach the prod api. So every run here is isolated three ways:
``--network none`` (the prod ``internal`` network is never joined), an unreachable
``LEAN_LOCAL_API_BASE_URL`` stub, and a dummy non-empty bearer. The reference
strategies under ``research/lean/projects/`` do not POST at all (preferred for the
parser fixture), and the isolation env is wired through the DOCKER backend only —
the ``lean_cli`` fallback carries none of it, which is why ``posts_to_api`` specs
are rejected on that backend (defense in depth).

**Data isolation (design §4.2, R1).** The data mount is ALWAYS a read-only host
COPY (``research/data/cache/lean_bars``); the live ``trading_lean_data`` Docker
volume is never mounted. :func:`_guard_data_root` refuses the live volume by name.

LEAN/Docker may be absent locally + in CI: :func:`run_backtest` raises
:class:`research.lean.images.LeanUnavailableError`, which callers turn into a
visible skip — never a silent pass. The command ASSEMBLY is pure + unit-tested
(esp. the isolation invariants); only the subprocess is gated.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

from research.lean import images
from research.lean.config_render import LeanProjectConfig, write_config
from research.lean.images import Backend, Mount
from research.lean.results import ParsedLeanResult, parse_lean_result

_log = structlog.get_logger(__name__)

#: The production-faithful research image (operator builds/tags from
#: ``infrastructure/lean_local/Dockerfile`` or pulls from ghcr). Carries the
#: entrypoint that merges our throwaway config + honours the isolation env.
DEFAULT_LEAN_LOCAL_IMAGE: Final = "trading-lean-local:latest"

#: Unreachable POST stub — guarantees V1's per-cycle POSTs fail harmlessly instead
#: of reaching the prod api (port 9 is the "discard" port; nothing listens).
RESEARCH_API_STUB_URL: Final = "http://127.0.0.1:9"
#: Dummy bearer — non-empty so ``initialize()`` does not fail-close; authenticates
#: nothing (the POST never reaches an api).
RESEARCH_BEARER_STUB: Final = "research-stub-not-a-real-token"  # noqa: S105 — not a secret

#: In-container paths the ``lean_local`` entrypoint expects (see entrypoint.sh).
_CONTAINER_ALGO_DIR: Final = "/Lean/Algorithm"
_CONTAINER_ALGO_FILE: Final = f"{_CONTAINER_ALGO_DIR}/v1_strategy.py"  # entrypoint hard-codes this
_CONTAINER_CONFIG: Final = f"{_CONTAINER_ALGO_DIR}/lean.json"  # entrypoint TEMPLATE_PATH
_CONTAINER_STRATEGIES: Final = "/Lean/strategies"  # `strategies.*` package mount
#: Where the repo ``services/`` tree is mounted (read-only) when a spec needs it.
#: ``/Lean`` is already on the container PYTHONPATH (entrypoint.sh), but the
#: backtest-order path loads ``services/risk/sizing.py`` by FILE PATH (importlib)
#: rather than as ``services.risk.sizing`` — importing the package would run
#: ``services/risk/__init__.py``, which pulls in sqlalchemy + ``services.audit``
#: (absent in the LEAN image). Loading the single module file by path sidesteps
#: that: ``sizing.py``'s own imports are numpy / structlog / ``strategies.*`` only,
#: all present in the image. Used ONLY by the (backtest-only) V1 order path.
_CONTAINER_SERVICES: Final = "/Lean/services"
_CONTAINER_DATA: Final = "/Lean/Data"
_CONTAINER_RESULTS: Final = "/Results"

#: Live volume names that must NEVER be a research data mount source (R1) —
#: the bare compose volume, the default-project prefix, and the live-project prefix.
_FORBIDDEN_DATA_ROOTS: Final = frozenset(
    {"lean_data", "trading_lean_data", "trading-live_lean_data"}
)
#: Docker-managed volume storage — a resolved mount source under here is some
#: container's volume (potentially the live one); never a legitimate COPY.
_DOCKER_VOLUMES_ROOT: Final = Path("/var/lib/docker/volumes")


class LeanRunError(RuntimeError):
    """A LEAN invocation failed (non-zero exit, or no result produced)."""


@dataclass(frozen=True, slots=True)
class LeanRunSpec:
    """Everything one daily LEAN backtest needs.

    ``algorithm_source`` is the ``.py`` to run: the repo's ``lean/v1_strategy.py``
    for the reproduce-V1 run, or a self-contained reference under
    ``research/lean/projects/`` for the parity rail. ``symbol`` / ``multiplier`` /
    ``strategy_name`` are result metadata LEAN's output does not carry.
    ``extra_package_mounts`` are host dirs added to the container's package path —
    the repo ``strategies/`` for V1 (its ``strategies.*`` imports), empty for a
    self-contained reference.
    """

    algorithm_type_name: str
    algorithm_source: Path
    parameters: dict[str, str]
    data_root: Path
    symbol: str
    multiplier: float
    strategy_name: str
    extra_package_mounts: tuple[Path, ...] = ()
    #: Optional repo ``services/`` dir, mounted read-only at ``/Lean/services``.
    #: Only the V1 backtest-order run needs it (to load ``services/risk/sizing.py``
    #: by path for Stage 0-5 sizing); reference strategies leave it ``None``.
    service_package_mount: Path | None = None
    starting_cash: float | None = None
    environment: str = "backtesting"
    #: Marks an algorithm that POSTs to the api (V1). Forces the DOCKER backend —
    #: the only one that wires the isolation env (``lean_cli`` is rejected for
    #: these specs) — and lets a report flag the run.
    posts_to_api: bool = False


def isolation_env() -> dict[str, str]:
    """The env that neutralizes the V1 POST hazard (design §9 P2)."""
    return {
        "LEAN_LOCAL_API_BASE_URL": RESEARCH_API_STUB_URL,
        "LEAN_LOCAL_BEARER_TOKEN": RESEARCH_BEARER_STUB,
        "LEAN_LIVE_MODE": "false",
    }


def _guard_resolved_mount_source(resolved: Path, *, role: str) -> None:
    """Refuse a RESOLVED mount source that lands in/under the live data volume.

    The pre-resolve name check alone misses a symlink with an innocuous name
    pointing INTO the live volume, so the forbidden-name check re-runs on the
    resolved ``.parts``; anything under docker's volume storage is refused too.
    """
    if any(part in _FORBIDDEN_DATA_ROOTS for part in resolved.parts):
        raise ValueError(
            f"refusing to mount {role} {resolved} into a research run: it resolves "
            "into the live data volume (design §4.2 R1). Use a read-only COPY under "
            "research/data/cache/lean_bars."
        )
    # On macOS /var is a symlink to /private/var, so compare against the resolved
    # constant as well — the VPS (Linux) hits the unresolved form.
    if resolved.is_relative_to(_DOCKER_VOLUMES_ROOT) or resolved.is_relative_to(
        _DOCKER_VOLUMES_ROOT.resolve()
    ):
        raise ValueError(
            f"refusing to mount {role} {resolved} into a research run: it resolves "
            f"under {_DOCKER_VOLUMES_ROOT} (a docker-managed volume — potentially the "
            "live one; design §4.2 R1). Use a read-only COPY under "
            "research/data/cache/lean_bars."
        )


def _guard_data_root(data_root: Path) -> Path:
    """Refuse the live volume; require an existing host directory (the COPY)."""
    if data_root.name in _FORBIDDEN_DATA_ROOTS or str(data_root) in _FORBIDDEN_DATA_ROOTS:
        raise ValueError(
            f"refusing to mount the live data volume {data_root!r} into a research run "
            "(design §4.2 R1). Point data_root at a read-only COPY under "
            "research/data/cache/lean_bars."
        )
    resolved = data_root.expanduser().resolve()
    _guard_resolved_mount_source(resolved, role="data_root")
    if not resolved.is_dir():
        raise FileNotFoundError(
            f"data_root {resolved} is not a directory. Snapshot a COPY of the on-disk "
            "LEAN bars there first (research/README §Quickstart) — never the live volume."
        )
    return resolved


@dataclass(frozen=True, slots=True)
class _Assembled:
    """The product of assembling a run: rendered config + docker mounts + env."""

    config_path: Path
    mounts: list[Mount] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def assemble_docker_run(
    spec: LeanRunSpec, project_dir: Path, output_dir: Path
) -> _Assembled:
    """Render the config + compute the docker mounts/env for the raw backend (pure).

    Safety invariants asserted by unit tests: the data mount is the read-only COPY
    (never the live volume); ``--network none`` is used (set by the command
    builder); ``LEAN_LOCAL_API_BASE_URL`` is the stub; the bearer is the dummy.
    """
    data_root = _guard_data_root(spec.data_root)
    # A docker bind mount with a MISSING source silently creates an empty root-owned
    # dir in the container — fail loud here instead (relative spec paths like
    # lean/v1_strategy.py + strategies/ resolve against the CWD).
    algorithm_source = spec.algorithm_source.resolve()
    if not algorithm_source.is_file():
        raise FileNotFoundError(
            f"algorithm_source {spec.algorithm_source} is not a file (resolved "
            f"{algorithm_source}) — are you running from the repo root?"
        )
    for pkg in (*spec.extra_package_mounts, spec.service_package_mount):
        if pkg is not None and not pkg.resolve().is_dir():
            raise FileNotFoundError(
                f"package mount {pkg} is not a directory (resolved {pkg.resolve()}) — "
                "are you running from the repo root?"
            )
    if len(spec.extra_package_mounts) > 1:
        # Every entry binds the SAME /Lean/strategies target; docker would let the
        # last -v silently win, so a second entry is a config error, not a feature.
        raise ValueError(
            f"extra_package_mounts supports at most one entry (each binds the single "
            f"{_CONTAINER_STRATEGIES} target); got {len(spec.extra_package_mounts)}"
        )
    output_resolved = output_dir.resolve()
    _guard_resolved_mount_source(output_resolved, role="output_dir")
    cfg = LeanProjectConfig(
        algorithm_type_name=spec.algorithm_type_name,
        # The lean_local entrypoint force-sets algorithm-location to
        # /Lean/Algorithm/v1_strategy.py, so the source is mounted there regardless
        # of which algorithm it is; algorithm-type-name selects the class.
        algorithm_location=_CONTAINER_ALGO_FILE,
        parameters=spec.parameters,
        environment=spec.environment,
        results_destination=_CONTAINER_RESULTS,
        description=f"research throwaway — {spec.strategy_name}",
    )
    config_path = write_config(project_dir, cfg)

    mounts = [
        Mount(config_path, _CONTAINER_CONFIG, read_only=True),
        Mount(algorithm_source, _CONTAINER_ALGO_FILE, read_only=True),
        Mount(data_root, _CONTAINER_DATA, read_only=True),
        Mount(output_resolved, _CONTAINER_RESULTS, read_only=False),
    ]
    for pkg in spec.extra_package_mounts:
        mounts.append(Mount(pkg.resolve(), _CONTAINER_STRATEGIES, read_only=True))
    if spec.service_package_mount is not None:
        mounts.append(
            Mount(spec.service_package_mount.resolve(), _CONTAINER_SERVICES, read_only=True)
        )

    return _Assembled(config_path=config_path, mounts=mounts, env=isolation_env())


_LAUNCHER_CMD: Final = ["dotnet", "/Lean/Launcher/bin/Debug/QuantConnect.Lean.Launcher.dll"]


def build_run_argv(
    spec: LeanRunSpec,
    project_dir: Path,
    output_dir: Path,
    *,
    backend: Backend,
    image: str | None = None,
    container_name: str | None = None,
) -> list[str]:
    """Build the invocation argv for ``backend`` (pure; tested without LEAN).

    ``container_name`` (docker backend only) makes the container addressable for
    the driver's timeout cleanup; the CLI backend manages its own containers.
    """
    if backend == "docker":
        assembled = assemble_docker_run(spec, project_dir, output_dir)
        return images.build_docker_command(
            image or DEFAULT_LEAN_LOCAL_IMAGE,
            mounts=assembled.mounts,
            env=assembled.env,
            command=_LAUNCHER_CMD,
            network="none",
            name=container_name,
        )
    # CLI backend: stage a self-contained project dir (config + the algorithm file)
    # and point `lean backtest` at it. NB the POST-stub env is NOT wired through the
    # CLI, so this path is for the POST-free reference strategies only — V1
    # (posts_to_api) is rejected here (and again, earlier, in run_backtest) and must
    # use the docker backend.
    if spec.posts_to_api:
        raise LeanRunError(
            "V1 (posts_to_api) cannot run via the lean_cli backend — the POST-stub "
            "env is only wired through the docker backend. Use backend='docker'."
        )
    cfg = LeanProjectConfig(
        algorithm_type_name=spec.algorithm_type_name,
        algorithm_location=spec.algorithm_source.name,
        parameters=spec.parameters,
        environment=spec.environment,
        description=f"research throwaway — {spec.strategy_name}",
    )
    write_config(project_dir, cfg)
    shutil.copy2(spec.algorithm_source, project_dir / spec.algorithm_source.name)
    return images.build_cli_command(project_dir, output_dir, image=image or images.DEFAULT_LEAN_IMAGE)


def _output_tail(stdout: object, stderr: object, *, limit: int = 2000) -> str:
    """Last ``limit`` chars of BOTH captured streams, labeled (never either-or)."""
    sections: list[str] = []
    for label, captured in (("stderr", stderr), ("stdout", stdout)):
        if not captured:
            continue
        text = captured.decode("utf-8", "replace") if isinstance(captured, bytes) else str(captured)
        sections.append(f"--- {label} ---\n{text}")
    return "\n".join(sections)[-limit:]


def _remove_container(name: str) -> None:
    """Best-effort ``docker rm -f`` of a timed-out run's container.

    ``--rm`` only fires when the run completes; killing the docker CLI on timeout
    leaves the daemon-owned container running (holding the data mount), so it is
    removed by its deterministic name. Failure is swallowed (warn-only) — the
    timeout error itself is what the caller must see.
    """
    docker = shutil.which("docker") or "docker"
    try:
        subprocess.run(
            [docker, "rm", "-f", name], capture_output=True, text=True, timeout=30.0, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("research_lean_container_remove_failed", container=name, error=str(exc))


def run_backtest(
    spec: LeanRunSpec,
    *,
    work_dir: Path,
    backend: Backend | None = None,
    image: str | None = None,
    timeout_s: float = 1800.0,
) -> ParsedLeanResult:
    """Run one daily LEAN backtest end-to-end → ``ParsedLeanResult``.

    Selects an available backend (raises :class:`images.LeanUnavailableError` if
    none — callers skip on that). ``work_dir`` holds the throwaway project + output
    dirs. Raises :class:`LeanRunError` on a non-zero exit, a timeout, or a missing
    result.
    """
    if spec.posts_to_api and backend == "lean_cli":
        raise LeanRunError(
            "V1 (posts_to_api) cannot run via the lean_cli backend — the POST-stub "
            "env is only wired through the docker backend. Use backend='docker'."
        )
    chosen = backend or images.select_backend("docker" if spec.posts_to_api else None)
    if chosen is None:
        # require_backend raises LeanUnavailableError (no backend); the assert is
        # unreachable but tells mypy this branch terminates.
        images.require_backend("docker" if spec.posts_to_api else None)
        raise AssertionError("unreachable: require_backend should have raised")

    project_dir = work_dir / "project"
    output_dir = work_dir / "output"
    project_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    container_name = f"research-lean-{work_dir.name}"
    argv = build_run_argv(
        spec, project_dir, output_dir, backend=chosen, image=image, container_name=container_name
    )
    _log.info(
        "research_lean_backtest_starting",
        backend=chosen,
        algorithm=spec.algorithm_type_name,
        symbol=spec.symbol,
        posts_to_api=spec.posts_to_api,
        # The isolation env (POST stub + dummy bearer) is wired through the docker
        # backend only; the CLI path is reserved for POST-free references.
        isolated=chosen == "docker",
    )
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:  # must precede SubprocessError (subclass)
        if chosen == "docker":
            _remove_container(container_name)
        tail = _output_tail(exc.stdout, exc.stderr)
        raise LeanRunError(
            f"LEAN backtest timed out after {timeout_s:g}s (backend={chosen}; container "
            f"{container_name} removed best-effort).\n--- log tail ---\n{tail}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise LeanRunError(f"LEAN invocation failed to start: {exc}") from exc
    if proc.returncode != 0:
        tail = _output_tail(proc.stdout, proc.stderr)
        raise LeanRunError(
            f"LEAN backtest exited {proc.returncode} (backend={chosen}).\n--- log tail ---\n{tail}"
        )

    try:
        parsed = parse_lean_result(
            output_dir,
            symbol=spec.symbol,
            multiplier=spec.multiplier,
            strategy_name=spec.strategy_name,
            starting_cash=spec.starting_cash,
        )
    except FileNotFoundError as exc:
        # A zero-exit run with no result document is still a failed run; the
        # docstring contract is LeanRunError for a missing result.
        raise LeanRunError(f"LEAN run produced no parseable result: {exc}") from exc
    _log.info(
        "research_lean_backtest_complete",
        backend=chosen,
        symbol=spec.symbol,
        bars=len(parsed.result.dates),
        trades=len(parsed.trades),
        total_return=round(parsed.result.total_return, 6),
    )
    return parsed


def new_work_dir(runs_dir: Path) -> Path:
    """A fresh timestamped work dir under ``runs_dir`` (mirrors research/run.py).

    Second-resolution stamps collide when two runs start in the same second;
    ``exist_ok=False`` + a bounded numeric suffix keeps each run's dir its own.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for suffix in ("", *(f"-{i:02d}" for i in range(1, 100))):
        work = runs_dir / f"lean_{stamp}{suffix}"
        try:
            work.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return work
    raise LeanRunError(f"could not allocate a work dir under {runs_dir} for stamp {stamp}")
