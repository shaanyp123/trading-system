# GitHub Actions workflows

Two workflows gate every PR merge into `main`:

| File | Purpose | Required by branch protection |
|---|---|---|
| `ci.yml` | Build, lint, test, type-check, secret-scan | Yes — via the single `ci-gate` job |
| `forbidden-paths.yml` | Enforces `risk-review-approved` label on forbidden-whitelist paths | Yes — `forbidden-paths-gate` job |

This README documents `ci.yml`. The forbidden-paths workflow is small and self-documenting.

---

## Cost profile

| Change type | Jobs that run | Approx wall time |
|---|---|---|
| Docs-only (`Docs/**`, `**.md`, `LICENSE`, etc.) | precheck + lint + dep-drift + gitleaks + ci-gate | ~30s |
| Backend Python (api-bundled source: `services/**`, `infrastructure/**`, `strategies/**`, `alembic/**`, `scripts/**`) | + typecheck + test + `docker-build-api` + any other touched service's docker build | ~5-6m |
| Frontend only (`apps/web/**`) | + frontend-test + docker-build-web | ~3m |
| Workflow self-modify (`ci.yml`) | All conditional jobs fire (full matrix) | ~18m |
| `[skip ci]` in commit/PR | precheck + gitleaks + ci-gate | ~25s |

The breakdown is intentional: every minute spent inspects something relevant to the change. Lint, dep-drift, and gitleaks run on every push because they're cheap (~30s combined) and they catch issues that don't correlate with file paths (formatting drift, dep table desync, secrets pasted into any file).

**On a push to `main`,** an api-bundled source change additionally runs `docker-publish-api`, which pushes `ghcr.io/<owner>/trading-api:<sha>` + `:latest` so the VPS can `docker compose pull api`. That is why the `api` filter spans the whole image build context (every tree the Dockerfile COPYs), not just `services/api/**` — a narrower filter let `:latest` go stale when bundled code outside `services/api/**` changed (2026-06-08).

---

## Jobs

### precheck (always; ~10s)

Computes two things and exposes them as outputs:

1. **`skip_all`** — true if `[skip ci]`, `[ci skip]`, `[no ci]`, or `[skip actions]` appears in the head commit message or PR title.
2. **Path-filter flags** via `dorny/paths-filter@v3` — one boolean per area of the repo:
   - `workflow_self` — `.github/workflows/ci.yml` or `scripts/check_dockerfile_deps_against_pyproject.py`
   - `python_backend` — all Python source (`services/**`, `strategies/**`, `infrastructure/**`, `tests/**`, `scripts/**`, `alembic/**`, plus `pyproject.toml`, `Makefile`, `conftest.py`)
   - `frontend` — `apps/web/**` or `packages/**`
   - `api` — the api image's **full build context** (`services/**`, `infrastructure/**`, `strategies/**`, `alembic/**`, `alembic.ini`, `scripts/**`, `pyproject.toml`) — i.e. every tree `services/api/Dockerfile` COPYs into the image, **not** just `services/api/**`. Deliberately broad: it gates both `docker-build-api` (PR) and `docker-publish-api` (main push), and if it were narrower the published `:latest` would silently go stale whenever bundled-but-non-`api` code changed (root-caused 2026-06-08 — see the comment on the filter in `ci.yml`). Keep it in lockstep with the Dockerfile's COPY layers.
   - `discord_bot`, `webhook_pusher` — per-service Python source + `pyproject.toml` (build-only; these images are not published yet, so a narrow filter is correct for them)

Every downstream job references these flags in its `if:` condition.

### Always-on jobs

- **`lint`** (~10s) — `ruff format --check` + `ruff check`
- **`dep-drift-check`** (~5-10s) — diffs `pyproject.toml` runtime deps vs each `services/*/Dockerfile`'s pip-install list
- **`gitleaks`** (~10s) — secret scan over the working tree

These run on every push that doesn't have `[skip ci]`. `gitleaks` runs even with `[skip ci]` — secret-scanning is a universal security gate.

### Conditional jobs

| Job | Fires when |
|---|---|
| `typecheck` (mypy --strict) | `python_backend` or `workflow_self` |
| `test` (pytest --cov) | `python_backend` or `workflow_self` |
| `frontend-test` | `frontend` or `workflow_self` |
| `docker-build-api` | `api` or `workflow_self` |
| `docker-build-discord_bot` | `discord_bot` or `workflow_self` |
| `docker-build-web` | `frontend` or `workflow_self` |
| `docker-build-webhook_pusher` | `webhook_pusher` or `workflow_self` |

All five docker builds use `docker/build-push-action@v5` with GHA buildx cache (`type=gha`) — warm rebuilds reuse layers between PR runs.

### ci-gate (always; ~5s)

The single job branch protection should require.

`needs:` lists every conditional job. `if: always()` runs the gate even when upstreams skip. The verification step inspects each upstream's `result` and:

- **Passes** if all are `success` or `skipped`
- **Fails** if any are `failure` or `cancelled`

`skipped` counts as passing for the gate — this is the whole point of path-conditioning the expensive jobs.

---

## `[skip ci]` marker

Standard markers recognized: `[skip ci]`, `[ci skip]`, `[no ci]`, `[skip actions]`. Detected in the head commit message OR the PR title (PR events bypass GitHub's native marker handling, so we re-implement it explicitly).

**What it skips:** lint, dep-drift, typecheck, test, frontend-test, every docker build.

**What it does NOT skip:** gitleaks (universal security gate), precheck, ci-gate.

**When to use it:** genuine emergencies. For routine docs-only changes, just push — path filters already skip the expensive jobs automatically.

---

## Branch protection setup

After this rework lands, set required status checks to ONLY:

- `ci-gate (required status check)` — from `.github/workflows/ci.yml`
- `forbidden-paths (risk-review-approved gate)` — from `.github/workflows/forbidden-paths.yml`

**Remove** every individual job name from the required list (`lint`, `test`, `docker build (api)`, …). They still run in the workflow, but `ci-gate` aggregates them. If individual jobs stayed required, docs-only PRs would deadlock (skipped → branch protection treats as not-passing → merge blocked).

---

## Adding a new conditional job

1. Add a filter to `precheck.changes` (`filters:` block).
2. Add an output for it to `precheck.outputs`.
3. Write the job with:
   ```yaml
   needs: precheck
   if: >-
     needs.precheck.outputs.skip_all != 'true' &&
     (needs.precheck.outputs.YOUR_FILTER == 'true' ||
      needs.precheck.outputs.workflow_self == 'true')
   ```
4. **Add the new job to `ci-gate.needs`.** Forgetting this means the job's failure will not block merge — silent supply-chain risk.

---

## Local equivalent

`make ci` runs lint + dep-drift + typecheck + test + frontend-test against the developer's checkout. Docker builds are skipped locally (they're CI-only). `dep-drift-check` is the gate that catches drift between Dockerfiles and pyproject.toml — run it locally before pushing if you touched either.

---

## Action pinning

Per dev-guide §11, GHA actions pin to a major version at minimum (`@v3`), never a floating tag (`@main`, `@latest`). First-party `actions/*` and `docker/*` are at major-version pins; third-party `dorny/paths-filter` is at `@v3` per the same policy. If supply-chain concerns sharpen, upgrade third-party pins to SHA pinning.
