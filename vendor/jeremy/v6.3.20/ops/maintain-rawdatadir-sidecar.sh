#!/usr/bin/env bash
set -Eeuo pipefail

# Keep a low-priority local sidecar copy close to the live datadir. It does not
# publish directly by itself. The IPFS content and segment sidecars seal and
# publish verified content after this low-priority copy is safe.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BDAG_STACK_DEFAULTS_FILE="${BDAG_STACK_DEFAULTS_FILE:-$PROJECT_ROOT/ops/config/stack-defaults.env}"
if [[ -f "$BDAG_STACK_DEFAULTS_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$BDAG_STACK_DEFAULTS_FILE"
  set +a
fi
REQUESTED_NETWORK="${BDAG_RAWDATADIR_NETWORK:-mainnet}"
if [[ "${REQUESTED_NETWORK,,}" != "mainnet" ]]; then
  printf '[%s] raw datadir sidecar refuses non-mainnet network: %s\n' "$(date -Is)" "$REQUESTED_NETWORK" >&2
  exit 2
fi
NETWORK="mainnet"
ACTIVE_NODE_SERVICE="${BDAG_RAWDATADIR_ACTIVE_SERVICE:-${BDAG_NODE_SERVICE:-node}}"
ACTIVE_NODE_SERVICE="${ACTIVE_NODE_SERVICE:-node}"
DEFAULT_NODE_DIR="${BDAG_NODE_DATA_DIR:-$PROJECT_ROOT/data/node}"
SOURCE_DIR="${BDAG_RAWDATADIR_SIDECAR_SOURCE:-$DEFAULT_NODE_DIR/$NETWORK}"
SIDECAR_DIR="${BDAG_RAWDATADIR_SIDECAR_DIR:-$PROJECT_ROOT/data-restore/btrfs-checkpoints/rawdatadir-sidecar/$NETWORK}"
LOCK_FILE="${BDAG_RAWDATADIR_SIDECAR_LOCK:-$PROJECT_ROOT/ops/runtime/rawdatadir-sidecar.lock}"
LOG_FILE="${BDAG_RAWDATADIR_SIDECAR_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-sidecar-$(date +%Y%m%d).log}"
STATUS_FILE="${BDAG_RAWDATADIR_SIDECAR_SAFETY_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-sidecar-safety-status.json}"
SAFE_STATUS_FILE="${BDAG_RAWDATADIR_SIDECAR_SAFE_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-sidecar-safe-status.json}"
DELETE_MODE="${BDAG_RAWDATADIR_SIDECAR_DELETE:-1}"
BWLIMIT="${BDAG_RAWDATADIR_SIDECAR_RSYNC_BWLIMIT:-4096}"
DELAY_UPDATES="${BDAG_RAWDATADIR_SIDECAR_DELAY_UPDATES:-0}"
USE_SUDO="${BDAG_RAWDATADIR_SIDECAR_USE_SUDO:-auto}"
SIDECAR_MODE="${BDAG_RAWDATADIR_SIDECAR_MODE:-auto}"
FINAL_STOPPED_SYNC="${BDAG_RAWDATADIR_SIDECAR_FINAL_STOPPED_SYNC:-0}"
CONTENT_MODE="${BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE:-auto}"
CONTENT_SCRIPT="$PROJECT_ROOT/ops/seal_rawdatadir_sidecar_content.py"
OPEN_RESTORE_ENABLED="${BDAG_RAWDATADIR_OPEN_SIDECAR_ENABLED:-1}"
OPEN_RESTORE_BASE="${BDAG_RAWDATADIR_OPEN_SIDECAR_BASE:-$PROJECT_ROOT/data-restore/btrfs-checkpoints/rawdatadir-sidecar-open/$NETWORK}"
OPEN_RESTORE_KEEP="${BDAG_RAWDATADIR_OPEN_SIDECAR_KEEP:-12}"
LOCAL_SIDECAR_COPY="${BDAG_RAWDATADIR_LOCAL_SIDECAR_COPY:-1}"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")" "$SIDECAR_DIR"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] raw datadir sidecar sync already running" | tee -a "$LOG_FILE"
  exit 0
}

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

maintenance_backoff_reason() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PROJECT_ROOT/ops" BDAG_PROJECT_ROOT="$PROJECT_ROOT" python3 - "$1" <<'PY'
import sys

from pool_ops import background_maintenance_decision, collect_status_cached

decision = background_maintenance_decision(sys.argv[1], collect_status_cached(include_logs=False))
if not decision.get("allowed", True):
    print("; ".join(str(item) for item in decision.get("reasons", []) if item))
