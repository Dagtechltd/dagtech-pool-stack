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

collector_pythonpath=""
add_pythonpath_dir() {
  if [ -d "$1" ]; then
    collector_pythonpath="${collector_pythonpath}${collector_pythonpath:+:}$1"
  fi
}

add_pythonpath_dir /opt/collector/ops
add_pythonpath_dir "$BDAG_PROJECT_ROOT/ops"
if [ -n "$collector_pythonpath" ]; then
  export PYTHONPATH="$collector_pythonpath${PYTHONPATH:+:$PYTHONPATH}"
fi

mkdir -p "$BDAG_RUNTIME_DIR"

app_dir=/opt/collector
app=/opt/collector/collector.py
if [ ! -f "$app" ] && [ -f /opt/collector/ops/collector.py ]; then
  app_dir=/opt/collector/ops
  app=/opt/collector/ops/collector.py
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
exec python3 - "$app" <<'PY'
from pathlib import Path
import runpy
import sys

BDAG_CHILD_EXECUTABLES = {"bdag", "blockdag-node"}


def command_is_bdag_child(command: str) -> bool:
    parts = command.split()
    if not parts:
        return False
    executable_name = Path(parts[0]).name
    if executable_name in BDAG_CHILD_EXECUTABLES:
        return True
    if executable_name == "rosetta" or executable_name.startswith("qemu-"):
        wrapped_executable_name = Path(parts[1]).name if len(parts) > 1 else ""
        return wrapped_executable_name in BDAG_CHILD_EXECUTABLES
    return False


def bdag_child_running_from_top(top: str) -> bool:
    for line in top.splitlines()[1:]:
        parts = line.split(None, 7)
        command = parts[7] if len(parts) >= 8 else line
        if command_is_bdag_child(command):
            return True
    return False


try:
    import pool_ops

    pool_ops.BDAG_CHILD_EXECUTABLES = BDAG_CHILD_EXECUTABLES
    pool_ops.command_is_bdag_child = command_is_bdag_child
    pool_ops.bdag_child_running_from_top = bdag_child_running_from_top
except Exception as exc:
    print(f"collector-entrypoint: warning: could not patch node child detector: {exc}", file=sys.stderr)

runpy.run_path(sys.argv[1], run_name="__main__")
PY
