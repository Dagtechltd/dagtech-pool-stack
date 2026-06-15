#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SNAPSHOT_STAGE_ROOT="${BDAG_SNAPSHOT_STAGE_ROOT:-$PROJECT_ROOT/data-restore/.hourly-stage}"
LOCK_FILE="${BDAG_PRESYNC_LOCK:-$PROJECT_ROOT/ops/runtime/chain-presync.lock}"
STAGE_LOCK_FILE="${BDAG_SNAPSHOT_STAGE_LOCK:-$PROJECT_ROOT/ops/runtime/chain-snapshot-stage.lock}"
LOG_FILE="${BDAG_PRESYNC_LOG:-$PROJECT_ROOT/ops/runtime/logs/chain-presync.log}"
PRESYNC_BACKOFF_BLOCKS="${BDAG_PRESYNC_BACKOFF_BLOCKS:-0}"
PRESYNC_MAX_BLOCK_LAG="${BDAG_PRESYNC_MAX_BLOCK_LAG:-5}"
PRESYNC_UNKNOWN_BACKOFF="${BDAG_PRESYNC_UNKNOWN_BACKOFF:-1}"
PRESYNC_ONE_NODE="${BDAG_PRESYNC_ONE_NODE:-1}"
PRESYNC_NODE_DIRS="${BDAG_PRESYNC_NODE_DIRS:-${BDAG_NODE_DATA_DIRS:-node1}}"
PRESYNC_STATE_FILE="${BDAG_PRESYNC_STATE_FILE:-$PROJECT_ROOT/ops/runtime/chain-presync-state}"

source "$PROJECT_ROOT/ops/chain-snapshot-common.sh"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$STAGE_LOCK_FILE")" "$(dirname "$LOG_FILE")" "$SNAPSHOT_STAGE_ROOT"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(date -Is)] pre-sync already running" >> "$LOG_FILE"
  exit 0
fi

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

exec 8>"$STAGE_LOCK_FILE"
if ! flock -n 8; then
  log "snapshot staging is busy; skipping this pre-sync run"
  exit 0
fi

read -r sync_status sync_remaining sync_unknown sync_block_lag < <(snapshot_sync_summary "$PROJECT_ROOT" 2>>"$LOG_FILE" || printf 'unknown -1 1 -1\n')
if [[ "$PRESYNC_UNKNOWN_BACKOFF" == "1" && "$sync_unknown" =~ ^[0-9]+$ && "$sync_unknown" -gt 0 ]]; then
  log "skipping pre-sync: sync state unknown for $sync_unknown node(s), preserving node resources"
  exit 0
fi
if [[ "$sync_remaining" =~ ^[0-9]+$ ]] && (( sync_remaining > PRESYNC_BACKOFF_BLOCKS )); then
  log "skipping pre-sync: chain catch-up has priority status=$sync_status max_remaining=${sync_remaining} threshold=$PRESYNC_BACKOFF_BLOCKS unknown_nodes=$sync_unknown"
  exit 0
fi
if [[ "$sync_block_lag" =~ ^[0-9]+$ ]] && (( sync_block_lag > PRESYNC_MAX_BLOCK_LAG )); then
  log "skipping pre-sync: node block lag has priority block_lag=${sync_block_lag} threshold=$PRESYNC_MAX_BLOCK_LAG"
  exit 0
fi

sync_node() {
  local node_dir="$1"
  local source_dir="$PROJECT_ROOT/data/$node_dir"
  local stage_dir="$SNAPSHOT_STAGE_ROOT/$node_dir"

  if [[ ! -d "$source_dir" ]]; then
    log "skipping $node_dir: source missing: $source_dir"
    return 0
  fi

  log "pre-syncing $node_dir"
  if snapshot_rsync_node "$source_dir" "$stage_dir" >> "$LOG_FILE" 2>&1; then
    log "pre-sync complete for $node_dir"
  else
    log "pre-sync partial for $node_dir; live database changed while copying"
  fi
}

configured_node_dirs() {
  local raw="$PRESYNC_NODE_DIRS"
  local item seen_node1=0 emitted=0
  raw="${raw//;/,}"
  IFS=',' read -ra items <<< "$raw"
  for item in "${items[@]}"; do
    item="${item//[[:space:]]/}"
    case "$item" in
      node1)
        if [[ "$seen_node1" == "0" ]]; then
          printf '%s\n' node1
          seen_node1=1
          emitted=1
        fi
        ;;
    esac
  done
  if [[ "$emitted" == "0" ]]; then
    printf '%s\n' node1
  fi
}

if [[ "$PRESYNC_ONE_NODE" == "1" ]]; then
  mapfile -t node_dirs < <(configured_node_dirs)
  previous="$(cat "$PRESYNC_STATE_FILE" 2>/dev/null || true)"
  next="${node_dirs[0]}"
  if ((${#node_dirs[@]} > 1)); then
    for index in "${!node_dirs[@]}"; do
      if [[ "${node_dirs[$index]}" == "$previous" ]]; then
        next="${node_dirs[$(((index + 1) % ${#node_dirs[@]}))]}"
        break
      fi
    done
  fi
  sync_node "$next"
  printf '%s\n' "$next" > "$PRESYNC_STATE_FILE"
else
  while IFS= read -r node_dir; do
    sync_node "$node_dir"
  done < <(configured_node_dirs)
fi
