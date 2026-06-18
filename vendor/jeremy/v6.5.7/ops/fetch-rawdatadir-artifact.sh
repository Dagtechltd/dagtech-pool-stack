#!/usr/bin/env bash
set -Eeuo pipefail

# Fetch a raw-datadir FastArtifact V2 artifact over libp2p, verify it, and
# optionally install it into a stopped local node datadir with rollback parked
# beside the target. This script never deletes the old datadir.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REQUESTED_NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
if [[ "${REQUESTED_NETWORK,,}" != "mainnet" ]]; then
  printf '[%s] raw datadir artifact fetch refuses non-mainnet network: %s\n' "$(date -Is)" "$REQUESTED_NETWORK" >&2
  exit 2
fi
NETWORK="mainnet"
PEERS="${BDAG_RAWDATADIR_PEERS:-${BDAG_FASTSNAP_PEERS:-}}"
FASTSNAP_BIN="${BDAG_RAWDATADIR_FASTSNAP_BINARY:-fastsnap}"
STAGING_BASE="${BDAG_RAWDATADIR_DOWNLOAD_BASE:-$PROJECT_ROOT/data-restore/rawdatadir-downloads}"
TARGET_DIR="${BDAG_RAWDATADIR_IMPORT_TARGET:-}"
REPLACE_EXISTING="${BDAG_RAWDATADIR_IMPORT_REPLACE:-0}"
ALLOW_UNSIGNED="${BDAG_RAWDATADIR_ALLOW_UNSIGNED:-0}"
TRUSTED_SIGNERS="${BDAG_RAWDATADIR_TRUSTED_SIGNERS:-${BDAG_FASTSNAP_TRUSTED_SIGNERS:-}}"
MIN_TIP_EXPLICIT="${BDAG_RAWDATADIR_MIN_TIP:-}"
TARGET_TIP="${BDAG_RAWDATADIR_TARGET_TIP:-${BDAG_FASTSNAP_TARGET_TIP:-}}"
ACCEPTABLE_STARTUP_LAG_BLOCKS="${BDAG_RAWDATADIR_ACCEPTABLE_STARTUP_LAG_BLOCKS:-${BDAG_SYNC_ACCEPTABLE_STARTUP_LAG_BLOCKS:-4000}}"
COPY_MINUTE_BLOCK_ALLOWANCE="${BDAG_RAWDATADIR_COPY_MINUTE_BLOCK_ALLOWANCE:-${BDAG_SYNC_COPY_MINUTE_BLOCK_ALLOWANCE:-4}}"
LAST_COPY_SECONDS_FILE="${BDAG_RAWDATADIR_LAST_COPY_SECONDS_FILE:-$PROJECT_ROOT/ops/runtime/rawdatadir-fetch-last-copy-seconds}"
TIMEOUT="${BDAG_RAWDATADIR_TIMEOUT:-300s}"
PARALLELISM="${BDAG_RAWDATADIR_PARALLELISM:-4}"
LOG_FILE="${BDAG_RAWDATADIR_FETCH_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-fetch-$(date +%Y%m%d).log}"
EXISTING_DOWNLOAD_DIR="${BDAG_RAWDATADIR_EXISTING_DIR:-}"

source "$PROJECT_ROOT/ops/sync-startup-lag-policy.sh"
MIN_TIP="$(bdag_sync_min_tip_for_target "$TARGET_TIP" "$MIN_TIP_EXPLICIT" "$ACCEPTABLE_STARTUP_LAG_BLOCKS" "$COPY_MINUTE_BLOCK_ALLOWANCE" "$LAST_COPY_SECONDS_FILE")"
ACCEPTABLE_LAG_BLOCKS="$(bdag_sync_lag_threshold_blocks "$ACCEPTABLE_STARTUP_LAG_BLOCKS" "$COPY_MINUTE_BLOCK_ALLOWANCE" "$LAST_COPY_SECONDS_FILE")"

if [[ -n "$EXISTING_DOWNLOAD_DIR" ]]; then
  mkdir -p "$(dirname "$LOG_FILE")"
else
  mkdir -p "$STAGING_BASE" "$(dirname "$LOG_FILE")"
fi

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

fastsnap_help_has_flag() {
  local flag="${1#--}"
  grep -Eq "(^|[[:space:]])-{1,2}${flag}([[:space:]=]|$)" <<<"$FASTSNAP_HELP"
}

if [[ -z "$EXISTING_DOWNLOAD_DIR" ]]; then
  if [[ -z "$PEERS" ]]; then
    log "set BDAG_RAWDATADIR_PEERS to one or more libp2p multiaddrs"
    exit 1
  fi
  if ! command -v "$FASTSNAP_BIN" >/dev/null 2>&1 && [[ ! -x "$FASTSNAP_BIN" ]]; then
    log "fastsnap binary not found: $FASTSNAP_BIN"
    exit 1
  fi
fi
FASTSNAP_HELP=""
if [[ -z "$EXISTING_DOWNLOAD_DIR" ]]; then
  FASTSNAP_HELP="$("$FASTSNAP_BIN" --help 2>&1 || true)"
  if ! fastsnap_help_has_flag "--dir-out"; then
    log "fastsnap binary does not support directory artifact downloads (--dir-out): $FASTSNAP_BIN"
    exit 1
  fi
  if ! fastsnap_help_has_flag "--artifact-type"; then
    log "fastsnap binary does not support selecting raw datadir artifacts (--artifact-type): $FASTSNAP_BIN"
    exit 1
  fi
fi

