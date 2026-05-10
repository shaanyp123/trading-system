"""Fail-fast guard against Dockerfile/pyproject.toml runtime-dep drift.

Background
----------

Day 11 carryover (PR #47) discovered that PR #44 added ``httpx>=0.28`` and
``resend>=2.4`` to ``pyproject.toml``'s ``[project] dependencies`` block but
did NOT update ``services/api/Dockerfile``'s explicit pip-install list. The
Dockerfile builder's import sanity check
(``from services.api.main import create_app; create_app()``) did NOT catch
the drift because ``services.api.main`` doesn't transitively import the new
modules — the ``ModuleNotFoundError: No module named 'httpx'`` only surfaced
when the operator runbook smoke (``deploy/webhook_pusher/README.md`` Step 3)
tried to ``import services.webhook_pusher.cli`` from inside the api container.

This script closes that gap. Run it in CI on every PR; if the two lists ever
diverge again, CI fails at the same PR that introduced the dep-drift, before
any merge → before any wasted operator-side debugging.

Usage
-----

    $ python3 scripts/check_dockerfile_deps_against_pyproject.py
    OK: 18 runtime dep(s) match between pyproject.toml and services/api/Dockerfile
    INFO: Dockerfile additionally installs 1 dev dep(s): psycopg2-binary>=2.9

    $ python3 scripts/check_dockerfile_deps_against_pyproject.py
    FAIL: 2 runtime dep(s) in pyproject.toml are missing from services/api/Dockerfile:
      - httpx>=0.28
      - resend>=2.4
    Add them to the RUN pip install block (matching the version pins) and rebuild.

Exit codes
----------

* 0 — match (or only extra dev-only deps in Dockerfile, like psycopg2-binary)
* 1 — drift (missing or version-mismatched runtime deps)

Design notes
------------

* Uses stdlib ``tomllib`` (Python 3.11+) — no external deps so this script
  runs even when the venv hasn't been built yet.
* Parses the Dockerfile via a single regex block scrape rather than full Docker
  parsing — the only RUN block we care about is the one with ``pip install``
  and the explicit dep list. If that block's structure changes substantially,
  this script's regex needs an update; the comment in the Dockerfile flags
  this script's existence so the next agent who edits the RUN block sees the
  reference.
* Dependencies are normalized as ``name<spec>`` after stripping quotes and
  whitespace. Comparison is exact string match — drift in either name or spec
  fails. (If we wanted partial-pin tolerance, e.g., Dockerfile pins
  ``httpx==0.28.0`` while pyproject says ``httpx>=0.28``, we'd compare
  parsed PEP 508 versions; today's surface is simple enough that exact match
  is correct.)
* ``[project.optional-dependencies] dev`` is NOT required in the Dockerfile
  (the runtime image doesn't need pytest/mypy/ruff/etc.). The Dockerfile is
  allowed to have EXTRA deps not in pyproject (e.g., ``psycopg2-binary`` for
  alembic migration support); these are reported as INFO, not FAIL.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PYPROJECT_PATH: Final[Path] = REPO_ROOT / "pyproject.toml"
DOCKERFILE_PATH: Final[Path] = REPO_ROOT / "services" / "api" / "Dockerfile"

# Regex to scrape the explicit pip-install dep list from the Dockerfile.
# Matches the block:
#   RUN python -m venv /opt/venv \
#       && /opt/venv/bin/pip install --upgrade pip wheel \
#       && /opt/venv/bin/pip install hatchling \
#       && /opt/venv/bin/pip install \
#           "fastapi>=0.115" \
#           ...
#           "psycopg2-binary>=2.9"
#
# The regex captures everything between the final ``pip install \`` line and
# the next blank line (which terminates the RUN block under our Dockerfile
# style). Each captured line should contain a single quoted dep spec.
_DOCKERFILE_RUN_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"&&\s*/opt/venv/bin/pip install\s*\\\s*\n((?:\s*\"[^\"]+\"\s*\\?\s*\n)+)",
    re.MULTILINE,
)

# Within the captured block, this matches each individual ``"name<spec>"`` line.
_QUOTED_DEP_RE: Final[re.Pattern[str]] = re.compile(r'"([^"]+)"')


def _parse_pyproject_runtime_deps() -> list[str]:
    """Read ``[project] dependencies`` from pyproject.toml.

    Returns the list of dep specs verbatim (e.g. ``"httpx>=0.28"``), in the
    order they appear in the TOML file. Order doesn't matter for the diff
    but the input ordering is preserved for human-friendly error output.
    """
    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)
    deps = data.get("project", {}).get("dependencies", [])
    if not isinstance(deps, list):
        raise ValueError(
            f"pyproject.toml [project] dependencies is not a list (got {type(deps).__name__})"
        )
    return [str(d) for d in deps]


def _parse_dockerfile_pip_deps() -> list[str]:
    """Extract the explicit pip-install dep list from the api Dockerfile.

    Returns the list of dep specs verbatim (e.g. ``"httpx>=0.28"``), in the
    order they appear in the Dockerfile.
    """
    text = DOCKERFILE_PATH.read_text()
    match = _DOCKERFILE_RUN_BLOCK_RE.search(text)
    if not match:
        raise ValueError(
            f"could not find the explicit pip-install RUN block in {DOCKERFILE_PATH}; "
            f"the Dockerfile's structure may have changed — update _DOCKERFILE_RUN_BLOCK_RE"
        )
    block = match.group(1)
    deps = _QUOTED_DEP_RE.findall(block)
    if not deps:
        raise ValueError(
            "matched the RUN pip-install block but extracted 0 deps from it; "
            "the per-line quote pattern may have changed — update _QUOTED_DEP_RE"
        )
    return deps


def main() -> int:
    pyproject_deps = _parse_pyproject_runtime_deps()
    dockerfile_deps = _parse_dockerfile_pip_deps()

    pyproject_set = set(pyproject_deps)
    dockerfile_set = set(dockerfile_deps)

    missing_in_dockerfile = pyproject_set - dockerfile_set
    extra_in_dockerfile = dockerfile_set - pyproject_set

    if missing_in_dockerfile:
        print(
            f"FAIL: {len(missing_in_dockerfile)} runtime dep(s) in pyproject.toml "
            f"are missing from {DOCKERFILE_PATH.relative_to(REPO_ROOT)}:"
        )
        # Print in pyproject order for stable, easy-to-scan output.
        for dep in pyproject_deps:
            if dep in missing_in_dockerfile:
                print(f"  - {dep}")
        print(
            "Add them to the RUN pip install block (matching the version pins) "
            "and rebuild via `docker compose --env-file deploy/.env build api`."
        )
        return 1

    print(
        f"OK: {len(pyproject_set)} runtime dep(s) match between "
        f"pyproject.toml and {DOCKERFILE_PATH.relative_to(REPO_ROOT)}"
    )
    if extra_in_dockerfile:
        # Extras in the Dockerfile are intentional (e.g., psycopg2-binary for
        # alembic migration support — listed in pyproject's [dev] section but
        # the runtime image needs it too). Inform but don't fail.
        print(
            f"INFO: Dockerfile additionally installs {len(extra_in_dockerfile)} "
            f"dev dep(s): {', '.join(sorted(extra_in_dockerfile))}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
