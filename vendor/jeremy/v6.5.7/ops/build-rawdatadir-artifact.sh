#!/usr/bin/env bash
set -Eeuo pipefail

# Build a signed raw-datadir FastArtifact V2 directory artifact from a finalized
# sidecar copy. Production use must point BDAG_RAWDATADIR_SOURCE_DIR at the
# sidecar after an operator-approved final sync window.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}"
COMPOSE_FILE="${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}"
ARTIFACT_BASE="${BDAG_RAWDATADIR_ARTIFACT_BASE:-$PROJECT_ROOT/data-restore/rawdatadir}"
ARTIFACT_KEEP="${BDAG_RAWDATADIR_ARTIFACT_KEEP:-3}"
REQUESTED_NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
if [[ "${REQUESTED_NETWORK,,}" != "mainnet" ]]; then
  printf '[%s] raw datadir artifact builder refuses non-mainnet network: %s\n' "$(date -Is)" "$REQUESTED_NETWORK" >&2
  exit 2
fi
NETWORK="mainnet"
CHAIN_ID="${BDAG_RAWDATADIR_CHAIN_ID:-1404}"
NODE_IMAGE="${BDAG_RAWDATADIR_NODE_IMAGE:-${BDAG_FASTSNAP_NODE_IMAGE:-${BLOCKDAG_NODE_IMAGE:-}}}"
FASTSNAP_BIN="${BDAG_RAWDATADIR_FASTSNAP_BINARY:-}"
SOURCE_DIR="${BDAG_RAWDATADIR_SOURCE_DIR:-}"
SOURCE_LABEL="${BDAG_RAWDATADIR_SOURCE_LABEL:-}"
LOCK_FILE="${BDAG_RAWDATADIR_LOCK:-$PROJECT_ROOT/ops/runtime/rawdatadir-artifact.lock}"
LOG_FILE="${BDAG_RAWDATADIR_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-artifact-$(date +%Y%m%d).log}"
REQUIRE_SIGNED="${BDAG_RAWDATADIR_REQUIRE_SIGNED:-1}"
REQUIRE_STATE_ROOT="${BDAG_RAWDATADIR_REQUIRE_STATE_ROOT:-1}"
ARCHIVE_USE_SUDO="${BDAG_RAWDATADIR_ARCHIVE_USE_SUDO:-auto}"
STATUS_FILE="${BDAG_RAWDATADIR_SOURCE_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-source-status.json}"
DOCKER_CPU_SHARES="${BDAG_RAWDATADIR_DOCKER_CPU_SHARES:-128}"
DOCKER_BLKIO_WEIGHT="${BDAG_RAWDATADIR_DOCKER_BLKIO_WEIGHT:-10}"
DOCKER_CPUS="${BDAG_RAWDATADIR_DOCKER_CPUS:-1.5}"
ANCHOR_RPC_URL="${BDAG_RAWDATADIR_ANCHOR_RPC_URL:-${NODE_RPC_URL:-http://127.0.0.1:38131}}"
RPC_USER="${NODE_RPC_USER:-test}"
RPC_PASS="${NODE_RPC_PASS:-test}"

mkdir -p "$ARTIFACT_BASE/artifacts" "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] raw datadir artifact build already running" | tee -a "$LOG_FILE"
  exit 0
}

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
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

docker_run_low_priority() {
  local command=(docker run --rm --cpu-shares "$DOCKER_CPU_SHARES" --blkio-weight "$DOCKER_BLKIO_WEIGHT")
  if [[ -n "$DOCKER_CPUS" ]]; then
    command+=(--cpus "$DOCKER_CPUS")
  fi
  command+=("$@")
  run_low_priority "${command[@]}"
}

wait_db_lock_free() {
  local lock_path="$1"
  local deadline=$((SECONDS + 45))
  while ((SECONDS < deadline)); do
    if [[ -e "$lock_path" ]] && command -v fuser >/dev/null 2>&1 && fuser "$lock_path" >/dev/null 2>&1; then
      sleep 1
      continue
    fi
    return 0
  done
  return 1
}