STAMP="$(date +%Y%m%d-%H%M%S%Z)"
if [[ -n "$EXISTING_DOWNLOAD_DIR" ]]; then
  DOWNLOAD_DIR="$(readlink -m "$EXISTING_DOWNLOAD_DIR")"
  if [[ ! -d "$DOWNLOAD_DIR" ]]; then
    log "existing raw datadir artifact directory not found: $DOWNLOAD_DIR"
    exit 1
  fi
  log "using existing raw datadir artifact directory $DOWNLOAD_DIR"
else
  DOWNLOAD_DIR="$STAGING_BASE/rawdatadir-$STAMP"
  mkdir -p "$DOWNLOAD_DIR"
fi

fastsnap_args=(
  --artifact-type raw_datadir_checkpoint
  --legacy-fallback=false
  --network "$NETWORK"
  --min-tip "$MIN_TIP"
  --timeout "$TIMEOUT"
  --dir-out "$DOWNLOAD_DIR"
  --parallelism "$PARALLELISM"
)

old_ifs="$IFS"
IFS=', '
for peer in $PEERS; do
  [[ -n "$peer" ]] || continue
  fastsnap_args+=(--peer "$peer")
done
IFS="$old_ifs"

if [[ "$ALLOW_UNSIGNED" == "1" ]]; then
  fastsnap_args+=(--allow-unsigned)
fi

IFS=', '
for signer in $TRUSTED_SIGNERS; do
  [[ -n "$signer" ]] || continue
  fastsnap_args+=(--trusted-signer "$signer")
done
IFS="$old_ifs"

if [[ -z "$EXISTING_DOWNLOAD_DIR" ]]; then
  log "fetching raw datadir artifact into $DOWNLOAD_DIR"
  if [[ "$TARGET_TIP" =~ ^[0-9]+$ && -z "$MIN_TIP_EXPLICIT" ]]; then
    log "startup lag policy: target_tip=$TARGET_TIP acceptable_lag_blocks=$ACCEPTABLE_LAG_BLOCKS min_tip=$MIN_TIP"
  fi
  "$FASTSNAP_BIN" "${fastsnap_args[@]}" 2>&1 | tee -a "$LOG_FILE"
fi

ARCHIVE="$(find "$DOWNLOAD_DIR" -maxdepth 2 -type f -name '*no-private-keys.tar.zst' | sort | head -n1 || true)"
if [[ -z "$ARCHIVE" || ! -s "$ARCHIVE" ]]; then
  log "download did not contain a raw datadir archive"
  exit 1
fi

if [[ -s "$DOWNLOAD_DIR/SHA256SUMS" ]]; then
  (cd "$DOWNLOAD_DIR" && sha256sum -c SHA256SUMS) 2>&1 | tee -a "$LOG_FILE"
fi
tar --zstd -tf "$ARCHIVE" >/dev/null
log "raw datadir artifact downloaded and archive verified: $ARCHIVE"

if [[ -z "$TARGET_DIR" ]]; then
  log "download complete only; set BDAG_RAWDATADIR_IMPORT_TARGET to install"
  exit 0
fi

TARGET_DIR="$(readlink -m "$TARGET_DIR")"
PARENT_DIR="$(dirname "$TARGET_DIR")"
TMP_DIR="$PARENT_DIR/.rawdatadir-import-$STAMP.tmp"
BACKUP_DIR="$TARGET_DIR.before-rawdatadir-$STAMP"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"

if [[ -e "$TARGET_DIR" && "$REPLACE_EXISTING" != "1" ]]; then
  log "target exists; set BDAG_RAWDATADIR_IMPORT_REPLACE=1 after stopping the receiver node: $TARGET_DIR"
  exit 1
fi

log "extracting raw datadir archive to temporary target $TMP_DIR"
tar --zstd -xpf "$ARCHIVE" -C "$TMP_DIR"
if [[ ! -d "$TMP_DIR/BdagChain" ]]; then
  log "extracted archive does not contain BdagChain at target root"
  exit 1
fi

preserve_identity_path() {
  local rel="$1"
  local src="$TARGET_DIR/$rel"
  local dst="$TMP_DIR/$rel"
  if [[ ! -e "$src" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  if [[ -e "$dst" ]]; then
    if ! rm -rf "$dst" 2>/dev/null; then
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo rm -rf "$dst"
      else
        log "cannot replace staged identity path $rel; destination is not writable by $(id -un) and passwordless sudo is unavailable"
        return 1
      fi
    fi
  fi
  if ! cp -a "$src" "$dst" 2>/dev/null; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      sudo cp -a "$src" "$dst"
    else
      log "cannot preserve local identity path $rel; source is not readable by $(id -un) and passwordless sudo is unavailable"
      return 1
    fi
  fi
  log "preserved local identity path $rel"
}

if [[ -d "$TARGET_DIR" ]]; then
  preserve_identity_path network.key
  preserve_identity_path bdageth/nodekey
  preserve_identity_path keystore
  preserve_identity_path bdageth/keystore
  preserve_identity_path peerstore
fi

if [[ -e "$TARGET_DIR" ]]; then
  mv "$TARGET_DIR" "$BACKUP_DIR"
  log "parked old datadir at $BACKUP_DIR"
fi
mv "$TMP_DIR" "$TARGET_DIR"

log "raw datadir installed at $TARGET_DIR"
bdag_sync_record_copy_seconds "$LAST_COPY_SECONDS_FILE" "$SECONDS"
log "raw datadir fetch/import duration was ${SECONDS}s; next acceptable lag threshold $(bdag_sync_lag_threshold_blocks "$ACCEPTABLE_STARTUP_LAG_BLOCKS" "$COPY_MINUTE_BLOCK_ALLOWANCE" "$LAST_COPY_SECONDS_FILE") block(s)"
log "rollback: stop the node, move $TARGET_DIR aside, then mv $BACKUP_DIR $TARGET_DIR"
