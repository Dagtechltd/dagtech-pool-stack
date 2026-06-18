#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_ROOT="${BDAG_LIVE_RUNTIME_ROOT:-}"
BACKUP_ROOT="${BDAG_DEPLOY_BACKUP_ROOT:-/home/jeremy/blockdag-deploy-backups}"
RESTART_SERVICES="${BDAG_DEPLOY_RESTART_SERVICES:-bdag-dashboard.service bdag-watchdog.service}"
POST_DEPLOY_HEALTH_CHECK="${BDAG_DEPLOY_HEALTH_CHECK:-1}"
POST_DEPLOY_HEALTH_TIMEOUT="${BDAG_DEPLOY_HEALTH_TIMEOUT_SECONDS:-120}"
POST_DEPLOY_HEALTH_INTERVAL="${BDAG_DEPLOY_HEALTH_INTERVAL_SECONDS:-3}"
POST_DEPLOY_DASHBOARD_URL="${BDAG_DEPLOY_DASHBOARD_URL:-http://127.0.0.1:8088/api/status}"
POST_DEPLOY_ALLOWED_OVERALL="${BDAG_DEPLOY_ALLOWED_OVERALL:-ok syncing}"
POST_DEPLOY_DEFAULT_CONTAINERS="${BDAG_DEPLOY_CRITICAL_CONTAINERS:-}"
POST_DEPLOY_WATCHDOG_STATE="${BDAG_DEPLOY_WATCHDOG_STATE:-ops/runtime/watchdog-state.json}"
DRY_RUN=0
MARK_RUNTIME_COMPOSE=0
ROLLBACK_DIR=""
COMPOSE_BACKUP_BEFORE_MARK=""
FILES=(
  "AGENTS.md"
  "sql/pool-schema.sql"
  "docker-compose-miner.yml"
  "docker/entrypoint-nodeworker.sh"
  "docs/five-asic-template-conversion-guard.html"
  "docs/ipfs-content-sidecar.html"
  "docs/mining-resource-priority-policy.html"
  "docs/platform-adaptive-runtime.md"
  "docs/t430-appliance-hardening.md"
  "host/mining-appliance/bdag-runtime-priority.timer"
  "host/mining-appliance/bdag-node-child-guard"
  "ops/README.md"
  "ops/apply-mining-host-tuning.sh"
  "ops/automation_control.py"
  "ops/build-rawdatadir-artifact.sh"
  "ops/chain-state-self-heal.sh"
  "ops/config/stack-defaults.env"
  "ops/fastartifact_source_eligibility.py"
  "ops/fetch-rawdatadir-artifact.sh"
  "ops/maintain-rawdatadir-sidecar.sh"
  "ops/publish-rawdatadir-artifact.sh"
  "ops/verify-rawdatadir-sidecar.py"
  "ops/pool_ops.py"
  "ops/status_sampler.py"
  "ops/dashboard.py"
  "ops/deploy-live-runtime-update.sh"
  "ops/hourly-chain-snapshot.sh"
  "ops/incident_journal.py"
  "ops/incident_reporter.py"
  "ops/install-dashboard.sh"
  "ops/install-p2p-services.sh"
  "ops/latest_chain_candidate.py"
  "ops/node_child_guard.py"
  "ops/optimization_measurement.py"
  "ops/p2p_guard.py"
  "ops/release-install.sh"
  "ops/compose_migrations.py"
  "ops/stack_sentinel.py"
  "ops/sync_coordinator.py"
  "ops/tests/test_chain_rpc_resilience.py"
  "ops/tests/test_deployment_portability.py"
  "ops/tests/test_earnings_onchain_sources.py"
  "ops/tests/test_miner_retirement_identity.py"
  "ops/tests/test_mining_host_tuning.py"
  "ops/tests/test_no_miner_collect_status.py"
  "ops/tests/test_mining_appliance_preflight.py"
  "ops/tests/test_optimization_measurement.py"
  "ops/tests/test_compose_migrations.py"
  "ops/tests/test_status_sampler_mining_imperative.py"
  "ops/tests/test_stack_defaults.py"
  "ops/tests/test_stack_naming_coherence.py"
  "ops/tests/test_node_child_guard.py"
  "ops/tests/test_sync_coordinator_fast_catchup.py"
  "ops/tests/test_watchdog_miner_source_counts.py"
  "ops/update-local-peers.py"
  "ops/watchdog.py"
  "ops/systemd/user-bdag-chain-restore-guard.timer"
  "ops/systemd/user-bdag-chain-state-self-heal.service"
  "ops/systemd/user-bdag-hourly-snapshot.timer"
  "ops/systemd/user-bdag-incident-reporter.timer"
  "ops/systemd/user-bdag-rawdatadir-sidecar.service"
  "ops/systemd/user-bdag-rawdatadir-sidecar.timer"
  "ops/systemd/user-bdag-rawdatadir-source.service"
  "ops/systemd/user-bdag-rawdatadir-source.timer"
  "ops/systemd/user-bdag-mining-30min-guard.service"
  "ops/systemd/user-bdag-mining-30min-guard.timer"
  "ops/systemd/user-bdag-node-child-guard.timer"
  "ops/systemd/user-bdag-stack-sentinel.timer"
  "ops/systemd/bdag-dashboard.service"
  "ops/systemd/bdag-mining-host-tuning.service"
  "ops/systemd/bdag-mining-host-tuning.timer"
  "ops/systemd/bdag-status-sampler.service"
  "ops/systemd/bdag-watchdog.service"
  "ops/systemd/user-bdag-status-sampler.service"
  "ops/systemd/user-bdag-sync-coordinator.timer"
  "scripts/validate-rc-local.sh"
  "scripts/validate-stack-defaults.py"
  "scripts/install-mining-appliance-profile.sh"
  "scripts/mining-appliance-preflight.py"
  "scripts/verify-release-architecture.py"
  ".env.example"
  "README.md"
  "release-downloads/index.html"
)

