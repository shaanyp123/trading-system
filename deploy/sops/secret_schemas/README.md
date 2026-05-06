# Secret Schema Templates

Per-environment YAML templates showing the canonical secret schema from `backend-spec.md §8.1.1`. **These files are not actually used at runtime** — they're the unencrypted seed shapes that Day 3 sops setup encrypts into `secrets/{dev,paper,live}.enc.yaml`.

| File | Purpose | When values get filled |
| --- | --- | --- |
| `dev.template.yaml` | Local laptop dev — mock or null values throughout | Day 3 09:00 (sops setup) |
| `paper.template.yaml` | Hetzner Ashburn paper VPS — real QC paper account, real Discord, real Resend | Day 3 (initial values); Week 1+ as services come online |
| `live.template.yaml` | Hetzner Ashburn live VPS — real IBKR Pro, real QC live | Week 8 IBKR cutover (per `secrets/README.md`) |

The templates carry placeholder values:
- `<your-domain>` — substituted at deploy via env var (anti-pattern A18 — never bare `<domain>`)
- `<TODO_FROM_DAY_X>` — value lands on the indicated day; null until then
- `MOCK_*` — dev-only mock values; intentionally not real

After Day 3 sops setup, the `secrets/<env>.enc.yaml` files supersede these templates as the source of truth. The templates remain in this directory as documentation of the schema shape.

## Workflow on Day 3

```bash
# Copy each template to the secrets dir (still plaintext at this point):
cp deploy/sops/secret_schemas/dev.template.yaml   secrets/dev.enc.yaml
cp deploy/sops/secret_schemas/paper.template.yaml secrets/paper.enc.yaml
cp deploy/sops/secret_schemas/live.template.yaml  secrets/live.enc.yaml

# Substitute real values for the immediate Day 3 set
# (GitHub App private key, Discord bot token, etc.) using `sops`:
sops secrets/paper.enc.yaml      # opens in $EDITOR; sops re-encrypts on save

# Verify encryption:
grep -c "ENC\[" secrets/paper.enc.yaml      # should be > 0; raw values are gone
```

## Why these aren't in `secrets/` directly

The `secrets/.gitignore` blocks anything other than `*.enc.yaml` in that directory. Day 3 will copy + encrypt; until then plaintext templates with placeholder shapes belong here in `deploy/sops/`.
