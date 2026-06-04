"""LEAN invocation backends + availability checks (design §4.3).

Two backends, chosen by availability:

1. **LEAN CLI** (``lean backtest``) — the proposed default. Wraps the Launcher +
   Docker; takes a project dir + a ``lean.json`` and writes results to ``--output``.
2. **Raw ``docker run``** against the Launcher — the fallback when the CLI is not
   installed. Mirrors the production ``infrastructure/lean_local`` entrypoint model
   (deep-merge ``/Lean/Algorithm/lean.json`` onto the upstream config, run
   ``QuantConnect.Lean.Launcher.dll``), but ISOLATED: ``--network none``, a
   read-only data COPY at ``/Lean/Data``, and the POST-stub env (see
   :mod:`research.lean.driver`).

**Availability is probed via the EXECUTABLE / DAEMON, never ``importlib``.** Some
venvs carry an empty ``lean`` *namespace-package* artifact that is importable
(``importlib.util.find_spec("lean")`` is truthy) yet is NOT the QuantConnect CLI —
no console script, no real module. Probing ``shutil.which("lean")`` and a bounded
``docker info`` is the only reliable signal. A missing backend must make the real
run SKIP (visible), never silently pass (design §9 P2 hard reality).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import structlog

_log = structlog.get_logger(__name__)

#: LEAN engine image — same base family as the VPS ``lean_local`` (design §4.3).
DEFAULT_LEAN_IMAGE: Final = "quantconnect/lean:latest"

#: Bounded probe so a wedged Docker socket can't hang the harness.
_DOCKER_PROBE_TIMEOUT_S: Final = 10.0

#: The two invocation backends.
Backend = Literal["lean_cli", "docker"]


class LeanUnavailableError(RuntimeError):
    """Raised when a real LEAN run is requested but no backend is available."""


def lean_cli_available() -> bool:
    """True iff the real ``lean`` CLI executable is on ``PATH``.

    Probes the console script, NOT ``importlib.util.find_spec("lean")`` — the
    latter is fooled by an empty ``lean`` namespace-package artifact present in
    some venvs (importable, but not the QuantConnect CLI).
    """
    return shutil.which("lean") is not None


def docker_available() -> bool:
    """True iff a Docker client is on ``PATH`` AND its daemon answers."""
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        proc = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def cli_backend_runnable() -> bool:
    """The CLI backend needs both the ``lean`` CLI and a live Docker daemon."""
    return lean_cli_available() and docker_available()


def docker_backend_runnable() -> bool:
    """The raw backend needs only a live Docker daemon."""
    return docker_available()


def select_backend(prefer: Backend | None = None) -> Backend | None:
    """Pick an available backend (CLI preferred), honouring ``prefer``; else ``None``.

    Returning ``None`` is the SKIP signal — callers turn it into a visible
    ``pytest.skip`` / operator-facing message, never a silent pass.
    """
    if prefer == "lean_cli":
        return "lean_cli" if cli_backend_runnable() else None
    if prefer == "docker":
        return "docker" if docker_backend_runnable() else None
    if cli_backend_runnable():
        return "lean_cli"
    if docker_backend_runnable():
        return "docker"
    return None


def availability_report() -> dict[str, bool]:
    """Snapshot of what's installed — for log lines + skip-reason messages."""
    return {
        "lean_cli": lean_cli_available(),
        "docker": docker_available(),
        "cli_backend_runnable": cli_backend_runnable(),
        "docker_backend_runnable": docker_backend_runnable(),
    }


def require_backend(prefer: Backend | None = None) -> Backend:
    """Return an available backend or raise :class:`LeanUnavailableError`.

    The message is actionable (what to install) so a missing LEAN is loud.
    """
    backend = select_backend(prefer)
    if backend is not None:
        return backend
    report = availability_report()
    raise LeanUnavailableError(
        "no LEAN backend available "
        f"(lean_cli={report['lean_cli']}, docker={report['docker']}). "
        "Install the CLI (`pip install lean`) + start Docker Desktop, OR run the "
        "raw-docker backend against the VPS lean_local image. A research backtest "
        "of V1 must run ISOLATED (`--network none`, POST stub) — never on the prod "
        "docker network. See research/lean/README + design §4.3 / §9 P2."
    )


def build_cli_command(
    project_dir: Path,
    output_dir: Path,
    *,
    image: str = DEFAULT_LEAN_IMAGE,
    lean_config: Path | None = None,
    lean_exe: str | None = None,
) -> list[str]:
    """Build the ``lean backtest`` argv (pure; the driver runs it).

    ``lean backtest [OPTIONS] PROJECT`` writes results to ``--output``. We point
    PROJECT at the rendered throwaway project dir and capture its output dir.
    """
    exe = lean_exe or shutil.which("lean") or "lean"
    cmd = [exe, "backtest", str(project_dir), "--output", str(output_dir), "--image", image]
    if lean_config is not None:
        cmd += ["--lean-config", str(lean_config)]
    return cmd


@dataclass(frozen=True, slots=True)
class Mount:
    """A bind mount for the raw-docker backend (``-v source:target[:ro]``)."""

    source: Path
    target: str
    read_only: bool = True

    def to_flag(self) -> str:
        return f"{self.source}:{self.target}:ro" if self.read_only else f"{self.source}:{self.target}"


def build_docker_command(
    image: str,
    *,
    mounts: list[Mount],
    env: dict[str, str],
    command: list[str],
    network: str = "none",
    docker_exe: str | None = None,
) -> list[str]:
    """Build a ``docker run`` argv for the raw Launcher backend (pure; tested).

    ``network="none"`` is the isolation default: a research backtest of V1 emits
    POSTs on every cycle (``lean/v1_strategy.py``), so it must NOT be able to reach
    the prod api. ``--network none`` plus the POST-stub ``env`` (set by the driver)
    makes the POSTs fail harmlessly. ``--rm`` so the throwaway container is cleaned
    up. Env is sorted for deterministic argv (stable tests).
    """
    exe = docker_exe or shutil.which("docker") or "docker"
    argv = [exe, "run", "--rm", "--network", network]
    for key in sorted(env):
        argv += ["-e", f"{key}={env[key]}"]
    for mount in mounts:
        argv += ["-v", mount.to_flag()]
    argv.append(image)
    argv += command
    return argv
