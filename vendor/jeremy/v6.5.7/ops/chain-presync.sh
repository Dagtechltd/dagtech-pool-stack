#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SNAPSHOT_STAGE_ROOT="${BDAG_SNAPSHOT_STAGE_ROOT:-$PROJECT_ROOT/data-restore/.hourly-stage}"
LOCK_FILE="${BDAG_PRESYNC_LOCK:-$PROJECT_ROOT/ops/runtime/chain-presync.lock}"
STAGE_LOCK_FILE="${BDAG_SNAPSHOT_STAGE_LOCK:-$PROJECT_ROOT/ops/runtime/chain-snapshot-stage.lock}"
LOG_FILE="${BDAG_PRESYNC_LOG:-$PROJECT_ROOT/ops/runtime/logs/chain-presync.log}"
PRESYNC_BACKOFF_BLOCKS="${BDAG_PRESYNC_BACKOFF_BLOCKS:-0}"
PRESYNC_MAX_BLOCK_LAG="${BDAG_PRESYNC_MAX_BLOCK_LAG:-}"
PRESYNC_ACCEPTABLE_BLOCK_LAG_FLOOR="${BDAG_PRESYNC_ACCEPTABLE_BLOCK_LAG_FLOOR:-${BDAG_SYNC_ACCEPTABLE_STARTUP_LAG_BLOCKS:-4000}}"
PRESYNC_COPY_MINUTE_BLOCK_ALLOWANCE="${BDAG_PRESYNC_COPY_MINUTE_BLOCK_ALLOWANCE:-${BDAG_SYNC_COPY_MINUTE_BLOCK_ALLOWANCE:-4}}"
PRESYNC_LAST_COPY_SECONDS_FILE="${BDAG_PRESYNC_LAST_COPY_SECONDS_FILE:-$PROJECT_ROOT/ops/runtime/chain-presync-last-copy-seconds}"
PRESYNC_UNKNOWN_BACKOFF="${BDAG_PRESYNC_UNKNOWN_BACKOFF:-1}"
PRESYNC_STATE_FILE="${BDAG_PRESYNC_STATE_FILE:-$PROJECT_ROOT/ops/runtime/chain-presync-state}"
PRESYNC_SOURCE_DIR="${BDAG_PRESYNC_SOURCE_DIR:-${BDAG_NODE_DATA_DIR:-$PROJECT_ROOT/data/node}}"

source "$PROJECT_ROOT/ops/chain-snapshot-common.sh"
source "$PROJECT_ROOT/ops/sync-startup-lag-policy.sh"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$STAGE_LOCK_FILE")" "$(dirname "$LOG_FILE")" "$SNAPSHOT_STAGE_ROOT"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(date -Is)] pre-sync already running" >> "$LOG_FILE"
  exit 0
fi

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

presync_effective_max_block_lag() {
  if [[ "$PRESYNC_MAX_BLOCK_LAG" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$PRESYNC_MAX_BLOCK_LAG"
    return 0
  fi
  bdag_sync_lag_threshold_blocks \
    "$PRESYNC_ACCEPTABLE_BLOCK_LAG_FLOOR" \
    "$PRESYNC_COPY_MINUTE_BLOCK_ALLOWANCE" \
    "$PRESYNC_LAST_COPY_SECONDS_FILE"
}

exec 8>"$STAGE_LOCK_FILE"
if ! flock -n 8; then
  log "snapshot staging is busy; skipping this pre-sync run"
  exit 0
fi

read -r sync_status sync_remaining sync_unknown sync_block_lag < <(snapshot_sync_summary "$PROJECT_ROOT" 2>>"$LOG_FILE" || printf 'unknown -1 1 -1\n')
effective_max_block_lag="$(presync_effective_max_block_lag)"
if [[ "$PRESYNC_UNKNOWN_BACKOFF" == "1" && "$sync_unknown" =~ ^[0-9]+$ && "$sync_unknown" -gt 0 ]]; then
  log "skipping pre-sync: sync state unknown for $sync_unknown node(s), preserving node resources"
  exit 0
fi
if [[ "$sync_remaining" =~ ^[0-9]+$ ]] && (( sync_remaining > PRESYNC_BACKOFF_BLOCKS )); then
  log "skipping pre-sync: chain catch-up has priority status=$sync_status max_remaining=${sync_remaining} threshold=$PRESYNC_BACKOFF_BLOCKS unknown_nodes=$sync_unknown"
  exit 0
fi
if [[ "$sync_block_lag" =~ ^[0-9]+$ ]] && (( sync_block_lag > effective_max_block_lag )); then
  log "skipping pre-sync: node block lag has priority block_lag=${sync_block_lag} threshold=$effective_max_block_lag policy_floor=$PRESYNC_ACCEPTABLE_BLOCK_LAG_FLOOR copy_minute_allowance=$PRESYNC_COPY_MINUTE_BLOCK_ALLOWANCE"
  exit 0
fi

sync_node() {
  local source_dir="$PRESYNC_SOURCE_DIR"
  local stage_dir="$SNAPSHOT_STAGE_ROOT/node"

  if [[ ! -d "$source_dir" ]]; then
    log "skipping node: source missing: $source_dir"
    return 0
  fi

  log "pre-syncing node with acceptable block lag threshold $(presync_effective_max_block_lag)"
  local started_at finished_at duration_seconds
  started_at="$(date +%s)"
  if snapshot_rsync_node "$source_dir" "$stage_dir" >> "$LOG_FILE" 2>&1; then
    finished_at="$(date +%s)"
    duration_seconds=$((finished_at - started_at))
    bdag_sync_record_copy_seconds "$PRESYNC_LAST_COPY_SECONDS_FILE" "$duration_seconds"
    log "pre-sync complete for node"
  else
    finished_at="$(date +%s)"
    duration_seconds=$((finished_at - started_at))
    bdag_sync_record_copy_seconds "$PRESYNC_LAST_COPY_SECONDS_FILE" "$duration_seconds"
    log "pre-sync partial for node; live database changed while copying"
  fi
  log "pre-sync copy duration for node was ${duration_seconds}s; next acceptable lag threshold $(presync_effective_max_block_lag) block(s)"
}

sync_node
printf '%s\n' "node" > "$PRESYNC_STATE_FILE"
