#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_CONTAINER="${BDAG_FASTARTIFACT_SOURCE_CONTAINER:-pool-stack-docker-node-1}"
SOURCE_P2P_PORT="${BDAG_FASTARTIFACT_SOURCE_P2P_PORT:-8150}"
RECEIVER_PROJECT="${BDAG_FASTARTIFACT_RECEIVER_PROJECT:-bdag-v2-receiver}"
SIM_ROOT="${BDAG_FASTARTIFACT_SIM_ROOT:-${TMPDIR:-/tmp}/bdag-fastartifact-receiver}"
REQUESTED_NETWORK="${BDAG_FASTARTIFACT_NETWORK:-mainnet}"
if [[ "${REQUESTED_NETWORK,,}" != "mainnet" ]]; then
  printf 'fastartifact local receiver refuses non-mainnet network: %s\n' "$REQUESTED_NETWORK" >&2
  exit 2
fi
NETWORK="mainnet"
HOST_GW="${BDAG_FASTARTIFACT_HOST_GW:-$(ip -4 addr show docker0 | awk '/inet /{print $2}' | cut -d/ -f1)}"
COMPOSE_FILE="${BDAG_FASTARTIFACT_COMPOSE_FILE:-$ROOT/docker-compose.yml}"
DOCKERFILE="${BDAG_FASTARTIFACT_DOCKERFILE:-dockerfile-dev}"
STACK_SRC_CONTEXT="${BDAG_FASTARTIFACT_STACK_SRC_CONTEXT:-$ROOT}"
BLOCKDAG_CORECHAIN_CONTEXT="${BDAG_FASTARTIFACT_BLOCKDAG_CORECHAIN_CONTEXT:-$ROOT/../blockdag-corechain}"
POOL_SRC_CONTEXT="${BDAG_FASTARTIFACT_POOL_SRC_CONTEXT:-$ROOT/../pool}"
CPU_MINER_SRC_CONTEXT="${BDAG_FASTARTIFACT_CPU_MINER_CONTEXT:-$ROOT/../cpu-miner}"
DASHBOARD_SRC_CONTEXT="${BDAG_FASTARTIFACT_DASHBOARD_CONTEXT:-$ROOT/../dashboard2}"
NODE_RPC_USER="${NODE_RPC_USER:-test}"
NODE_RPC_PASS="${NODE_RPC_PASS:-test}"
TRUSTED_SIGNERS="${BDAG_FASTSNAP_TRUSTED_SIGNERS:-${BDAG_FASTSNAP_TRUSTED_SIGNER:-}}"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=(sudo docker)
fi

compose_receiver() {
  "${DOCKER[@]}" compose -p "$RECEIVER_PROJECT" --env-file "$SIM_ROOT/receiver.env" \
    -f "$COMPOSE_FILE" -f "$SIM_ROOT/receiver.override.yml" "$@"
}

source_peer_id() {
  if [ -n "${BDAG_FASTARTIFACT_SOURCE_PEER_ID:-}" ]; then
    printf '%s\n' "$BDAG_FASTARTIFACT_SOURCE_PEER_ID"
    return 0
  fi
  local id
  id="$("${DOCKER[@]}" logs "$SOURCE_CONTAINER" 2>&1 | sed -n 's/.*peer_id=\([^ ]*\).*/\1/p' | tail -n 1)"
  if [ -n "$id" ]; then
    printf '%s\n' "$id"
    return 0
  fi
  echo "could not discover source peer id; set BDAG_FASTARTIFACT_SOURCE_PEER_ID" >&2
  return 1
}

