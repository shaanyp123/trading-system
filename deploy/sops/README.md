# Sops + Age — Operator Runbook

Operator guide for generating the three age key pairs (dev / paper / live), wiring them into `.sops.yaml`, populating the per-environment encrypted secret files, and printing the recovery papers for the safe.

This runbook is the **Day 2 15:00** task in `implementation-guide.md` §11. Once complete, **Day 3 09:00** sops initialization (encrypting the GitHub App private key + Discord bot token + initial schema) becomes a 15-minute follow-on rather than a fresh setup.

The threat model and recovery model are locked in `Docs/backend-spec.md §8.1`. This runbook is the procedure; do not deviate without an entry in `Docs/decisions-log.md` documenting the deviation.

---

## What you're producing

| Artifact | Where it lives | Sensitivity |
| --- | --- | --- |
| Three age private keys | `~/.config/sops/age/keys.txt` (one per line) | **Critical** — paper-in-safe backup is the only recovery |
| Three age public keys | `.sops.yaml` (committed) | Public — safe to share |
| Three printed paper copies | Fireproof safe (acid-free archival paper) | **Critical** — physical recovery |
| Populated `.sops.yaml` | Repo root | Public |
| Three encrypted env files | `secrets/{dev,paper,live}.enc.yaml` | Encrypted at rest; safe to commit |

The `~/.config/sops/age/keys.txt` file at `0600` perms is the only digital location of the private keys. The encrypted env files cannot be decrypted without it; the printed paper copies are the disaster-recovery source.

---

## Prerequisites

```bash
# macOS via Homebrew:
brew install age sops

# Verify:
age --version    # >= 1.1.1
sops --version   # >= 3.9.0
```

If you already have `age` and `sops` installed, skip to Step 1.

---

## Step 1 — Generate the three age keys

```bash
cd ~                            # generate at home, NOT inside the repo
age-keygen -o dev-key.txt
age-keygen -o paper-key.txt
age-keygen -o live-key.txt
```

Each command produces a file like:

```
# created: 2026-05-05T15:32:18-04:00
# public key: age1abc...                     # <-- this is the recipient (safe to share)
AGE-SECRET-KEY-1XYZ...                       # <-- this is the private key (NEVER share)
```

**At this point the private keys exist as plaintext on your laptop.** Steps 2 and 3 are about getting them out of those files and into their final homes.

---

## Step 2 — Wire public keys into `.sops.yaml`

The repo ships `.sops.yaml` with placeholder strings. Substitute your three public keys via the helper script:

```bash
cd <repo-root>

DEV_PK=$(awk '/^# public key:/ {print $4}' ~/dev-key.txt)
PAPER_PK=$(awk '/^# public key:/ {print $4}' ~/paper-key.txt)
LIVE_PK=$(awk '/^# public key:/ {print $4}' ~/live-key.txt)

bash scripts/sops_init.sh "$DEV_PK" "$PAPER_PK" "$LIVE_PK"
```

The script:
- Validates each input looks like an `age1...` recipient
- Substitutes the three placeholders in `.sops.yaml`
- Prints the diff so you can verify
- Exits non-zero if anything looks wrong

**Verify:**
```bash
grep -c "^    age:" .sops.yaml      # should print 3
grep "PLACEHOLDER" .sops.yaml       # should print nothing
```

Commit the populated `.sops.yaml` on a feature branch — public keys are not secrets and belong in version control. (The Day 3 PR that does the actual encryption work also merges this `.sops.yaml` change; both can ride the same PR.)

---

## Step 3 — Install private keys + point sops at them

All three private keys go in one file at the conventional path. **You must also export `SOPS_AGE_KEY_FILE` in your shell rc** — sops 3.12+ does not auto-resolve `~/.config/sops/age/keys.txt` on macOS (see `Docs/decisions-log.md` 2026-05-05 Day 2 entry).

### 3a. Install the keys file

```bash
mkdir -p ~/.config/sops/age
chmod 700 ~/.config/sops/age

# Concatenate the secret-key lines from each generated file.
# Strip the comment lines; keep only the AGE-SECRET-KEY-* lines.
{
  grep '^AGE-SECRET-KEY-' ~/dev-key.txt
  grep '^AGE-SECRET-KEY-' ~/paper-key.txt
  grep '^AGE-SECRET-KEY-' ~/live-key.txt
} > ~/.config/sops/age/keys.txt

chmod 600 ~/.config/sops/age/keys.txt

# Verify: should show three AGE-SECRET-KEY-* lines.
wc -l ~/.config/sops/age/keys.txt
```

### 3b. Point sops at the file via shell rc

```bash
# zsh (macOS default):
echo 'export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"' >> ~/.zshrc
source ~/.zshrc

# bash equivalent:
# echo 'export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"' >> ~/.bashrc
# source ~/.bashrc

# Verify the env var is set and the keys derive correctly:
echo "$SOPS_AGE_KEY_FILE"                       # absolute path to keys.txt
age-keygen -y < "$SOPS_AGE_KEY_FILE"            # three age1... public keys
```

The three public keys from the second verify command must match the three recipients in `.sops.yaml`. Cross-check with:

