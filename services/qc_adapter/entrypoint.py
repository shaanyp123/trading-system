"""services/qc_adapter/entrypoint.py — container entrypoint.

Reads the sops-decrypted secrets bundle at ``/run/secrets/decrypted.yaml``
(written by the host-side sops -d step in ``deploy/day5-bringup.sh``),
exports the fields the qc_adapter needs as ``QC_ADAPTER_*`` env vars,
then exec()s ``python -m services.qc_adapter.main`` so signals (SIGTERM
from docker stop) flow correctly to the asyncio orchestrator loop.

Mirrors ``services/api/entrypoint.py`` + ``services/discord_bot/entrypoint.py``:

  * stdlib + pyyaml only (this file runs before any qc_adapter modules
    are imported, so the deps must be minimal).
  * Fail-closed on missing required keys → exit 2.

Required sops keys (sourced from ``deploy/qc_adapter/README.md``):

  * ``postgres.app_service_password``  → ``QC_ADAPTER_DATABASE_URL``
  * ``quantconnect.user_id``           → ``QC_ADAPTER_QC_USER_ID``
  * ``quantconnect.api_token``         → ``QC_ADAPTER_QC_API_TOKEN``

Optional (defaults in services/qc_adapter/config.py apply when unset):

  * ``QC_ADAPTER_ENVIRONMENT``       — default ``dev``; production sets ``paper``
  * ``QC_ADAPTER_VERSION``           — git SHA exported by docker-compose
  * ``QC_ADAPTER_PHASE_AT_EMIT``     — default ``0``; ``1`` once Phase 1 ships
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SECRETS_PATH = Path("/run/secrets/decrypted.yaml")


def _looks_like_placeholder(value: str | int | None) -> bool:
    if value is None:
        return True
    s = str(value)
    if not s:
        return True
    return s.startswith("<TODO") or s == "null"


def _load_secrets(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded or {}


def _build_database_url(pg_password: str) -> str:
    host = os.environ.get("QC_ADAPTER_DB_HOST", "postgres")
    port = os.environ.get("QC_ADAPTER_DB_PORT", "5432")
    name = os.environ.get("QC_ADAPTER_DB_NAME", "trading")
    user = os.environ.get("QC_ADAPTER_DB_USER", "app_service")
    return f"postgresql+asyncpg://{user}:{pg_password}@{host}:{port}/{name}"


def _exit(message: str, code: int = 2) -> int:
    print(f"[qc-adapter-entrypoint] {message}", file=sys.stderr, flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    secrets_path = Path(os.environ.get("QC_ADAPTER_SECRETS_PATH", str(DEFAULT_SECRETS_PATH)))
    secrets = _load_secrets(secrets_path)

    # --- Required: DATABASE_URL ----------------------------------------------
    if "QC_ADAPTER_DATABASE_URL" not in os.environ:
        pg_password = (secrets.get("postgres") or {}).get("app_service_password")
        if _looks_like_placeholder(pg_password):
            return _exit(
                f"postgres.app_service_password missing or placeholder in "
                f"{secrets_path}; fill via `sops secrets/<env>.enc.yaml` and "
                f"redeploy per deploy/qc_adapter/README.md",
            )
        assert isinstance(pg_password, str)
        os.environ["QC_ADAPTER_DATABASE_URL"] = _build_database_url(pg_password)

    # --- Required: QC user_id (sops yaml `quantconnect.user_id`) ------------
    if "QC_ADAPTER_QC_USER_ID" not in os.environ:
        qc_user_id = (secrets.get("quantconnect") or {}).get("user_id")
        if _looks_like_placeholder(qc_user_id):
            return _exit(
                f"quantconnect.user_id missing or placeholder in "
                f"{secrets_path}; this is the numeric QC user_id used for "
                f"HTTP Basic auth (see deploy/qc_adapter/README.md)",
            )
        os.environ["QC_ADAPTER_QC_USER_ID"] = str(qc_user_id)

    # --- Required: QC api_token ---------------------------------------------
    if "QC_ADAPTER_QC_API_TOKEN" not in os.environ:
        qc_token = (secrets.get("quantconnect") or {}).get("api_token")
        if _looks_like_placeholder(qc_token):
            return _exit(
                f"quantconnect.api_token missing or placeholder in "
                f"{secrets_path}; rotate via QC account dashboard and fill "
                f"via `sops secrets/<env>.enc.yaml`",
            )
        os.environ["QC_ADAPTER_QC_API_TOKEN"] = str(qc_token)

    # --- Optional env vars (defaults flow through to config.py) -------------
    os.environ.setdefault("QC_ADAPTER_ENVIRONMENT", os.environ.get("ENVIRONMENT", "dev"))
    os.environ.setdefault("QC_ADAPTER_VERSION", os.environ.get("RELEASE_SHA", "dev"))
    os.environ.setdefault("QC_ADAPTER_LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO"))

    cmd = argv or ["python", "-m", "services.qc_adapter.main"]
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main())
