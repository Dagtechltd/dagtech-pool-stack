#!/bin/sh
set -eu

log() {
  printf '[%s] collector-entrypoint: %s\n' "$(date -Is)" "$*" >&2
}

export BDAG_PROJECT_ROOT="${BDAG_PROJECT_ROOT:-/workspace}"
export BDAG_RUNTIME_DIR="${BDAG_RUNTIME_DIR:-/var/lib/bdag-collector/runtime}"
export BDAG_POOL_ENV_FILE="${BDAG_POOL_ENV_FILE:-$BDAG_PROJECT_ROOT/.env}"
export BDAG_COLLECTOR_BIND="${BDAG_COLLECTOR_BIND:-0.0.0.0}"
export BDAG_COLLECTOR_PORT="${BDAG_COLLECTOR_PORT:-9280}"
if [ -d "$BDAG_PROJECT_ROOT/ops" ]; then
  export PYTHONPATH="$BDAG_PROJECT_ROOT/ops${PYTHONPATH:+:$PYTHONPATH}"
fi
if [ -d /opt/collector/ops ]; then
  export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}/opt/collector/ops"
fi

mkdir -p "$BDAG_RUNTIME_DIR"

app_dir=/opt/collector
app=/opt/collector/ops/collector.py
if [ ! -f "$app" ] && [ -f /opt/collector/collector.py ]; then
  app=/opt/collector/collector.py
fi
if [ ! -f "$app" ]; then
  log "collector.py not found in collector checkout"
  find /opt/collector -maxdepth 2 -type f | sort >&2 || true
  exit 1
fi

if [ ! -S /var/run/docker.sock ]; then
  log "warning: /var/run/docker.sock is not mounted; container status and logs will be limited"
fi

log "starting collector from $app"
cd "$app_dir"
exec python3 "$app"