```bash
diff <(age-keygen -y < "$SOPS_AGE_KEY_FILE" | sort) \
     <(grep -E "^    age: age1" .sops.yaml | awk '{print $2}' | sort)
# (no output = match)
```

If they match, sops can decrypt anything encrypted under `.sops.yaml`'s rules. sops decrypts by trying every key in `keys.txt` against each file's recipient list — so having all three live in the same file is fine and necessary (you'll edit `live.enc.yaml` from the same laptop that runs `sops dev.enc.yaml`).

---

## Step 4 — Print the paper recovery copies

**This step is non-negotiable per `Docs/backend-spec.md §8.1.2`.** Loss of the laptop's `keys.txt` without paper backup = unrecoverable encrypted secrets.

For each of the three keys (dev, paper, live):

1. Open `~/<env>-key.txt` in a text editor that prints with comments visible (TextEdit on macOS, etc.).
2. Print **on acid-free archival paper** (100-year rated). Standard inkjet/laser is fine; the paper is what matters.
3. Title each page clearly: `trading-system / sops / <env> private key — generated 2026-05-05`.
4. Verify the printout includes:
   - The `# public key: age1...` comment line (so future-you can identify which key without decrypting)
   - The `AGE-SECRET-KEY-1...` line
5. Place all three pages in the fireproof safe. Recommended: a small archival sleeve, separated from the WebAuthn backup-codes paper (different failure modes; don't co-locate).

**Annual rotation reminder:** add a calendar event for one year out — "rotate sops age keys per backend-spec §8.1.2." Procedure: generate new keys → re-encrypt all `secrets/*.enc.yaml` with `sops updatekeys` → print new papers → destroy old papers.

---

## Step 5 — Delete the plaintext source files

After Steps 3 and 4 complete (and only after — once these are gone they cannot be recovered without the paper):

```bash
# Verify backups are in place first.
ls -l ~/.config/sops/age/keys.txt              # exists, 0600 perms
wc -l ~/.config/sops/age/keys.txt              # = 3

# Then destroy the plaintext source.
rm ~/dev-key.txt ~/paper-key.txt ~/live-key.txt

# Confirm gone.
ls ~/dev-key.txt ~/paper-key.txt ~/live-key.txt 2>&1 | grep -c "No such file"
# should print 3
```

`~/.config/sops/age/keys.txt` is the only digital home for the private keys after this step. The paper copies in the safe are the disaster-recovery source.

---

## Step 6 — Smoke test

```bash
cd <repo-root>

# Encrypt a dummy file scoped to dev:
mkdir -p /tmp/sops-smoke
echo 'foo: bar' > secrets/dev.enc.yaml
sops -e -i secrets/dev.enc.yaml

# Inspect — should show ENC[...] ciphertext blocks:
cat secrets/dev.enc.yaml

# Decrypt — should print 'foo: bar':
sops -d secrets/dev.enc.yaml

# Clean up the smoke test:
rm secrets/dev.enc.yaml
```

If both encrypt and decrypt round-trip, sops is wired correctly. Day 3 builds on top of this with the real schema.

---

## What Day 3 will do (preview)

Day 3 09:00 sops setup picks up where this runbook leaves off:

1. Author the canonical secret schema templates (the unencrypted shapes referenced in `backend-spec.md §8.1.1`).
2. Encrypt initial values: GitHub App private key (from PR #3 runbook), Discord bot token (from PR #6 runbook).
3. Commit the three `secrets/{dev,paper,live}.enc.yaml` files in encrypted form.
4. Add CI step to verify each env file decrypts in a controlled context.

The schema templates already in `deploy/sops/secret_schemas/` (this PR) document the shape; Day 3 fills the values.

---

## Troubleshooting

**`sops -d` fails with "identity did not match any of the recipients" + "Did not find keys in locations 'SOPS_AGE_SSH_PRIVATE_KEY_FILE'..."** — sops 3.12+ on macOS does not auto-resolve `~/.config/sops/age/keys.txt`. Confirm `SOPS_AGE_KEY_FILE` is set in your current shell:

```bash
echo "$SOPS_AGE_KEY_FILE"
```

If empty, follow Step 3b — add the export to your shell rc and `source` it (or open a new terminal). This is the most common cause of decrypt failure on a fresh laptop.

**`sops: failed to decrypt`** (other variants) — `~/.config/sops/age/keys.txt` is missing one of the three private keys, or the `.sops.yaml` `age:` recipient doesn't match what's in `keys.txt`. Verify:

```bash
# Public keys in .sops.yaml:
grep -E "    age: age1" .sops.yaml | awk '{print $2}'

# Public keys derivable from your private keys:
age-keygen -y < ~/.config/sops/age/keys.txt
```

The two lists must match.

**`age-keygen: command not found`** — `brew install age` (macOS) or check your system's package manager.

**`.sops.yaml` was never substituted (still has placeholders)** — you can re-run `scripts/sops_init.sh` any time before files are encrypted. Once a file IS encrypted to a placeholder string, sops will fail; you'd need to decrypt with the original real recipient and re-encrypt with `sops updatekeys`.
