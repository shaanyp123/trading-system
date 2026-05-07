"""services/api/routes/health.py — public liveness probe.

Per backend-spec §7.3 the watchdog calls `GET /api/health` every 5 minutes.
The endpoint returns 200 when degraded-but-running, 503 when something is
wired but unreachable. The Day-5 Phase-0 implementation only checks Postgres
reachability + the `SELECT 1` latency budget; the §7.3 criteria around
qc_adapter cursor lag and reconciliation freshness wire in when those
services land.

Response body (matches the implementation-guide Day 5 contract):

    {
      "status": "ok" | "degraded",
      "environment": "dev" | "paper" | "live-small" | "live-scale",
      "version": "<git sha or build tag>",
      "db_connected": true | false,
      "checks": [{"name": "postgres", "ok": bool, "latency_ms": float?, "detail": str?}]
    }
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response

from services.api import db as api_db
from services.api.config import get_settings

router = APIRouter()


@router.get("/api/health", tags=["health"])
async def health(response: Response) -> dict[str, Any]:
    settings = get_settings()
    start = time.perf_counter()
    db_ok, db_err = await api_db.ping()
    db_latency_ms = round((time.perf_counter() - start) * 1000.0, 2)

    checks = [
        {
            "name": "postgres",
            "ok": db_ok,
            "latency_ms": db_latency_ms,
            "detail": db_err,
        }
    ]

    overall_ok = db_ok and db_latency_ms < 200.0
    status = "ok" if overall_ok else "degraded"
    if not db_ok:
        # 503 ONLY when Postgres is unreachable. Latency-only degradation
        # surfaces in the body but stays 200 so the watchdog doesn't alert
        # on transient slow queries.
        response.status_code = 503

    return {
        "status": status,
        "environment": settings.environment,
        "version": settings.version,
        "db_connected": db_ok,
        "checks": checks,
    }
