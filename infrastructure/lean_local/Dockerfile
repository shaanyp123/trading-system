# infrastructure/lean_local/Dockerfile — LEAN Local container image.
#
# Pivot-PR-A (post-pivot 2026-05-12) baseline. Extends quantconnect/lean:latest
# with:
#   1. tini (PID 1 signal handling)
#   2. python3-yaml (sops bundle parsing at entrypoint)
#   3. structlog (used by the strategy module)
#   4. The custom entrypoint that resolves sops bundle → env vars before
#      launching LEAN's runtime
#
# The image's /Lean/Algorithm + /Lean/Strategies volumes are mounted by
# docker-compose (./lean + ./strategies from the host repo); we don't
# COPY the strategy code into the image itself so a `docker compose up
# -d lean_local` after `git pull` picks up new strategy code without an
# image rebuild.
#
# DATA-LAYER PIVOT v2 (Option C; 2026-05-20 evening): the v1 attempt's
# multi-stage NuGet pull of `QuantConnect.Brokerages.InteractiveBrokers`
# DLLs is REVERTED. The IBKR plugin's IBAutomater design tries to
# launch its own gateway process (conflicts with the api's clientId=1
# ownership of the ib_gateway sidecar — see the 2026-05-20 evening
# decisions-log entry for the postmortem). Bar data is now api-managed
# (services/data/bar_sync.py reads from IBKR on clientId=2 and writes
# to the shared lean_data Docker volume); LEAN reads on-disk via
# FakeDataQueue + SubscriptionDataReaderHistoryProvider, no plugin DLLs
# needed.

FROM quantconnect/lean:latest

USER root

# Install tini + pyyaml. apt-based image (LEAN ships on Ubuntu 22.04 LTS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini python3-yaml \
    && rm -rf /var/lib/apt/lists/*

# Install structlog into the LEAN container's Python (miniconda-based;
# /opt/miniconda3/bin/python3) so the strategy module
# (`strategies/v1_trend_following/strategy.py`) can use it for the
# `v1_indicator_compute_failed` log line. Pivot-PR-D 2026-05-12: the
# strategy module imports structlog at top level; without it, LEAN's
# `AlgorithmPythonWrapper` fails to load v1_strategy.py with
# `No module named 'structlog'`.
RUN /opt/miniconda3/bin/python3 -m pip install --no-cache-dir "structlog>=24.4"

# Entrypoint script: resolves /run/secrets/decrypted.yaml → env vars then
# execs LEAN's standard runtime. The entrypoint script comes from this
# directory.
COPY infrastructure/lean_local/entrypoint.sh /usr/local/bin/lean_local_entrypoint.sh
RUN chmod +x /usr/local/bin/lean_local_entrypoint.sh

# Working directory inside the LEAN container. The mount points
# (/Lean/Algorithm + /Lean/Strategies + /Lean/Data) are set up by
# docker-compose at start time; LEAN's runtime resolves the algorithm
# class via lean.json. /Lean/Data is the shared lean_data volume the
# api's BarSyncWorker writes to (post data-layer-pivot-v2 2026-05-20).
WORKDIR /Lean

# tini is PID 1 so SIGTERM from `docker compose stop lean_local` is
# forwarded to the LEAN process cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/lean_local_entrypoint.sh"]

# Default command: run LEAN's standard runtime against the rendered
# lean.json. The entrypoint script handles config rendering before
# execing this command via `exec "$@"`. The launcher .dll lives under
# /Lean/Launcher/bin/Debug/ in the quantconnect/lean:latest image (Apr
# 2026 build) — /Lean/QuantConnect.Lean.Launcher.dll does not exist
# at the WORKDIR level.
CMD ["dotnet", "/Lean/Launcher/bin/Debug/QuantConnect.Lean.Launcher.dll"]
