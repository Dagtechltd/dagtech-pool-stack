#!/usr/bin/env bash
set -euo pipefail

ROOT="/opt/miner/pool-stack-docker"
PROJECT="bdagminer"
COMPOSE_FILE="docker-compose-miner.yml"
ENV_FILE=".env"

LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/single-node-watchdog.log"

STATE_DIR="/opt/miner/backup-state/watchdog"
LOCK_FILE="/tmp/bdag-single-node-watchdog.lock"
LAST_RESTART_FILE="$STATE_DIR/last-pool-restart.epoch"

POOL_CONTAINER="bdagminer-pool-1"
POOL_SERVICE="pool"

EXPECTED=(
  bdagminer-postgres-1
  bdagminer-node-1
  bdagminer-pool-1
  bdagminer-dashboard-1
)

SERVICES=(
  postgres
  node
  pool
  dashboard
)

WINDOW="90s"
VERIFY_WINDOW="45s"
COOLDOWN_SECONDS=300
MIN_EXPIRED_ERRORS=30

mkdir -p "$LOG_DIR" "$STATE_DIR"
cd "$ROOT"

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" >> "$LOG_FILE"
}

run_compose() {
  docker compose \
    -p "$PROJECT" \
    -f "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
    "$@"
}

count_pool_logs() {
  local window="$1"

  docker logs --since "$window" "$POOL_CONTAINER" 2>&1 | awk '
    /Submit Error/ && /not found in acceptedJobs/ && /Expired/ { expired++ }
    /valid share accepted/ { accepted++ }
    /💵 REVENUE/ { revenue++ }
    /Block submitted successfully/ { blocks++ }
    END {
      printf "expired=%d accepted=%d revenue=%d blocks=%d\n", expired+0, accepted+0, revenue+0, blocks+0
    }
  '
}

get_count() {
  local key="$1"
  local counts="$2"

  printf '%s\n' "$counts" |
    tr ' ' '\n' |
    awk -F= -v k="$key" '$1 == k { print $2 }'
}

reconcile_containers() {
  log "container reconcile requested"

  run_compose up -d --no-build "${SERVICES[@]}" >> "$LOG_FILE" 2>&1

  log "container reconcile completed"
}

restart_pool_only() {
  log "semantic failure detected; restarting pool only"

  run_compose restart "$POOL_SERVICE" >> "$LOG_FILE" 2>&1

  date +%s > "$LAST_RESTART_FILE"
}

(
  flock -n 9 || exit 0

  if ! docker info >/dev/null 2>&1; then
    log "docker unavailable; skipping"
    exit 0
  fi

  need_reconcile=0

  for name in "${EXPECTED[@]}"; do
    state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || true)"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null || true)"

    case "$state" in
      running)
        if [[ "$health" == "unhealthy" ]]; then
          log "$name running but unhealthy"
          need_reconcile=1
        fi
        ;;
      "")
        log "$name missing"
        need_reconcile=1
        ;;
      *)
        log "$name state=$state"
        need_reconcile=1
        ;;
    esac
  done

  if [[ "$need_reconcile" -eq 1 ]]; then
    reconcile_containers
    exit 0
  fi

  counts="$(count_pool_logs "$WINDOW")"

  expired="$(get_count expired "$counts")"
  accepted="$(get_count accepted "$counts")"
  revenue="$(get_count revenue "$counts")"
  blocks="$(get_count blocks "$counts")"

  log "sample window=$WINDOW $counts"

  if (( expired >= MIN_EXPIRED_ERRORS && accepted == 0 && revenue == 0 )); then
    now="$(date +%s)"
    last_restart=0

    if [[ -f "$LAST_RESTART_FILE" ]]; then
      last_restart="$(cat "$LAST_RESTART_FILE" 2>/dev/null || echo 0)"
    fi

    if (( now - last_restart < COOLDOWN_SECONDS )); then
      log "trigger detected but cooldown active: $counts"
      exit 0
    fi

    report="$STATE_DIR/pool-expired-job-$(date +%Y%m%d-%H%M%S).json"

    cat > "$report" <<REPORT_EOF
{
  "timestamp": "$(date --iso-8601=seconds)",
  "container": "$POOL_CONTAINER",
  "window": "$WINDOW",
  "expired_errors": $expired,
  "accepted_shares": $accepted,
  "revenue_lines": $revenue,
  "block_submissions": $blocks,
  "action": "restart_pool"
}
REPORT_EOF

    log "trigger report=$report $counts"

    restart_pool_only

    sleep 30

    verify_counts="$(count_pool_logs "$VERIFY_WINDOW")"

    verify_accepted="$(get_count accepted "$verify_counts")"
    verify_revenue="$(get_count revenue "$verify_counts")"

    if (( verify_accepted > 0 || verify_revenue > 0 )); then
      log "recovery ok: $verify_counts"
      exit 0
    fi

    log "recovery uncertain: $verify_counts"
    exit 1
  fi
) 9>"$LOCK_FILE"
