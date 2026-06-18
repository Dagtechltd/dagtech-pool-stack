#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SNAPSHOT_DIR="${BDAG_SNAPSHOT_DIR:-$PROJECT_ROOT/data-restore/hourly}"
SNAPSHOT_STAGE_ROOT="${BDAG_SNAPSHOT_STAGE_ROOT:-$PROJECT_ROOT/data-restore/.hourly-stage}"
SNAPSHOT_RETAIN="${BDAG_SNAPSHOT_RETAIN:-12}"
LOCK_FILE="${BDAG_SNAPSHOT_LOCK:-$PROJECT_ROOT/ops/runtime/hourly-chain-snapshot.lock}"
STAGE_LOCK_FILE="${BDAG_SNAPSHOT_STAGE_LOCK:-$PROJECT_ROOT/ops/runtime/chain-snapshot-stage.lock}"
LOG_FILE="${BDAG_SNAPSHOT_LOG:-$PROJECT_ROOT/ops/runtime/logs/hourly-chain-snapshot.log}"
STATE_FILE="${BDAG_SNAPSHOT_STATE:-$PROJECT_ROOT/ops/runtime/hourly-chain-snapshot-state}"
SNAPSHOT_STOP_STATE_FILE="${BDAG_SNAPSHOT_STOP_STATE_FILE:-$PROJECT_ROOT/ops/runtime/snapshot-stop-state.json}"
ENV_FILE="${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}"
COMPOSE_FILE="${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}"
SNAPSHOT_BACKOFF_BLOCKS="${BDAG_SNAPSHOT_BACKOFF_BLOCKS:-0}"
SNAPSHOT_MAX_BLOCK_LAG="${BDAG_SNAPSHOT_MAX_BLOCK_LAG:-5}"
SNAPSHOT_UNKNOWN_BACKOFF="${BDAG_SNAPSHOT_UNKNOWN_BACKOFF:-1}"
SNAPSHOT_COMPRESS="${BDAG_SNAPSHOT_COMPRESS:-0}"
SNAPSHOT_AVOID_RPC_PRIMARY="${BDAG_SNAPSHOT_AVOID_RPC_PRIMARY:-1}"
SNAPSHOT_RPC_RECOVERY_SECONDS="${BDAG_SNAPSHOT_RPC_RECOVERY_SECONDS:-180}"
SNAPSHOT_FINAL_STOP_SYNC="${BDAG_SNAPSHOT_FINAL_STOP_SYNC:-0}"
SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS="${BDAG_SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS:-45}"
SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS="${BDAG_SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS:-2700}"
SNAPSHOT_SOURCE_DIR="${BDAG_SNAPSHOT_SOURCE_DIR:-${BDAG_NODE_DATA_DIR:-$PROJECT_ROOT/data/node}}"

source "$PROJECT_ROOT/ops/chain-snapshot-common.sh"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$STAGE_LOCK_FILE")" "$(dirname "$LOG_FILE")" "$SNAPSHOT_DIR" "$SNAPSHOT_STAGE_ROOT"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(date -Is)] hourly snapshot already running" >> "$LOG_FILE"
  exit 0
fi

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

