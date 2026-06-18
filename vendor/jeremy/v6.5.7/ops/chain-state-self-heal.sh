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

Fail-closed repair for BlockDAG node chain-state corruption. The script checks
dashboard status for needs_chain_data_restore / chain_state_blocker, stops the
pool, stops the node, quarantines the damaged node datadir, restores from a
configured trusted source or local snapshot, restarts node/dashboard, and leaves
the pool stopped until normal readiness gates pass.

Configure trusted restore input with one of:
  BDAG_CHAIN_STATE_RESTORE_SOURCE=/local/path/or/user@host:/path/to/mainnet
  BDAG_CHAIN_STATE_RESTORE_SNAPSHOT=/path/to/latest.bdsnap

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

enabled="${BDAG_CHAIN_STATE_SELF_HEAL_ENABLED:-1}"
if [[ "$enabled" != "1" && "$FORCE" != "1" ]]; then
  log "self-heal disabled by BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=$enabled"
  json_state "disabled" "BDAG_CHAIN_STATE_SELF_HEAL_ENABLED is not 1"
  exit 0
fi

REQUESTED_NETWORK="${BDAG_CHAIN_STATE_NETWORK:-${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}}"
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
elif [[ -d "$CHAIN_DATA_DIR/node1/mainnet" ]]; then
  NODE_DATA_DIR="$CHAIN_DATA_DIR/node1"
else
  NODE_DATA_DIR="$CHAIN_DATA_DIR/node"
fi
NODE_NETWORK_DIR="$NODE_DATA_DIR/$NETWORK"
QUARANTINE_ROOT="${BDAG_CHAIN_STATE_QUARANTINE_DIR:-$CHAIN_DATA_DIR/chain-quarantine}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
QUARANTINE_PATH="$QUARANTINE_ROOT/$(basename "$NODE_DATA_DIR")-damaged-$STAMP"
TMP_DIR="$RUNTIME_DIR/chain-state-self-heal-$STAMP"
mkdir -p "$TMP_DIR" "$QUARANTINE_ROOT"

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

copy_existing_snapshot() {
  local snapshot="$NODE_NETWORK_DIR/snapshot.bdsnap"
  if [[ "${BDAG_CHAIN_STATE_REUSE_EXISTING_SNAPSHOT:-1}" == "1" && -s "$snapshot" ]]; then
    cp -a "$snapshot" "$TMP_DIR/snapshot.bdsnap"
    for companion in "$NODE_NETWORK_DIR/snapshot.bdsnap.manifest" "$NODE_NETWORK_DIR/snapshot.bdsnap.json" "$NODE_NETWORK_DIR/manifest.json"; do
      [[ -f "$companion" ]] && cp -a "$companion" "$TMP_DIR/$(basename "$companion")"
    done
    log "staged existing local snapshot for fallback restore"
  fi
}

choose_restore_source() {
  RESTORE_MODE_USED=""
  RESTORE_SOURCE_USED=""
  if [[ -n "${BDAG_CHAIN_STATE_RESTORE_SOURCE:-}" ]]; then
    RESTORE_MODE_USED="source"
    RESTORE_SOURCE_USED="$BDAG_CHAIN_STATE_RESTORE_SOURCE"
    return 0
  fi
  if [[ -n "${BDAG_CHAIN_STATE_RESTORE_SNAPSHOT:-}" ]]; then
    RESTORE_MODE_USED="snapshot"
    RESTORE_SOURCE_USED="$BDAG_CHAIN_STATE_RESTORE_SNAPSHOT"
    return 0
  fi
  local candidates=(
    "$ROOT/data-restore/rawdatadir-sidecar-content/current/mainnet"
    "$ROOT/data-restore/rawdatadir-sidecar-content/current"
    "$ROOT/data-restore/rawdatadir/current/mainnet"
    "$ROOT/data-restore/rawdatadir/current"
    "$ROOT/data-restore/latest/mainnet"
    "$ROOT/data-restore/latest"
  )
  for candidate in "${candidates[@]}"; do
    if [[ -d "$candidate" ]]; then
      RESTORE_MODE_USED="source"
      RESTORE_SOURCE_USED="$candidate"
      return 0
    fi
  done
  if [[ -s "$TMP_DIR/snapshot.bdsnap" ]]; then
    RESTORE_MODE_USED="snapshot"
    RESTORE_SOURCE_USED="$TMP_DIR/snapshot.bdsnap"
    return 0
  fi
  return 1
}

restore_from_source() {
  local source="$1"
  mkdir -p "$NODE_NETWORK_DIR"
  if [[ "$source" == *:* && "$source" != /* ]]; then
    local ssh_command="${BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND:-ssh -o BatchMode=yes}"
    log "restoring chain data from remote rsync source $source"
    rsync -a --delete -e "$ssh_command" "${source%/}/" "$NODE_NETWORK_DIR/"
  else
    local local_source
    local_source="$(abs_path "$source")"
    if [[ -d "$local_source/mainnet" ]]; then
      local_source="$local_source/mainnet"
    fi
    log "restoring chain data from local source $local_source"
    rsync -a --delete "$local_source/" "$NODE_NETWORK_DIR/"
  fi
}

restore_from_snapshot() {
  local snapshot="$1"
  local snapshot_path
  snapshot_path="$(abs_path "$snapshot")"
  mkdir -p "$NODE_NETWORK_DIR"
  log "restoring chain data from snapshot $snapshot_path"
  cp -a "$snapshot_path" "$NODE_NETWORK_DIR/snapshot.bdsnap"
  for companion in "${snapshot_path}.manifest" "${snapshot_path}.json" "$(dirname "$snapshot_path")/manifest.json"; do
    [[ -f "$companion" ]] && cp -a "$companion" "$NODE_NETWORK_DIR/$(basename "$companion")"
  done
}

copy_existing_snapshot
if ! choose_restore_source; then
  log "no trusted restore source or local snapshot was found"
  stop_service_best_effort "$POOL_SERVICE" || true
  json_state "blocked" "chain-state restore needed but no restore source/snapshot is configured"
  exit 1
fi

json_state "started" "chain-state restore started"
stop_service_best_effort "$POOL_SERVICE" || true
stop_service_best_effort "$NODE_SERVICE" || true

if [[ -d "$NODE_DATA_DIR" ]]; then
  mkdir -p "$(dirname "$QUARANTINE_PATH")"
  mv "$NODE_DATA_DIR" "$QUARANTINE_PATH"
  log "quarantined damaged node data at $QUARANTINE_PATH"
fi
mkdir -p "$NODE_DATA_DIR"

case "$RESTORE_MODE_USED" in
  source) restore_from_source "$RESTORE_SOURCE_USED" ;;
  snapshot) restore_from_snapshot "$RESTORE_SOURCE_USED" ;;
  *)
    log "internal error: unknown restore mode $RESTORE_MODE_USED"
    json_state "failed" "unknown restore mode"
    exit 1
    ;;
esac

if compose up -d --no-build --pull never "$NODE_SERVICE" "$DASHBOARD_SERVICE" >>"$LOG_FILE" 2>&1; then
  log "restarted node/dashboard after chain-state restore"
else
  log "failed to restart node/dashboard after chain-state restore"
  json_state "failed" "node/dashboard restart failed after restore"
  exit 1
fi

stop_service_best_effort "$POOL_SERVICE" || true
json_state "restored" "chain-state restore completed; pool remains stopped until readiness passes"
log "chain-state self-heal completed; pool remains stopped until readiness gates pass"
