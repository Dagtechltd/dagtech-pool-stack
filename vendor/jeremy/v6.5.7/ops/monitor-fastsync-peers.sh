#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${BDAG_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="${BDAG_POOL_ENV_FILE:-$STACK_DIR/.env}"
STATE_DIR="$STACK_DIR/ops/runtime"
LOG_DIR="$STATE_DIR/logs"
STATE_FILE="$STATE_DIR/fastsync-peer-monitor-state.json"
MARKER_FILE="$STATE_DIR/fastsync-peer-visible"
NODE_CONTAINER="${BDAG_FASTSYNC_MONITOR_NODE:-node}"
RPC_URL="${BDAG_FASTSYNC_MONITOR_RPC_URL:-${BDAG_NODE_RPC_URL:-}}"

mkdir -p "$LOG_DIR"

if ! command -v jq >/dev/null 2>&1; then
  echo "$(date -Is) jq missing; install jq or disable fastsync peer monitor" >>"$LOG_DIR/fastsync-peer-monitor.log"
  exit 0
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "$(date -Is) env file missing: $ENV_FILE" >>"$LOG_DIR/fastsync-peer-monitor.log"
  exit 1
fi

set -a
. "$ENV_FILE"
set +a

if [[ -z "$RPC_URL" ]]; then
  node_ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$NODE_CONTAINER" 2>/dev/null || true)"
  if [[ -n "$node_ip" ]]; then
    RPC_URL="http://${node_ip}:38131/"
  else
    RPC_URL="http://127.0.0.1:38131/"
  fi
fi
RPC_LABEL="${RPC_URL#http://}"
RPC_LABEL="${RPC_LABEL#https://}"
RPC_LABEL="${RPC_LABEL%/}"

rpc_error_file="$(mktemp "$STATE_FILE.rpc.XXXXXX")"
if ! raw_json="$(curl -fsS --max-time 10 \
  --user "$NODE_RPC_USER:$NODE_RPC_PASS" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"1.0","id":"fastsync-peer-monitor","method":"getPeerInfo","params":[]}' \
  "$RPC_URL" 2>"$rpc_error_file")"; then
  rpc_error="$(tr '\n' ' ' <"$rpc_error_file" | sed 's/[[:space:]]*$//')"
  rm -f "$rpc_error_file"
  state_json="$(jq -n --arg now "$(date -Is)" --arg rpc "$RPC_LABEL" --arg error "$rpc_error" '{
    checked_at: $now,
    local_node_rpc: $rpc,
    rpc_available: false,
    fastsync_visible: false,
    upgraded_peers: [],
    highest_peer_mainorder: null,
    connected_peer_count: 0,
    error: $error,
    note: "RPC is not ready for peer inspection yet; this is expected while the node is starting or refusing templates during sync."
  }')"
  tmp="$(mktemp "$STATE_FILE.XXXXXX")"
  printf '%s\n' "$state_json" >"$tmp"
  mv "$tmp" "$STATE_FILE"
  rm -f "$MARKER_FILE"
  echo "$(date -Is) rpc_unavailable error=${rpc_error:-unknown}" >>"$LOG_DIR/fastsync-peer-monitor.log"
  exit 0
fi
rm -f "$rpc_error_file"

state_json="$(printf '%s' "$raw_json" | jq --arg now "$(date -Is)" --arg rpc "$RPC_LABEL" '
  def peer_order:
    (.graphstate.mainorder // .graphstate.best_main_order // .graphstate.order // null);
  def is_upgraded:
    ((.protocol // .protocolversion // .protocolVersion // 0) >= 46)
    or ((.services // "") | tostring | test("FastSync|Unknown"));
  {
    checked_at: $now,
    local_node_rpc: $rpc,
    rpc_available: true,
    fastsync_visible: (([.result[]? | select(is_upgraded)] | length) > 0),
    upgraded_peers: [
      .result[]?
      | select(is_upgraded)
      | {
          id,
          address,
          direction,
          protocol: (.protocol // .protocolversion // .protocolVersion // null),
          services,
          mainorder: peer_order,
          version
        }
    ],
    highest_peer_mainorder: ([.result[]? | peer_order] | map(select(. != null)) | max // null),
    connected_peer_count: ([.result[]?] | length),
    note: "FastSync source selection is P2P-only. Use complete libp2p multiaddrs; do not classify candidates by LAN, VPN, or route type."
  }')"

tmp="$(mktemp "$STATE_FILE.XXXXXX")"
printf '%s\n' "$state_json" >"$tmp"
mv "$tmp" "$STATE_FILE"

summary="$(printf '%s' "$state_json" | jq -r '
  "fastsync_visible=\(.fastsync_visible) upgraded=\(.upgraded_peers|length) highest=\(.highest_peer_mainorder // "n/a")"
')"
echo "$(date -Is) $summary" >>"$LOG_DIR/fastsync-peer-monitor.log"

if printf '%s' "$state_json" | jq -e '.fastsync_visible' >/dev/null; then
  printf '%s\n' "$state_json" >"$MARKER_FILE"
else
  rm -f "$MARKER_FILE"
fi