PY
}

run_low_priority() {
  local command=("$@")
  if command -v ionice >/dev/null 2>&1; then
    command=(ionice -c3 "${command[@]}")
  fi
  if command -v nice >/dev/null 2>&1; then
    command=(nice -n 19 "${command[@]}")
  fi
  "${command[@]}"
}

append_seal_env_if_set() {
  local key="$1"
  local value="${!key:-}"
  if [[ -n "$value" ]]; then
    seal_env+=("$key=$value")
  fi
}

create_open_restore_point() {
  case "${OPEN_RESTORE_ENABLED,,}" in
    0|false|no|off|disabled)
      log "open sidecar restore-point preservation disabled by BDAG_RAWDATADIR_OPEN_SIDECAR_ENABLED=$OPEN_RESTORE_ENABLED"
      return 0
      ;;
  esac
  if [[ ! -d "$SIDECAR_DIR/BdagChain" || ! -f "$SIDECAR_DIR/BdagChain/CURRENT" ]]; then
    log "open sidecar restore point skipped: current sidecar is not yet usable"
    return 0
  fi
  local stamp tmp target current_manifest
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="$OPEN_RESTORE_BASE/$stamp"
  tmp="$target.tmp.$$"
  mkdir -p "$OPEN_RESTORE_BASE"
  local copy_status=0
  mkdir "$tmp" || {
    log "open sidecar restore point preservation failed: could not create $tmp"
    return 0
  }
  if [[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo -n cp -al "$SIDECAR_DIR/." "$tmp/" || copy_status=$?
  else
    cp -al "$SIDECAR_DIR/." "$tmp/" || copy_status=$?
  fi
  if [[ "$copy_status" -eq 0 ]] &&
    [[ -d "$tmp/BdagChain" && -f "$tmp/BdagChain/CURRENT" ]] &&
    current_manifest="$(tr -d '\r\n' < "$SIDECAR_DIR/BdagChain/CURRENT" 2>/dev/null || true)" &&
    cat > "$tmp/open-sidecar-restore-point.json" <<EOF
{
  "document_type": "bdag_open_sidecar_restore_point_v1",
  "generated_at": "$(date -Is)",
  "network": "$NETWORK",
  "source_sidecar_dir": "$SIDECAR_DIR",
  "restore_point_dir": "$target",
  "bdagchain_current": "$current_manifest",
  "policy": "Hard-linked open restore point captured before the mutable sidecar is refreshed. Consensus validity must still be checked before restore."
}
EOF
    mv "$tmp" "$target"; then
    log "open sidecar restore point preserved: $target"
  else
    rm -rf "$tmp" 2>/dev/null || {
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo -n rm -rf "$tmp" || true
      fi
    }
    log "open sidecar restore point preservation failed; copy_status=$copy_status; continuing with sidecar refresh"
  fi
  if [[ "$OPEN_RESTORE_KEEP" =~ ^[0-9]+$ && "$OPEN_RESTORE_KEEP" -gt 0 ]]; then
    find "$OPEN_RESTORE_BASE" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -printf '%T@ %p\n' 2>/dev/null |
      sort -nr |
      awk -v keep="$OPEN_RESTORE_KEEP" 'NR > keep {sub(/^[^ ]+ /, ""); print}' |
      while IFS= read -r stale; do
        rm -rf "$stale"
        log "pruned old open sidecar restore point: $stale"
      done
  fi
}

sidecar_safety_reasons() {
  local status_file="$1"
  python3 - "$status_file" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("status_unavailable")
    raise SystemExit(0)
for reason in payload.get("reasons") or []:
    print(str(reason))
PY
}

local_sidecar_copy_can_ignore_reasons() {
  case "${LOCAL_SIDECAR_COPY,,}" in
    0|false|no|off|disabled)
      return 1
      ;;
  esac
  local reason saw_reason=0
  while IFS= read -r reason; do
    [[ -n "$reason" ]] || continue
    saw_reason=1
    case "$reason" in
      sidecar_mode_disabled)
        ;;
      *)
        return 1
        ;;
    esac
  done < <(sidecar_safety_reasons "$STATUS_FILE")
  [[ "$saw_reason" -eq 1 ]]
}

source_datadir_exists() {
  if [[ -d "$SOURCE_DIR/BdagChain" ]]; then
    return 0
  fi
  case "${USE_SUDO,,}" in
    1|true|yes|on|auto)
      command -v sudo >/dev/null 2>&1 && sudo -n test -d "$SOURCE_DIR/BdagChain"
      return
      ;;
  esac
  return 1
}