write_snapshot_manifest() {
  local published_path="$1"
  local manifest_path="$2"
  local latest_manifest_link="$3"
  local final_stop_sync="$4"
  local source_node_service="$5"
  local source_node_key="$6"
  local source_node_dir="$7"

  if PYTHONPATH="$PROJECT_ROOT/ops" python3 - "$PROJECT_ROOT" "$published_path" "$source_node_service" "$source_node_key" "$source_node_dir" "$final_stop_sync" > "$manifest_path.tmp" <<'PY'
import json
import sys
import time
from pathlib import Path

project_root = Path(sys.argv[1])
published_path = Path(sys.argv[2])
source_node_service = sys.argv[3]
source_node_key = sys.argv[4]
source_node_dir = sys.argv[5]
final_stop_sync = sys.argv[6] == "1"

try:
    from pool_ops import collect_status_cached, now_iso
    status = collect_status_cached(include_logs=False)
    generated_at = now_iso()
except Exception as exc:  # noqa: BLE001 - snapshot publication must not fail because metadata collection failed.
    status = {"metadata_error": str(exc)}
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

nodes = status.get("nodes") if isinstance(status, dict) else {}
node_info = nodes.get(source_node_service, {}) if isinstance(nodes, dict) else {}
sync_progress = status.get("sync_progress") if isinstance(status, dict) else {}
payload = {
    "document_type": "bdag_chain_restore_manifest",
    "generated_at": generated_at,
    "project_root": str(project_root),
    "published_path": str(published_path),
    "source_node_service": source_node_service,
    "source_node_key": source_node_key,
    "source_node_dir": source_node_dir,
    "consistent_final_stopped_sync": final_stop_sync,
    "published_from_online_warm_copy": not final_stop_sync,
    "stack_overall": status.get("overall") if isinstance(status, dict) else None,
    "sync_status": sync_progress.get("status") if isinstance(sync_progress, dict) else None,
    "sync_remaining_blocks": sync_progress.get("remaining_blocks") if isinstance(sync_progress, dict) else None,
    "source_latest_block": node_info.get("latest_block") if isinstance(node_info, dict) else None,
    "source_importing": node_info.get("importing") if isinstance(node_info, dict) else None,
    "source_last_import_at": node_info.get("last_import_at") if isinstance(node_info, dict) else None,
    "source_template_probe_failing": node_info.get("template_probe_failing") if isinstance(node_info, dict) else None,
    "node_heights": {
        name: info.get("latest_block")
        for name, info in nodes.items()
        if isinstance(info, dict)
    } if isinstance(nodes, dict) else {},
    "restore_guidance": "Prefer the newest manifest with stack_overall=ok and sync_status=synced. Preserve node identity files when restoring chain data.",
}
print(json.dumps(payload, indent=2, sort_keys=True))
PY
  then
    mv "$manifest_path.tmp" "$manifest_path"
    ln -sfn "$latest_manifest_link" "$PROJECT_ROOT/data-restore/latest-hourly.manifest.json"
    log "wrote snapshot manifest: $manifest_path"
  else
    rm -f "$manifest_path.tmp"
    log "warning: failed to write snapshot manifest for $published_path"
  fi
}

write_snapshot_stop_marker() {
  local event="$1"
  local epoch
  epoch="$(date -u +%s)"
  cat > "$SNAPSHOT_STOP_STATE_FILE" <<EOF
{"node":"$node_service","event":"$event","written_at":"$(date -Is)","written_epoch":$epoch,"recovery_seconds":$SNAPSHOT_RPC_RECOVERY_SECONDS}
EOF
}

exec 8>"$STAGE_LOCK_FILE"
log "waiting for exclusive snapshot staging lock"
flock 8

pressure_reason="$(maintenance_backoff_reason hourly_snapshot 2>>"$LOG_FILE" || true)"
if [[ -n "$pressure_reason" ]]; then
  log "skipping hourly snapshot: background maintenance backoff active: $pressure_reason"
  exit 0
fi

read -r sync_status sync_remaining sync_unknown sync_block_lag < <(snapshot_sync_summary "$PROJECT_ROOT" 2>>"$LOG_FILE" || printf 'unknown -1 1 -1\n')
if [[ "$SNAPSHOT_UNKNOWN_BACKOFF" == "1" && "$sync_unknown" =~ ^[0-9]+$ && "$sync_unknown" -gt 0 ]]; then
  log "skipping hourly snapshot: sync state unknown, preserving node resources"
  exit 0
fi
if [[ "$sync_remaining" =~ ^[0-9]+$ ]] && (( sync_remaining > SNAPSHOT_BACKOFF_BLOCKS )); then
  log "skipping hourly snapshot: chain catch-up has priority status=$sync_status max_remaining=${sync_remaining} threshold=$SNAPSHOT_BACKOFF_BLOCKS"
  exit 0
fi
if [[ "$sync_block_lag" =~ ^[0-9]+$ ]] && (( sync_block_lag > SNAPSHOT_MAX_BLOCK_LAG )); then
  log "skipping hourly snapshot: node block lag has priority block_lag=${sync_block_lag} threshold=$SNAPSHOT_MAX_BLOCK_LAG"
  exit 0
fi

cleanup_stale_temps() {
  find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name '.bdag-node-hourly-*.tar.gz.tmp' -mmin +30 -delete
  find "$SNAPSHOT_DIR" -maxdepth 1 -type d -name '.bdag-node-hourly-*.tmp' -mmin +30 -exec rm -rf {} +
}

prune_orphan_snapshot_manifests() {
  local manifest base
  find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name 'bdag-node-hourly-*.manifest.json' -print0 |
    while IFS= read -r -d '' manifest; do
      base="${manifest%.manifest.json}"
      if [[ ! -d "$base" && ! -f "$base.tar.gz" ]]; then
        rm -f "$manifest"
      fi
    done
}

