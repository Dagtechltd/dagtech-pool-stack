#!/usr/bin/env bash
set -euo pipefail

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

ENV_FILE="${BDAG_POOL_ENV_FILE:-$ROOT/.env}"
RUNTIME_DIR="${BDAG_RUNTIME_DIR:-$ROOT/ops/runtime}"
LOG_DIR="$RUNTIME_DIR/logs"
STATE_FILE="${BDAG_CHAIN_STATE_SELF_HEAL_STATE_FILE:-$RUNTIME_DIR/chain-state-self-heal-state.json}"
LOG_FILE="${BDAG_CHAIN_STATE_SELF_HEAL_LOG_FILE:-$LOG_DIR/chain-state-self-heal.log}"
LOCK_FILE="${BDAG_CHAIN_STATE_SELF_HEAL_LOCK_FILE:-$RUNTIME_DIR/chain-state-self-heal.lock}"

FORCE=0
SYSTEMD_MODE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --from-systemd) SYSTEMD_MODE=1 ;;
    --help|-h)
      cat <<'USAGE'
Usage: ops/chain-state-self-heal.sh [--force] [--from-systemd]

Fail-closed repair for BlockDAG node chain-state corruption. This destructive
restore path is disabled by default and requires an explicitly configured
trusted rawdatadir/IPFS source. When enabled, the script checks dashboard status for
needs_chain_data_restore / chain_state_blocker, stops the pool, stops the node,
quarantines the damaged node datadir, restores from the configured restore
input, restarts node/dashboard, and leaves the pool stopped until normal
readiness gates pass.

Configure trusted restore input with one of:
  BDAG_CHAIN_STATE_RESTORE_SOURCE=/local/path/or/user@host:/path/to/mainnet
  BDAG_CHAIN_STATE_RESTORE_IPFS_ARTIFACT_CID=bafy...
  BDAG_CHAIN_STATE_RESTORE_IPFS_INDEX_CID=bafy...

Remote restore uses rsync and can set:
  BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND='ssh -i /path/to/key -o BatchMode=yes'

No passwords or secrets should be stored in source or checked-in env files.
USAGE
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$arg" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE" >&2
}

sudo_available() {
  command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1
}

mkdir_p() {
  if mkdir -p "$@" 2>/dev/null; then
    return 0
  fi
  if sudo_available; then
    sudo -n mkdir -p "$@"
    return $?
  fi
  mkdir -p "$@"
}

path_is_dir() {
  [[ -d "$1" ]] && return 0
  sudo_available && sudo -n test -d "$1"
}

mv_path() {
  if mv "$1" "$2" 2>/dev/null; then
    return 0
  fi
  if sudo_available; then
    sudo -n mv "$1" "$2"
    return $?
  fi
  mv "$1" "$2"
}

rsync_path() {
  if rsync "$@" 2>/dev/null; then
    return 0
  fi
  if sudo_available; then
    sudo -n rsync "$@"
    return $?
  fi
  rsync "$@"
}

chown_path_recursive() {
  local owner="$1" path="$2"
  if chown -R "$owner" "$path" 2>/dev/null; then
    return 0
  fi
  if sudo_available; then
    sudo -n chown -R "$owner" "$path"
    return $?
  fi
  chown -R "$owner" "$path"
}

json_state() {
  local status="$1" reason="$2"
  STATUS_VALUE="$status" \
  REASON_VALUE="$reason" \
  SOURCE_VALUE="${RESTORE_SOURCE_USED:-}" \
  MODE_VALUE="${RESTORE_MODE_USED:-}" \
  QUARANTINE_VALUE="${QUARANTINE_PATH:-}" \
  python3 - "$STATE_FILE" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "schema_version": 1,
    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "epoch": time.time(),
    "status": os.environ.get("STATUS_VALUE", ""),
    "reason": os.environ.get("REASON_VALUE", ""),
    "restore_source": os.environ.get("SOURCE_VALUE", ""),
    "restore_mode": os.environ.get("MODE_VALUE", ""),
    "quarantine_path": os.environ.get("QUARANTINE_VALUE", ""),
}
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
}

automation_allows_self_heal() {
  local reason="$1"
  python3 - "$ROOT" "$reason" <<'PY'
import sys

root, reason = sys.argv[1], sys.argv[2]
sys.path.insert(0, f"{root}/ops")
import automation_control  # type: ignore

decision = automation_control.check_mutation_allowed(
    automation_control.ACTION_STACK_CLEAN_RESTORE,
    actor="chain-state-self-heal",
    target="chain-state-self-heal",
    reason=reason,
)
if decision.allowed:
    raise SystemExit(0)
print(decision.reason)
raise SystemExit(1)
PY
}

