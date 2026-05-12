# infrastructure/lean_local/Dockerfile — LEAN Local container image.
#
# Pivot-PR-A (post-pivot 2026-05-12). Extends quantconnect/lean:latest
# with:
#   1. PyYAML (for sops bundle parsing at entrypoint)
#   2. tini (PID 1 signal handling)
#   3. A custom entrypoint that resolves sops bundle → env vars before
#      launching LEAN's runtime
#
# The image's /Lean/Algorithm + /Lean/Strategies volumes are mounted by
# docker-compose (./lean + ./strategies from the host repo); we don't
# COPY the strategy code into the image itself so a `docker compose up
# -d lean_local` after `git pull` picks up new strategy code without an
# image rebuild.
#
# Image size budget: ~2GB (LEAN base ~1.5GB + Python/.NET runtime). The
# operator's CCX13 VPS has 8GB RAM + 80GB disk so this fits comfortably
# alongside the existing 7 containers.

FROM quantconnect/lean:latest

USER root

# Install tini + pyyaml. apk-based image (Alpine? actually it's Ubuntu;
# quantconnect/lean uses Ubuntu 22.04 LTS). apt is the package manager.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini python3-yaml \
    && rm -rf /var/lib/apt/lists/*

# Entrypoint script: resolves /run/secrets/decrypted.yaml → env vars then
# execs LEAN's standard runtime. The entrypoint script comes from this
# directory.
COPY infrastructure/lean_local/entrypoint.sh /usr/local/bin/lean_local_entrypoint.sh
RUN chmod +x /usr/local/bin/lean_local_entrypoint.sh

# Working directory inside the LEAN container. The mount points
# (/Lean/Algorithm + /Lean/Strategies + /Lean/Data) are set up by
# docker-compose at start time; LEAN's runtime resolves the algorithm
# class via lean.json.
WORKDIR /Lean

# tini is PID 1 so SIGTERM from `docker compose stop lean_local` is
# forwarded to the LEAN process cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/lean_local_entrypoint.sh"]

# Default command: run LEAN's standard runtime against the rendered
# lean.json. The entrypoint script handles config rendering before
# execing this command via `exec "$@"`.
CMD ["dotnet", "/Lean/QuantConnect.Lean.Launcher.dll"]