prune_directory_snapshots() {
  local old_names=()
  mapfile -t old_names < <(
    find "$SNAPSHOT_DIR" -maxdepth 1 -type d -name 'bdag-node-hourly-*' -printf '%f\n' |
      sed -E 's/^(.*-hourly-)([0-9]{8}T[0-9]{6}Z)(.*)$/\2 \0/' |
      sort -r |
      awk -v keep="$SNAPSHOT_RETAIN" 'NR > keep {print (NF > 1 ? $2 : $1)}'
  )
  if (( ${#old_names[@]} > 0 )); then
    local name
    for name in "${old_names[@]}"; do
      rm -rf "$SNAPSHOT_DIR/$name"
      rm -f "$SNAPSHOT_DIR/$name.manifest.json"
    done
  fi
  prune_orphan_snapshot_manifests
}

prune_compressed_snapshots() {
  local old_names=()
  mapfile -t old_names < <(
    find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name 'bdag-node-hourly-*.tar.gz' -printf '%f\n' |
      sed 's/\.tar\.gz$//' |
      sed -E 's/^(.*-hourly-)([0-9]{8}T[0-9]{6}Z)(.*)$/\2 \0/' |
      sort -r |
      awk -v keep="$SNAPSHOT_RETAIN" 'NR > keep {print (NF > 1 ? $2 : $1)}'
  )
  if (( ${#old_names[@]} > 0 )); then
    local name
    for name in "${old_names[@]}"; do
      rm -f "$SNAPSHOT_DIR/$name.tar.gz"
      rm -f "$SNAPSHOT_DIR/$name.tar.gz.manifest.json"
      rm -f "$SNAPSHOT_DIR/$name.manifest.json"
    done
  fi
  prune_orphan_snapshot_manifests
}

compose() {
  if [[ -f "$ENV_FILE" ]]; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

node_service="node"
SNAPSHOT_SOURCE="$SNAPSHOT_SOURCE_DIR"
SNAPSHOT_STAGE="$SNAPSHOT_STAGE_ROOT/node"
mkdir -p "$SNAPSHOT_STAGE"

node_stopped=0
restart_node_if_needed() {
  if [[ "$node_stopped" == "1" ]]; then
    log "restarting $node_service after interrupted snapshot"
    compose start "$node_service" >> "$LOG_FILE" 2>&1 || true
    write_snapshot_stop_marker "restarted-after-interrupted-snapshot"
  fi
}
trap restart_node_if_needed EXIT

bounded_final_sync() {
  local source_dir="$1"
  local stage_dir="$2"

  if command -v timeout >/dev/null 2>&1 && [[ "$SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && (( SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS > 0 )); then
    export BDAG_SNAPSHOT_RSYNC_BWLIMIT_KB
    export -f run_low_priority snapshot_rsync_node
    timeout --kill-after=10s "${SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS}s" \
      bash -c 'snapshot_rsync_node "$1" "$2"' _ "$source_dir" "$stage_dir"
  else
    snapshot_rsync_node "$source_dir" "$stage_dir"
  fi
}

bounded_warm_sync() {
  local source_dir="$1"
  local stage_dir="$2"

  if command -v timeout >/dev/null 2>&1 && [[ "$SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && (( SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS > 0 )); then
    export BDAG_SNAPSHOT_RSYNC_BWLIMIT_KB
    export -f run_low_priority snapshot_rsync_node
    timeout --kill-after=10s "${SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS}s" \
      bash -c 'snapshot_rsync_node "$1" "$2"' _ "$source_dir" "$stage_dir"
  else
    snapshot_rsync_node "$source_dir" "$stage_dir"
  fi
}

if [[ ! -d "$SNAPSHOT_SOURCE" ]]; then
  log "snapshot source missing: $SNAPSHOT_SOURCE"
  exit 1
fi
printf '%s\n' "node" > "$STATE_FILE"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot_name="bdag-node-hourly-$stamp"
snapshot_tmp="$SNAPSHOT_DIR/.$snapshot_name.tmp"
snapshot_path="$SNAPSHOT_DIR/$snapshot_name"
archive_name="$snapshot_name.tar.gz"
archive_tmp="$SNAPSHOT_DIR/.$archive_name.tmp"
archive_path="$SNAPSHOT_DIR/$archive_name"

if [[ "$SNAPSHOT_COMPRESS" == "1" ]]; then
  log "starting hourly chain snapshot for $node_service: $archive_path"
else
  log "starting hourly chain snapshot for $node_service: $snapshot_path"
fi
cleanup_stale_temps
log "refreshing warm copy for node while stack remains online"
warm_copy_ok=0
if bounded_warm_sync "$SNAPSHOT_SOURCE" "$SNAPSHOT_STAGE" >> "$LOG_FILE" 2>&1; then
  warm_copy_ok=1
  log "warm copy complete for node"
else
  log "warm copy partial for node; will only publish if final stopped sync succeeds"
fi

pressure_reason="$(maintenance_backoff_reason hourly_snapshot_final_sync 2>>"$LOG_FILE" || true)"
if [[ -n "$pressure_reason" ]]; then
  log "skipping final stopped sync: background maintenance backoff active: $pressure_reason"
  exit 0
fi

read -r sync_status sync_remaining sync_unknown sync_block_lag < <(snapshot_sync_summary "$PROJECT_ROOT" 2>>"$LOG_FILE" || printf 'unknown -1 1 -1\n')
if [[ "$SNAPSHOT_UNKNOWN_BACKOFF" == "1" && "$sync_unknown" =~ ^[0-9]+$ && "$sync_unknown" -gt 0 ]]; then
  log "skipping final stopped sync: sync state became unknown"
  exit 0
fi
if [[ "$sync_remaining" =~ ^[0-9]+$ ]] && (( sync_remaining > SNAPSHOT_BACKOFF_BLOCKS )); then
  log "skipping final stopped sync: chain catch-up has priority status=$sync_status max_remaining=${sync_remaining} threshold=$SNAPSHOT_BACKOFF_BLOCKS"
  exit 0
fi
if [[ "$sync_block_lag" =~ ^[0-9]+$ ]] && (( sync_block_lag > SNAPSHOT_MAX_BLOCK_LAG )); then
  log "skipping final stopped sync: node block lag has priority block_lag=${sync_block_lag} threshold=$SNAPSHOT_MAX_BLOCK_LAG"
  exit 0
fi

if [[ "$SNAPSHOT_FINAL_STOP_SYNC" == "1" ]]; then
  log "stopping only $node_service for final consistent sync"
  write_snapshot_stop_marker "stopping-for-final-sync"
  compose stop "$node_service" >> "$LOG_FILE" 2>&1
  node_stopped=1

  log "final sync while $node_service is stopped, timeout=${SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS}s"
  if ! bounded_final_sync "$SNAPSHOT_SOURCE" "$SNAPSHOT_STAGE" >> "$LOG_FILE" 2>&1; then
    log "final stopped sync failed or timed out; restarting $node_service and skipping snapshot publish"
    compose start "$node_service" >> "$LOG_FILE" 2>&1 || true
    write_snapshot_stop_marker "started-after-final-sync-failed"
    node_stopped=0
    exit 1
  fi

  log "starting $node_service before publishing restore point"
  compose start "$node_service" >> "$LOG_FILE" 2>&1
  write_snapshot_stop_marker "started-after-final-sync"
  node_stopped=0
else
  if [[ "$warm_copy_ok" != "1" ]]; then
    log "not publishing online warm copy because warm rsync did not complete successfully"
    exit 1
  fi
  log "skipping stopped final sync because BDAG_SNAPSHOT_FINAL_STOP_SYNC=${SNAPSHOT_FINAL_STOP_SYNC}; publishing online warm copy"
  write_snapshot_stop_marker "published-online-warm-copy"
fi

if [[ "$SNAPSHOT_COMPRESS" == "1" ]]; then
  log "compressing staged snapshot"
  run_low_priority tar -C "$SNAPSHOT_STAGE" -czf "$archive_tmp" .
  mv "$archive_tmp" "$archive_path"
  ln -sfn "hourly/$archive_name" "$PROJECT_ROOT/data-restore/latest-hourly.tar.gz"
  write_snapshot_manifest "$archive_path" "$archive_path.manifest.json" "hourly/$archive_name.manifest.json" "$SNAPSHOT_FINAL_STOP_SYNC" "$node_service" "node" "$SNAPSHOT_SOURCE"
  log "pruning old compressed hourly snapshots, keeping $SNAPSHOT_RETAIN"
  prune_compressed_snapshots
  log "hourly chain snapshot complete: $archive_path"
else
  log "publishing hardlinked restore directory without compression"
  rm -rf "$snapshot_tmp"
  mkdir -p "$snapshot_tmp"
  run_low_priority cp -al "$SNAPSHOT_STAGE"/. "$snapshot_tmp"/
  mv "$snapshot_tmp" "$snapshot_path"
  ln -sfn "hourly/$snapshot_name" "$PROJECT_ROOT/data-restore/latest-hourly"
  write_snapshot_manifest "$snapshot_path" "$snapshot_path.manifest.json" "hourly/$snapshot_name.manifest.json" "$SNAPSHOT_FINAL_STOP_SYNC" "$node_service" "node" "$SNAPSHOT_SOURCE"

  log "pruning old directory hourly snapshots, keeping $SNAPSHOT_RETAIN"
  prune_directory_snapshots
  log "hourly chain snapshot complete: $snapshot_path"
fi
