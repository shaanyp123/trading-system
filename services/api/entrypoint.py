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


def _find_float_secret_paths(node: Any, *, path: tuple[str, ...] = ()) -> list[str]:
    """Walk a sops-decrypted dict tree; return dotted paths of any float leaves.

    Catches the YAML numeric-coercion gotcha documented in
    ``Docs/decisions-log.md`` 2026-05-12 (late) — IBKR's 24-digit FlexQuery
    token, when stored unquoted in sops yaml, round-trips through sops's
    internal float-aware writer as ``"1.527484903607521e+23"`` (scientific
    notation = float64 approximation; precision-loss silently mangles the
    token). The float→str coercion on next `sops -d` read produces a
    Python ``float`` where the api expected a ``str``.

    Fix: catch floats in secret context at boot, exit with operator-readable
    hint pointing at the quote-the-value fix + the decisions-log entry.

    A05-adjacent: money values are Decimal, but secrets are strings. The
    spec doesn't enumerate "no float secrets" because all canonical secret
    fields (passwords, tokens, API keys, emails, account numbers) ARE
    strings by construction. A float in the secrets bundle is a sentinel
    that YAML numeric coercion ate something.

    Returns
    -------
    list[str]
        Dotted paths (e.g., ``["ibkr.flex_query_token"]``). Empty list if
        no floats found. Sorted for deterministic exit output.

    Edge cases:
      * Ints are allowed (sops yaml legitimately carries int values like
        ``ibkr.flex_query_id: 1505530`` — confirmed via the existing
        ``_looks_like_placeholder`` carve-out for non-string sops values).
      * Bools are allowed (yaml-loaded as bool, not float).
      * NoneType is allowed (covered by ``_looks_like_placeholder``).
      * Lists are walked recursively (a yaml list-of-numbers would expose
        the same gotcha; defensive coverage).
    """
    matches: list[str] = []
    if isinstance(node, float):
        matches.append(".".join(path) if path else "<root>")
        return matches
    if isinstance(node, dict):
        for key, value in node.items():
            matches.extend(_find_float_secret_paths(value, path=(*path, str(key))))
        return matches
    if isinstance(node, list):
        for idx, value in enumerate(node):
            matches.extend(_find_float_secret_paths(value, path=(*path, f"[{idx}]")))
        return matches
    return matches


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

    # Defensive guard against the YAML numeric-coercion gotcha documented
    # in Docs/decisions-log.md 2026-05-12 (late). PyYAML's safe_load turns
    # unquoted scientific-notation values into Python floats; if any leaf
    # secret is a float, sops likely round-tripped a long-digit string
    # through float64 and lost precision. Fail loud + tell the operator
    # the fix is to quote the value in sops.
    float_paths = _find_float_secret_paths(secrets)
    if float_paths:
        path_list = ", ".join(f"`{p}`" for p in sorted(float_paths))
        return _exit(
            f"sops bundle at {secrets_path} contains float-typed value(s) at: "
            f"{path_list}. "
            "This usually means YAML numeric coercion ate a long-digit "
            "string secret (e.g., IBKR FlexQuery token). Edit "
            "`secrets/<env>.enc.yaml` via `sops` and wrap the value in "
            'double quotes (e.g., `flex_query_token: "152748490360752094531342"`), '
            "then re-deploy. See Docs/decisions-log.md 2026-05-12 (late) "
            "'FlexQuery debug journey' for the full root cause + fix.",
        )

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

    # Worker-PR-1 follow-up (post-pivot 2026-05-12): IBKR account number
    # for the api-resident OrderPlacementWorker + EOD recon + bar_sync.
    # Sourced from sops.
    #
    # CORRECTED 2026-05-29: IBKR PAPER accounts have a DISTINCT account id
    # (e.g. ``DUQ825170``) from the live IBKR Pro account (e.g. ``U25655583``)
    # — they are NOT "a single number across paper and live" as the prior
    # comment claimed. The broker's ``reqPositions`` / account view is keyed
    # on the env-specific number, so we must resolve the ENV-SPECIFIC key
    # FIRST (``ibkr.paper_account`` for paper/dev, ``ibkr.live_account`` for
    # live), falling back to the generic ``ibkr.account_number`` only when the
    # env-specific key is unset/placeholder (single-account setups).
    #
    # The prior logic took ``account_number`` FIRST, which on the paper
    # gateway silently mis-keyed every account-filtered ``get_positions()`` to
    # the LIVE account number -> 0 broker positions -> nightly false-positive
    # recon ``position_qty`` breaks + kill-switch halts, and a position-blind
    # order-worker margin pre-check. See decisions-log 2026-05-29.
    #
    # When the resolved value is unset / placeholder the worker still starts
    # but the IBKR adapter falls back to the default account on the TWS
    # session.
    if "API_IBKR_ACCOUNT" not in os.environ:
        ibkr = secrets.get("ibkr") or {}
        env_tag = os.environ.get("API_ENVIRONMENT") or os.environ.get("ENVIRONMENT", "dev")
        # Env-specific account FIRST; generic ``account_number`` as fallback.
        if env_tag in ("paper", "dev"):
            primary: Any = ibkr.get("paper_account")
        else:
            primary = ibkr.get("live_account")
        fallback: Any = ibkr.get("account_number")
        acc: Any = primary if (primary and not _looks_like_placeholder(primary)) else fallback
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

    # Reconciliation alert_dispatch_hook (PR for follow-up #2; closes the
    # operator-visibility seam from PR #135). Sources Discord webhook URLs
    # + Resend identity from sops — same fields the standalone
    # webhook_pusher uses (per `deploy/webhook_pusher/README.md`). When
    # `discord.webhook_urls.alerts` is unset, the api skips constructing
    # the hook entirely + recon still runs (see services/api/main.py
    # `_build_alert_dispatch_hook` for the soft-fail semantics).
    webhook_urls = (secrets.get("discord") or {}).get("webhook_urls") or {}
    if "API_DISCORD_WEBHOOK_URL_ALERTS" not in os.environ:
        url = webhook_urls.get("alerts")
        if url and not _looks_like_placeholder(url):
            os.environ["API_DISCORD_WEBHOOK_URL_ALERTS"] = url
    if "API_DISCORD_WEBHOOK_URL_CRITICAL" not in os.environ:
        url = webhook_urls.get("critical")
        if url and not _looks_like_placeholder(url):
            os.environ["API_DISCORD_WEBHOOK_URL_CRITICAL"] = url
    if "API_RESEND_API_KEY" not in os.environ:
        rk = (secrets.get("resend") or {}).get("api_key")
        if rk and not _looks_like_placeholder(rk):
            os.environ["API_RESEND_API_KEY"] = rk
    if "API_RESEND_FROM_ADDRESS" not in os.environ:
        fa = (secrets.get("resend") or {}).get("from_address")
        if fa and not _looks_like_placeholder(fa):
            os.environ["API_RESEND_FROM_ADDRESS"] = fa
    if "API_RESEND_TO_ADDRESS" not in os.environ:
        ta = (secrets.get("resend") or {}).get("to_address")
        if ta and not _looks_like_placeholder(ta):
            os.environ["API_RESEND_TO_ADDRESS"] = ta

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
