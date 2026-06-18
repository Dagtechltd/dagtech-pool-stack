#!/usr/bin/env bash
# Fix ownership of persisted paths on every container start. Docker volumes are
# often populated as root, which prevents bdagStack from opening chain data.
set -euo pipefail

timestamp_iso() {
  date -Is 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z'
}

log() {
  printf '[%s] node-entrypoint: %s\n' "$(timestamp_iso)" "$*" >&2
}

lower_ascii() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

FASTSNAP_BOOTSTRAP_MUTATED=0

mainnet_only_network() {
  local requested="${1:-mainnet}"
  if [ -z "$requested" ]; then
    requested="mainnet"
  fi
  case "$(lower_ascii "$requested")" in
    mainnet)
      printf 'mainnet\n'
      ;;
    *)
      log "refusing non-mainnet FastSnap network: $requested"
      exit 2
      ;;
  esac
}

ensure_owned_runtime_dirs() {
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  chown bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
}

fix_ownership_if_needed() {
  local mode="${BDAG_ENTRYPOINT_CHOWN_MODE:-needed}"
  local uid gid path mismatched
  case "$mode" in
    never|off|0|false)
      log "recursive ownership repair disabled by BDAG_ENTRYPOINT_CHOWN_MODE=$mode"
      return 0
      ;;
  esac

  uid="$(id -u bdagStack)"
  gid="$(id -g bdagStack)"
  for path in /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack; do
    [ -e "$path" ] || continue
    mismatched=""
    if [ "$(stat -c '%u:%g' "$path" 2>/dev/null || printf '')" != "$uid:$gid" ]; then
      mismatched="$path"
    elif [ "$mode" = "always" ]; then
      mismatched="$path"
    else
      mismatched="$(find "$path" \( ! -uid "$uid" -o ! -gid "$gid" \) -print -quit 2>/dev/null || true)"
    fi
    [ -n "$mismatched" ] || continue
    log "repairing ownership below $path due to ${mismatched#$path/}"
    chown -R bdagStack:bdagStack "$path" || true
  done
}

nodeworker_arg_present() {
  local key="$1"
  shift
  local arg
  for arg in "$@"; do
    case "$arg" in
      --"$key"|--"$key"=*)
        return 0
        ;;
    esac
  done
  return 1
}

node_arg_value() {
  local key="$1"
  local node_args="$2"
  local next=0
  local word
  for word in $node_args; do
    if [ "$next" = "1" ]; then
      printf '%s\n' "$word"
      return 0
    fi
    case "$word" in
      --"$key"=*)
        printf '%s\n' "${word#*=}"
        return 0
        ;;
      --"$key")
        next=1
        ;;
    esac
  done
  return 1
}

read_config_value() {
  local config_file="$1"
  local key="$2"
  [ -f "$config_file" ] || return 1
  awk -F= -v key="$key" '
    $1 == key {
      value = $0
      sub("^[^=]*=", "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      print value
      exit
    }
  ' "$config_file"
}

network_datadir() {
  local data_parent="$1"
  local network="$2"
  case "$data_parent" in
    */"$network") printf '%s\n' "$data_parent" ;;
    *) printf '%s/%s\n' "$data_parent" "$network" ;;
  esac
}

node_args_from_argv() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --node-args=*)
        printf '%s\n' "${arg#*=}"
        return 0
        ;;
    esac
  done
  return 1
}

addpeer_values() {
  local node_args="$1"
  local word
  for word in $node_args; do
    case "$word" in
      --addpeer=*)
        printf '%s\n' "${word#*=}"
        ;;
    esac
  done
}

