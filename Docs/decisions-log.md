# Decisions Log

Canonical log of decisions made and deviations from the specs as the build progresses. The specs (`backend-spec.md`, `frontend-spec.md`, `claude-dev-guide.md`) define the **target** architecture and conventions; this log records where reality differs and why.

**Update protocol:** every session that makes a non-trivial decision adds an entry here, then references it from any spec section that's now wrong or incomplete. Entries are append-only; if a decision is reversed, add a new entry referencing the old one rather than editing history.

**Format per entry:**
- **Date** — when decided (absolute date, not "today")
- **Topic** — short descriptor
- **Spec reference** — section that's affected
- **Spec said** — the original assumption
- **Actual decision** — what was chosen
- **Rationale** — why
- **Cost / scope impact** — concrete delta

---

## Entries

### 2026-05-05 — Day 1 — Repo name `trading-system` (matches spec naming)

- **Spec reference:** `Docs/backend-spec.md` §1.1 ("trading/" placeholder); `implementation-guide.md` §11 Day 1 ("Create new repo `trading-system`")
- **Spec said:** create new repo `trading-system`.
- **Actual decision:** existing pre-development repo `Trading` was renamed to `trading-system` rather than creating a fresh empty one. Docs (CLAUDE.md, README.md, implementation-guide.md, Docs/) were already committed; renaming preserves provenance and avoids duplicate-repo confusion.
- **Rationale:** docs and code are intended to live in the same repo per CLAUDE.md ("When code lands during Phase 0..."). Renaming is a one-line `gh repo rename` with auto-redirect on old URLs.
- **Cost / scope impact:** none. Local working directory `/Users/shaanpatel/Documents/GitHub/Trading/` retained as-is (filesystem path doesn't have to match GitHub repo name). GHCR image namespace permanently `ghcr.io/shaanyp123/trading-system-*`.

---

### 2026-05-05 — Day 1 — Hetzner watchdog moved Falkenstein → Nuremberg

- **Spec reference:** `Docs/backend-spec.md` §1.6 ("Falkenstein (locked — different region from Ashburn)"); `implementation-guide.md` §2.1 row 3 (Hetzner watchdog Falkenstein).
- **Spec said:** watchdog must run in Hetzner's Falkenstein DC.
- **Actual decision:** watchdog provisioned in Hetzner **Nuremberg** (NBG1) instead.
- **Rationale:** Falkenstein had no CX-line capacity at provisioning time on 2026-05-05. Nuremberg is in the same Hetzner German region with identical pricing and characteristics — satisfies the spec's intent ("EU watchdog geographically separated from US Ashburn primary"). The `(locked)` annotation in the spec was a logical lock on isolation properties, not a literal lock on the one DC.
- **Cost / scope impact:** none functionally. Watchdog static IP for Caddyfile allowlist becomes `188.245.37.16` (replaces `<watchdog_static_ip>` placeholder at deploy).

---

### 2026-05-05 — Day 1 — Hetzner shared-CPU line renamed (CX11 retired → CX23)

- **Spec reference:** `Docs/backend-spec.md` §1.6 (CX11); `implementation-guide.md` §2.1 row 3 + §2.3 cost table (CX11 ~$5/mo).
- **Spec said:** watchdog runs on Hetzner CX11 (1 vCPU, 2 GB).
- **Actual decision:** watchdog runs on **CX23** (2 vCPU, 4 GB, 40 GB SSD, 20 TB traffic) — Hetzner retired the CX11 SKU and renamed the shared-CPU line. The `CX22/32/42/52` from prior generations is now `CX23/33/43/53`. Pricing on CX23 is $4.99/mo + $0.60 IPv4 = **$5.59/mo total**.
- **Rationale:** CX11 no longer exists; CX23 is the current entry tier. Workload (5-min cron `curl /api/health`) doesn't need anywhere near CX23's resources, but it's the smallest current option.
- **Cost / scope impact:** +$0.59/mo over spec's $5/mo target. Negligible.

---

### 2026-05-05 — Day 1 — GitHub Pro upgrade ($4/mo) for branch protection

- **Spec reference:** `implementation-guide.md` §2.3 cost table (no GitHub line item — assumed free); §11 Day 1 ("Add branch protection to `main`"); Week 1 verification gate ("direct push to `main` blocked").
- **Spec said:** GitHub free tier sufficient.
- **Actual decision:** upgraded the operator's GitHub account to **Pro at $4/mo**. Branch protection (and rulesets) are paywalled features on private repos for free-tier users. Without the upgrade we cannot mechanically enforce the Week 1 gate.
- **Rationale:** spec was written assuming branch protection on private repos was free. GitHub gated it post-spec. The alternative (make repo public to access free branch protection) would expose strategy code and audit logic — wrong trade for $4/mo. Other Pro perks (Actions minutes 2000→3000, storage 500MB→2GB) are useful as the CI matrix grows.
- **Cost / scope impact:** +$4/mo Phase 0 burn. Inside the $200/mo soft alert and $300/mo hard alert.

---

### 2026-05-05 — Day 1 — Branch protection rules applied to `main`

- **Spec reference:** `implementation-guide.md` §11 Day 1 ("require CI to pass; no direct push to `main`").
- **Spec said:** require CI pass + no direct push.
- **Actual decision:** applied via GitHub API. Required status checks: `lint (ruff)` + `gitleaks (secret scan)` (strict mode — branch must be up to date with `main` before merge). Required PR for any change (0 approvals required, since solo operator). Force pushes blocked. Branch deletion blocked. `enforce_admins=false` (operator can bypass for emergency hot-fix only — sensible for solo operations).
- **Rationale:** matches dev-guide §1.2 + §10.1 mechanical-gating intent. `enforce_admins=false` is a deliberate concession to the solo-operator constraint: there's no second account to bypass for emergencies, and pre-paying that fragility every time isn't worth it. Re-evaluate when (if) a second contributor joins.
- **Cost / scope impact:** none. Workflow change: from now on, all changes touching `main` (including by Claude Code) go through a feature branch + PR + CI.

---

### 2026-05-05 — Day 1 — Hot-fix CI: `gitleaks-action@v2` → gitleaks CLI

- **Spec reference:** none — this is a CI implementation choice, not spec'd.
- **Decision:** the initial scaffold used `gitleaks/gitleaks-action@v2`, which failed with HTTP 403 ("Resource not accessible by integration") because the default `GITHUB_TOKEN` lacks `pull-requests: read` permission on private repos. Switched to gitleaks CLI installed inline in the workflow (no GitHub API access needed). Also dropped the git-history scan (false-positive on the string `"Argon2id-verify"` in a Mermaid sequence diagram in `frontend-spec.md` from a pre-`Docs/`-move commit). Working-tree-only scan is the correct PR gate.
- **Rationale:** CLI is more portable (no action permission surface), simpler to debug locally (`gitleaks detect --no-git`), and version-explicit (pinned to 8.24.3 in the workflow).
- **Cost / scope impact:** none.

---

### 2026-05-05 — Day 1 — QuantConnect Researcher tier $60/mo (was $20 in spec)

- **Spec reference:** `implementation-guide.md` §2.1 row 2 ("Quant Researcher ($20/mo)"); §2.3 cost table ($20/mo); references throughout to "Quant Researcher" tier.
- **Spec said:** Quant Researcher tier at $20/mo provides what we need.
- **Actual decision:** upgraded to QC's **Researcher** tier at **$60/mo** (1 backtest node, 1 research node, **1 live trading node**, ObjectStore expandable). Free tier has 0 live trading nodes and is unviable for Phase 1 (which requires the QC paper algo running 24/7). Researcher is the cheapest tier with any live trading capacity.
- **Rationale:** spec's $20 figure was outdated. QC's pricing page is now a configurator with no published flat-rate "Quant Researcher $20" SKU. The cheapest config that enables our Phase 1 architecture is $60/mo. No realistic alternative exists short of skipping QC entirely (which means doing direct-IBKR Phase 2 architecture from start, a 3+ month scope expansion).
- **Cost / scope impact:** +$40/mo over spec ($480/yr). Still inside soft alert ($200/mo) and hard alert ($300/mo). Eats roughly 5–10% of the $5–10k Phase 0 infra reserve from §2.3.

---

### 2026-05-05 — Day 1 — QC API token rotated due to chat leak

- **Spec reference:** `Docs/claude-dev-guide.md` §11 anti-pattern A11 (no inline secrets).
- **Decision:** the first-generated QC API token was accidentally pasted into the Claude Code conversation. The token is therefore in Anthropic's logs and the local session JSONL file. Rotated immediately via QC's "Reset Token" — old token invalidated. New token saved only to 1Password.
- **Rationale:** rotation is the only complete remediation for a credential leak. Caught immediately (zero live trading active), so blast radius is zero, but discipline matters now to set the pattern for when real money is at stake.
- **Cost / scope impact:** none. ~5 minutes of operator time.
- **Lesson for future sessions:** the chat is not a safe place for any secret. Even the first 4 chars of a token. Even with "I'll edit it out later" intent. Token-grade secrets go directly into 1Password (or the sops-encrypted file), full stop.

---

### 2026-05-05 — Day 1 — Phase 0 monthly burn revised

- **Spec reference:** `implementation-guide.md` §2.3 ("Total Phase 0 estimate $58–73/mo"); `Docs/backend-spec.md` no fixed budget cited.
- **Decision:** revised Phase 0 monthly burn to **$103–118/mo** (fixed) + Anthropic API variable.
- **Drivers:**
  - Hetzner Ashburn CCX13: **$25** (matches spec)
  - Hetzner Nuremberg CX23: **$5.59** (vs spec's $5; CX11 retired)
  - QC Researcher: **$60** (vs spec's $20; pricing reality)
  - GitHub Pro: **$4** (new line; spec assumed free)
  - Domain (Cloudflare apex): **$1** (matches)
  - S3/B2: **$2** (matches; not yet provisioned)
  - Resend, Sentry: **$0** free tier (matches)
  - Anthropic API: **$5–20** variable (matches)
- **Total fixed:** $97.59 + Anthropic = **$103–118/mo**.
- **Rationale:** sum of above deviations. None individually breaks budget; together they push Phase 0 ~30–45/mo above spec upper bound. Still well below soft alert ($200) and hard alert ($300) ceilings.
- **Cost / scope impact:** ~$540/yr more than spec. Within the $5–10k Phase 0 infra reserve from §2.3.

---

### 2026-05-05 — Day 2 — sops 3.12 macOS: explicit `SOPS_AGE_KEY_FILE` required

- **Spec reference:** `deploy/sops/README.md` Step 3 (original wording: "sops looks up age keys at this path by default"); `Docs/backend-spec.md` §8.1 (sops/age threat & recovery model).
- **Spec said:** sops auto-resolves `~/.config/sops/age/keys.txt` as the default age identity file — no env var or config required.
- **Actual decision:** sops 3.12.2 on macOS does NOT auto-resolve that path. Empirically: `sops -e -i secrets/dev.enc.yaml` succeeded (encryption only needs the public recipient from `.sops.yaml`), but `sops -d secrets/dev.enc.yaml` failed with `age: identity did not match any of the recipients` plus a fallback list mentioning only SSH-related env vars (`SOPS_AGE_SSH_PRIVATE_KEY_FILE`, `SOPS_AGE_KEY_FILE`, etc.) — notably no mention of the `~/.config/sops/age/keys.txt` default path. Workaround: `export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"` in `~/.zshrc`. Decryption then succeeded (`foo: bar` round-trip).
- **Rationale:** unclear whether this is a sops 3.12 regression, a long-standing macOS-specific behavior, or our particular install (Homebrew sops 3.12.2 on Darwin 25.3) somehow disabled the default lookup. The empirical fix (explicit env var) is reliable and version-agnostic; we add it to the runbook as a required step rather than relying on the buggy default.
- **Cost / scope impact:** none. One line in `~/.zshrc` per operator laptop. Not a deploy concern (CI/Hetzner contexts will set `SOPS_AGE_KEY_FILE` explicitly in their own env). Runbook updated in same PR (`deploy/sops/README.md` Step 3 split into 3a-install / 3b-export, Troubleshooting section now leads with this issue).

---

### 2026-05-05 — Day 2 — Three age keys generated, `.sops.yaml` populated, paper backups in safe

- **Spec reference:** `deploy/sops/README.md` (full runbook); `Docs/backend-spec.md` §8.1.2 (paper-in-safe recovery model).
- **Decision:** completed the Day 2 15:00 sops setup task. Three age keypairs generated (dev / paper / live); public keys substituted into `.sops.yaml` via `scripts/sops_init.sh`; private keys concatenated into `~/.config/sops/age/keys.txt` (perms 600); three printed copies on acid-free archival paper placed in fireproof safe (separate sleeves; not co-located with WebAuthn backup-codes paper); plaintext source files (`~/<env>-key.txt`) deleted. Annual rotation reminder set for 2027-05-05. Smoke test (encrypt → decrypt round-trip) passed after the env-var fix above.
- **Public key fingerprints (for safe-to-digital cross-check during rotations):**
  - dev: `age1f5mmkn6st8694n36wulj8emzjf5pl0a8herqfr9lqwmcpak5yeqqsfwujj`
  - paper: `age1dth25vwm75fpc32an0e77y39je2q8uyqe4sx3ysxjlamnlu6n43qrpa4wh`
  - live: `age1srnd3y9sx2ze0w2258v7dda3pvqw26r665uhr64kz50jw3w7sazqr63mrf`
- **Cost / scope impact:** none. Day 3 09:00 sops initialization (encrypting GitHub App private key + Discord bot token + initial schema into `secrets/{dev,paper,live}.enc.yaml`) is now a 15-minute follow-on per the runbook's "What Day 3 will do" section.

---

### 2026-05-05 — Day 2 — GitHub App created via UI walkthrough, not API

- **Spec reference:** `implementation-guide.md` §11 Day 2 09:30 ("Create the GitHub App via gh CLI: `gh api POST /user/apps` or walk through the GitHub Apps settings UI").
- **Spec said:** create via gh CLI OR walk through the UI.
- **Actual decision:** UI walkthrough only. There is no `POST /user/apps` endpoint on GitHub (the spec's example is fictitious). The two real options are (a) the manifest flow, which requires a live HTTP redirect URL the backend doesn't yet have on Day 2, and (b) the GitHub Apps settings UI. We went with the latter and shipped a canonical declaration (`deploy/github-app/manifest.json`) + an operator click-by-click runbook (`deploy/github-app/README.md`).
- **Rationale:** non-existent API can't be used; manifest flow's redirect requirement is incompatible with current state (backend not deployed). Manual UI walkthrough is the correct path. The manifest.json in the repo is the source-of-truth the operator fills against.
- **Cost / scope impact:** none. ~10 minutes of operator time when they execute the runbook (Day 2 14:00 or later — operator's choice; non-blocking).

---

### 2026-05-05 — Day 2 — Hurst exponent estimator: R/S (rescaled-range), not DFA

- **Spec reference:** `Docs/backend-spec.md §2.3` ("Hurst exponent ≥ HURST_THRESHOLD over the same lookback").
- **Spec said:** use the Hurst exponent — does not specify the estimator.
- **Actual decision:** classical R/S (rescaled-range) analysis. Implemented in `strategies/v1_trend_following/indicators.py:hurst_exponent_rs`. Tuned for the 60-bar default lookback: `min_chunks=4`, `min_chunk_size=4`, `min_closes=40`. Two regression points at the 60-bar window (chunks of 14 and 7); more at longer windows.
- **Rationale:** R/S is the textbook Hurst estimator and matches QC's standard library; choosing it gives byte-for-byte parity between LEAN and our backend backtester (the weekly parity gate per backend-spec §10.2 cares about this). DFA (detrended fluctuation analysis) is more robust on short series and is a credible Phase 2 swap, but that's a strategy logic change requiring a `risk-review-approved` PR; deferring until calibration evidence supports it.
- **Cost / scope impact:** none. Documented small-sample bias (R/S over-estimates H on short windows); HURST_THRESHOLD is configurable so the operator/agent can compensate.

---

### 2026-05-05 — Day 2 — Phase 1 candidate sub-universe LOCKED (operator-confirmed)

- **Spec reference:** `Docs/backend-spec.md §2.3` ("micro futures + bond ETFs"); `implementation-guide.md` §3 Week 2 Tue (sub-universe verification).
- **Spec said:** Phase 1 trades a sub-universe of CME micro futures + NYSE bond ETFs; specific markets not enumerated.
- **Actual decision (LOCKED):** the candidate pool below is the strategy's target universe. `strategies/v1_trend_following/parameters.py:V1_CANDIDATE_UNIVERSE`:
  - **Micros:** /MES, /MNQ, /MYM, /M2K, /MGC, /MCL, /MBT
  - **Bond ETFs:** TLT, IEF, SHY, TIP
- **Rationale:** smallest plausible sub-universe covering equity-index (4 micros), commodity (gold + crude), crypto (BTC), and rates (4-point bond ETF curve). Each candidate carries a one-line notional rationale in the source. The active set at runtime is the OUTPUT of `services/risk/sizing.py` Stage 0 (1-contract-notional ≤ 50% × equity per backend-spec §2.4.1) — that filter is dynamic per equity tier and accomplishes the per-equity-tier filtering automatically. Week 2 verification (§3 Week 2 Tue) is a separate concern: a data-executability check (QC bundled data availability per market) that may flag specific markets as unavailable; it does NOT drive the candidate list.
- **Operator confirmation 2026-05-05:** locked. Per-tier exclusion expectations recorded for Week 2 reference: at $15k equity, Stage 0 likely admits only the bond ETFs + /MCL (~$8k notional) + possibly /M2K/MBT (~$10k each); /MES /MNQ /MYM /MGC excluded at $15k tier. /MES gets the spec's explicit 50%-override at $20k (backend-spec §2.4.1 Stage 2). /MNQ likely needs ≥$72k equity. Cost / scope impact: none.

---

### 2026-05-05 — Day 2 — HURST_THRESHOLD locked at 0.55 (was 0.50 in Day 2 morning skeleton)

- **Spec reference:** `Docs/backend-spec.md §2.3` (HURST_THRESHOLD configurable); `Docs/backend-spec.md §12.3` (`tighten_parameter` enum — agent-mutable, tighten direction is up).
- **Decision:** raised default from 0.50 to **0.55** to compensate for the R/S estimator's small-sample upward bias on the V1 60-bar lookback (~+0.05 typical inflation). 0.55 is what 0.50 buys you on noise after de-biasing — i.e. "moderate persistence" rather than "any positive autocorrelation." Direction is tightening (up = stricter) so it's within the agent-mutable surface; further tightening to 0.60+ does not require a PR. Loosening below 0.50 does require a PR.
- **Operator confirmation 2026-05-05:** locked. Decision made on PR #4 review.
- **Cost / scope impact:** modestly fewer signals expected vs. 0.50 floor; signal acceptance rate should still clear the spec's 90% target (the post-Stage-0/post-Stage-5 denominator) since Hurst is one of three entry filters and the others (Donchian, MA) typically bind first.

---

### 2026-05-05 — Day 2 — DNS propagation verified for spratcapital.com

- **Spec reference:** `implementation-guide.md` §11 Day 2 09:00 ("Verify DNS propagation").
- **Verification (operator laptop, 20:23 ET):**
  - `dig +short spratcapital.com` → `178.156.239.84`
  - `dig +short www.spratcapital.com` → `178.156.239.84`
  - whois on the IP → `Hetzner Online GmbH` (matches expected Ashburn primary owner)
  - Authoritative NS → `brit.ns.cloudflare.com` (matches Cloudflare-managed DNS per operator identifier memory)
  - `curl http://spratcapital.com` → connection timed out on port 80 (no service listening yet; expected — Caddy lands Day 5).
- **Outcome:** DNS is propagated and resolves to the expected Hetzner-owned IP. The implementation-guide's Day 2 verification bar ("DNS working = anything other than NXDOMAIN") is met. Caddy/TLS deploy at Day 5 will start serving on port 443; until then port 80/443 timeouts are expected.

---

### 2026-05-05 — Day 2 — Added `strategies/__init__.py` (was missing)

- **Spec reference:** `Docs/claude-dev-guide.md §2.1` (repo layout — `strategies/` is a top-level package).
- **Decision:** added an empty (with module docstring) `strategies/__init__.py`. Without it, mypy reports "Source file found twice under different module names" because the file is reachable both via the repo root and via the `strategies/` subdirectory. Pure plumbing fix discovered while typechecking the v1 strategy module.
- **Rationale:** standard Python packaging hygiene; should have been in the Day 1 scaffold but wasn't caught because no Python code referenced `strategies.*` until Day 2.
- **Cost / scope impact:** none.

---

### 2026-05-05 — Day 2 — Discord server + bot setup complete

- **Spec reference:** `implementation-guide.md` §11 Day 2 14:00; `Docs/backend-spec.md §8.1.1` (`discord:` secret schema).
- **Status:** complete. Operator ran `deploy/discord/README.md` Steps 1–6 end-to-end.
- **Confirmed (operator-reported, captured in 1Password):**
  - Private guild created.
  - Seven Phase 0–1 channels exist (`#daily-brief`, `#signals`, `#fills`, `#alerts`, `#critical`, `#ops`, `#audit`).
  - Bot application `Trading System Bot` created; OAuth invite executed; bot is a member of the guild.
  - Bot token stored in 1Password as `discord-bot-token` (interim); migrates to `secrets/{paper,live}.enc.yaml discord.bot_token` on Day 3 sops setup.
  - Guild ID + 7 channel IDs captured (operator's notes / 1Password).
  - Privileged Gateway Intents all OFF (Presence, Server Members, Message Content) — minimum surface.
- **Not in this log:** the actual Guild ID and channel IDs are not secrets but are co-located with the bot token in `secrets/{paper,live}.enc.yaml` per the spec schema. They land there on Day 3.

---

### 2026-05-05 — Day 2 — GitHub App creation status

- **Spec reference:** `implementation-guide.md` §11 Day 2 09:30; `deploy/github-app/README.md`.
- **Status:** **operator-confirm pending.** Operator's working assumption is that the runbook was executed but is not certain. Claude Code cannot independently verify because GitHub's `GET /user/installations` endpoint requires a JWT signed by the app's own private key (chicken-and-egg) and there is no operator-token-accessible "list my apps" endpoint.
- **Operator action to close this entry:** visit https://github.com/settings/apps . If `trading-system-pr-review` (or the suffix-variant the operator chose) is listed, capture the App ID and append below. Then visit https://github.com/settings/installations to capture the Installation ID. If the runbook was NOT executed, schedule it (~10 min) — non-blocking for any other Day 2 work.
- **App ID:** `<TODO_FROM_OPERATOR>`
- **Installation ID:** `<TODO_FROM_OPERATOR>`

---

## Day 2 verdict

All Day 2 implementation-guide §11 tasks complete pending one operator confirmation (GitHub App). Both [CLAUDE_CODE] tasks (09:30 GitHub App runbook, 11:00 v1 strategy) shipped via PRs #3 and #4. Both [OPERATOR] tasks (14:00 Discord, 15:00 sops/age) executed by operator on the canonical runbooks in `deploy/`. DNS verification passed.

---

## Open follow-ups (post-Day-2)

### From Day 1 (carried)
- [ ] **Day 4** — Watchdog Python script + systemd timer not yet deployed to Nuremberg.
- [ ] **Day 5** — Caddy / TLS / `/api/health` not yet running on Ashburn.
- [ ] **Optional** — Ashburn root SSH still allowed (B9 hardening; can disable any time).

### New from Day 2
- [ ] **[OPERATOR]** Confirm GitHub App status; append App ID + Installation ID to the entry above. Non-blocking.
- [ ] **Day 3 09:00** — populate `secrets/{dev,paper,live}.enc.yaml` from `deploy/sops/secret_schemas/*.template.yaml`; encrypt with sops; commit. Captured Day 2 values that land here: GitHub App private key (from 1Password), Discord bot token + guild_id + 7 channel_ids (from operator notes / 1Password).
- [ ] **Week 2 Mon** — `services/risk/sizing.py` Stage 0 implementation (forbidden whitelist; `risk-review-approved` label required).
- [ ] **Week 2 Tue** — sub-universe data-executability check (QC bundled data availability per locked candidate); active universe at $15k–$25k tier emerges from Stage 0 dynamically (no manual finalization needed since the candidate pool is already locked).
- [ ] **Week 3–4** — implement `V1TrendFollowing.generate_exit_candidates` (currently scaffolded with `NotImplementedError`).
- [ ] **Week 4** — full LEAN/QC algorithm wiring (`lean/v1_qc_algorithm.py` is heartbeat-only on Day 2).
- [ ] **2027-05-05** — annual rotation: regenerate age keys, `sops updatekeys` all `secrets/*.enc.yaml`, print new papers, destroy old papers.

---

## How specs reference this log

When a spec claim is now wrong or incomplete because of an entry above, the spec gets a one-line annotation pointing here. We do NOT rewrite specs to match reality — specs remain the canonical "what we agreed to build"; this log is the canonical "what we actually did."

Cross-references in current edits:
- `Docs/backend-spec.md` §1.6 → "Falkenstein (locked)" annotated with pointer to the Nuremberg deviation entry above.
- `implementation-guide.md` §2.1, §2.3 → cost table updated in-place to reflect actual prices; original spec ranges retained in this log for archaeology.
