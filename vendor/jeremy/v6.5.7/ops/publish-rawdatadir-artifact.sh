#!/usr/bin/env bash
set -Eeuo pipefail

# Refresh the raw-datadir sidecar and, when an operator-approved finalization
# window is active, publish a signed immutable FastArtifact V2 generation from
# the finalized sidecar. The live node is never stopped unless
# BDAG_RAWDATADIR_FINALIZE=1 is set for that run.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REQUESTED_NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
if [[ "${REQUESTED_NETWORK,,}" != "mainnet" ]]; then
  printf '[%s] raw datadir artifact publish refuses non-mainnet network: %s\n' "$(date -Is)" "$REQUESTED_NETWORK" >&2
  exit 2
fi
NETWORK="mainnet"
STATUS_FILE="${BDAG_RAWDATADIR_SOURCE_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-source-status.json}"
LOCK_FILE="${BDAG_RAWDATADIR_PUBLISH_LOCK:-$PROJECT_ROOT/ops/runtime/rawdatadir-publish.lock}"
LOG_FILE="${BDAG_RAWDATADIR_PUBLISH_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-publish-$(date +%Y%m%d).log}"
FINALIZE_PUBLISH="${BDAG_RAWDATADIR_FINALIZE:-0}"
NODE_SERVICES_CSV="${BDAG_NODE_SERVICES:-node}"
ACTIVE_SERVICE="${BDAG_RAWDATADIR_ACTIVE_SERVICE:-${NODE_SERVICES_CSV%%,*}}"
ACTIVE_SERVICE="${ACTIVE_SERVICE:-node}"
SIDECAR_DIR="${BDAG_RAWDATADIR_SIDECAR_DIR:-$PROJECT_ROOT/data-restore/rawdatadir-sidecar/$NETWORK}"
ANCHOR_RPC_URL="${BDAG_RAWDATADIR_ANCHOR_RPC_URL:-${NODE_RPC_URL:-http://127.0.0.1:38131}}"
RPC_USER="${NODE_RPC_USER:-test}"
RPC_PASS="${NODE_RPC_PASS:-test}"
REQUIRE_STATE_ROOT="${BDAG_RAWDATADIR_REQUIRE_STATE_ROOT:-1}"
FINALIZATION_ANCHOR_FILE="${BDAG_RAWDATADIR_FINALIZATION_ANCHOR_FILE:-$PROJECT_ROOT/ops/runtime/rawdatadir-finalization-anchor.env}"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")"
ACTIVE_NODE_STOPPED=0

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] raw datadir publish already running" | tee -a "$LOG_FILE"
  exit 0
}

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

