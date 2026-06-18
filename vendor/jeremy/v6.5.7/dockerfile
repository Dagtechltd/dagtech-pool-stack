# syntax=docker/dockerfile:1.7
# Pool release build (GITHUB `pool-v*` tarball, build context is repo root).
#
# Expects unpacked layout:
#
#   ./
#   ├── bin/blockdag-node, bin/nodeworker, bin/mining-pool, bin/dashboard-api, bin/dashboard
#   ├── docker/             (e.g. no-snapshot.marker; see SNAPSHOT_PATH)
#   ├── .env.example, docker-compose.yml, …
#
# Snapshot import is runtime-only via a read-only bind mount handled by
# docker/entrypoint-nodeworker.sh. Do not copy .bdsnap files into the image.

# ----------------------------------------------------------------------------
# Common base
# ----------------------------------------------------------------------------
FROM golang:1.26-bookworm AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
    make git gcc g++ libc6-dev jq ca-certificates wget \
 && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# Node Build Stage (blockdag-corechain)
# ----------------------------------------------------------------------------
FROM base AS node-build
WORKDIR /src
COPY bin ./bin
RUN set -eu; mkdir -p /out; \
    test -f ./bin/blockdag-node || { echo 'ERROR: ./bin/blockdag-node missing'; exit 1; }; \
    test -f ./bin/nodeworker     || { echo 'ERROR: ./bin/nodeworker missing'; exit 1; }; \
    cp -f ./bin/blockdag-node /out/blockdag-node && \
    cp -f ./bin/nodeworker    /out/nodeworker && \
    chmod +x /out/blockdag-node /out/nodeworker

# ----------------------------------------------------------------------------
# Pool Build Stage (asic-pool) — binaries from tarball bin/
# ----------------------------------------------------------------------------
FROM base AS pool-build
WORKDIR /src
COPY bin ./bin
RUN set -eu; mkdir -p /out; \
    test -f ./bin/mining-pool    || { echo 'ERROR: ./bin/mining-pool missing'; exit 1; }; \
    cp -f ./bin/mining-pool    /out/mining-pool && \
    chmod +x /out/mining-pool 

# ----------------------------------------------------------------------------
# Collector Source Stage (packaged from BlockdagEngineering/collector)
# ----------------------------------------------------------------------------
FROM alpine:3.20 AS collector-source
COPY --from=collector_src . /src/collector
RUN rm -rf /src/collector/.git /src/collector/.github \
 && find /src/collector -type d -name __pycache__ -prune -exec rm -rf {} +

# ----------------------------------------------------------------------------
# Dashboard Build Stage
# ----------------------------------------------------------------------------
FROM base AS dashboard-build
WORKDIR /src
COPY bin ./bin
RUN set -eu; mkdir -p /out; \
    test -f ./bin/dashboard || { echo 'ERROR: ./bin/dashboard missing'; exit 1; }; \
    cp -f ./bin/dashboard /out/dashboard && \
    chmod +x /out/dashboard

# ----------------------------------------------------------------------------
# Node Runtime Stage (with optional snapshot import)
# ----------------------------------------------------------------------------
FROM ubuntu:24.04 AS node

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r bdagStack && useradd -r -g bdagStack -d /var/lib/bdagStack -m bdagStack
RUN mkdir -p /etc/bdagStack /var/lib/bdagStack/node/mainnet /var/lib/bdagStack/nodeworker /var/log/bdagStack \
 && chown -R bdagStack:bdagStack /var/lib/bdagStack /var/log/bdagStack /etc/bdagStack

COPY --from=node-build /out/blockdag-node  /usr/local/bin/blockdag-node
COPY --from=node-build /out/nodeworker     /usr/local/bin/nodeworker
RUN chmod +x /usr/local/bin/blockdag-node /usr/local/bin/nodeworker

COPY docker/entrypoint-nodeworker.sh /usr/local/bin/docker-entrypoint-nodeworker.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-nodeworker.sh

# Snapshot path is relative to build context (Compose sets this in .env for dev vs release).
COPY ${SNAPSHOT_PATH} /tmp/snapshot-candidate.bdsnap

RUN set -eu; \
    if [ "$(stat -c%s /tmp/snapshot-candidate.bdsnap)" -ge 1024 ]; then \
      echo "Importing local snapshot ($(stat -c%s /tmp/snapshot-candidate.bdsnap) bytes)"; \
      /usr/local/bin/blockdag-node snap import \
        --datadir /var/lib/bdagStack/node/mainnet \
        --path /tmp/snapshot-candidate.bdsnap; \
      cp -f /tmp/snapshot-candidate.bdsnap /var/lib/bdagStack/node/mainnet/snapshot.bdsnap; \
      chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/log/bdagStack; \
      echo "Snapshot import finished and P2P snapshot archive published"; \
    else \
      echo "No snapshot file (marker or tiny file); node will sync from genesis or P2P and cannot serve a P2P snapshot until snapshot.bdsnap exists in its datadir"; \
    fi; \
    rm -f /tmp/snapshot-candidate.bdsnap