load_env_file() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ "$line" == *=* ]] || continue
    local key="${line%%=*}"
    local value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    if [[ -z "${!key+x}" ]]; then
      export "$key=$value"
    fi
  done < "$file"
}

load_env_file "$ENV_FILE"
load_env_file "${BDAG_RUNTIME_ENV_FILE:-$RUNTIME_DIR/ops.env}"

enabled="${BDAG_CHAIN_STATE_SELF_HEAL_ENABLED:-0}"
if [[ "$enabled" != "1" && "$FORCE" != "1" ]]; then
  log "self-heal disabled by BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=$enabled"
  json_state "disabled" "BDAG_CHAIN_STATE_SELF_HEAL_ENABLED is not 1"
  exit 0
fi

REQUESTED_NETWORK="${BDAG_CHAIN_STATE_NETWORK:-${BDAG_RAWDATADIR_NETWORK:-mainnet}}"
if [[ "${REQUESTED_NETWORK,,}" != "mainnet" ]]; then
  log "chain-state self-heal refuses non-mainnet network: $REQUESTED_NETWORK"
  json_state "blocked" "non-mainnet chain-state restore network is unsupported:$REQUESTED_NETWORK"
  exit 2
fi
NETWORK="mainnet"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another chain-state self-heal run is already active"
  json_state "locked" "another self-heal run holds the lock"
  exit 0
fi

cooldown="${BDAG_CHAIN_STATE_SELF_HEAL_COOLDOWN_SECONDS:-21600}"
if [[ "$FORCE" != "1" && "$cooldown" =~ ^[0-9]+$ && "$cooldown" -gt 0 && -f "$STATE_FILE" ]]; then
  if python3 - "$STATE_FILE" "$cooldown" <<'PY'
import json
import sys
import time

path = sys.argv[1]
cooldown = int(sys.argv[2])
try:
    payload = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(1)
status = str(payload.get("status") or "")
epoch = float(payload.get("epoch") or 0)
if status in {"restored", "blocked", "failed"} and time.time() - epoch < cooldown:
    sys.exit(0)
sys.exit(1)
PY
  then
    log "cooldown active after recent self-heal state; use --force to override"
    json_state "cooldown" "recent self-heal state is still inside cooldown"
    exit 0
  fi
fi

status_needs_restore() {
  python3 - <<'PY'
import json
import os
import sys
import urllib.request

url = os.environ.get("BDAG_CHAIN_STATE_SELF_HEAL_STATUS_URL") or ""
timeout = float(os.environ.get("BDAG_CHAIN_STATE_SELF_HEAL_STATUS_TIMEOUT", "5"))
payload = None
if url:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        payload = None
if payload is None:
    for path in (
        os.environ.get("BDAG_STATUS_SAMPLER_FILE"),
        os.environ.get("BDAG_CHAIN_STATE_SELF_HEAL_STATUS_FILE"),
    ):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
                break
        except Exception:
            continue
if not isinstance(payload, dict):
    print("status unavailable")
    sys.exit(2)
sync = payload.get("sync_health") if isinstance(payload.get("sync_health"), dict) else {}
nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
chain_state = bool(sync.get("needs_chain_data_restore") or sync.get("chain_state_blocker"))
if not chain_state:
    chain_state = any(isinstance(info, dict) and info.get("chain_state_blocker") for info in nodes.values())
if chain_state:
    print("chain state restore required")
    sys.exit(0)
print("no chain state restore trigger")
sys.exit(1)
PY
}

if [[ "$FORCE" != "1" ]]; then
  status_url_default="http://127.0.0.1:${BDAG_DASHBOARD_PORT:-8088}/api/status"
  export BDAG_CHAIN_STATE_SELF_HEAL_STATUS_URL="${BDAG_CHAIN_STATE_SELF_HEAL_STATUS_URL:-$status_url_default}"
  if ! reason="$(status_needs_restore 2>&1)"; then
    log "no chain-state restore trigger: $reason"
    json_state "no_trigger" "$reason"
    exit 0
  fi
  log "restore trigger confirmed: $reason"
else
  log "forced chain-state restore requested"
fi