usage() {
  cat <<'USAGE'
Deploy a safe dashboard/watchdog runtime update into an installed stack.

Usage:
  ops/deploy-live-runtime-update.sh --target DIR [options]
  ops/deploy-live-runtime-update.sh --rollback BACKUP_DIR --target DIR

Options:
  --source DIR              Source checkout. Default: parent of this ops dir.
  --target DIR              Installed runtime root to update.
  --file PATH               Add one source-relative path to the copy whitelist.
  --restart-services LIST   Space-separated user services to restart.
                            Default: bdag-dashboard.service bdag-watchdog.service
  --health-check            Wait for dashboard/watchdog/container health after restart (default).
  --no-health-check         Skip post-restart health wait.
  --mark-runtime-compose    Add the generated-runtime compose marker if missing.
  --backup-root DIR         Backup parent. Default: /home/jeremy/blockdag-deploy-backups
  --dry-run                 Print planned changes without copying or restarting.
  --rollback BACKUP_DIR     Restore files from a previous backup manifest.
  -h, --help                Show this help.

The script never copies .env, data/, ops/runtime, chain data,
or Docker images. It refuses runtime compose files with build/dockerfile entries.
USAGE
}

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'deploy-live-runtime-update failed: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE_ROOT="$(cd "${2:?--source requires a directory}" && pwd)"
      shift 2
      ;;
    --target)
      TARGET_ROOT="$(cd "${2:?--target requires a directory}" && pwd)"
      shift 2
      ;;
    --file)
      FILES+=("${2:?--file requires a source-relative path}")
      shift 2
      ;;
    --restart-services)
      RESTART_SERVICES="${2:-}"
      shift 2
      ;;
    --health-check)
      POST_DEPLOY_HEALTH_CHECK=1
      shift
      ;;
    --no-health-check)
      POST_DEPLOY_HEALTH_CHECK=0
      shift
      ;;
    --mark-runtime-compose)
      MARK_RUNTIME_COMPOSE=1
      shift
      ;;
    --backup-root)
      BACKUP_ROOT="${2:?--backup-root requires a directory}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --rollback)
      ROLLBACK_DIR="$(cd "${2:?--rollback requires a backup directory}" && pwd)"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ -n "$TARGET_ROOT" ]] || die "--target DIR is required"
[[ -d "$SOURCE_ROOT" ]] || die "source root not found: $SOURCE_ROOT"
[[ -d "$TARGET_ROOT" ]] || die "target root not found: $TARGET_ROOT"

