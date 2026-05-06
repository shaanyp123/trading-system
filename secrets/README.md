# Secrets

This directory holds sops-encrypted YAML files. **Plaintext secrets are blocked by `.gitignore`** and additionally rejected by the `gitleaks` CI gate (anti-pattern A11, dev-guide §11).

The canonical schema for each env's secret file is in `deploy/sops/secret_schemas/{dev,paper,live}.template.yaml` — those are the unencrypted shapes the Day 3 09:00 sops setup copies here and encrypts. The setup procedure (age-keygen, `.sops.yaml` wiring, paper-in-safe backup, `SOPS_AGE_KEY_FILE` env var) is in `deploy/sops/README.md`.

## Files

| File | Environment | Status |
|---|---|---|
| `dev.enc.yaml` | Local laptop dev | created Day 3 (Wed Week 1); mock values throughout |
| `paper.enc.yaml` | Hetzner paper VPS | created Day 3 (Wed Week 1); Day 2/3 captured set filled (PR #11); checkpoint-deferred fields land at their respective day checkpoints |
| `live.enc.yaml` | Hetzner live VPS | created Day 3 (Wed Week 1) with placeholder values; live-specific values (account_number, flex_query_token, postgres passwords, bearer tokens, webauthn rp_id/origin) land at Week 8 IBKR cutover |

## Workflow

```bash
# Edit (decrypts to $EDITOR, re-encrypts on save):
sops secrets/paper.enc.yaml

# Decrypt to stdout (for scripting; never pipe to a plaintext file):
sops -d secrets/paper.enc.yaml

# Extract a specific value:
sops -d --extract '["qc"]["api_token"]' secrets/paper.enc.yaml
```

## Recovery

Three age private keys (dev/paper/live) live at `~/.config/sops/age/keys.txt` on the operator's laptop. **A printed copy is in the operator's fireproof safe** (acid-free paper). Loss of all copies = irreversible loss of every encrypted secret in this repo. Treat the safe copy with the same care as a hardware wallet seed phrase.

See `Docs/backend-spec.md §8.1.1` for the canonical secret schema.