write_files() {
  local peer_id peer_addr
  peer_id="$(source_peer_id)"
  peer_addr="/ip4/$HOST_GW/tcp/$SOURCE_P2P_PORT/p2p/$peer_id"

  mkdir -p "$SIM_ROOT/data/node" "$SIM_ROOT/data/nodeworker" "$SIM_ROOT/dashboard/logs"
  cat > "$SIM_ROOT/node.conf" <<EOF_CONF
listen=0.0.0.0
port=8150
rpclisten=0.0.0.0:38131
evm.http.addr=0.0.0.0
evm.http.port=18545
evm.ws.addr=0.0.0.0
evm.ws.port=18546
addpeer=$peer_addr
maxpeers=64
rpcuser=$NODE_RPC_USER
rpcpass=$NODE_RPC_PASS
datadir=/var/lib/bdagStack/node
logdir=/var/log/bdagStack
metrics=true
profile=0.0.0.0:6060
modules=Blockdag
modules=miner
evm.http.api=eth,net,web3,txpool,debug
evmenv="--rpc.allow-unprotected-txs --metrics --metrics.addr 0.0.0.0 --metrics.port 6060"
EOF_CONF

  cat > "$SIM_ROOT/receiver.env" <<EOF_ENV
DOCKERFILE=$DOCKERFILE
STACK_SRC_CONTEXT=$STACK_SRC_CONTEXT
BLOCKDAG_CORECHAIN_CONTEXT=$BLOCKDAG_CORECHAIN_CONTEXT
POOL_SRC_CONTEXT=$POOL_SRC_CONTEXT
CPU_MINER_SRC_CONTEXT=$CPU_MINER_SRC_CONTEXT
DASHBOARD_SRC_CONTEXT=$DASHBOARD_SRC_CONTEXT
DOCKER_PLATFORM=linux/amd64
SNAPSHOT_PATH=docker/no-snapshot.marker
NETWORK=$NETWORK
P2P_PORT=28150
BDAG_RPC_PORT=58131
EVM_HTTP_PORT=28545
EVM_WS_PORT=28546
NODE_METRICS_PORT=26060
POOL_BIND_PORT=23334
POOL_API_PORT=29090
DASHBOARD_HOST_PORT=29280
NODE_RPC_USER=$NODE_RPC_USER
NODE_RPC_PASS=$NODE_RPC_PASS
POSTGRES_USER=bdag_pool
POSTGRES_DB=bdagpool
POSTGRES_PASSWORD=local_receiver_only
POOL_BIND_ADDR=0.0.0.0:3334
PPLNS_N_WORK=1000
POOL_FEE_PERCENTAGE=1.0
POOL_BLOCK_MATURITY=10
POOL_PAYOUT_MATURITY=4096
MINING_POOL_ADDRESS=0x0000000000000000000000000000000000000000
BDAG_FASTSNAP_ENABLED=1
BDAG_FASTSNAP_NETWORK=$NETWORK
BDAG_FASTSNAP_PEERS=$peer_addr
BDAG_FASTSNAP_MIN_TIP=${BDAG_FASTSNAP_MIN_TIP:-0}
BDAG_FASTSNAP_TIMEOUT=${BDAG_FASTSNAP_TIMEOUT:-300s}
BDAG_FASTSNAP_PARALLELISM=${BDAG_FASTSNAP_PARALLELISM:-4}
BDAG_FASTSNAP_RETRIES=${BDAG_FASTSNAP_RETRIES:-5}
BDAG_FASTSNAP_RETRY_DELAY=${BDAG_FASTSNAP_RETRY_DELAY:-10s}
BDAG_FASTSNAP_TRUSTED_SIGNERS=$TRUSTED_SIGNERS
BDAG_FASTSNAP_ALLOW_UNSIGNED=${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}
BDAG_FASTSNAP_KEEP_ARCHIVE=${BDAG_FASTSNAP_KEEP_ARCHIVE:-0}
NODE_ARGS_APPEND=
BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID=
BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX=
BDAG_FASTSYNC_ARTIFACT_MANIFEST_TTL=24h
EOF_ENV

  cat > "$SIM_ROOT/receiver.override.yml" <<EOF_YML
services:
  node:
    volumes:
      - $SIM_ROOT/data/node:/var/lib/bdagStack/node
      - $SIM_ROOT/data/nodeworker:/var/lib/bdagStack/nodeworker
      - $SIM_ROOT/node.conf:/etc/bdagStack/node.conf:ro
  dashboard:
    volumes:
      - $SIM_ROOT/dashboard/logs:/app/logs
EOF_YML

  echo "$peer_addr" > "$SIM_ROOT/source-peer.addr"
  echo "receiver configured to sync from $peer_addr"
}

up() {
  write_files
  compose_receiver up -d --build postgres node pool dashboard
}

node_up() {
  write_files
  compose_receiver up -d --build node
}

clean() {
  if [ -f "$SIM_ROOT/receiver.env" ] && [ -f "$SIM_ROOT/receiver.override.yml" ]; then
    compose_receiver down --remove-orphans || true
  fi
}

case "${1:-up}" in
  write-files) write_files ;;
  node-up) node_up ;;
  up) up ;;
  clean) clean ;;
  logs) compose_receiver logs -f "${2:-node}" ;;
  ps) compose_receiver ps ;;
  *)
    echo "usage: $0 [write-files|node-up|up|clean|logs [service]|ps]" >&2
    exit 2
    ;;
esac
