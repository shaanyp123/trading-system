"""services/signal/worker_entrypoint.py — strategy_worker container PID-1 shim.

The strategy_worker compose service reuses the **api image** (same code
tree, same deps — pandas/numpy for the engine, coinbase-advanced-py for
the transport, websockets for the marks feed are all already in it; no
second Dockerfile to keep dep-drift-checked). This module reuses the api
entrypoint's secrets→env mapping wholesale (host secrets file at
``/run/secrets/secrets.yaml`` → ``API_DATABASE_URL`` +
``API_COINBASE_API_KEY_NAME`` + ``API_COINBASE_API_PRIVATE_KEY`` + env
identity), then exec()s the worker runner instead of uvicorn:

    ENTRYPOINT tini -- python -m services.signal.worker_entrypoint
        → services.api.entrypoint.main(argv=[python, -m, services.signal.worker_main])
        → os.execvp → worker_main.main()

Fail-closed semantics are inherited: a missing/placeholder
``postgres.app_service_password`` exits 2 before the worker ever starts;
missing Coinbase credentials exit 2 inside ``worker_main`` (nothing that
trades starts half-configured).

A02 BINDS — ``services/signal/**`` forbidden path.
"""

from __future__ import annotations

import sys

from services.api import entrypoint as api_entrypoint


def main() -> int:
    """Map secrets → env via the api entrypoint, then exec the worker."""
    return api_entrypoint.main(argv=["python", "-m", "services.signal.worker_main"])


if __name__ == "__main__":
    sys.exit(main())