resolve_node_image() {
  if [[ -n "$NODE_IMAGE" ]]; then
    printf '%s\n' "$NODE_IMAGE"
    return
  fi
  local image_id
  image_id="$(compose images -q node 2>/dev/null | head -n1 || true)"
  if [[ -n "$image_id" ]]; then
    printf '%s\n' "$image_id"
    return
  fi
  log "set BDAG_RAWDATADIR_NODE_IMAGE, BDAG_FASTSNAP_NODE_IMAGE, or BLOCKDAG_NODE_IMAGE"
  return 1
}

collect_anchor_env() {
  PYTHONDONTWRITEBYTECODE=1 python3 - "$ANCHOR_RPC_URL" "$RPC_USER" "$RPC_PASS" "$REQUIRE_STATE_ROOT" <<'PY'
import base64
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request

url, user, password, require_state_root = sys.argv[1:5]
require_state_root = require_state_root.lower() not in {"0", "false", "no", "off"}

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

def env(name, value):
    print(f"{name}={shlex.quote(str(value))}")

configured_block_total = os.getenv("BDAG_RAWDATADIR_BLOCK_TOTAL")
configured_tip_order = os.getenv("BDAG_RAWDATADIR_TIP_ORDER")
configured_tip_hash = os.getenv("BDAG_RAWDATADIR_TIP_HASH")
configured_state_root = os.getenv("BDAG_RAWDATADIR_STATE_ROOT")
genesis_hash = os.getenv("BDAG_RAWDATADIR_GENESIS_HASH", "")
zero = "0x" + ("0" * 64)

def zero_hash(value):
    return not value or str(value).lower() in {"0x" + ("0" * 64), "0" * 64}

for attempt in range(24):
    block_total = configured_block_total
    tip_order = configured_tip_order
    tip_hash = configured_tip_hash
    state_root = configured_state_root
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
            pass
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
    if not missing:
        break
    if attempt == 23:
        raise SystemExit("raw datadir anchor unavailable from live RPC: " + ",".join(missing))
    time.sleep(5)

if not genesis_hash:
    for _ in range(3):
        try:
            genesis_hash = str(rpc("getBlockhash", [0]))
            break
        except Exception:
            time.sleep(1)

env("RAW_BLOCK_TOTAL", block_total)
env("RAW_TIP_ORDER", tip_order)
env("RAW_TIP_HASH", tip_hash)
env("RAW_STATE_ROOT", state_root or zero)
env("RAW_GENESIS_HASH", genesis_hash)
PY
}

