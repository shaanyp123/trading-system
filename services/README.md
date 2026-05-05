# Services

Each subdirectory is a standalone Python service that ships as its own Docker image. Per [backend-spec §1.4](../Docs/backend-spec.md), the Phase 1 stack is 19 services.

## Inventory

| # | Service | Container image | PR-required? | Phase |
|---|---|---|---|---|
| 1 | `api/` | `ghcr.io/shaanyp123/trading-api` | ✅ (forbidden) | 1 + 2 |
| 2 | `signal/` | `trading-signal` | ✅ (forbidden) | 1 + 2 |
| 3 | `risk/` | `trading-risk` | ✅ (forbidden) | 1 + 2 |
| 4 | `execution/` | `trading-execution` | ✅ (forbidden) | 1 (QC ObjStore) → 2 (ib-async) |
| 5 | `reconciliation/` | `trading-reconciliation` | ✅ (forbidden) | 1 + 2 |
| 6 | `audit/` | `trading-audit` | ✅ (forbidden) | 1 + 2 |
| 7 | `calibration/` | `trading-calibration` | ✅ (forbidden) | 1 + 2 |
| 8 | `scheduler/` | `trading-scheduler` | ❌ (hot-fix) | 1 + 2 |
| 9 | `qc_adapter/` | `trading-qc-adapter` | ❌ (hot-fix) | 1; backfill-only Phase 2 |
| 10 | `discord_bot/` | `trading-discord-bot` | ❌ (hot-fix) | 1 + 2 |
| 11 | `webhook_pusher/` | `trading-webhook-pusher` | ❌ (hot-fix) | 1 + 2 |
| 12 | `monitoring/` | `trading-monitoring` | ❌ (hot-fix) | 1 + 2 |
| 13 | `observability/` | `trading-observability` | ❌ (hot-fix) | 1 + 2 |
| 14 | `agent/decisions/` | `trading-agent` | ✅ (forbidden) | 1 + 2 |
| 14 | `agent/risk_actions/` | (same image) | ✅ (forbidden) | 1 + 2 |
| 14 | `agent/parameter_changes/` | (same image) | ✅ (forbidden) | 1 + 2 |
| 14 | `agent/prompts/decision/` | (same image) | ✅ (forbidden) | 1 + 2 |
| 14 | `agent/reporting/` | (same image) | ❌ (hot-fix) | 1 + 2 |
| 14 | `agent/monitoring/` | (same image) | ❌ (hot-fix) | 1 + 2 |
| 14 | `agent/integrations/` | (same image) | ❌ (hot-fix) | 1 + 2 |
| 14 | `agent/prompts/system/` | (same image) | ❌ (hot-fix) | 1 + 2 |

Plus 5 third-party containers in `docker-compose.yml`: `caddy`, `postgres`, `gitea`, `prometheus`, `grafana`. Phase 2 adds `ib_gateway` and `lean_local` behind the `phase2` profile.

## Path classification

- **Forbidden whitelist** (changes require `risk-review-approved` PR label; pre-merge linter blocks otherwise): see `.github/CODEOWNERS` and [dev-guide §2.2](../Docs/claude-dev-guide.md).
- **Hot-fix whitelist** (auto-deploy permitted; auto-rollback within 30 min on metric breach): [dev-guide §2.3](../Docs/claude-dev-guide.md).

## Build order (Phase 0)

1. Week 3 — `audit/`, `qc_adapter/`, `api/` (skeleton + health endpoint)
2. Week 4 — golden tests + immutability triggers ([implementation-guide.md §3 Week 4](../implementation-guide.md))
3. Week 5 — REST scaffolding + SSE + `calibration/`
4. Week 6 — `discord_bot/`
5. Week 7 — full signal-to-fill round trip