normalize_file() {
  local rel="$1"
  [[ -n "$rel" ]] || die "empty relative path in whitelist"
  [[ "$rel" != /* ]] || die "whitelist path must be relative: $rel"
  [[ "$rel" != *".."* ]] || die "whitelist path must not contain '..': $rel"
  printf '%s\n' "$rel"
}

preflight_copy_contract() {
  local raw_rel rel src
  for raw_rel in "${FILES[@]}"; do
    rel="$(normalize_file "$raw_rel")"
    src="$SOURCE_ROOT/$rel"
    [[ -f "$src" ]] || die "source file missing: $rel"
    if [[ "$rel" == ".env" || "$rel" == data/* || "$rel" == ops/runtime* || "$rel" == chain-data/* ]]; then
      die "refusing unsafe live-runtime file path: $rel"
    fi
  done

  :
}

runtime_compose_guard() {
  local compose="$TARGET_ROOT/docker-compose.yml"
  [[ -f "$compose" ]] || die "missing target docker-compose.yml"
  if grep -Eq '^[[:space:]]*(build|dockerfile):' "$compose"; then
    die "target docker-compose.yml contains build/dockerfile entries; refusing dev compose"
  fi
  if ! grep -q '^# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1$' "$compose"; then
    if [[ "$MARK_RUNTIME_COMPOSE" -ne 1 ]]; then
      die "target compose lacks BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1; rerun with --mark-runtime-compose after confirming this is the generated runtime compose"
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      say "Would add generated-runtime compose marker to $compose"
    else
      local tmp
      tmp="$(mktemp)"
      COMPOSE_BACKUP_BEFORE_MARK="$(mktemp)"
      cp -a "$compose" "$COMPOSE_BACKUP_BEFORE_MARK"
      {
        printf '# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1\n'
        printf '# Generated Pi5 runtime compose. Do not replace with the source/dev compose file.\n'
        cat "$compose"
      } > "$tmp"
      mv "$tmp" "$compose"
    fi
  fi
}

run_source_validation() {
  say "Validating source checkout"
  (cd "$SOURCE_ROOT" && bash scripts/validate-rc-local.sh)
}

run_target_validation() {
  say "Validating live runtime target file set"
  [[ -f "$TARGET_ROOT/docker-compose.yml" ]] || die "missing target docker-compose.yml"
  [[ -f "$TARGET_ROOT/ops/dashboard.py" ]] || die "missing target ops/dashboard.py"
  [[ -f "$TARGET_ROOT/ops/watchdog.py" ]] || die "missing target ops/watchdog.py"
}

ensure_target_automation_control() {
  say "Ensuring automation-control gate exists"
  python3 "$TARGET_ROOT/ops/automation_control.py" ensure-normal \
    --owner "deploy-live-runtime-update" \
    --owner-unit "ops/deploy-live-runtime-update.sh" \
    --reason "Provision default automation control state for watchdog and sentinel repairs" \
    --correlation-id "live-runtime-update-${commit}-${stamp}"
}

target_env_value() {
  local name="$1"
  local env_file="$TARGET_ROOT/.env"
  [[ -f "$env_file" ]] || return 1
  python3 - "$env_file" "$name" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
name = sys.argv[2]
for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() == name:
        print(value.strip().strip('"').strip("'"))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

manifest_has_rel() {
  local rel="$1"
  [[ -f "$backup_dir/manifest.tsv" ]] || return 1
  awk -F '\t' -v rel="$rel" '$1 == rel { found = 1 } END { exit(found ? 0 : 1) }' "$backup_dir/manifest.tsv"
}

backup_target_file_once() {
  local rel="$1"
  if manifest_has_rel "$rel"; then
    return 0
  fi
  if [[ -e "$TARGET_ROOT/$rel" ]]; then
    mkdir -p "$backup_dir/files/$(dirname "$rel")"
    cp -a "$TARGET_ROOT/$rel" "$backup_dir/files/$rel"
    printf '%s\texisting\n' "$rel" >> "$backup_dir/manifest.tsv"
  else
    printf '%s\tabsent\n' "$rel" >> "$backup_dir/manifest.tsv"
  fi
}

migrate_runtime_compose() {
  local compose="$TARGET_ROOT/docker-compose.yml"
  [[ -f "$compose" ]] || die "missing target docker-compose.yml"
  local key
  local missing=0
  for key in \
    POOL_SUBMIT_STALE_BLOCK_CANDIDATES \
    POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED \
    POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD
  do
    if ! grep -q "${key}:" "$compose"; then
      missing=1
      break
    fi
  done
  if [[ "$missing" -eq 0 ]]; then
    return 0
  fi
  say "Migrating live runtime compose: pool submit hardening settings"
  backup_target_file_once "docker-compose.yml"
  python3 "$SOURCE_ROOT/ops/compose_migrations.py" --ensure-pool-submit-hardening "$compose"
}

post_deploy_critical_containers() {
  if [[ -n "$POST_DEPLOY_DEFAULT_CONTAINERS" ]]; then
    printf '%s\n' "$POST_DEPLOY_DEFAULT_CONTAINERS" | tr ',' ' '
    return
  fi
  local services
  services="$(target_env_value BDAG_STACK_SERVICES 2>/dev/null || true)"
  if [[ -n "$services" ]]; then
    printf '%s\n' "$services" | tr ',' ' '
    return
  fi
  printf '%s\n' "postgres node pool"
}

dashboard_api_ready() {
  python3 - "$POST_DEPLOY_DASHBOARD_URL" "$POST_DEPLOY_ALLOWED_OVERALL" <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1]
allowed = {item.strip() for item in sys.argv[2].replace(",", " ").split() if item.strip()}
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    print(f"dashboard API unavailable: {exc}")
    raise SystemExit(1)
overall = str(payload.get("overall") or "")
if allowed and overall not in allowed:
    print(f"dashboard overall={overall!r} not in {sorted(allowed)}")
    raise SystemExit(1)
print(f"dashboard overall={overall}")
PY
}

critical_containers_ready() {
  local failed=0
  local container
  for container in $(post_deploy_critical_containers); do
    [[ -n "$container" ]] || continue
    if [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" != "true" ]]; then
      printf 'container not running: %s\n' "$container"
      failed=1
    fi
  done
  [[ "$failed" -eq 0 ]]
}

watchdog_freshness_required() {
  [[ " $RESTART_SERVICES " == *" bdag-watchdog.service "* ]]
}

watchdog_state_fresh() {
  if ! watchdog_freshness_required; then
    printf 'watchdog freshness skipped: bdag-watchdog.service not restarted\n'
    return 0
  fi
  local state_path="$TARGET_ROOT/$POST_DEPLOY_WATCHDOG_STATE"
  python3 - "$state_path" "$1" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
start_epoch = int(sys.argv[2])
if not path.exists():
    print(f"watchdog state missing: {path}")
    raise SystemExit(1)
mtime = int(path.stat().st_mtime)
if mtime < start_epoch:
    print(f"watchdog state stale: mtime={mtime} start={start_epoch}")
    raise SystemExit(1)
print(f"watchdog state fresh: mtime={mtime}")
PY
}

validate_rollback_restored() {
  local manifest="$ROLLBACK_DIR/manifest.tsv"
  local failed=0 rel state
  while IFS=$'\t' read -r rel state; do
    [[ -n "$rel" ]] || continue
    case "$state" in
      existing)
        if [[ ! -f "$TARGET_ROOT/$rel" ]]; then
          printf 'rollback validation missing restored file: %s\n' "$rel" >&2
          failed=1
        elif ! cmp -s "$ROLLBACK_DIR/files/$rel" "$TARGET_ROOT/$rel"; then
          printf 'rollback validation content mismatch: %s\n' "$rel" >&2
          failed=1
        fi
        ;;
      absent)
        if [[ -e "$TARGET_ROOT/$rel" ]]; then
          printf 'rollback validation expected absent path: %s\n' "$rel" >&2
          failed=1
        fi
        ;;
      *)
        printf 'rollback validation invalid state for %s: %s\n' "$rel" "$state" >&2
        failed=1
        ;;
    esac
  done < "$manifest"
  [[ "$failed" -eq 0 ]]
}

post_deploy_health_check() {
  [[ "$POST_DEPLOY_HEALTH_CHECK" == "1" ]] || {
    warn "Post-deploy health check skipped by BDAG_DEPLOY_HEALTH_CHECK=0"
    return 0
  }
  local start_epoch="$1"
  local deadline=$((start_epoch + POST_DEPLOY_HEALTH_TIMEOUT))
  local now
  say "Waiting for post-deploy health: dashboard API, watchdog freshness, and critical containers"
  while true; do
    if dashboard_api_ready && critical_containers_ready && watchdog_state_fresh "$start_epoch"; then
      say "Post-deploy health check passed"
      return 0
    fi
    now="$(date +%s)"
    if (( now >= deadline )); then
      return 1
    fi
    sleep "$POST_DEPLOY_HEALTH_INTERVAL"
  done
}

rollback_from_backup() {
  local manifest="$ROLLBACK_DIR/manifest.tsv"
  [[ -f "$manifest" ]] || die "rollback manifest not found: $manifest"
  say "Rolling back from $ROLLBACK_DIR"
  while IFS=$'\t' read -r rel state; do
    [[ -n "$rel" ]] || continue
    case "$state" in
      existing)
        mkdir -p "$TARGET_ROOT/$(dirname "$rel")"
        cp -a "$ROLLBACK_DIR/files/$rel" "$TARGET_ROOT/$rel"
        ;;
      absent)
        rm -f "$TARGET_ROOT/$rel"
        ;;
      *)
        die "invalid rollback state for $rel: $state"
        ;;
    esac
  done < "$manifest"
  validate_rollback_restored || die "rollback file verification failed"
  say "Rollback complete"
}

if [[ -n "$ROLLBACK_DIR" ]]; then
  rollback_from_backup
  exit 0
fi

runtime_compose_guard
preflight_copy_contract
run_source_validation

stamp="$(date +%Y%m%d-%H%M%S)"
commit="$(git -C "$SOURCE_ROOT" rev-parse --short=12 HEAD 2>/dev/null || printf 'nogit')"
backup_dir="$BACKUP_ROOT/live-runtime-update-${commit}-${stamp}"

say "Preparing backup: $backup_dir"
if [[ "$DRY_RUN" -eq 0 ]]; then
  mkdir -p "$backup_dir/files"
fi

if [[ "$DRY_RUN" -eq 0 && -n "$COMPOSE_BACKUP_BEFORE_MARK" ]]; then
  mkdir -p "$backup_dir/files"
  cp -a "$COMPOSE_BACKUP_BEFORE_MARK" "$backup_dir/files/docker-compose.yml"
  printf '%s\texisting\n' "docker-compose.yml" >> "$backup_dir/manifest.tsv"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  migrate_runtime_compose
fi

for raw_rel in "${FILES[@]}"; do
  rel="$(normalize_file "$raw_rel")"
  src="$SOURCE_ROOT/$rel"
  dst="$TARGET_ROOT/$rel"
  [[ -f "$src" ]] || die "source file missing: $rel"
  if [[ "$rel" == ".env" || "$rel" == data/* || "$rel" == ops/runtime* || "$rel" == chain-data/* ]]; then
    die "refusing unsafe live-runtime file path: $rel"
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'would copy %s -> %s\n' "$src" "$dst"
    continue
  fi
  backup_target_file_once "$rel"
  mkdir -p "$(dirname "$dst")"
  cp -a "$src" "$dst"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  say "Dry run complete"
  exit 0
fi

if ! run_target_validation; then
  warn "Target validation failed; rolling back copied files"
  ROLLBACK_DIR="$backup_dir"
  rollback_from_backup
  exit 1
fi

ensure_target_automation_control

if [[ -n "$RESTART_SERVICES" ]]; then
  say "Restarting user services: $RESTART_SERVICES"
  restart_started_at="$(date +%s)"
  if ! systemctl --user restart $RESTART_SERVICES; then
    warn "Service restart failed; rolling back copied files"
    ROLLBACK_DIR="$backup_dir"
    rollback_from_backup
    exit 1
  fi
  if ! systemctl --user is-active $RESTART_SERVICES; then
    warn "Service active check failed; rolling back copied files"
    ROLLBACK_DIR="$backup_dir"
    rollback_from_backup
    systemctl --user restart $RESTART_SERVICES || warn "Service restart after rollback failed; inspect $backup_dir"
    exit 1
  fi
  if ! post_deploy_health_check "$restart_started_at"; then
    warn "Post-deploy health check failed; rolling back copied files"
    ROLLBACK_DIR="$backup_dir"
    rollback_from_backup
    systemctl --user restart $RESTART_SERVICES || warn "Service restart after rollback failed; inspect $backup_dir"
    exit 1
  fi
fi

say "Live runtime update complete"
printf 'Backup: %s\n' "$backup_dir"