write_status_note() {
  local note="$1"
  python3 - "$STATUS_FILE" "$note" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
note = sys.argv[2]
payload = {}
if path.exists():
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
payload["last_publish_note"] = note
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
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

run_eligibility() {
  if ! "$PROJECT_ROOT/ops/fastartifact_source_eligibility.py" --full --json --status-file "$STATUS_FILE" 2>&1 | tee -a "$LOG_FILE"; then
    log "raw datadir source eligibility denied; see $STATUS_FILE"
    exit 0
  fi
}

compose() {
  docker compose --env-file "${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}" -f "${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}" "$@"
}

collect_finalization_anchor_env() {
  PYTHONDONTWRITEBYTECODE=1 python3 - "$ANCHOR_RPC_URL" "$RPC_USER" "$RPC_PASS" "$REQUIRE_STATE_ROOT" <<'PY'
import base64
import json
import os
import shlex
import sys
import time
import urllib.request

url, user, password, require_state_root = sys.argv[1:5]
require_state_root = require_state_root.lower() not in {"0", "false", "no", "off"}
zero = "0x" + ("0" * 64)


def rpc(method, params=None):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        decoded = json.loads(resp.read().decode())
    if decoded.get("error"):
        raise RuntimeError(f"{method}: {decoded['error']}")
    return decoded.get("result")


def quantity(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise ValueError(value)


def zero_hash(value):
    text = str(value or "").strip().lower()
    return not text or text in {zero, zero[2:]}


def export_env(name, value):
    print(f"export {name}={shlex.quote(str(value))}")


configured_block_total = os.getenv("BDAG_RAWDATADIR_BLOCK_TOTAL")
configured_tip_order = os.getenv("BDAG_RAWDATADIR_TIP_ORDER")
configured_tip_hash = os.getenv("BDAG_RAWDATADIR_TIP_HASH")
configured_state_root = os.getenv("BDAG_RAWDATADIR_STATE_ROOT")
configured_genesis_hash = os.getenv("BDAG_RAWDATADIR_GENESIS_HASH")

last_missing = []
for attempt in range(12):
    block_total = configured_block_total
    tip_order = configured_tip_order
    tip_hash = configured_tip_hash
    state_root = configured_state_root
    genesis_hash = configured_genesis_hash

    if not block_total:
        for method in ("getBlockTotal", "getBlockCount"):
            try:
                block_total = str(quantity(rpc(method)))
                break
            except Exception:
                pass
    if not tip_order:
        try:
            tip_order = str(quantity(rpc("getMainChainHeight")))
        except Exception:
            tip_order = block_total
    if not tip_hash and tip_order:
        for method, params in (("getBlockhash", [int(tip_order)]), ("getBestBlockHash", [])):
            try:
                tip_hash = str(rpc(method, params))
                break
            except Exception:
                pass
    if not state_root and tip_hash:
        for method, params in (("getBlockHeader", [tip_hash, True]), ("getStateRoot", [int(tip_order or 0), False])):
            try:
                result = rpc(method, params)
                if isinstance(result, dict):
                    state_root = result.get("stateRoot") or result.get("stateroot") or result.get("StateRoot")
                elif isinstance(result, str):
                    state_root = result
                if state_root:
                    break
            except Exception:
                pass
    if not genesis_hash:
        try:
            genesis_hash = str(rpc("getBlockhash", [0]))
        except Exception:
            pass

    missing = []
    try:
        if not block_total or quantity(block_total) <= 1:
            missing.append("block_total")
    except Exception:
        missing.append("block_total")
    try:
        if not tip_order or quantity(tip_order) <= 1:
            missing.append("tip_order")
    except Exception:
        missing.append("tip_order")
    if zero_hash(tip_hash):
        missing.append("tip_hash")
    if require_state_root and zero_hash(state_root):
        missing.append("state_root")
    if zero_hash(genesis_hash):
        missing.append("genesis_hash")
    if not missing:
        break
    last_missing = missing
    time.sleep(5)
else:
    raise SystemExit("raw datadir finalization anchor unavailable from live RPC before node stop: " + ",".join(last_missing))

export_env("BDAG_RAWDATADIR_BLOCK_TOTAL", quantity(block_total))
export_env("BDAG_RAWDATADIR_TIP_ORDER", quantity(tip_order))
export_env("BDAG_RAWDATADIR_TIP_HASH", tip_hash)
export_env("BDAG_RAWDATADIR_STATE_ROOT", state_root or zero)
export_env("BDAG_RAWDATADIR_GENESIS_HASH", genesis_hash)
PY
}

stop_active_node_for_final_sync() {
  if [[ "$FINALIZE_PUBLISH" != "1" ]]; then
    log "raw datadir artifact publish requires BDAG_RAWDATADIR_FINALIZE=1; refreshed sidecar only"
    write_status_note "publish skipped: raw datadir finalization was not approved"
    exit 0
  fi
  log "capturing raw datadir finalization anchor metadata before stopping $ACTIVE_SERVICE"
  mkdir -p "$(dirname "$FINALIZATION_ANCHOR_FILE")"
  collect_finalization_anchor_env >"$FINALIZATION_ANCHOR_FILE" 2>>"$LOG_FILE"
  log "captured raw datadir finalization anchor metadata: $FINALIZATION_ANCHOR_FILE"
  log "operator-approved finalization: stopping $ACTIVE_SERVICE for final sidecar sync"
  compose stop "$ACTIVE_SERVICE" 2>&1 | tee -a "$LOG_FILE"
  ACTIVE_NODE_STOPPED=1
}

start_active_node_after_final_sync() {
  if [[ "$ACTIVE_NODE_STOPPED" == "1" ]]; then
    log "restarting $ACTIVE_SERVICE after final sidecar sync"
    compose start "$ACTIVE_SERVICE" 2>&1 | tee -a "$LOG_FILE"
    ACTIVE_NODE_STOPPED=0
  fi
}

if ! pressure_reason="$(maintenance_backoff_reason rawdatadir_publish 2>>"$LOG_FILE")"; then
  log "skipping raw datadir artifact publish: background maintenance gate unavailable"
  write_status_note "publish skipped: background maintenance gate unavailable"
  exit 0
fi
if [[ -n "$pressure_reason" ]]; then
  log "skipping raw datadir artifact publish: background maintenance backoff active: $pressure_reason"
  write_status_note "publish skipped: background maintenance backoff active: $pressure_reason"
  exit 0
fi

log "refreshing raw datadir sidecar"
"$PROJECT_ROOT/ops/maintain-rawdatadir-sidecar.sh" 2>&1 | tee -a "$LOG_FILE"

run_eligibility

stop_active_node_for_final_sync
trap start_active_node_after_final_sync EXIT INT TERM

if [[ "$FINALIZE_PUBLISH" == "1" ]]; then
  log "running final sidecar sync while $ACTIVE_SERVICE is stopped"
  # shellcheck disable=SC1090
  source "$FINALIZATION_ANCHOR_FILE"
  BDAG_RAWDATADIR_SIDECAR_FINAL_STOPPED_SYNC=1 \
  BDAG_RAWDATADIR_REQUIRE_EVM_REFERENCE_FRESH=0 \
  BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED=1 \
    "$PROJECT_ROOT/ops/maintain-rawdatadir-sidecar.sh" 2>&1 | tee -a "$LOG_FILE"
  start_active_node_after_final_sync
  trap - EXIT INT TERM
fi

if [[ ! -d "$SIDECAR_DIR/BdagChain" ]]; then
  log "sidecar does not look publishable: $SIDECAR_DIR"
  write_status_note "publish skipped: sidecar missing BdagChain"
  exit 1
fi

log "building raw datadir artifact from finalized sidecar $SIDECAR_DIR"
BDAG_RAWDATADIR_SOURCE_DIR="$SIDECAR_DIR" \
BDAG_RAWDATADIR_SOURCE_LABEL="${BDAG_RAWDATADIR_SOURCE_LABEL:-finalized-sidecar}" \
  "$PROJECT_ROOT/ops/build-rawdatadir-artifact.sh" 2>&1 | tee -a "$LOG_FILE"
write_status_note "publish complete"