if ! source_datadir_exists; then
  log "source dir does not look like a $NETWORK datadir: $SOURCE_DIR"
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  log "rsync is required for raw datadir sidecar sync"
  exit 1
fi
case "${SIDECAR_MODE,,}" in
  0|false|no|off|disabled)
    log "raw datadir sidecar sync disabled by BDAG_RAWDATADIR_SIDECAR_MODE=$SIDECAR_MODE"
    exit 0
    ;;
esac
case "${FINAL_STOPPED_SYNC,,}" in
  1|true|yes|on)
    log "final stopped sidecar sync: skipping live-status background maintenance gate"
    ;;
  *)
    if ! pressure_reason="$(maintenance_backoff_reason rawdatadir_sidecar 2>>"$LOG_FILE")"; then
      log "skipping raw datadir sidecar sync: background maintenance gate unavailable"
      exit 0
    fi
    if [[ -n "$pressure_reason" ]]; then
      log "skipping raw datadir sidecar sync: background maintenance backoff active: $pressure_reason"
      exit 0
    fi
    ;;
esac

safety_require_evm_reference_fresh="${BDAG_RAWDATADIR_SIDECAR_REQUIRE_EVM_REFERENCE_FRESH:-0}"
case "${FINAL_STOPPED_SYNC,,}" in
  1|true|yes|on)
    safety_require_evm_reference_fresh=0
    log "final stopped sidecar sync: enforcing storage/path safety without live EVM freshness"
    ;;
esac

# A sidecar refresh is not a public source/publish decision. Keep retrying the
# low-priority copy after mining pressure clears, but still refuse unsafe
# storage/topology conditions such as USB/removable paths or insufficient space.
if ! BDAG_RAWDATADIR_SIDECAR_MODE="$SIDECAR_MODE" \
  BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH="$safety_require_evm_reference_fresh" \
  "$PROJECT_ROOT/ops/rawdatadir_sidecar_safety.py" --status-file "$STATUS_FILE" >/dev/null; then
  if local_sidecar_copy_can_ignore_reasons; then
    log "raw datadir sidecar local copy continuing despite sidecar-only safety reason: sidecar_mode_disabled"
  else
    log "raw datadir sidecar safety check deferred sync; see $STATUS_FILE"
    exit 0
  fi
fi

rsync_args=(
  -a
  --numeric-ids
  --one-file-system
  --partial
  --partial-dir=.rsync-partial
  "--exclude=/network.key*"
  "--exclude=/bdageth/nodekey*"
  "--exclude=/bdageth/LOCK"
  "--exclude=/bdageth/chaindata/LOCK"
  "--exclude=/bdageth/nodes*"
  "--exclude=/keystore*"
  "--exclude=/bdageth/keystore*"
  "--exclude=/peerstore*"
  "--exclude=/nodes*"
  "--exclude=/bdageth/transactions.rlp"
  "--exclude=/.rsync-partial"
  "--exclude=/snap""shot.bd""snap"
  "--exclude=/artifact.manifest.json"
  "--exclude=/LOCK"
  "--exclude=/BdagChain/LOCK"
  "--exclude=*.ipc"
  "--exclude=*.sock"
)
case "${DELAY_UPDATES,,}" in
  1|true|yes|on)
    rsync_args+=(--delay-updates)
    ;;
esac
if [[ "$DELETE_MODE" == "1" ]]; then
  rsync_args+=(--delete --delete-excluded)
fi
if [[ -n "$BWLIMIT" ]]; then
  rsync_args+=(--bwlimit "$BWLIMIT")
fi

rsync_command=(rsync)
case "${USE_SUDO,,}" in
  1|true|yes|on)
    if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; then
      log "BDAG_RAWDATADIR_SIDECAR_USE_SUDO is enabled, but passwordless sudo is unavailable"
      exit 1
    fi
    rsync_command=(sudo -n rsync)
    ;;
  auto)
    if [[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      rsync_command=(sudo -n rsync)
    fi
    ;;
  0|false|no|off)
    ;;
  *)
    log "invalid BDAG_RAWDATADIR_SIDECAR_USE_SUDO=$USE_SUDO"
    exit 1
    ;;
esac

