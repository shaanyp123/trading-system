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


def _looks_like_placeholder(value: Any) -> bool:
    if value is None or value == "":
        return True
    if not isinstance(value, str):
        # Non-string sops values (e.g., yaml-int flex_query_id) cannot
        # be `<TODO_...>` placeholders by construction; treat as real.
        return False
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

    # Day 23: Discord-bot bearer (sops yaml `discord.api_bearer_token`).
    # When unset the api still boots — BotAuthMiddleware degrades to a
    # noop and the bot path is simply not served until the operator
    # adds the secret per `deploy/discord_bot/README.md`.
    if "API_DISCORD_BOT_BEARER_TOKEN" not in os.environ:
        bb = (secrets.get("discord") or {}).get("api_bearer_token")
        if bb and not _looks_like_placeholder(bb):
            os.environ["API_DISCORD_BOT_BEARER_TOKEN"] = bb

    # Pivot-PR-A (post-pivot 2026-05-12): LEAN Local bearer (sops yaml
    # `lean.api_bearer_token`). When unset the api still boots —
    # LeanAuthMiddleware degrades to a noop and the LEAN path is simply
    # not served until the operator adds the secret per
    # `deploy/lean_local/README.md`. Distinct from `discord.api_bearer_token`
    # so a compromise of one container can't grant access via the other.
    if "API_LEAN_LOCAL_BEARER_TOKEN" not in os.environ:
        lb = (secrets.get("lean") or {}).get("api_bearer_token")
        if lb and not _looks_like_placeholder(lb):
            os.environ["API_LEAN_LOCAL_BEARER_TOKEN"] = lb

    # Day 21 carryover: TOTP column-encryption key (sops yaml
    # `totp.encryption_key`). Base64url-encoded 32-byte AES-256-GCM key
    # per services/api/config.py `totp_encryption_key`. When unset the
    # TOTP-touching endpoints fail closed with `TOTP_KEY_MISSING` —
    # never silently fall back to plaintext storage.
    if "API_TOTP_ENCRYPTION_KEY" not in os.environ:
        tk = (secrets.get("totp") or {}).get("encryption_key")
        if tk and not _looks_like_placeholder(tk):
            os.environ["API_TOTP_ENCRYPTION_KEY"] = tk

    # Day 21 carryover: WebAuthn relying-party identity (sops yaml
    # `webauthn.rp_id` + `.rp_name` + `.origin`). Defaults in
    # services/api/config.py are dev-only (localhost rp_id) — production
    # MUST override via these env vars so credentials register against
    # the apex domain. When any single key is unset, the corresponding
    # APISettings field falls back to its default; WebAuthn ceremonies
    # against a placeholder rp_id will fail at verify time loudly.
    if "API_WEBAUTHN_RP_ID" not in os.environ:
        rp_id = (secrets.get("webauthn") or {}).get("rp_id")
        if rp_id and not _looks_like_placeholder(rp_id):
            os.environ["API_WEBAUTHN_RP_ID"] = rp_id

    if "API_WEBAUTHN_RP_NAME" not in os.environ:
        rp_name = (secrets.get("webauthn") or {}).get("rp_name")
        if rp_name and not _looks_like_placeholder(rp_name):
            os.environ["API_WEBAUTHN_RP_NAME"] = rp_name

    if "API_WEBAUTHN_ORIGIN" not in os.environ:
        origin = (secrets.get("webauthn") or {}).get("origin")
        if origin and not _looks_like_placeholder(origin):
            os.environ["API_WEBAUTHN_ORIGIN"] = origin

    # Worker-PR-1 follow-up (post-pivot 2026-05-12): IBKR account
    # number for the api-resident OrderPlacementWorker. Sourced from
    # sops; canonical key is ``ibkr.account_number`` (single field —
    # the operator's IBKR Pro account is a single number across paper
    # and live; the env-tag distinction is in IBKR's portal, not in
    # the account number). Per-env fallback keys
    # ``ibkr.paper_account`` / ``ibkr.live_account`` are accepted for
    # forward-compat with possible future multi-account setups.
    #
    # When unset / placeholder the worker still starts but the IBKR
    # adapter falls back to the default account on the TWS session.
    # That works for single-account setups; ``account_number`` makes
    # the binding explicit for clearer audit logs + multi-account
    # safety.
    if "API_IBKR_ACCOUNT" not in os.environ:
        ibkr = secrets.get("ibkr") or {}
        env_tag = os.environ.get("API_ENVIRONMENT") or os.environ.get("ENVIRONMENT", "dev")
        # Canonical first; env-specific fallback second.
        acc: Any = ibkr.get("account_number")
        if not acc or _looks_like_placeholder(acc):
            if env_tag in ("paper", "dev"):
                acc = ibkr.get("paper_account")
            else:
                acc = ibkr.get("live_account")
        if acc and not _looks_like_placeholder(acc):
            os.environ["API_IBKR_ACCOUNT"] = str(acc)

    # Worker-PR-3b follow-up (post-pivot 2026-05-12): IBKR FlexQuery
    # credentials for the api-resident ReconciliationScheduler. Sourced
    # from sops `ibkr.flex_query_id` + `ibkr.flex_query_token`. When
    # unset / placeholder, the scheduler does NOT start at api boot —
    # the api still serves requests + the warning is visible in api
    # logs at startup. Operator populates the sops fields once the
    # IBKR-portal FlexQuery template is created.
    if "API_FLEX_QUERY_ID" not in os.environ or "API_FLEX_QUERY_TOKEN" not in os.environ:
        ibkr = secrets.get("ibkr") or {}
        flex_id: Any = ibkr.get("flex_query_id")
        flex_token: Any = ibkr.get("flex_query_token")
        if (
            "API_FLEX_QUERY_ID" not in os.environ
            and flex_id
            and not _looks_like_placeholder(flex_id)
        ):
            os.environ["API_FLEX_QUERY_ID"] = str(flex_id)
        if (
            "API_FLEX_QUERY_TOKEN" not in os.environ
            and flex_token
            and not _looks_like_placeholder(flex_token)
        ):
            os.environ["API_FLEX_QUERY_TOKEN"] = str(flex_token)

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
