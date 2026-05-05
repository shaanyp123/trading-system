# Secrets

This directory holds sops-encrypted YAML files. **Plaintext secrets are blocked by `.gitignore`** and additionally rejected by the `gitleaks` CI gate (anti-pattern A11, dev-guide §11).

## Files

| File | Environment | Status |
|---|---|---|
| `dev.enc.yaml` | Local laptop dev | created Day 3 (Wed Week 1) |
| `paper.enc.yaml` | Hetzner paper VPS | created Day 3 (Wed Week 1) |
| `live.enc.yaml` | Hetzner live VPS | created Day 8 (Wed Week 2); live values added at Week 8 IBKR cutover |

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
