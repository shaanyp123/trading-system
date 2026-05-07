"""services/api/entrypoint.py — container entrypoint.

Reads the sops-decrypted secrets bundle (`/run/secrets/decrypted.yaml`,
written by the `sops_init` sidecar at compose-up time), exports the
fields the api needs as `API_*` env vars, then exec()s uvicorn so signals
flow correctly to the asgi worker.

Fail-closed mode: if Postgres password is missing or still a `<TODO_*>`
placeholder, the container exits 2 immediately. Operators see the message
in `docker compose logs api` and patch via `sops secrets/<env>.enc.yaml`.

This file deliberately uses stdlib + pyyaml only — every other api module
depends on settings being configured first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SECRETS_PATH = Path("/run/secrets/decrypted.yaml")


def _looks_like_placeholder(value: str | None) -> bool:
    if not value:
        return True
    return value.startswith("<TODO") or value == "null"


def _load_secrets(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded or {}


def _build_database_url(pg_password: str) -> str:
    host = os.environ.get("API_DB_HOST", "postgres")
    port = os.environ.get("API_DB_PORT", "5432")
    name = os.environ.get("API_DB_NAME", "trading")
    user = os.environ.get("API_DB_USER", "app_service")
    return f"postgresql+asyncpg://{user}:{pg_password}@{host}:{port}/{name}"


def _exit(message: str, code: int = 2) -> int:
    print(f"[api-entrypoint] {message}", file=sys.stderr, flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    secrets_path = Path(os.environ.get("API_SECRETS_PATH", str(DEFAULT_SECRETS_PATH)))
    secrets = _load_secrets(secrets_path)

    if "API_DATABASE_URL" not in os.environ:
        pg_password = (secrets.get("postgres") or {}).get("app_service_password")
        if _looks_like_placeholder(pg_password):
            return _exit(
                f"postgres.app_service_password missing or placeholder in {secrets_path}; "
                "fill via `sops secrets/<env>.enc.yaml` and redeploy",
            )
        assert isinstance(pg_password, str)  # narrowed by _looks_like_placeholder above
        os.environ["API_DATABASE_URL"] = _build_database_url(pg_password)

    if "API_WATCHDOG_BEARER_TOKEN" not in os.environ:
        wb = (secrets.get("internal") or {}).get("watchdog_bearer_token")
        if wb and not _looks_like_placeholder(wb):
            os.environ["API_WATCHDOG_BEARER_TOKEN"] = wb

    if "API_VERSION" not in os.environ:
        os.environ.setdefault("API_VERSION", os.environ.get("RELEASE_SHA", "dev"))
    if "API_ENVIRONMENT" not in os.environ:
        os.environ.setdefault("API_ENVIRONMENT", os.environ.get("ENVIRONMENT", "dev"))

    cmd = argv or [
        "uvicorn",
        "services.api.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--proxy-headers",
        "--forwarded-allow-ips",
        "*",
    ]
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main())
