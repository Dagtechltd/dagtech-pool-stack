#!/usr/bin/env bash
set -euo pipefail

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BDAG_STACK_DEFAULTS_FILE="${BDAG_STACK_DEFAULTS_FILE:-$ROOT/ops/config/stack-defaults.env}"
if [[ -f "$BDAG_STACK_DEFAULTS_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$BDAG_STACK_DEFAULTS_FILE"
  set +a
fi
P2P_PORT="${P2P_PORT:-8150}"
P2P_PROTOCOLS="${BDAG_P2P_PROTOCOLS:-tcp}"

warn() { printf 'WARNING: %s\n' "$*" >&2; }

env_file_value() {
  local key="$1"
  grep -E "^${key}=" "$ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

strip_env_quotes() {
  local value="$1"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

env_value() {
  local key="$1" fallback="${2:-}" value
  value="$(env_file_value "$key")"
  if [[ -z "$value" ]]; then
    value="${!key:-}"
  fi
  value="${value:-$fallback}"
  value="$(strip_env_quotes "$value")"
  printf '%s\n' "$value"
}

ensure_ipfs_segment_identity() {
  if [[ ! -f "$ROOT/.env" || ! -x "$ROOT/ops/ipfs_segment_identity.py" ]]; then
    return 0
  fi
  if ! python3 "$ROOT/ops/ipfs_segment_identity.py" --env-file "$ROOT/.env" --json >/dev/null; then
    warn "Could not provision IPFS segment writer identity. Install python3-cryptography and rerun support-service setup."
    return 1
  fi
}

install_native_reference_rpc() {
  local mode reference_url ssh_target source_rpc_url args=()
  mode="$(env_value BDAG_NATIVE_REFERENCE_RPC_MODE auto)"
  if [[ "$mode" =~ ^(0|false|no|off|disabled)$ ]]; then
    warn "Native reference RPC setup disabled by BDAG_NATIVE_REFERENCE_RPC_MODE=$mode"
    return 0
  fi
  if [[ ! -x "$ROOT/ops/setup_native_reference_rpc.py" ]]; then
    warn "Native reference RPC setup helper is missing under $ROOT/ops"
    return 0
  fi
  reference_url="$(env_value BDAG_NATIVE_REFERENCE_RPC_URL "$(env_value BDAG_CHAIN_REFERENCE_RPC_URL "")")"
  ssh_target="$(env_value BDAG_NATIVE_REFERENCE_RPC_SSH_TARGET "")"
  if [[ -z "$reference_url" && -z "$ssh_target" ]]; then
    return 0
  fi
  source_rpc_url="$(env_value BDAG_CHAIN_SOURCE_RPC_URL "http://127.0.0.1:38131")"
  args=(--env-file "$ROOT/.env" --source-rpc-url "$source_rpc_url")
  if [[ -n "$reference_url" ]]; then
    args+=(--reference-rpc-url "$reference_url")
  fi
  if [[ -n "$ssh_target" ]]; then
    args+=(
      --ssh-target "$ssh_target"
      --remote-rpc-host "$(env_value BDAG_NATIVE_REFERENCE_RPC_REMOTE_HOST 127.0.0.1)"
      --remote-rpc-port "$(env_value BDAG_NATIVE_REFERENCE_RPC_REMOTE_PORT 38131)"
      --local-bind "$(env_value BDAG_NATIVE_REFERENCE_RPC_LOCAL_BIND 127.0.0.1)"
      --local-port "$(env_value BDAG_NATIVE_REFERENCE_RPC_LOCAL_PORT 38141)"
      --key "$(env_value BDAG_NATIVE_REFERENCE_RPC_KEY_PATH "$ROOT/ops/runtime/native-reference-rpc/id_ed25519")"
      --known-hosts "$(env_value BDAG_NATIVE_REFERENCE_RPC_KNOWN_HOSTS "$ROOT/ops/runtime/native-reference-rpc/known_hosts")"
    )
    if [[ "$(env_value BDAG_NATIVE_REFERENCE_RPC_START_TUNNEL 1)" =~ ^(1|true|yes|on)$ ]]; then
      args+=(--start-tunnel)
    fi
  fi
  local strict=0 output
  if [[ "$(env_value BDAG_NATIVE_REFERENCE_RPC_STRICT 0)" =~ ^(1|true|yes|on)$ ]]; then
    strict=1
    args+=(--strict)
  fi
  if ! output="$(python3 "$ROOT/ops/setup_native_reference_rpc.py" "${args[@]}" --json)"; then
    warn "Native reference RPC validation failed; IPFS segment publication will keep deferring until a native reference is configured."
    return 1
  fi
  if ! python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ok") else 1)' <<<"$output"; then
    warn "Native reference RPC validation failed; IPFS segment publication will keep deferring until a native reference is configured."
    [[ "$strict" -eq 1 ]] && return 1
  fi
}

need_sudo() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

retire_legacy_rawdatadir_source_timer() {
  local user_systemd_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local removed=0
  if [[ -f "$user_systemd_dir/bdag-rawdatadir-source.service" || -f "$user_systemd_dir/bdag-rawdatadir-source.timer" ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now bdag-rawdatadir-source.timer bdag-rawdatadir-source.service >/dev/null 2>&1 || true
    fi
    rm -f "$user_systemd_dir/bdag-rawdatadir-source.service" "$user_systemd_dir/bdag-rawdatadir-source.timer"
    removed=1
  fi
  if [[ "$removed" -eq 1 ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    warn "Removed retired bdag-rawdatadir-source systemd unit; IPFS raw checkpoints are published by bdag-ipfs-content-sidecar."
  fi
}

install_firewall() {
  if [[ ! -f "$ROOT/ops/allow-p2p-iptables.sh" || ! -f "$ROOT/ops/systemd/bdag-p2p-firewall.service" ]]; then
    warn "P2P firewall files are missing under $ROOT/ops"
    return 0
  fi
  need_sudo install -m 0755 "$ROOT/ops/allow-p2p-iptables.sh" /usr/local/sbin/bdag-allow-p2p-iptables
  need_sudo install -m 0644 "$ROOT/ops/systemd/bdag-p2p-firewall.service" /etc/systemd/system/bdag-p2p-firewall.service
  printf 'P2P_PORT=%s\nBDAG_P2P_PROTOCOLS=%s\n' "$P2P_PORT" "$P2P_PROTOCOLS" | need_sudo tee /etc/default/bdag-p2p-firewall >/dev/null
  need_sudo systemctl daemon-reload
  need_sudo systemctl enable --now bdag-p2p-firewall.service
}

install_local_peer_timer() {
  if [[ ! -x "$ROOT/ops/update-local-peers.py" || ! -f "$ROOT/ops/systemd/user-bdag-local-peers.timer" ]]; then
    warn "Local peer discovery files are missing under $ROOT/ops"
    return 0
  fi
  local user_systemd_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$user_systemd_dir"
  cat > "$user_systemd_dir/bdag-local-peers.service" <<EOF
[Unit]
Description=BlockDAG local P2P peer discovery
After=default.target docker.service

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Nice=15
IOSchedulingClass=best-effort
IOSchedulingPriority=7
CPUWeight=25
IOWeight=25
ExecStart=$ROOT/ops/update-local-peers.py --apply
EOF
  install -m 0644 "$ROOT/ops/systemd/user-bdag-local-peers.timer" "$user_systemd_dir/bdag-local-peers.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now bdag-local-peers.timer
}

install_rawdatadir_sidecar_timers() {
  local mode
  retire_legacy_rawdatadir_source_timer
  mode="$(env_value BDAG_RAWDATADIR_SIDECAR_MODE auto)"
  if [[ "$mode" =~ ^(0|false|no|off|disabled)$ ]]; then
    warn "Raw datadir sidecar disabled by BDAG_RAWDATADIR_SIDECAR_MODE=$mode"
    return 0
  fi
  if [[ ! -x "$ROOT/ops/maintain-rawdatadir-sidecar.sh" || ! -x "$ROOT/ops/verify-rawdatadir-sidecar.py" ]]; then
    warn "Raw datadir sidecar files are missing under $ROOT/ops"
    return 0
  fi

  local user_systemd_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local user_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
  local network active_service source_dir sidecar_dir artifact_base
  local sidecar_content_base

  network="$(env_value BDAG_RAWDATADIR_NETWORK mainnet)"
  if [[ "${network,,}" != "mainnet" ]]; then
    warn "Raw datadir sidecar refuses non-mainnet BDAG_RAWDATADIR_NETWORK=$network"
    return 1
  fi
  network="mainnet"
  active_service="$(env_value BDAG_NODE_SERVICE node)"
  source_dir="$(env_value BDAG_NODE_DATA_DIR ./data/node)/$network"
  sidecar_dir="$(env_value BDAG_RAWDATADIR_SIDECAR_DIR ./data-restore/btrfs-checkpoints/rawdatadir-sidecar/$network)"
  artifact_base="$(env_value BDAG_RAWDATADIR_ARTIFACT_BASE ./data-restore/btrfs-checkpoints/rawdatadir-artifacts)"
  sidecar_content_base="$(env_value BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE ./data-restore/btrfs-checkpoints/rawdatadir-sidecar-content)"
  ensure_ipfs_segment_identity || return 1

  mkdir -p "$user_systemd_dir" "$user_config_dir"
  cat > "$user_config_dir/bdag-rawdatadir-sidecar.env" <<EOF
# Generated by ops/install-p2p-services.sh. Timers remain lazy/retrying in auto
# mode; services self-defer on mining pressure and unsafe storage/topology.
BDAG_PROJECT_ROOT=$ROOT
BDAG_ENV_FILE=$ROOT/.env
BDAG_COMPOSE_FILE=$ROOT/docker-compose.yml
BDAG_RAWDATADIR_SIDECAR_MODE=$mode
BDAG_RAWDATADIR_NETWORK=$network
BDAG_RAWDATADIR_ACTIVE_SERVICE=$active_service
BDAG_RAWDATADIR_SIDECAR_SOURCE=$source_dir
BDAG_RAWDATADIR_SIDECAR_DIR=$sidecar_dir
BDAG_RAWDATADIR_SIDECAR_SAFE_STATUS=$ROOT/ops/runtime/rawdatadir-sidecar-safe-status.json
BDAG_RAWDATADIR_ARTIFACT_BASE=$artifact_base
BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE=$(env_value BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE auto)
BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE=$sidecar_content_base
BDAG_RAWDATADIR_SIDECAR_CONTENT_KEEP=$(env_value BDAG_RAWDATADIR_SIDECAR_CONTENT_KEEP 2)
BDAG_RAWDATADIR_SIDECAR_CONTENT_CHUNK_SIZE=$(env_value BDAG_RAWDATADIR_SIDECAR_CONTENT_CHUNK_SIZE 67108864)
BDAG_RAWDATADIR_SIDECAR_CONTENT_REQUIRE_SIGNED=$(env_value BDAG_RAWDATADIR_SIDECAR_CONTENT_REQUIRE_SIGNED 1)
BDAG_RAWDATADIR_SIDECAR_RSYNC_BWLIMIT=$(env_value BDAG_RAWDATADIR_SIDECAR_RSYNC_BWLIMIT 4096)
BDAG_RAWDATADIR_SIDECAR_CATCHUP_RSYNC_BWLIMIT=$(env_value BDAG_RAWDATADIR_SIDECAR_CATCHUP_RSYNC_BWLIMIT 1024)
BDAG_RAWDATADIR_SIDECAR_DELAY_UPDATES=$(env_value BDAG_RAWDATADIR_SIDECAR_DELAY_UPDATES 0)
BDAG_RAWDATADIR_REQUIRE_SIGNED=$(env_value BDAG_RAWDATADIR_REQUIRE_SIGNED 1)
BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH=$(env_value BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH 1)
BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG=$(env_value BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG 1000)
BDAG_PUBLIC_RPC_URLS=$(env_value BDAG_PUBLIC_RPC_URLS blockdag-engineering-rpc=https://rpc.blockdag.engineering,bdagscan-rpc=https://rpc.bdagscan.com)
BDAG_RAWDATADIR_FINALIZE=$(env_value BDAG_RAWDATADIR_FINALIZE 0)
BDAG_RAWDATADIR_SIGNING_KEY_ID=$(env_value BDAG_RAWDATADIR_SIGNING_KEY_ID "")
BDAG_RAWDATADIR_SIGNING_KEY_HEX=$(env_value BDAG_RAWDATADIR_SIGNING_KEY_HEX "")
BDAG_RAWDATADIR_SIGNING_KEY_FILE=$(env_value BDAG_RAWDATADIR_SIGNING_KEY_FILE "$ROOT/ops/runtime/ipfs-content/segment-writer.key")
BDAG_RAWDATADIR_TRUSTED_SIGNERS=$(env_value BDAG_RAWDATADIR_TRUSTED_SIGNERS "")
BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER=$(env_value BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER 1)
BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR=$(env_value BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR 1)
BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL=$(env_value BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL https://rpc.blockdag.engineering)
BDAG_RAWDATADIR_CHAIN_ANCHOR_TIMEOUT=$(env_value BDAG_RAWDATADIR_CHAIN_ANCHOR_TIMEOUT 8)
BDAG_RAWDATADIR_CHAIN_ANCHOR_FINALITY_BLOCKS=$(env_value BDAG_RAWDATADIR_CHAIN_ANCHOR_FINALITY_BLOCKS 600)
BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE=$(env_value BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE "$ROOT/ops/runtime/ipfs-content/segment-writer.key")
BDAG_IPFS_SEGMENT_WRITER_ID=$(env_value BDAG_IPFS_SEGMENT_WRITER_ID "")
EOF

  install -m 0644 "$ROOT/ops/systemd/user-bdag-rawdatadir-sidecar.service" "$user_systemd_dir/bdag-rawdatadir-sidecar.service"
  install -m 0644 "$ROOT/ops/systemd/user-bdag-rawdatadir-sidecar.timer" "$user_systemd_dir/bdag-rawdatadir-sidecar.timer"
  install -m 0644 "$ROOT/ops/systemd/user-bdag-rawdatadir-sidecar-verify.service" "$user_systemd_dir/bdag-rawdatadir-sidecar-verify.service"
  install -m 0644 "$ROOT/ops/systemd/user-bdag-rawdatadir-sidecar-verify.timer" "$user_systemd_dir/bdag-rawdatadir-sidecar-verify.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now bdag-rawdatadir-sidecar.timer
  systemctl --user enable --now bdag-rawdatadir-sidecar-verify.timer
}

install_ipfs_content_sidecar_timer() {
  local mode
  mode="$(env_value BDAG_IPFS_CONTENT_SIDECAR_MODE auto)"
  if [[ "$mode" =~ ^(0|false|no|off|disabled)$ ]]; then
    warn "IPFS content sidecar disabled by BDAG_IPFS_CONTENT_SIDECAR_MODE=$mode"
    return 0
  fi
  if [[ ! -x "$ROOT/ops/ipfs_content_sidecar.py" || ! -f "$ROOT/ops/systemd/user-bdag-ipfs-content-sidecar.service" || ! -f "$ROOT/ops/systemd/user-bdag-ipfs-content-sidecar.timer" ]]; then
    warn "IPFS content sidecar files are missing under $ROOT/ops"
    return 0
  fi
  local user_systemd_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local user_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
  mkdir -p "$user_systemd_dir" "$user_config_dir"
  cat > "$user_config_dir/bdag-ipfs-content-sidecar.env" <<EOF
# Generated by ops/install-p2p-services.sh. The sidecar is lazy/retrying:
# pressure defers work, but finalized safe artifacts are not blocked by source
# eligibility snapshots. CIDs remain untrusted transport hints.
BDAG_PROJECT_ROOT=$ROOT
BDAG_ENV_FILE=$ROOT/.env
BDAG_IPFS_CONTENT_SIDECAR_MODE=$mode
BDAG_RAWDATADIR_ARTIFACT_BASE=$(env_value BDAG_RAWDATADIR_ARTIFACT_BASE ./data-restore/btrfs-checkpoints/rawdatadir-artifacts)
BDAG_IPFS_CONTENT_ARTIFACT_DIR=$(env_value BDAG_IPFS_CONTENT_ARTIFACT_DIR "$(env_value BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE ./data-restore/btrfs-checkpoints/rawdatadir-sidecar-content)/current")
BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST=$(env_value BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST "$(env_value BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE ./data-restore/btrfs-checkpoints/rawdatadir-sidecar-content)/current/manifest.json")
BDAG_IPFS_CONTENT_STATUS_FILE=$ROOT/ops/runtime/ipfs-content-sidecar-status.json
BDAG_IPFS_CONTENT_LATEST_INDEX_PATH=$ROOT/ops/runtime/ipfs-content/latest-index.json
BDAG_IPFS_CONTENT_DISCOVERY_FILE=$ROOT/ops/ipfs-content-discovery.json
BDAG_IPFS_CONTENT_LATEST_IPNS=$(env_value BDAG_IPFS_CONTENT_LATEST_IPNS /ipns/k51qzi5uqu5djjlh4vxtmzyswx0qk4s3wdlf3yrpkszp38gq5sl71zcgmmc3jk)
BDAG_IPFS_CONTENT_DEFAULT_INDEX_CID=$(env_value BDAG_IPFS_CONTENT_DEFAULT_INDEX_CID bafkreia7jk2ljqi3raiohugp6nw3633njfp7jmnuvqh47po52et4kupu2a)
BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH=$(env_value BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH "$ROOT/ops/runtime/ipfs-content/rawdatadir-content-index.json")
BDAG_IPFS_RAWDATADIR_CONTENT_DEFAULT_INDEX_CID=$(env_value BDAG_IPFS_RAWDATADIR_CONTENT_DEFAULT_INDEX_CID "")
BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS=$(env_value BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS auto)
BDAG_IPFS_RAWDATADIR_CONTENT_IPNS_KEY=$(env_value BDAG_IPFS_RAWDATADIR_CONTENT_IPNS_KEY "")
BDAG_IPFS_STATE_CHECKPOINT_REQUIRED=$(env_value BDAG_IPFS_STATE_CHECKPOINT_REQUIRED 1)
BDAG_RESTORE_POINT_MAX_AGE_SECONDS=$(env_value BDAG_RESTORE_POINT_MAX_AGE_SECONDS 600)
BDAG_RESTORE_GUARD_IPFS_TIMERS=$(env_value BDAG_RESTORE_GUARD_IPFS_TIMERS "bdag-rawdatadir-sidecar.timer,bdag-rawdatadir-sidecar-verify.timer,bdag-ipfs-content-sidecar.timer")
BDAG_IPFS_CONTENT_DEFAULT_ROOT_CID=$(env_value BDAG_IPFS_CONTENT_DEFAULT_ROOT_CID "")
BDAG_IPFS_CONTENT_ALLOW_UNSIGNED_ARTIFACT=$(env_value BDAG_IPFS_CONTENT_ALLOW_UNSIGNED_ARTIFACT 0)
BDAG_RAWDATADIR_TRUSTED_SIGNERS=$(env_value BDAG_RAWDATADIR_TRUSTED_SIGNERS "")
BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER=$(env_value BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER 1)
BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR=$(env_value BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR 1)
BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL=$(env_value BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL https://rpc.blockdag.engineering)
BDAG_RAWDATADIR_CHAIN_ANCHOR_TIMEOUT=$(env_value BDAG_RAWDATADIR_CHAIN_ANCHOR_TIMEOUT 8)
BDAG_RAWDATADIR_CHAIN_ANCHOR_FINALITY_BLOCKS=$(env_value BDAG_RAWDATADIR_CHAIN_ANCHOR_FINALITY_BLOCKS 600)
BDAG_IPFS_CONTENT_PUBLISH_IPNS=$(env_value BDAG_IPFS_CONTENT_PUBLISH_IPNS auto)
BDAG_IPFS_CONTENT_IPNS_KEY=$(env_value BDAG_IPFS_CONTENT_IPNS_KEY "")
BDAG_IPFS_CONTENT_REPUBLISH_IPNS_WHILE_WAITING=$(env_value BDAG_IPFS_CONTENT_REPUBLISH_IPNS_WHILE_WAITING 1)
BDAG_IPFS_CONTENT_IPNS_TTL=$(env_value BDAG_IPFS_CONTENT_IPNS_TTL 1m)
BDAG_IPFS_CONTENT_IPNS_LIFETIME=$(env_value BDAG_IPFS_CONTENT_IPNS_LIFETIME 8760h)
EOF
  install -m 0644 "$ROOT/ops/systemd/user-bdag-ipfs-content-sidecar.service" "$user_systemd_dir/bdag-ipfs-content-sidecar.service"
  install -m 0644 "$ROOT/ops/systemd/user-bdag-ipfs-content-sidecar.timer" "$user_systemd_dir/bdag-ipfs-content-sidecar.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now bdag-ipfs-content-sidecar.timer
}

install_ipfs_segment_writer_timer() {
  local mode
  mode="$(env_value BDAG_IPFS_SEGMENT_WRITER_MODE auto)"
  if [[ "$mode" =~ ^(0|false|no|off|disabled)$ ]]; then
    warn "IPFS segment writer disabled by BDAG_IPFS_SEGMENT_WRITER_MODE=$mode"
    return 0
  fi
  if [[ ! -x "$ROOT/ops/ipfs_segment_writer.py" || ! -f "$ROOT/ops/systemd/user-bdag-ipfs-segment-writer.service" || ! -f "$ROOT/ops/systemd/user-bdag-ipfs-segment-writer.timer" ]]; then
    warn "IPFS segment writer files are missing under $ROOT/ops"
    return 0
  fi
  ensure_ipfs_segment_identity || return 1
  local user_systemd_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local user_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
  mkdir -p "$user_systemd_dir" "$user_config_dir"
  cat > "$user_config_dir/bdag-ipfs-segment-writer.env" <<EOF
# Generated by ops/install-p2p-services.sh. The writer is lazy/retrying:
# pressure defers work, but the timer keeps trying. IPFS/IPNS are transport
# hints only; consumers must verify segment continuity and chain consensus.
BDAG_PROJECT_ROOT=$ROOT
BDAG_ENV_FILE=$ROOT/.env
BDAG_IPFS_SEGMENT_WRITER_MODE=$mode
BDAG_IPFS_SEGMENT_WRITER_ID=$(env_value BDAG_IPFS_SEGMENT_WRITER_ID "")
BDAG_IPFS_SEGMENT_WRITER_ROSTER=$(env_value BDAG_IPFS_SEGMENT_WRITER_ROSTER "")
BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE=$(env_value BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE rendezvous_sha256_v1)
BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH=$(env_value BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH 0)
BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH=$(env_value BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH 0)
BDAG_IPFS_SEGMENT_START_POLICY=$(env_value BDAG_IPFS_SEGMENT_START_POLICY live_tail)
BDAG_IPFS_SEGMENT_STALE_HEAD_RESET_ENABLED=$(env_value BDAG_IPFS_SEGMENT_STALE_HEAD_RESET_ENABLED 1)
BDAG_IPFS_SEGMENT_STALE_HEAD_MAX_LAG_ORDERS=$(env_value BDAG_IPFS_SEGMENT_STALE_HEAD_MAX_LAG_ORDERS 3600)
BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS=$(env_value BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS 600)
BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT=$(env_value BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT 300)
BDAG_CHAIN_INTEGRITY_MAX_SEGMENT_ORDERS=$(env_value BDAG_CHAIN_INTEGRITY_MAX_SEGMENT_ORDERS "$(env_value BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT 300)")
BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN=$(env_value BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN 1)
BDAG_IPFS_SEGMENT_MAX_RPC_PER_SECOND=$(env_value BDAG_IPFS_SEGMENT_MAX_RPC_PER_SECOND 25)
BDAG_IPFS_SEGMENT_RPC_TIMEOUT=$(env_value BDAG_IPFS_SEGMENT_RPC_TIMEOUT 8)
BDAG_IPFS_SEGMENT_BLOCK_RPC_RETRIES=$(env_value BDAG_IPFS_SEGMENT_BLOCK_RPC_RETRIES 2)
BDAG_CHAIN_REFERENCE_RPC_URL=$(env_value BDAG_CHAIN_REFERENCE_RPC_URL "")
BDAG_IPFS_SEGMENT_REFERENCE_RPC_URL=$(env_value BDAG_IPFS_SEGMENT_REFERENCE_RPC_URL "")
BDAG_PUBLIC_RPC_URLS=$(env_value BDAG_PUBLIC_RPC_URLS "$BDAG_PUBLIC_RPC_URLS")
BDAG_IPFS_SEGMENT_PUBLISH_IPNS=$(env_value BDAG_IPFS_SEGMENT_PUBLISH_IPNS auto)
BDAG_IPFS_SEGMENT_IPNS_KEY=$(env_value BDAG_IPFS_SEGMENT_IPNS_KEY "")
BDAG_IPFS_SEGMENT_RESTORE_DIR=$(env_value BDAG_IPFS_SEGMENT_RESTORE_DIR "$ROOT/ops/runtime/ipfs-segment-restore-drills")
BDAG_IPFS_SEGMENT_IPNS_TTL=$(env_value BDAG_IPFS_SEGMENT_IPNS_TTL 1m)
BDAG_IPFS_SEGMENT_IPNS_LIFETIME=$(env_value BDAG_IPFS_SEGMENT_IPNS_LIFETIME 8760h)
BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE=$(env_value BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE "$ROOT/ops/runtime/ipfs-content/segment-writer.key")
BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS=$(env_value BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS "")
BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES=$(env_value BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES 1)
BDAG_IPFS_SEGMENT_STATUS_FILE=$(env_value BDAG_IPFS_SEGMENT_STATUS_FILE "$ROOT/ops/runtime/ipfs-content/segment-writer-status.json")
BDAG_IPFS_SEGMENT_INDEX_PATH=$(env_value BDAG_IPFS_SEGMENT_INDEX_PATH "$ROOT/ops/runtime/ipfs-content/latest-index.json")
BDAG_IPFS_RESTORE_ACCEPTED_HEAD_ENABLED=$(env_value BDAG_IPFS_RESTORE_ACCEPTED_HEAD_ENABLED 1)
BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE=$(env_value BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE "$ROOT/ops/runtime/ipfs-content/restore-accepted-head.json")
BDAG_IPFS_RESTORE_CHAIN_ANCHOR_ENABLED=$(env_value BDAG_IPFS_RESTORE_CHAIN_ANCHOR_ENABLED 1)
BDAG_IPFS_RESTORE_REQUIRE_CHAIN_ANCHOR=$(env_value BDAG_IPFS_RESTORE_REQUIRE_CHAIN_ANCHOR 0)
BDAG_IPFS_RESTORE_CHAIN_SOURCE_RPC_URL=$(env_value BDAG_IPFS_RESTORE_CHAIN_SOURCE_RPC_URL "")
BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL=$(env_value BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL "")
BDAG_IPFS_RESTORE_CHAIN_ANCHOR_FULL_SPAN_MAX_ORDERS=$(env_value BDAG_IPFS_RESTORE_CHAIN_ANCHOR_FULL_SPAN_MAX_ORDERS 300)
BDAG_IPFS_RESTORE_CHAIN_ANCHOR_SKIP_ENVIRONMENT_GATES=$(env_value BDAG_IPFS_RESTORE_CHAIN_ANCHOR_SKIP_ENVIRONMENT_GATES 1)
BDAG_IPFS_CONTENT_DISCOVERY_FILE=$(env_value BDAG_IPFS_CONTENT_DISCOVERY_FILE "$ROOT/ops/ipfs-content-discovery.json")
BDAG_IPFS_CONTENT_LATEST_IPNS=$(env_value BDAG_IPFS_CONTENT_LATEST_IPNS /ipns/k51qzi5uqu5djjlh4vxtmzyswx0qk4s3wdlf3yrpkszp38gq5sl71zcgmmc3jk)
EOF
  install -m 0644 "$ROOT/ops/systemd/user-bdag-ipfs-segment-writer.service" "$user_systemd_dir/bdag-ipfs-segment-writer.service"
  install -m 0644 "$ROOT/ops/systemd/user-bdag-ipfs-segment-writer.timer" "$user_systemd_dir/bdag-ipfs-segment-writer.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now bdag-ipfs-segment-writer.timer
}

install_mining_host_tuning() {
  if [[ ! -x "$ROOT/ops/apply-mining-host-tuning.sh" || ! -f "$ROOT/ops/systemd/bdag-mining-host-tuning.service" || ! -f "$ROOT/ops/systemd/bdag-mining-host-tuning.timer" ]]; then
    warn "Mining host tuning files are missing under $ROOT/ops"
    return 0
  fi
  need_sudo install -m 0755 "$ROOT/ops/apply-mining-host-tuning.sh" /usr/local/sbin/bdag-apply-mining-host-tuning
  need_sudo install -m 0644 "$ROOT/ops/systemd/bdag-mining-host-tuning.service" /etc/systemd/system/bdag-mining-host-tuning.service
  need_sudo install -m 0644 "$ROOT/ops/systemd/bdag-mining-host-tuning.timer" /etc/systemd/system/bdag-mining-host-tuning.timer
  # The installed script runs from /usr/local/sbin under systemd, so persist
  # the release root explicitly. This lets active/passive tuning read the pool
  # metrics/env and prioritize the currently selected mining-template lane.
  {
    printf 'BDAG_PROJECT_ROOT=%s\n' "$ROOT"
    printf 'BDAG_NODE_MEMORY_LOW=%s\n' "$(env_value BDAG_NODE_MEMORY_LOW 768M)"
    printf 'BDAG_NODE_MEMORY_HIGH=%s\n' "$(env_value BDAG_NODE_MEMORY_HIGH auto)"
    printf 'BDAG_NODE_MEMORY_HIGH_PERCENT=%s\n' "$(env_value BDAG_NODE_MEMORY_HIGH_PERCENT 60)"
    printf 'BDAG_NODE_MEMORY_HIGH_MIN=%s\n' "$(env_value BDAG_NODE_MEMORY_HIGH_MIN 3072M)"
    printf 'BDAG_POOL_MEMORY_LOW=%s\n' "$(env_value BDAG_POOL_MEMORY_LOW 256M)"
    printf 'BDAG_POOL_DB_MEMORY_LOW=%s\n' "$(env_value BDAG_POOL_DB_MEMORY_LOW 512M)"
    printf 'BDAG_DASHBOARD_MEMORY_LOW=%s\n' "$(env_value BDAG_DASHBOARD_MEMORY_LOW 64M)"
  } | need_sudo tee /etc/default/bdag-mining-host-tuning >/dev/null
  need_sudo systemctl daemon-reload
  need_sudo systemctl enable --now bdag-mining-host-tuning.service
  need_sudo systemctl enable --now bdag-mining-host-tuning.timer
}

install_firewall
install_local_peer_timer
install_mining_host_tuning
install_rawdatadir_sidecar_timers
install_ipfs_content_sidecar_timer
install_native_reference_rpc
install_ipfs_segment_writer_timer
