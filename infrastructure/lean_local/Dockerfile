# infrastructure/lean_local/Dockerfile — LEAN Local container image.
#
# Pivot-PR-A (post-pivot 2026-05-12) baseline + 2026-05-20 data-layer
# sub-pivot. Extends quantconnect/lean:latest with:
#   1. tini (PID 1 signal handling)
#   2. python3-yaml (sops bundle parsing at entrypoint)
#   3. structlog (used by the strategy module)
#   4. NEW 2026-05-20: QuantConnect.Brokerages.InteractiveBrokers.dll
#      (+ QuantConnect.IBAutomater.dll) baked in via NuGet during a
#      multi-stage build. The bare quantconnect/lean:latest image does
#      NOT ship these — that gap was the root cause of the 2026-05-12
#      `Sequence contains no matching element` boot crash (PR #120 swapped
#      to PaperBrokerage to dodge it). With the 2026-05-20 data-layer
#      sub-pivot LEAN reads market data from IBKR via the
#      `InteractiveBrokersBrokerage` data-queue-handler + history-provider
#      (delayed quotes 1-3s actual lag + reqHistoricalData), so the DLL
#      is required again. live-mode-brokerage stays PaperBrokerage — the
#      DLL is needed for the DATA path only; the api owns the order path
#      on a separate clientId. See Docs/decisions-log.md 2026-05-20 entry.
#   5. The custom entrypoint that resolves sops bundle → env vars before
#      launching LEAN's runtime
#
# The image's /Lean/Algorithm + /Lean/Strategies volumes are mounted by
# docker-compose (./lean + ./strategies from the host repo); we don't
# COPY the strategy code into the image itself so a `docker compose up
# -d lean_local` after `git pull` picks up new strategy code without an
# image rebuild.
#
# Image size budget: ~2GB (LEAN base ~1.5GB + Python/.NET runtime). The
# builder stage is ~1.5GB on top but is discarded — only the two IBKR
# plugin DLLs (~5MB combined) land in the final image. The operator's
# CCX13 VPS has 8GB RAM + 80GB disk so this fits comfortably alongside
# the existing 7 containers.

# ------------------------------------------------------------------
# Stage 1: pull the IBKR brokerage plugin from NuGet via dotnet SDK.
# ------------------------------------------------------------------
# Why this stage exists: NuGet's package resolver handles the transitive
# dep graph for us. The IBKR plugin v2.5.17699 (released 2026-05-06)
# declares dependencies on Newtonsoft.Json + QuantConnect.Brokerages +
# QuantConnect.IBAutomater + QuantConnect.Lean.Engine. The first three
# of those already ship in `quantconnect/lean:latest`; only
# QuantConnect.IBAutomater.dll is "new" in addition to the plugin DLL
# itself. Using `dotnet publish` against a stub csproj cleanly resolves
# which DLLs are in `lib/<tfm>/` and which are runtime-only — the right
# subset lands in /publish.
#
# Pin IBKR_PLUGIN_VERSION explicitly so a NuGet upstream change (which
# Anthropic / QuantConnect could push at any time) doesn't drift our
# image build. Override via:
#   docker compose build --build-arg IBKR_PLUGIN_VERSION=2.5.X.Y lean_local
ARG IBKR_PLUGIN_VERSION=2.5.17699

FROM mcr.microsoft.com/dotnet/sdk:10.0 AS ib-plugin-builder

# Re-declare ARG inside this stage so it's visible to RUN commands here.
# (ARG declared before the first FROM is global to the file but only
# available in subsequent ARG declarations inside each stage.)
ARG IBKR_PLUGIN_VERSION

WORKDIR /build

# Stub csproj that references the IBKR plugin. `dotnet publish` resolves
# the full transitive dep graph and emits all required DLLs to /publish.
# We don't bother with `dotnet new console -n ...` because the project
# template adds Program.cs scaffold we'd need to delete; a hand-written
# OutputType=Library csproj is the minimum surface.
RUN echo '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework><OutputType>Library</OutputType></PropertyGroup><ItemGroup><PackageReference Include="QuantConnect.Brokerages.InteractiveBrokers" Version="'"${IBKR_PLUGIN_VERSION}"'" /></ItemGroup></Project>' > stub.csproj \
    && dotnet publish stub.csproj -c Release -o /publish \
    && ls -la /publish/QuantConnect.Brokerages.InteractiveBrokers.dll \
    && ls -la /publish/QuantConnect.IBAutomater.dll \
    && ls -la /publish/CSharpAPI.dll \
    && ls -la /publish/IBAutomater.jar

