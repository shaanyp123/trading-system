# GitHub App — In-App PR Review Surface

Backend service identity for the in-app PR review surface (`/system/pr/:id`, frontend-spec §3.10; backend-spec §8.10).

The in-app PR review surface is the **canonical approval gate** for any change touching the forbidden whitelist (`services/risk/**`, `services/signal/**`, `services/audit/**`, `alembic/**`, etc.). Branch protection on `main` only requires CI pass; the human approval is the operator clicking **Approve** in the web UI, after which the backend GitHub App merges the PR via API.

The app is created **once**, by the operator, in the GitHub Apps settings UI. This directory is the canonical declaration of what the app must look like; the runbook below walks through the click-by-click creation.

---

## What this app does

| Action                                    | API used                                  | Permission         |
| ----------------------------------------- | ----------------------------------------- | ------------------ |
| Read PR metadata + diffs for review surface | `GET /repos/.../pulls/:n` + `.../files`   | `pull_requests:read`  |
| Read commit + check-run state             | `GET /repos/.../commits/:sha/check-runs`  | `checks:read`      |
| Read workflow run state for CI badge      | `GET /repos/.../actions/runs`             | `actions:read`     |
| Merge PR on operator approval             | `PUT /repos/.../pulls/:n/merge`           | `pull_requests:write` |
| Close PR on operator rejection            | `PATCH /repos/.../pulls/:n` (state=closed) | `pull_requests:write` |
| Comment on PR with approval rationale     | `POST /repos/.../issues/:n/comments`      | `pull_requests:write` (issues:read for thread)  |
| Push agent-drafted PRs to `agent/*` branches | `git push` via app installation token   | `contents:write`   |

It does **not** receive webhooks (the surface pulls on demand and on operator interaction). It does **not** need write access to actions, checks, secrets, workflows, members, or administration.

---

## Operator runbook — first-time creation

These steps run **once**. Total time: ~10 minutes. You will need 1Password (or equivalent password manager) ready.

### Step 1 — Create the app

1. Open https://github.com/settings/apps/new in a browser logged in as `shaanyp123`.
2. **GitHub App name:** `trading-system-pr-review`
   - GitHub requires global uniqueness. If taken, append a suffix (`-spratcap`, etc.) and record the actual name in `Docs/decisions-log.md`.
3. **Description:** `Backend identity for the in-app PR review surface — trading-system repo only.`
4. **Homepage URL:** `https://spratcapital.com/system`
5. **Identifying and authorizing users** section:
   - **Callback URL:** leave blank (no user OAuth flow).
   - **Expire user authorization tokens:** check (default).
6. **Webhook** section:
   - **Active:** **uncheck**. We don't subscribe to webhooks in Phase 0–1.
   - (Webhook URL + secret can be left blank when Active is unchecked.)
7. **Permissions** — set exactly these (anything not listed = "No access"):
   - **Repository permissions:**
     - `Contents`: **Read & write**
     - `Pull requests`: **Read & write**
     - `Metadata`: **Read-only** (auto-set; cannot disable)
     - `Actions`: **Read-only**
     - `Checks`: **Read-only**
     - `Issues`: **Read-only** (PR comments thread through issues API)
   - **Organization permissions:** none (app is on a personal account; section may not appear).
   - **Account permissions:** none.
8. **Subscribe to events:** none (webhooks disabled).
9. **Where can this GitHub App be installed?** **Only on this account**.
10. Click **Create GitHub App**.

You will land on the app settings page. **Capture the App ID** displayed near the top (a 6–7 digit integer). It is not secret; it is not a credential. Record in `Docs/decisions-log.md`.

### Step 2 — Generate the private key

1. Still on the app settings page, scroll to **Private keys**.
2. Click **Generate a private key**. A `.pem` file downloads — name like `trading-system-pr-review.YYYY-MM-DD.private-key.pem`.
3. **This file is a credential.** Treat it like the IBKR password.
   - Store it in 1Password as a Secure Note attachment with name `github-app-pr-review-private-key`.
   - On Day 3 (sops setup) it will be migrated into `secrets/{dev,paper,live}.enc.yaml` under `github.app_private_key`. Until Day 3, keep it in 1Password only.
   - **Do NOT** save it to disk under the repo. The `secrets/.gitignore` blocks `*.pem` from being committed even by mistake; the gitleaks CI gate would catch it; but the discipline is "credentials never sit on disk".
