#!/bin/sh
set -eu

log() {
  printf '[%s] dashboard-entrypoint: %s\n' "$(date -Is)" "$*" >&2
}

export BDAG_PROJECT_ROOT="${BDAG_PROJECT_ROOT:-/workspace}"
export BDAG_RUNTIME_DIR="${BDAG_RUNTIME_DIR:-$BDAG_PROJECT_ROOT/ops/runtime}"
export BDAG_POOL_ENV_FILE="${BDAG_POOL_ENV_FILE:-$BDAG_PROJECT_ROOT/.env}"
export BDAG_DASHBOARD_BIND="${BDAG_DASHBOARD_BIND:-0.0.0.0}"
export BDAG_DASHBOARD_PORT="${BDAG_DASHBOARD_PORT:-9290}"
export PYTHONPATH="/opt/dashboard/ops${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$BDAG_RUNTIME_DIR"

if [ ! -f /opt/dashboard/ops/dashboard.py ]; then
  log "ops/dashboard.py not found in dashboard checkout"
  find /opt/dashboard -maxdepth 2 -type f | sort >&2 || true
  exit 1
fi

if [ ! -S /var/run/docker.sock ]; then
  log "warning: /var/run/docker.sock is not mounted; container status and actions will be limited"
fi

log "starting dashboard from /opt/dashboard/ops/dashboard.py on ${BDAG_DASHBOARD_BIND}:${BDAG_DASHBOARD_PORT}"
cd /opt/dashboard/ops
exec python3 dashboard.py
