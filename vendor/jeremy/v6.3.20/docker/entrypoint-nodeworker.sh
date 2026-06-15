#!/usr/bin/env bash
# Fix ownership of persisted paths on every container start. Docker volumes are
# often populated as root, which prevents bdagStack from opening chain data.
set -euo pipefail

log() {
  printf '[%s] node-entrypoint: %s\n' "$(date -Is)" "$*" >&2
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

BOOTSTRAP_PEER_SEEN=
BOOTSTRAP_PEER_KEYS_SEEN=

peer_identity_key() {
  local peer="$1"
  local peer_id
  case "$peer" in
    */p2p/*)
      peer_id="${peer#*/p2p/}"
      peer_id="${peer_id%%/*}"
      [ -n "$peer_id" ] && printf 'p2p:%s\n' "$peer_id" && return 0
      ;;
  esac
  printf 'addr:%s\n' "$peer"
}

bootstrap_peer_limit() {
  local limit="${BDAG_NODE_PEER_LIMIT:-8}"
  case "$limit" in
    ''|*[!0-9]*) printf '8\n' ;;
    *) printf '%s\n' "$limit" ;;
  esac
}

append_unique_peer() {
  local bucket_name="$1"
  local peer="$2"
  local -n bucket="$bucket_name"
  local key limit

  [ -n "$peer" ] || return 0
  case "$peer" in
    none|null) return 0 ;;
  esac
  limit="$(bootstrap_peer_limit)"
  if [ "$limit" -gt 0 ] && [ "${#bucket[@]}" -ge "$limit" ]; then
    return 0
  fi
  key="$(peer_identity_key "$peer")"
  case "$BOOTSTRAP_PEER_KEYS_SEEN" in
    *"|$key|"*) return 0 ;;
  esac
  case "$BOOTSTRAP_PEER_SEEN" in
    *"|$peer|"*) return 0 ;;
  esac
  bucket+=("$peer")
  BOOTSTRAP_PEER_SEEN="${BOOTSTRAP_PEER_SEEN}|$peer|"
  BOOTSTRAP_PEER_KEYS_SEEN="${BOOTSTRAP_PEER_KEYS_SEEN}|$key|"
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
  joined="${bootstrap_peers[*]:-}"
  IFS="$old_ifs"
  printf '%s\n' "$joined"
}

ordered_bootstrap_peers() {
  local node_args="$1"
  local config_file config_peers generic_peers
  bootstrap_peers=()
  BOOTSTRAP_PEER_SEEN=
  BOOTSTRAP_PEER_KEYS_SEEN=

  config_file="$(node_arg_value configfile "$node_args" || true)"
  config_file="${config_file:-/etc/bdagStack/node.conf}"
  config_peers="$(config_addpeer_values "$config_file" | paste -sd, - || true)"

  generic_peers="${BOOTSTRAP_PEER_ADDRESSES:-} $config_peers ${BDAG_NODE_PEER_ADDRESSES:-} $(addpeer_values "$node_args" | paste -sd, - || true)"
  append_peer_list bootstrap_peers "$generic_peers"

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

apply_bootstrap_peers() {
  local node_args ordered addpeer_args total_count
  node_args="$(node_args_from_argv "$@" || true)"
  ordered="$(ordered_bootstrap_peers "$node_args")"
  [ -n "$ordered" ] || return 0

  total_count="$(printf '%s' "$ordered" | awk -F, '{print NF}')"
  log "P2P bootstrap peers configured; total=${total_count}"

  addpeer_args="$(addpeer_args_from_csv "$ordered")"
  NODE_ARGS_APPEND="${addpeer_args}${NODE_ARGS_APPEND:+ $NODE_ARGS_APPEND}"
  export NODE_ARGS_APPEND
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
      append_node_arg_prefix_once "--modules=${word}" "$node_args ${NODE_ARGS_APPEND:-}"
    done
  fi
  for word in ${BDAG_NODE_MINING_ARGS:-}; do
    case "$word" in
      --miningaddr=*) append_node_arg_prefix_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
      --*) append_node_arg_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
    esac
  done
}

apply_bootstrap_peers "$@"

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

apply_node_mining_runtime_args "$@"
apply_archival_flag "$@"

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