4. Once stored in 1Password, **delete the local download**.

### Step 3 — Install the app on the trading-system repo

1. Still on the app settings page, click **Install App** in the left sidebar.
2. Click **Install** next to `shaanyp123` (your account).
3. Choose **Only select repositories** → select `trading-system`.
4. Click **Install**.
5. After install, the URL contains the **Installation ID**:
   `https://github.com/settings/installations/<INSTALLATION_ID>` — capture this number.
6. Record the Installation ID in `Docs/decisions-log.md` alongside the App ID.

### Step 4 — Verify install (read-only smoke test)

You can verify the install is healthy without writing any code by using `gh api` with a JWT. Skip this if you'd rather wait for the Day 3 sops setup; the app is reachable as long as the install is in place.

### Step 5 — Day 3 sops migration

When Day 3 sops setup runs (`Docs/decisions-log.md` follow-up), add:

```yaml
github:
  app_id: "<APP_ID_FROM_STEP_1>"
  installation_id: "<INSTALLATION_ID_FROM_STEP_3>"
  app_private_key: |
    <paste the full PEM block from the .pem file in 1Password —
     including the BEGIN/END header lines that the GitHub-issued file already wraps it in>
```

to `secrets/dev.enc.yaml` and `secrets/paper.enc.yaml`. (The `live.enc.yaml` copy lands at Day 8 per the secrets/README.md schedule.) Backend-spec §8.1.1 expects only `app_id` + `app_private_key`; we add `installation_id` for caching (not strictly secret, but co-located by convention).

After all three env files are populated, **delete the 1Password copy** of the private key — sops + age becomes the single source of truth.

### Step 6 — Annual rotation (calendar reminder)

Add a calendar event for one year from creation: **rotate GitHub App private key**. The rotation procedure is:

1. Generate a new private key in the app settings (Step 2 above; can hold both old + new for ≤ 7 days).
2. Update `secrets/{dev,paper,live}.enc.yaml` with the new key; redeploy.
3. Once new key is in use, **revoke** the old key in the app settings.

This matches the age-key annual rotation cadence in backend-spec §8.1.2.

---

## Files in this directory

| Path                     | Purpose                                                                      |
| ------------------------ | ---------------------------------------------------------------------------- |
| `manifest.json`          | Canonical declaration of app permissions + metadata. Source of truth if the app needs to be re-created (rotation, recovery). The values in this file must match the GitHub UI config exactly. |
| `README.md` (this file)  | Operator runbook for creation, install, and rotation.                        |

---

## Why we don't auto-create via API

GitHub does not expose a `POST /user/apps` endpoint for unattended app creation. The two real options are:

1. **GitHub Apps settings UI** (manual click-through) — used here.
2. **App manifest flow** — POST a manifest to `https://github.com/settings/apps/new?manifest=…` and then exchange the resulting `code` for credentials at `POST /app-manifests/:code/conversions`. This requires the operator to have a live HTTP server at the redirect URL to receive the `code`. Since the trading-system backend isn't running on Day 2 (Phase 0 week 1, day 2), the manual UI flow is the correct path.

Once the backend is up (~Day 5–6), future apps (e.g., a separate one for the agent's PR drafts in Phase 2) **can** use the manifest flow against `/api/internal/github/app-manifest-callback`. We are not building that endpoint now.

---

## Notes for future Claude sessions

- **App ID is not a secret** — checking it into the decisions log is fine.
- **Installation ID is not a secret** — same.
- **Private key is a credential** — treat as critical. Never paste into chat (per `Docs/decisions-log.md` 2026-05-05 QC token incident).
- The PR review surface UI itself is a Phase 1 month 4 deliverable (claude-dev-guide §10.1). The app is created early so the agent's PR-draft tooling (Phase 1) and the surface (Phase 1 month 4) both have install credentials waiting.