config_addpeer_values() {
  local config_file="$1"
  [ -f "$config_file" ] || return 0
  awk -F= '
    $1 ~ /^[[:space:]]*addpeer[[:space:]]*$/ {
      value = $0
      sub("^[^=]*=", "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value != "") print value
    }
  ' "$config_file"
}

ORDERED_FASTSYNC_SEEN=
append_unique_peer() {
  local bucket_name="$1"
  local peer="$2"
  local -n bucket="$bucket_name"

  [ -n "$peer" ] || return 0
  case "$peer" in
    none|null) return 0 ;;
  esac
  case "$ORDERED_FASTSYNC_SEEN" in
    *"|$peer|"*) return 0 ;;
  esac
  bucket+=("$peer")
  ORDERED_FASTSYNC_SEEN="${ORDERED_FASTSYNC_SEEN}|$peer|"
}

append_peer_list() {
  local bucket_name="$1"
  local raw="$2"
  local old_ifs="$IFS"
  local peer
  IFS=', '
  for peer in $raw; do
    peer_allowed_for_p2p "$peer" || continue
    append_unique_peer "$bucket_name" "$peer"
  done
  IFS="$old_ifs"
}

peer_allowed_for_p2p() {
  local peer="$1"
  case "$peer" in
    */p2p/*) return 0 ;;
  esac
  return 1
}

join_peer_array() {
  local old_ifs="$IFS"
  local joined
  IFS=,
  joined="${fastsync_peers[*]:-}"
  IFS="$old_ifs"
  printf '%s\n' "$joined"
}

ordered_fastsync_peers() {
  local node_args="$1"
  local ordering="${BDAG_FASTSYNC_PEER_ORDERING:-p2p-latency}"
  local config_file config_peers generic_peers
  fastsync_peers=()
  ORDERED_FASTSYNC_SEEN=

  config_file="$(node_arg_value configfile "$node_args" || true)"
  config_file="${config_file:-/etc/bdagStack/node.conf}"
  config_peers="$(config_addpeer_values "$config_file" | paste -sd, - || true)"

  case "$ordering" in
    p2p-latency|p2p|latency|flat-latency|flat|tiered-latency|legacy-buckets|buckets) ;;
    *) log "unknown BDAG_FASTSYNC_PEER_ORDERING=$ordering; using p2p-latency" ;;
  esac
  generic_peers="${BDAG_FASTSYNC_PEERS:-} ${BDAG_FASTSNAP_PEERS:-} ${BOOTSTRAP_PEER_ADDRESSES:-} $config_peers $(addpeer_values "$node_args" | paste -sd, - || true)"
  append_peer_list fastsync_peers "$generic_peers"

  join_peer_array
}

addpeer_args_from_csv() {
  local csv="$1"
  local old_ifs="$IFS"
  local peer
  IFS=,
  for peer in $csv; do
    [ -n "$peer" ] && printf ' --addpeer=%s' "$peer"
  done
  IFS="$old_ifs"
}

apply_ordered_fastsync_peers() {
  case "${BDAG_FASTSYNC_PEER_ORDERING:-p2p-latency}" in
    0|off|false|none) return 0 ;;
  esac

  local node_args ordered addpeer_args total_count ordering
  ordering="${BDAG_FASTSYNC_PEER_ORDERING:-p2p-latency}"
  node_args="$(node_args_from_argv "$@" || true)"
  ordered="$(ordered_fastsync_peers "$node_args")"
  [ -n "$ordered" ] || return 0

  export BDAG_FASTSNAP_PEERS="$ordered"
  total_count="$(printf '%s' "$ordered" | awk -F, '{print NF}')"
  log "P2P latency/usefulness FastSync candidates enabled; libp2p selects the fastest useful artifact source; total=${total_count}"

  if [ "${BDAG_FASTSYNC_APPEND_ADDPEERS:-1}" = "1" ]; then
    addpeer_args="$(addpeer_args_from_csv "$ordered")"
    NODE_ARGS_APPEND="${addpeer_args}${NODE_ARGS_APPEND:+ $NODE_ARGS_APPEND}"
    export NODE_ARGS_APPEND
  fi
}

node_args_contains_word() {
  local node_args="$1"
  local needle="$2"
  local word
  for word in $node_args; do
    [ "$word" = "$needle" ] && return 0
  done
  return 1
}

append_node_arg_once() {
  local flag="$1"
  local node_args="$2"
  if node_args_contains_word "$node_args" "$flag"; then
    return 0
  fi
  NODE_ARGS_APPEND="${NODE_ARGS_APPEND:+$NODE_ARGS_APPEND }$flag"
  export NODE_ARGS_APPEND
}

remove_node_arg_prefix() {
  local prefix="$1"
  local filtered="" word
  for word in ${NODE_ARGS_APPEND:-}; do
    case "$word" in
      "$prefix"|"$prefix"=*) continue ;;
    esac
    filtered="${filtered:+$filtered }$word"
  done
  NODE_ARGS_APPEND="$filtered"
  export NODE_ARGS_APPEND
}

node_args_contains_prefix() {
  local node_args="$1"
  local prefix="$2"
  local word
  for word in $node_args; do
    case "$word" in
      "$prefix"|"$prefix"=*) return 0 ;;
    esac
  done
  return 1
}

append_node_arg_prefix_once() {
  local flag="$1"
  local node_args="$2"
  local prefix="${flag%%=*}"
  if node_args_contains_prefix "$node_args" "$prefix"; then
    return 0
  fi
  NODE_ARGS_APPEND="${NODE_ARGS_APPEND:+$NODE_ARGS_APPEND }$flag"
  export NODE_ARGS_APPEND
}

apply_node_mining_runtime_args() {
  case "${BDAG_ENABLE_NODE_MINING:-0}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) return 0 ;;
  esac

  local node_args modules word
  node_args="$(node_args_from_argv "$@" || true)"
  modules="${BDAG_NODE_MODULES:-}"
  if [ -n "$modules" ]; then
    modules="$(printf '%s' "$modules" | tr ',' ' ')"
    for word in $modules; do
      [ -n "$word" ] || continue
      append_node_arg_once "--modules=${word}" "$node_args ${NODE_ARGS_APPEND:-}"
    done
  fi
  for word in ${BDAG_NODE_MINING_ARGS:-}; do
    case "$word" in
      --miningaddr=*) append_node_arg_prefix_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
      --*) append_node_arg_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
    esac
  done
}

mount_source_for_path() {
  local path="$1" real best_src="" best_target="" src target fstype rest
  real="$(readlink -m "$path" 2>/dev/null || printf '%s' "$path")"
  [ -r /proc/mounts ] || {
    printf '\n'
    return 0
  }
  while read -r src target fstype rest; do
    target="${target//\\040/ }"
    if [[ "$real" == "$target" || "$real" == "$target"/* ]]; then
      if [ "${#target}" -gt "${#best_target}" ]; then
        best_target="$target"
        best_src="$src"
      fi
    fi
  done < /proc/mounts
  printf '%s\n' "$best_src"
}

block_device_from_source() {
  local source="$1" base
  case "$source" in
    /dev/*) ;;
    *) return 1 ;;
  esac
  base="$(basename "$source")"
  case "$base" in
    nvme*n*p*) printf '%s\n' "${base%p[0-9]*}" ;;
    mmcblk*p*) printf '%s\n' "${base%p[0-9]*}" ;;
    *) printf '%s\n' "${base%%[0-9]*}" ;;
  esac
}

path_is_usb_backed() {
  local path="$1" source block device_path
  source="$(mount_source_for_path "$path")"
  block="$(block_device_from_source "$source" 2>/dev/null || true)"
  [ -n "$block" ] || return 1
  device_path="$(readlink -f "/sys/block/$block/device" 2>/dev/null || true)"
  case "$device_path" in
    *usb*) return 0 ;;
    *) return 1 ;;
  esac
}

env_value_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|enabled|ENABLED) return 0 ;;
  esac
  return 1
}

env_value_false() {
  case "${1:-}" in
    0|false|FALSE|no|NO|off|OFF|disabled|DISABLED) return 0 ;;
  esac
  return 1
}

node_data_parent_from_args() {
  local node_args config_file data_parent
  node_args="$(node_args_from_argv "$@" || true)"
  config_file="$(node_arg_value configfile "$node_args" || true)"
  data_parent="${BDAG_FASTSNAP_DATADIR:-$(node_arg_value datadir "$node_args" || true)}"
  if [ -z "$data_parent" ] && [ -n "$config_file" ]; then
    data_parent="$(read_config_value "$config_file" datadir || true)"
  fi
  printf '%s\n' "${data_parent:-/var/lib/bdagStack/node}"
}

fastsync_serving_disable_reason() {
  local no_serve="${BDAG_NO_FASTSYNC_SERVE:-auto}"
  if env_value_true "$no_serve"; then
    printf 'BDAG_NO_FASTSYNC_SERVE=%s\n' "$no_serve"
    return 0
  fi
  if env_value_false "$no_serve"; then
    return 1
  fi

  local storage_profile="${BDAG_STORAGE_PROFILE:-}"
  storage_profile="$(lower_ascii "$storage_profile")"
  case "$storage_profile" in
    usb-chain-internal-runtime|single-usb-constrained)
      printf 'BDAG_STORAGE_PROFILE=%s\n' "$storage_profile"
      return 0
      ;;
  esac

  local data_parent
  data_parent="$(node_data_parent_from_args "$@")"
  if path_is_usb_backed "$data_parent"; then
    printf 'usb_backed_datadir=%s\n' "$data_parent"
    return 0
  fi

  return 1
}

node_binary_from_argv() {
  local arg
  if [ -n "${BDAG_NODE_BINARY:-}" ]; then
    printf '%s\n' "$BDAG_NODE_BINARY"
    return 0
  fi
  for arg in "$@"; do
    case "$arg" in
      --node-binary=*)
        printf '%s\n' "${arg#*=}"
        return 0
        ;;
    esac
  done
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$1"
    return 0
  fi
  return 1
}

node_binary_supports_arg() {
  local flag="$1" binary
  shift
  binary="$(node_binary_from_argv "$@" || true)"
  [ -n "$binary" ] || return 1
  if [ ! -x "$binary" ]; then
    binary="$(command -v "$binary" 2>/dev/null || true)"
  fi
  [ -n "$binary" ] || return 1
  "$binary" --help 2>&1 | grep -q -- "$flag"
}

apply_no_fastsync_serve_guard() {
  local disable_reason
  disable_reason="$(fastsync_serving_disable_reason "$@" || true)"
  if [ -z "$disable_reason" ]; then
    if env_value_false "${SYNC_SOURCE_NODE:-}"; then
      log "SYNC_SOURCE_NODE=${SYNC_SOURCE_NODE} disables raw datadir source publishing only; normal sync startup is unchanged unless storage/profile detection requires no-serve."
    fi
    return 0
  fi

  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  unset BDAG_FASTSYNC_ARTIFACT_DIRECTORY BDAG_FASTSYNC_ARTIFACT_MANIFEST
  if node_binary_supports_arg "--nofastsyncserve" "$@"; then
    append_node_arg_once "--nofastsyncserve" "$node_args ${NODE_ARGS_APPEND:-}"
    log "FastSync serving guard active ($disable_reason); disabling bulk FastSync, snapshot, and artifact serving while keeping normal outbound sync and block relay."
  else
    log "FastSync serving guard active ($disable_reason); selected node binary does not support --nofastsyncserve."
  fi
}

fastsnap_supports_directory_mode() {
  local fastsnap_bin="$1"
  "$fastsnap_bin" --help 2>&1 | grep -q -- "--dir-out"
}

maybe_fastsnap_bootstrap() {
  if [ "${BDAG_FASTSNAP_ENABLED:-1}" != "1" ]; then
    return 0
  fi

  local fastsnap_bin="${BDAG_FASTSNAP_BINARY:-/usr/local/bin/fastsnap}"
  [ -x "$fastsnap_bin" ] || {
    log "fastsnap binary missing; skipping P2P snapshot bootstrap"
    return 0
  }

  local node_binary
  node_binary="$(nodeworker_arg_value node-binary "$@" || true)"
  node_binary="${BDAG_FASTSNAP_NODE_BINARY:-${node_binary:-/usr/local/bin/blockdag-node}}"
  [ -x "$node_binary" ] || {
    log "node binary missing at $node_binary; skipping P2P snapshot bootstrap"
    return 0
  }

  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  local network
  network="$(mainnet_only_network "${BDAG_FASTSNAP_NETWORK:-mainnet}")"
  local config_file data_parent data_dir archive min_tip timeout peers peer tmp_archive tmp_dir directory_mode
  config_file="$(node_arg_value configfile "$node_args" || true)"
  data_parent="${BDAG_FASTSNAP_DATADIR:-$(node_arg_value datadir "$node_args" || true)}"
  if [ -z "$data_parent" ] && [ -n "$config_file" ]; then
    data_parent="$(read_config_value "$config_file" datadir || true)"
  fi
  data_parent="${data_parent:-/var/lib/bdagStack/node}"
  data_dir="$(network_datadir "$data_parent" "$network")"

  if [ -d "$data_dir/BdagChain" ]; then
    return 0
  fi

  archive="$data_dir/snapshot.bdsnap"
  mkdir -p "$data_dir"
  if [ -s "$archive" ]; then
    log "importing existing P2P snapshot archive before node startup: $archive"
    FASTSNAP_BOOTSTRAP_MUTATED=1
    "$node_binary" snap import --datadir "$data_dir" --path "$archive"
    return 0
  fi

  peers="${BDAG_FASTSNAP_PEERS:-${BOOTSTRAP_PEER_ADDRESSES:-}}"
  if [ -z "$peers" ]; then
    peers="$(addpeer_values "$node_args" | paste -sd, -)"
  fi
  if [ -z "$peers" ]; then
    log "no P2P snapshot peers configured; normal FastSync/legacy sync will start"
    return 0
  fi

  min_tip="${BDAG_FASTSNAP_MIN_TIP:-0}"
  timeout="${BDAG_FASTSNAP_TIMEOUT:-90s}"
  tmp_archive="$archive.download.$$"
  directory_mode="${BDAG_FASTSNAP_DIRECTORY_MODE:-1}"
  if [ "$directory_mode" = "1" ] && ! fastsnap_supports_directory_mode "$fastsnap_bin"; then
    log "fastsnap binary does not support directory install flags; using V2 archive fallback"
    directory_mode=0
  fi
  tmp_dir="${BDAG_FASTSNAP_DIRECTORY_STAGING:-$data_parent/.fastsnap-directory-$network.$$}"
  rm -f "$tmp_archive" "$tmp_archive.manifest.json"
  rm -rf "$tmp_dir" "$tmp_dir.manifest.json"

  local fastsnap_args=(
    --out "$tmp_archive"
    --network "$network"
    --min-tip "$min_tip"
    --timeout "$timeout"
  )
  if [ "$directory_mode" = "1" ]; then
    fastsnap_args+=(--dir-out "$tmp_dir" --install-dir "$data_dir")
    if [ "${BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING:-1}" = "1" ]; then
      fastsnap_args+=(--replace-existing)
    fi
    if [ "${BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING:-1}" = "1" ]; then
      fastsnap_args+=(--move-staging)
    fi
  fi
  local old_ifs="$IFS"
  IFS=', '
  for peer in $peers; do
    [ -n "$peer" ] || continue
    fastsnap_args+=(--peer "$peer")
  done
  IFS="$old_ifs"

  if [ "${BDAG_FASTSNAP_ARTIFACT_V2:-1}" = "0" ]; then
    fastsnap_args+=(--artifact-v2=false)
  fi
  if [ "${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}" = "1" ]; then
    fastsnap_args+=(--allow-unsigned)
  fi
  if [ -n "${BDAG_FASTSNAP_PARALLELISM:-}" ]; then
    fastsnap_args+=(--parallelism "$BDAG_FASTSNAP_PARALLELISM")
  fi
  if [ -n "${BDAG_FASTSNAP_LEDGER:-}" ]; then
    fastsnap_args+=(--ledger "$BDAG_FASTSNAP_LEDGER")
  fi

  log "trying P2P snapshot bootstrap with libp2p latency-first peer selection"
  if "$fastsnap_bin" "${fastsnap_args[@]}"; then
    if [ -d "$data_dir/BdagChain" ]; then
      if [ -f "$tmp_dir.manifest.json" ]; then
        mv "$tmp_dir.manifest.json" "$data_dir/artifact.manifest.json"
      fi
      rm -f "$tmp_archive" "$tmp_archive.manifest.json"
      rm -rf "$tmp_dir"
      log "downloaded and installed P2P directory artifact before node startup"
      FASTSNAP_BOOTSTRAP_MUTATED=1
      return 0
    fi
    if [ ! -s "$tmp_archive" ]; then
      log "fastsnap completed but did not install chain data or produce an archive"
      rm -f "$tmp_archive" "$tmp_archive.manifest.json"
      rm -rf "$tmp_dir" "$tmp_dir.manifest.json"
      if [ "${BDAG_FASTSNAP_REQUIRED:-0}" = "1" ]; then
        log "required P2P snapshot bootstrap failed"
        exit 1
      fi
      log "P2P snapshot bootstrap unavailable; falling back to normal FastSync/legacy sync"
      return 0
    fi
    mv "$tmp_archive" "$archive"
    if [ -f "$tmp_archive.manifest.json" ]; then
      mv "$tmp_archive.manifest.json" "$archive.manifest.json"
    fi
    log "importing downloaded P2P snapshot before node startup"
    FASTSNAP_BOOTSTRAP_MUTATED=1
    "$node_binary" snap import --datadir "$data_dir" --path "$archive"
    rm -rf "$tmp_dir" "$tmp_dir.manifest.json"
    return 0
  fi
  rm -f "$tmp_archive" "$tmp_archive.manifest.json"
  rm -rf "$tmp_dir" "$tmp_dir.manifest.json"

  if [ "${BDAG_FASTSNAP_REQUIRED:-0}" = "1" ]; then
    log "required P2P snapshot bootstrap failed"
    exit 1
  fi
  log "P2P snapshot bootstrap unavailable; falling back to normal FastSync/legacy sync"
}

apply_archival_flag() {
  case "${BDAG_NODE_ARCHIVAL:-0}" in
    1|true|True|yes) ;;
    *) return 0 ;;
  esac
  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  append_node_arg_once "--archival" "$node_args ${NODE_ARGS_APPEND:-}"
  log "archival mode enabled; node keeps full block history (--archival)"
}

node_binary_from_argv() {
  local arg
  if [ -n "${BDAG_NODE_BINARY:-}" ]; then
    printf '%s\n' "$BDAG_NODE_BINARY"
    return 0
  fi
  for arg in "$@"; do
    case "$arg" in
      --node-binary=*)
        printf '%s\n' "${arg#*=}"
        return 0
        ;;
    esac
  done
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$1"
    return 0
  fi
  return 1
}

# Bootstrap chain data from an HTTP(S) snapshot link before node startup.
# Order of precedence on an empty datadir: locally staged snapshot.bdsnap,
# then BDAG_SNAPSHOT_URL download, then the normal P2P/legacy sync paths.
maybe_http_snapshot_bootstrap() {
  if [ "${BDAG_ENTRYPOINT_PRINT_NODE_FLAGS:-0}" = "1" ]; then
    return 0
  fi

  local node_binary
  node_binary="$(node_binary_from_argv "$@" || true)"
  node_binary="${BDAG_FASTSNAP_NODE_BINARY:-${node_binary:-/usr/local/bin/blockdag-node}}"
  [ -x "$node_binary" ] || {
    log "node binary missing at $node_binary; skipping snapshot bootstrap"
    return 0
  }

  local node_args network config_file data_parent data_dir archive tmp min_bytes size
  node_args="$(node_args_from_argv "$@" || true)"
  network="$(mainnet_only_network "${BDAG_FASTSNAP_NETWORK:-mainnet}")"
  config_file="$(node_arg_value configfile "$node_args" || true)"
  data_parent="${BDAG_FASTSNAP_DATADIR:-$(node_arg_value datadir "$node_args" || true)}"
  if [ -z "$data_parent" ] && [ -n "$config_file" ]; then
    data_parent="$(read_config_value "$config_file" datadir || true)"
  fi
  data_parent="${data_parent:-/var/lib/bdagStack/node}"
  data_dir="$(network_datadir "$data_parent" "$network")"

  if [ -d "$data_dir/BdagChain" ]; then
    return 0
  fi

  archive="$data_dir/snapshot.bdsnap"
  mkdir -p "$data_dir"
  if [ -s "$archive" ]; then
    log "importing staged snapshot before node startup: $archive"
    if ! "$node_binary" snap import --datadir "$data_dir" --path "$archive"; then
      log "staged snapshot import failed; continuing with normal sync"
    fi
    return 0
  fi

  [ -n "${BDAG_SNAPSHOT_URL:-}" ] || return 0
  command -v curl >/dev/null 2>&1 || {
    log "curl missing; skipping HTTP snapshot download"
    return 0
  }

  min_bytes="${BDAG_SNAPSHOT_MIN_BYTES:-1048576}"
  tmp="$archive.download.$$"
  log "no chain data found; downloading snapshot from ${BDAG_SNAPSHOT_URL}"
  if ! curl --fail --location --silent --show-error --connect-timeout 20 --retry 2 --retry-delay 2 -o "$tmp" "$BDAG_SNAPSHOT_URL"; then
    rm -f "$tmp"
    log "snapshot download failed; continuing with P2P/legacy sync"
    return 0
  fi
  size="$(stat -c%s "$tmp" 2>/dev/null || echo 0)"
  if [ "$size" -lt "$min_bytes" ]; then
    rm -f "$tmp"
    log "downloaded snapshot too small ($size bytes < $min_bytes); continuing with P2P/legacy sync"
    return 0
  fi
  mv "$tmp" "$archive"
  log "importing downloaded snapshot before node startup ($size bytes)"
  if ! "$node_binary" snap import --datadir "$data_dir" --path "$archive"; then
    rm -f "$archive"
    log "downloaded snapshot import failed; continuing with normal sync"
  fi
}

apply_ordered_fastsync_peers "$@"
apply_no_fastsync_serve_guard "$@"
apply_node_mining_runtime_args "$@"
apply_archival_flag "$@"
maybe_http_snapshot_bootstrap "$@"

if [ -n "${NODE_ARGS_APPEND:-}" ]; then
  args=("$@")
  appended=0
  for i in "${!args[@]}"; do
    if [[ "${args[$i]}" == --node-args=* ]]; then
      args[$i]="${args[$i]} ${NODE_ARGS_APPEND}"
      appended=1
      break
    fi
  done
  if [ "${appended}" -eq 0 ]; then
    args+=("--node-args=${NODE_ARGS_APPEND}")
  fi
  set -- "${args[@]}"
fi

if [ "$(basename "${1:-}")" = "nodeworker" ] && ! nodeworker_arg_present "health.liveness-timeout" "$@"; then
  args=("$@")
  args+=("--health.liveness-timeout=${BDAG_NODEWORKER_LIVENESS_TIMEOUT:-5m}")
  set -- "${args[@]}"
fi

if [ "${BDAG_ENTRYPOINT_PRINT_NODE_FLAGS:-0}" = "1" ]; then
  printf 'NODE_ARGS_APPEND=%s\n' "${NODE_ARGS_APPEND:-}"
  exit 0
fi

if [ "$(id -u)" = 0 ]; then
  ensure_owned_runtime_dirs
  fix_ownership_if_needed
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
exec "$@"