log "syncing raw datadir sidecar source=$SOURCE_DIR target=$SIDECAR_DIR"
create_open_restore_point
set +e
run_low_priority "${rsync_command[@]}" "${rsync_args[@]}" "$SOURCE_DIR/" "$SIDECAR_DIR/" 2>&1 | tee -a "$LOG_FILE"
rsync_status="${PIPESTATUS[0]}"
set -e
case "$rsync_status" in
  0)
    ;;
  24)
    log "raw datadir sidecar sync saw vanished hot-db files; continuing with best-effort hot sidecar seal"
    ;;
  *)
    log "raw datadir sidecar sync failed rc=$rsync_status"
    exit "$rsync_status"
    ;;
esac
log "raw datadir sidecar sync complete"
if "$PROJECT_ROOT/ops/verify-rawdatadir-sidecar.py" \
  --source-dir "$SOURCE_DIR" \
  --sidecar-dir "$SIDECAR_DIR" \
  --status-file "$SAFE_STATUS_FILE" \
  --json >>"$LOG_FILE" 2>&1; then
  log "raw datadir sidecar safe check passed; status=$SAFE_STATUS_FILE"
else
  log "raw datadir sidecar safe check failed; status=$SAFE_STATUS_FILE"
  exit 1
fi
python3 - "$STATUS_FILE" "$SOURCE_DIR" "$SIDECAR_DIR" "$SAFE_STATUS_FILE" "$FINAL_STOPPED_SYNC" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
payload = {}
if path.exists():
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
payload.update({
    "last_sidecar_sync_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "last_sidecar_source": sys.argv[2],
    "last_sidecar_dir": sys.argv[3],
    "last_sidecar_safe_status": sys.argv[4],
    "last_sidecar_final_stopped_sync": sys.argv[5].strip().lower() in {"1", "true", "yes", "on"},
})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

case "${CONTENT_MODE,,}" in
  0|false|no|off|disabled)
    log "raw datadir sidecar content sealing disabled by BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE=$CONTENT_MODE"
    ;;
  *)
    if [[ -x "$CONTENT_SCRIPT" ]]; then
      case "${FINAL_STOPPED_SYNC,,}" in
        1|true|yes|on)
          log "final stopped sidecar sync: skipping content-seal live pressure gate"
          ;;
        *)
          if ! seal_pressure_reason="$(maintenance_backoff_reason rawdatadir_content_seal 2>>"$LOG_FILE")"; then
            log "deferring raw datadir sidecar content sealing: background maintenance gate unavailable"
            exit 0
          fi
          if [[ -n "$seal_pressure_reason" ]]; then
            log "deferring raw datadir sidecar content sealing: background maintenance backoff active: $seal_pressure_reason"
            exit 0
          fi
          ;;
      esac
      log "sealing raw datadir sidecar content artifact"
      seal_env=(
        "BDAG_PROJECT_ROOT=$PROJECT_ROOT"
        "BDAG_ENV_FILE=${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}"
        "BDAG_RAWDATADIR_NETWORK=$NETWORK"
        "BDAG_RAWDATADIR_SIDECAR_DIR=$SIDECAR_DIR"
        "BDAG_RAWDATADIR_SIDECAR_SAFETY_STATUS=$STATUS_FILE"
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE=$CONTENT_MODE"
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_OWNER_UID=$(id -u)"
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_OWNER_GID=$(id -g)"
      )
      append_seal_env_if_set BDAG_RAWDATADIR_SIGNING_KEY_FILE
      append_seal_env_if_set BDAG_RAWDATADIR_SIGNING_KEY_ID
      append_seal_env_if_set BDAG_RAWDATADIR_SIGNING_KEY_HEX
      append_seal_env_if_set BDAG_RAWDATADIR_TRUSTED_SIGNERS
      append_seal_env_if_set BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER
      append_seal_env_if_set BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE
      append_seal_env_if_set BDAG_IPFS_SEGMENT_WRITER_ID
      case "${FINAL_STOPPED_SYNC,,}" in
        1|true|yes|on)
          seal_env+=("BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED=1")
          ;;
        *)
          append_seal_env_if_set BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED
          ;;
      esac
      if [[ "${rsync_command[0]}" == "sudo" ]]; then
        if ! run_low_priority sudo -n env "${seal_env[@]}" python3 "$CONTENT_SCRIPT" 2>&1 | tee -a "$LOG_FILE"; then
          log "raw datadir sidecar content sealing failed; see status file"
        fi
      else
        if ! run_low_priority env "${seal_env[@]}" python3 "$CONTENT_SCRIPT" 2>&1 | tee -a "$LOG_FILE"; then
          log "raw datadir sidecar content sealing failed; see status file"
        fi
      fi
    else
      log "raw datadir sidecar content sealing skipped: missing $CONTENT_SCRIPT"
    fi
    ;;
esac