archive_source_datadir() {
  local source_mainnet="$1"
  local archive="$2"
  local tmp="$archive.tmp"
  rm -f "$tmp"
  local tar_args=(
    --xattrs
    --numeric-owner
    --one-file-system
    --zstd
    -cpf "$tmp"
    -C "$source_mainnet"
    "--exclude=./network.key*"
    "--exclude=./bdageth/nodekey*"
    "--exclude=./bdageth/LOCK"
    "--exclude=./bdageth/chaindata/LOCK"
    "--exclude=./keystore*"
    "--exclude=./bdageth/keystore*"
    "--exclude=./bdageth/nodes*"
    "--exclude=./peerstore*"
    "--exclude=./nodes*"
    "--exclude=./bdageth/transactions.rlp"
    "--exclude=./LOCK"
    "--exclude=./BdagChain/LOCK"
    "--exclude=./geth.ipc"
    "--exclude=./bdag.ipc"
    "--exclude=*.ipc"
    "--exclude=*.sock"
    .
  )
  local tar_command=(tar)
  case "${ARCHIVE_USE_SUDO,,}" in
    1|true|yes|on)
      if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; then
        log "BDAG_RAWDATADIR_ARCHIVE_USE_SUDO is enabled, but passwordless sudo is unavailable"
        return 1
      fi
      tar_command=(sudo -n tar)
      log "archiving raw datadir with sudo tar"
      ;;
    auto)
      if [[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        tar_command=(sudo -n tar)
        log "archiving raw datadir with sudo tar"
      fi
      ;;
    0|false|no|off)
      ;;
    *)
      log "invalid BDAG_RAWDATADIR_ARCHIVE_USE_SUDO=$ARCHIVE_USE_SUDO"
      return 1
      ;;
  esac

  if run_low_priority "${tar_command[@]}" "${tar_args[@]}" 2>>"$LOG_FILE"; then
    if [[ "${tar_command[0]}" == "sudo" ]]; then
      sudo chown "$(id -u):$(id -g)" "$tmp"
    fi
    mv -f "$tmp" "$archive"
    return 0
  fi
  if [[ "${tar_command[0]}" != "sudo" ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    log "retrying raw datadir archive with sudo because ordinary tar failed"
    run_low_priority sudo -n tar "${tar_args[@]}" 2>>"$LOG_FILE"
    sudo chown "$(id -u):$(id -g)" "$tmp"
    mv -f "$tmp" "$archive"
    return 0
  fi
  return 1
}

run_manifest_builder() {
  local stage="$1"
  local manifest="$2"
  shift 2
  if [[ "$REQUIRE_SIGNED" == "1" && -z "${BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX:-}" ]]; then
    log "refusing unsigned raw datadir artifact: set BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX or BDAG_RAWDATADIR_REQUIRE_SIGNED=0"
    return 1
  fi
  if [[ -n "$FASTSNAP_BIN" ]]; then
    "$FASTSNAP_BIN" --build-directory-manifest --artifact-root-dir "$stage" --manifest-out "$manifest" "$@"
    return
  fi
  if command -v fastsnap >/dev/null 2>&1; then
    fastsnap --build-directory-manifest --artifact-root-dir "$stage" --manifest-out "$manifest" "$@"
    return
  fi
  local image
  image="$(resolve_node_image)"
  docker_run_low_priority \
    --entrypoint /usr/local/bin/fastsnap \
    -e BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID="${BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID:-}" \
    -e BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX="${BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX:-}" \
    -v "$stage":/artifact \
    "$image" \
    --build-directory-manifest --artifact-root-dir /artifact --manifest-out /artifact/manifest.json "$@"
}

promote_current() {
  local stage="$1"
  local current="$ARTIFACT_BASE/current"
  local target
  target="$(realpath --relative-to "$ARTIFACT_BASE" "$stage" 2>/dev/null || printf '%s\n' "$stage")"
  ln -sfn "$target" "$current.tmp"
  mv -Tf "$current.tmp" "$current"
  log "raw datadir artifact current -> $target"
}

prune_old_artifacts() {
  [[ "$ARTIFACT_KEEP" =~ ^[0-9]+$ ]] || return 0
  ((ARTIFACT_KEEP > 0)) || return 0
  mapfile -t dirs < <(find "$ARTIFACT_BASE/artifacts" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -n | awk '{print $2}')
  local remove_count=$((${#dirs[@]} - ARTIFACT_KEEP))
  if ((remove_count <= 0)); then
    return 0
  fi
  local i
  for ((i=0; i<remove_count; i++)); do
    rm -rf "${dirs[$i]}"
    log "removed old raw datadir artifact ${dirs[$i]}"
  done
}

if [[ -n "$SOURCE_DIR" ]]; then
  SOURCE_MAINNET="$SOURCE_DIR"
  SOURCE_LABEL="${SOURCE_LABEL:-manual-source}"
  if [[ ! -d "$SOURCE_MAINNET/BdagChain" ]]; then
    log "source dir does not look like a $NETWORK datadir: $SOURCE_MAINNET"
    exit 1
  fi
  LIVE_NODE_MAINNET="$(readlink -m "${BDAG_RAWDATADIR_NODE_DATADIR:-$PROJECT_ROOT/data/node}/$NETWORK")"
  SOURCE_MAINNET_REAL="$(readlink -m "$SOURCE_MAINNET")"
  if [[ "${BDAG_RAWDATADIR_ALLOW_LIVE_SOURCE:-0}" != "1" && "$SOURCE_MAINNET_REAL" == "$LIVE_NODE_MAINNET" ]]; then
    log "refusing raw datadir artifact from live node datadir: $SOURCE_MAINNET_REAL"
    log "use ops/publish-rawdatadir-artifact.sh to refresh/finalize a sidecar first"
    exit 1
  fi
  wait_db_lock_free "$SOURCE_MAINNET/BdagChain/LOCK" || {
    log "source datadir lock is still held: $SOURCE_MAINNET/BdagChain/LOCK"
    exit 1
  }
else
  log "BDAG_RAWDATADIR_SOURCE_DIR is required; run ops/publish-rawdatadir-artifact.sh to refresh and finalize the sidecar"
  exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S%Z)"
STAGE="$ARTIFACT_BASE/artifacts/rawdatadir-$STAMP"
ARCHIVE="$STAGE/node-datadir-$NETWORK-no-private-keys.tar.zst"
MANIFEST="$STAGE/manifest.json"
mkdir -p "$STAGE"

ANCHOR_FILE="$STAGE/anchor.env"
collect_anchor_env > "$ANCHOR_FILE"
source "$ANCHOR_FILE"

log "archiving raw datadir source=$SOURCE_LABEL path=$SOURCE_MAINNET"
archive_source_datadir "$SOURCE_MAINNET" "$ARCHIVE"

if [[ ! -s "$ARCHIVE" ]]; then
  log "raw datadir archive was not created: $ARCHIVE"
  exit 1
fi

(
  cd "$STAGE"
  sha256sum "$(basename "$ARCHIVE")" > SHA256SUMS
  tar --zstd -tf "$(basename "$ARCHIVE")" >/dev/null
)

cat > "$STAGE/README-RAWDATADIR.txt" <<EOF
BlockDAG raw datadir artifact

Created: $(date -Is)
Network: $NETWORK
Chain ID: $CHAIN_ID
Source: $SOURCE_LABEL
Tip order: $RAW_TIP_ORDER
Tip hash: $RAW_TIP_HASH
State root: $RAW_STATE_ROOT

Excluded identity/secret material:
- network.key variants
- bdageth/nodekey variants
- keystore and bdageth/keystore variants
- peerstore, nodes, and bdageth/nodes variants
- IPC/socket files

Fetch with ops/fetch-rawdatadir-artifact.sh or fastsnap --artifact-type raw_datadir_checkpoint.
EOF

rm -f "$MANIFEST"
log "building raw datadir FastArtifact manifest"
run_manifest_builder "$STAGE" "$MANIFEST" \
  --artifact-type raw_datadir_checkpoint \
  --network "$NETWORK" \
  --chain-id "$CHAIN_ID" \
  --genesis-hash "$RAW_GENESIS_HASH" \
  --tip-order "$RAW_TIP_ORDER" \
  --tip-hash "$RAW_TIP_HASH" \
  --block-total "$RAW_BLOCK_TOTAL" \
  --state-root "$RAW_STATE_ROOT" \
  --metadata "raw_datadir_source=$SOURCE_LABEL" \
  --metadata "raw_datadir_archive=$(basename "$ARCHIVE")"

promote_current "$STAGE"
prune_old_artifacts

log "raw datadir artifact ready: $STAGE"
log "serve with BDAG_FASTSYNC_ARTIFACT_DIRECTORY=$ARTIFACT_BASE/current and BDAG_FASTSYNC_ARTIFACT_MANIFEST=$ARTIFACT_BASE/current/manifest.json"
python3 - "$STATUS_FILE" "$STAGE" "$MANIFEST" "$RAW_TIP_ORDER" "$RAW_TIP_HASH" <<'PY'
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
    "last_publish_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "last_publish_dir": sys.argv[2],
    "last_manifest": sys.argv[3],
    "artifact_tip_order": sys.argv[4],
    "artifact_tip_hash": sys.argv[5],
    "serving_directory_hint": str(Path(sys.argv[2]).parents[1] / "current"),
})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