WORKDIR /var/lib/bdagStack/node
EXPOSE 8150 38131 38132 18545 18546 6060
# Start as root so entrypoint can chown Docker volumes (often created as uid 0);
# nodeworker and blockdag-node run as bdagStack after that.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint-nodeworker.sh", \
    "/usr/local/bin/nodeworker", \
    "--node-binary=/usr/local/bin/blockdag-node", \
    "--node-args=--configfile /etc/bdagStack/node.conf", \
    "--rpc-url=ws://127.0.0.1:18546", \
    "--dag-rpc-url=http://127.0.0.1:38131", \
    "--persist-root=/var/lib/bdagStack/nodeworker", \
    "--health-min-peers=1", \
    "--rollout-window=30m"]

# ----------------------------------------------------------------------------
# Pool Runtime Stage
# ----------------------------------------------------------------------------
FROM ubuntu:24.04 AS pool
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r bdagStack && useradd -r -g bdagStack -d /var/lib/bdagStack -m bdagStack \
 && mkdir -p /etc/bdagStack /var/lib/bdagStack/pool /var/log/bdagStack \
 && chown -R bdagStack:bdagStack /var/lib/bdagStack /var/log/bdagStack /etc/bdagStack

COPY --from=pool-build /out/mining-pool    /usr/local/bin/mining-pool
RUN chmod +x /usr/local/bin/mining-pool 

# godotenv loads this path at runtime; tarball uses committed .env.example. Copy a
# local `.env` on the host only if you want non-default keys in-file.
COPY .env.example /var/lib/bdagStack/pool/.env
RUN chown bdagStack:bdagStack /var/lib/bdagStack/pool/.env

USER bdagStack
WORKDIR /var/lib/bdagStack/pool
EXPOSE 3334 8080
ENTRYPOINT ["/usr/local/bin/mining-pool"]

# ----------------------------------------------------------------------------
# Shared Ops Runtime Stage (Docker repair helpers)
# ----------------------------------------------------------------------------
FROM docker:27-cli AS ops-runtime

RUN apk add --no-cache \
    bash \
    ca-certificates \
    coreutils \
    curl \
    findutils \
    iproute2 \
    procps \
    py3-pip \
    python3 \
    shadow \
    tzdata

ENV PYTHONUNBUFFERED=1 \
    BDAG_PROJECT_ROOT=/workspace \
    BDAG_RUNTIME_DIR=/workspace/ops/runtime \
    BDAG_POOL_ENV_FILE=/workspace/.env

WORKDIR /workspace

# ----------------------------------------------------------------------------
# Collector Runtime Stage (read-only Python API)
# ----------------------------------------------------------------------------
FROM ops-runtime AS collector

COPY --from=collector-source /src/collector /opt/collector
COPY docker/entrypoint-collector.sh /usr/local/bin/entrypoint-collector.sh
RUN chmod +x /usr/local/bin/entrypoint-collector.sh \
 && mkdir -p /var/lib/bdag-collector/runtime /workspace \
 && if [ -f /opt/collector/requirements.txt ]; then \
      python3 -m pip install --break-system-packages --no-cache-dir -r /opt/collector/requirements.txt; \
    fi

ENV PYTHONUNBUFFERED=1 \
    BDAG_PROJECT_ROOT=/workspace \
    BDAG_RUNTIME_DIR=/var/lib/bdag-collector/runtime \
    BDAG_POOL_ENV_FILE=/workspace/.env \
    BDAG_COLLECTOR_BIND=0.0.0.0 \
    BDAG_COLLECTOR_PORT=9280

WORKDIR /opt/collector
EXPOSE 9280
ENTRYPOINT ["/usr/local/bin/entrypoint-collector.sh"]

# ----------------------------------------------------------------------------
# Watchdog Runtime Stage (containerized repair loop)
# ----------------------------------------------------------------------------
FROM ops-runtime AS watchdog
LABEL org.opencontainers.image.title="BlockDAG Stack Watchdog"

# ----------------------------------------------------------------------------
# Sentinel Runtime Stage (last-resort liveness repair loop)
# ----------------------------------------------------------------------------
FROM ops-runtime AS sentinel
LABEL org.opencontainers.image.title="BlockDAG Stack Sentinel"

# ----------------------------------------------------------------------------
# Dashboard Runtime Stage (Go UI over status API)
# ----------------------------------------------------------------------------
FROM ubuntu:24.04 AS dashboard
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=dashboard-build /out/dashboard /usr/local/bin/dashboard
RUN chmod +x /usr/local/bin/dashboard

ENV ADDR=0.0.0.0:8088 \
    BDAG_COLLECTOR_API=http://collector:9280

EXPOSE 8088
ENTRYPOINT ["/usr/local/bin/dashboard"]