abs_path() {
  local value="$1"
  if [[ "$value" = /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$ROOT/$value"
  fi
}

CHAIN_DATA_DIR="$(abs_path "${BDAG_CHAIN_DATA_DIR:-${BDAG_DATA_DIR:-./data}}")"
if [[ -n "${BDAG_NODE_DATA_DIR:-}" ]]; then
  NODE_DATA_DIR="$(abs_path "$BDAG_NODE_DATA_DIR")"
else
  NODE_DATA_DIR="$CHAIN_DATA_DIR/node"
fi
NODE_NETWORK_DIR="$NODE_DATA_DIR/$NETWORK"
DEFAULT_QUARANTINE_ROOT="$CHAIN_DATA_DIR/chain-quarantine"
case "$DEFAULT_QUARANTINE_ROOT/" in
  "$NODE_DATA_DIR"/*) DEFAULT_QUARANTINE_ROOT="$(dirname "$NODE_DATA_DIR")/chain-quarantine" ;;
esac
QUARANTINE_ROOT="${BDAG_CHAIN_STATE_QUARANTINE_DIR:-$DEFAULT_QUARANTINE_ROOT}"
case "$QUARANTINE_ROOT/" in
  "$NODE_DATA_DIR"/*)
    log "chain-state quarantine dir must not be inside node data dir: $QUARANTINE_ROOT"
    json_state "blocked" "chain-state quarantine dir is inside node data dir"
    exit 1
    ;;
esac
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
QUARANTINE_PATH="$QUARANTINE_ROOT/$(basename "$NODE_DATA_DIR")-damaged-$STAMP"
TMP_DIR="$RUNTIME_DIR/chain-state-self-heal-$STAMP"

POOL_SERVICE="${BDAG_CHAIN_STATE_POOL_SERVICE:-${BDAG_POOL_CONTAINER:-pool}}"
NODE_SERVICE="${BDAG_CHAIN_STATE_NODE_SERVICE:-node}"
DASHBOARD_SERVICE="${BDAG_CHAIN_STATE_DASHBOARD_SERVICE:-dashboard}"
COMPOSE_PROJECT_NAME="${BDAG_COMPOSE_PROJECT_NAME:-${COMPOSE_PROJECT_NAME:-}}"

compose() {
  docker compose "$@"
}

stop_service_best_effort() {
  local service="$1"
  if compose stop "$service" >>"$LOG_FILE" 2>&1; then
    log "stopped compose service $service"
    return 0
  fi
  if [[ -n "$COMPOSE_PROJECT_NAME" ]] && docker stop "${COMPOSE_PROJECT_NAME}-${service}-1" >>"$LOG_FILE" 2>&1; then
    log "stopped container ${COMPOSE_PROJECT_NAME}-${service}-1"
    return 0
  fi
  if docker stop "$service" >>"$LOG_FILE" 2>&1; then
    log "stopped container $service"
    return 0
  fi
  log "service/container $service was not stopped by best-effort stop path"
  return 1
}

choose_restore_source() {
  RESTORE_MODE_USED=""
  RESTORE_SOURCE_USED=""
  if [[ -n "${BDAG_CHAIN_STATE_RESTORE_SOURCE:-}" ]]; then
    RESTORE_MODE_USED="source"
    RESTORE_SOURCE_USED="$BDAG_CHAIN_STATE_RESTORE_SOURCE"
    return 0
  fi
  if [[ -n "${BDAG_CHAIN_STATE_RESTORE_IPFS_ARTIFACT_CID:-}" ]]; then
    RESTORE_MODE_USED="ipfs_artifact"
    RESTORE_SOURCE_USED="$BDAG_CHAIN_STATE_RESTORE_IPFS_ARTIFACT_CID"
    return 0
  fi
  if [[ -n "${BDAG_CHAIN_STATE_RESTORE_IPFS_INDEX_CID:-}" ]]; then
    RESTORE_MODE_USED="ipfs_index"
    RESTORE_SOURCE_USED="$BDAG_CHAIN_STATE_RESTORE_IPFS_INDEX_CID"
    return 0
  fi
  if [[ -n "${BDAG_CHAIN_STATE_RESTORE_IPFS_INDEX_FILE:-}" ]]; then
    RESTORE_MODE_USED="ipfs_index_file"
    RESTORE_SOURCE_USED="$BDAG_CHAIN_STATE_RESTORE_IPFS_INDEX_FILE"
    return 0
  fi
  return 1
}

resolve_local_restore_source() {
  local source="$1"
  local local_source
  local_source="$(abs_path "$source")"
  if [[ -d "$local_source/mainnet" ]]; then
    local_source="$local_source/mainnet"
  fi
  printf '%s\n' "$local_source"
}

reject_sealed_artifact_source() {
  local source="$1"
  case "$source" in
    *rawdatadir-sidecar-content*)
      log "restore source $source is sealed rawdatadir-sidecar-content, not a raw node datadir"
      return 1
      ;;
  esac
  if [[ -f "$source/DO_NOT_PUBLISH.txt" ]]; then
    log "restore source $source contains DO_NOT_PUBLISH.txt and is not a raw node datadir"
    return 1
  fi
  if [[ -d "$source/chunks" && -f "$source/manifest.json" ]]; then
    log "restore source $source contains chunks/ plus manifest.json and is not a raw node datadir"
    return 1
  fi
  if [[ -f "$source/manifest.json" ]]; then
    if python3 - "$source/manifest.json" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)

artifact_type = str(payload.get("artifact_type") or "")
chunks = payload.get("chunks")
if artifact_type in {"raw_datadir_checkpoint", "ipfs_segment_index", "ipfs_segment"}:
    sys.exit(0)
if isinstance(chunks, list):
    sys.exit(0)
sys.exit(1)
PY
    then
      log "restore source $source manifest describes sealed artifact chunks, not a raw node datadir"
      return 1
    fi
  fi
  return 0
}

reject_live_hot_rsync_source() {
  local source="$1" manifest
  for manifest in "$source/manifest.json" "$(dirname "$source")/manifest.json"; do
    [[ -f "$manifest" ]] || continue
    if python3 - "$manifest" "$NODE_NETWORK_DIR" <<'PY'
import json
import os
import sys

manifest_path, node_network_dir = sys.argv[1:3]
try:
    payload = json.load(open(manifest_path, encoding="utf-8"))
except Exception:
    sys.exit(1)

mode = str(payload.get("mode") or "")
source = str(payload.get("source") or "")
if mode != "live_hot_rsync" or not source:
    sys.exit(1)

try:
    source_real = os.path.realpath(source)
    node_real = os.path.realpath(node_network_dir)
except Exception:
    sys.exit(1)

sys.exit(0 if source_real == node_real else 1)
PY
    then
      log "restore source $source is a live_hot_rsync mirror of this node data according to $manifest"
      return 1
    fi
  done
  return 0
}

validate_restore_input() {
  local mode="$1" source="$2"
  case "$mode" in
    source)
      if [[ "$source" == *:* && "$source" != /* ]]; then
        return 0
      fi
      local local_source
      local_source="$(resolve_local_restore_source "$source")"
      if [[ ! -d "$local_source" ]]; then
        log "configured restore source does not exist or is not a directory: $local_source"
        return 1
      fi
      reject_sealed_artifact_source "$local_source"
      reject_live_hot_rsync_source "$local_source"
      ;;
    ipfs_artifact|ipfs_index|ipfs_index_file)
      if [[ ! -x "$ROOT/ops/restore-rawdatadir-segment-artifact.py" ]]; then
        log "IPFS rawdatadir restore tool is missing"
        return 1
      fi
      if ! command -v "${BDAG_IPFS_BINARY:-ipfs}" >/dev/null 2>&1; then
        log "IPFS rawdatadir restore requires Kubo CLI; set BDAG_IPFS_BINARY or install ipfs"
        return 1
      fi
      if [[ "$mode" == "ipfs_index_file" && ! -s "$(abs_path "$source")" ]]; then
        log "configured IPFS rawdatadir restore index file is missing or empty: $(abs_path "$source")"
        return 1
      fi
      ;;
    *)
      log "internal error: unknown restore mode $mode"
      return 1
      ;;
  esac
}

restore_from_source() {
  local source="$1"
  mkdir_p "$NODE_NETWORK_DIR"
  if [[ "$source" == *:* && "$source" != /* ]]; then
    local ssh_command="${BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND:-ssh -o BatchMode=yes}"
    log "restoring chain data from remote rsync source $source"
    rsync_path -a --delete -e "$ssh_command" "${source%/}/" "$NODE_NETWORK_DIR/"
  else
    local local_source
    local_source="$(resolve_local_restore_source "$source")"
    log "restoring chain data from local source $local_source"
    rsync_path -a --delete "$local_source/" "$NODE_NETWORK_DIR/"
  fi
}

restore_from_ipfs_artifact() {
  local mode="$1" source="$2" status_file timeout args=()
  mkdir_p "$NODE_NETWORK_DIR"
  status_file="${BDAG_CHAIN_STATE_RESTORE_IPFS_STATUS_FILE:-$RUNTIME_DIR/ipfs-rawdatadir-self-heal-restore-status.json}"
  timeout="${BDAG_IPFS_RAWDATADIR_RESTORE_IPFS_TIMEOUT:-600}"
  args=(--target-dir "$NODE_NETWORK_DIR" --status-file "$status_file" --ipfs-timeout "$timeout" --network "$NETWORK")
  case "$mode" in
    ipfs_artifact) args+=(--ipfs-artifact-cid "$source") ;;
    ipfs_index) args+=(--ipfs-index-cid "$source") ;;
    ipfs_index_file) args+=(--ipfs-index-file "$(abs_path "$source")") ;;
  esac
  if [[ -n "${BDAG_RAWDATADIR_TRUSTED_SIGNERS:-}" ]]; then
    args+=(--trusted-signers "$BDAG_RAWDATADIR_TRUSTED_SIGNERS")
  fi
  log "restoring chain data from signed IPFS rawdatadir artifact mode=$mode source=$source"
  python3 "$ROOT/ops/restore-rawdatadir-segment-artifact.py" "${args[@]}" >>"$LOG_FILE" 2>&1
}

if ! choose_restore_source; then
  log "no trusted rawdatadir/IPFS restore source was found"
  json_state "blocked" "chain-state restore needed but no rawdatadir/IPFS restore source is configured"
  exit 1
fi
if ! validate_restore_input "$RESTORE_MODE_USED" "$RESTORE_SOURCE_USED"; then
  log "selected restore input is not safe for destructive chain-state restore"
  json_state "blocked" "selected restore input is not a restore-safe raw datadir/IPFS artifact"
  exit 1
fi
if ! control_reason="$(automation_allows_self_heal "destructive chain-state restore mode=$RESTORE_MODE_USED source=$RESTORE_SOURCE_USED" 2>&1)"; then
  log "automation control blocked chain-state self-heal: $control_reason"
  json_state "blocked" "automation control blocked chain-state self-heal: $control_reason"
  exit 1
fi
mkdir_p "$TMP_DIR" "$QUARANTINE_ROOT"

json_state "started" "chain-state restore started"
stop_service_best_effort "$POOL_SERVICE" || true
stop_service_best_effort "$NODE_SERVICE" || true

if path_is_dir "$NODE_DATA_DIR"; then
  mkdir_p "$(dirname "$QUARANTINE_PATH")"
  mv_path "$NODE_DATA_DIR" "$QUARANTINE_PATH"
  log "quarantined damaged node data at $QUARANTINE_PATH"
fi
mkdir_p "$NODE_DATA_DIR"

case "$RESTORE_MODE_USED" in
  source) restore_from_source "$RESTORE_SOURCE_USED" ;;
  ipfs_artifact|ipfs_index|ipfs_index_file) restore_from_ipfs_artifact "$RESTORE_MODE_USED" "$RESTORE_SOURCE_USED" ;;
  *)
    log "internal error: unknown restore mode $RESTORE_MODE_USED"
    json_state "failed" "unknown restore mode"
    exit 1
    ;;
esac

RESTORE_CHOWN="${BDAG_CHAIN_STATE_RESTORE_CHOWN:-999:999}"
case "${RESTORE_CHOWN,,}" in
  ""|0|false|no|none|off) ;;
  *)
    chown_path_recursive "$RESTORE_CHOWN" "$NODE_DATA_DIR"
    log "set restored node data ownership to $RESTORE_CHOWN"
    ;;
esac

if compose up -d --no-build --pull never "$NODE_SERVICE" >>"$LOG_FILE" 2>&1; then
  log "restarted node after chain-state restore"
else
  log "failed to restart node after chain-state restore"
  json_state "failed" "node restart failed after restore"
  exit 1
fi

if [[ -n "$DASHBOARD_SERVICE" ]]; then
  if compose up -d --no-build --pull never "$DASHBOARD_SERVICE" >>"$LOG_FILE" 2>&1; then
    log "restarted dashboard after chain-state restore"
  else
    log "dashboard restart failed after chain-state restore; continuing because node restore completed"
  fi
fi

stop_service_best_effort "$POOL_SERVICE" || true
json_state "restored" "chain-state restore completed; pool remains stopped until readiness passes"
log "chain-state self-heal completed; pool remains stopped until readiness gates pass"