# ------------------------------------------------------------------
# Stage 2: final image based on quantconnect/lean:latest.
# ------------------------------------------------------------------
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

# 2026-05-20 data-layer sub-pivot: bake the IBKR brokerage plugin + its
# runtime deps into LEAN's launcher dir so live-mode boot can resolve the
# `InteractiveBrokersBrokerage` data-queue-handler + history-provider
# class names from lean.json's `paper-internal` env. Without ALL of
# these files, LEAN's Composer broker-factory lookup throws either
# `Sequence contains no matching element` (pre-2026-05-12 PR #120 fingerprint)
# OR `FileNotFoundException: Could not load file or assembly 'CSharpAPI'`
# (the 2026-05-20 boot-attempt-1 fingerprint discovered in this PR).
#
# We copy only the IBKR-specific files because the OTHER transitive deps
# (QuantConnect.Brokerages.dll, QuantConnect.Lean.Engine.dll,
# Newtonsoft.Json.dll, Microsoft.*, System.*) already ship in
# /Lean/Launcher/bin/Debug/ and shadowing those with NuGet-pulled versions
# risks subtle ABI skew.
#
# **2026-05-20 boot-attempt-1 incident:** initial Dockerfile only copied the
# two QuantConnect-named DLLs and missed the 5 IBKR-side runtime files
# below. LEAN's `Composer.LoadPartsSafely(.../InteractiveBrokers.dll)`
# raised `FileNotFoundException: Could not load file or assembly 'CSharpAPI,
# Version=1.0.0.0, ...'` and the broker-factory lookup for the data-queue-
# handler failed with `Unable to locate any exports matching the requested
# typeName: InteractiveBrokersBrokerage`. The `dotnet publish` step in the
# builder stage emits all 7 files to /publish/ (verified via direct probe);
# they just weren't being copied across the multi-stage boundary.
#
# Files copied:
#   1. QuantConnect.Brokerages.InteractiveBrokers.dll — the plugin (LEAN's
#      Composer-discovered data-queue-handler entry point)
#   2. QuantConnect.IBAutomater.dll — QC's .NET wrapper around IBC
#      (IB Controller). Idle in the 2026-05-20 use case (LEAN connects to
#      the existing ib_gateway sidecar; doesn't launch its own gateway)
#      but a hard dep of the brokerage plugin's ctor
#   3. CSharpAPI.dll — IBKR's official C# API client (~363KB). The
#      load-bearing file the brokerage plugin's
#      `<Reference Include="CSharpAPI" HintPath="CSharpAPI.dll" />` resolves to
#   4. CSharpAPI.deps.json — runtime-deps manifest paired with CSharpAPI.dll
#   5. IBAutomater.jar — Java agent IBC runs to drive the gateway GUI;
#      bundled in the QC NuGet plugin even though the LEAN data path
#      doesn't invoke it
#   6. IBAutomater.sh — shell launcher for the .jar (idle-but-required)
#   7. ICSharpCode.SharpZipLib.dll — zip library used by IBAutomater
COPY --from=ib-plugin-builder /publish/QuantConnect.Brokerages.InteractiveBrokers.dll /Lean/Launcher/bin/Debug/
COPY --from=ib-plugin-builder /publish/QuantConnect.IBAutomater.dll /Lean/Launcher/bin/Debug/
COPY --from=ib-plugin-builder /publish/CSharpAPI.dll /Lean/Launcher/bin/Debug/
COPY --from=ib-plugin-builder /publish/CSharpAPI.deps.json /Lean/Launcher/bin/Debug/
COPY --from=ib-plugin-builder /publish/IBAutomater.jar /Lean/Launcher/bin/Debug/
COPY --from=ib-plugin-builder /publish/IBAutomater.sh /Lean/Launcher/bin/Debug/
COPY --from=ib-plugin-builder /publish/ICSharpCode.SharpZipLib.dll /Lean/Launcher/bin/Debug/

# Entrypoint script: resolves /run/secrets/decrypted.yaml → env vars then
# execs LEAN's standard runtime. The entrypoint script comes from this
# directory.
COPY infrastructure/lean_local/entrypoint.sh /usr/local/bin/lean_local_entrypoint.sh
RUN chmod +x /usr/local/bin/lean_local_entrypoint.sh

# Working directory inside the LEAN container. The mount points
# (/Lean/Algorithm + /Lean/Strategies + /Lean/Data) are set up by
# docker-compose at start time; LEAN's runtime resolves the algorithm
# class via lean.json. (/Lean/Data is now a deprecated mount post-2026-05-20
# data-layer sub-pivot — LEAN no longer reads on-disk seed files — but
# the mount stays in docker-compose.yml for rollback safety.)
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
