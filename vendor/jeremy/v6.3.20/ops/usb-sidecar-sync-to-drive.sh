#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATUS_FILE="${BDAG_RAWDATADIR_SIDECAR_SAFE_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-sidecar-safe-status.json}"
USB_ROOT=""
EXECUTE=0
BWLIMIT="${BDAG_USB_RSYNC_BWLIMIT:-4096}"

usage() {
  cat <<'USAGE'
Usage:
  ops/usb-sidecar-sync-to-drive.sh --usb-root /path/to/usb/blockdag-portable [--dry-run]
  ops/usb-sidecar-sync-to-drive.sh --usb-root /path/to/usb/blockdag-portable --execute

Delta-sync the verified local raw-datadir sidecar to a portable USB holder.
The default is dry-run. Use --execute to create/update:
  <usb-root>/chain-sidecar/mainnet

The copy is rsync-friendly: reruns transfer only changed blocks/files.
Private node identity files, keystores, locks, sockets, and rsync temp files are
excluded so the USB can seed another system without cloning this node identity.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --usb-root) USB_ROOT="${2:-}"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --dry-run) EXECUTE=0; shift ;;
    --bwlimit) BWLIMIT="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$USB_ROOT" ]]; then
  echo "--usb-root is required" >&2
  usage >&2
  exit 2
fi
if [[ ! -f "$STATUS_FILE" ]]; then
  echo "missing sidecar safe-status file: $STATUS_FILE" >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required" >&2
  exit 1
fi

json_field() {
  python3 - "$STATUS_FILE" "$1" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload.get(sys.argv[2])
if isinstance(value, bool):
    print("1" if value else "0")
elif value is None:
    print("")
else:
    print(value)
PY
}

safe="$(json_field safe)"
usable="$(json_field usable)"
unsafe_count="$(json_field unsafe_path_count)"
sidecar_dir="$(json_field latest_safe_dir)"

if [[ "$safe" != "1" || "$usable" != "1" || "${unsafe_count:-999}" != "0" ]]; then
  echo "sidecar is not verified safe; refusing USB sync" >&2
  python3 -m json.tool "$STATUS_FILE" >&2 || true
  exit 1
fi
if [[ -z "$sidecar_dir" || ! -d "$sidecar_dir/BdagChain" ]]; then
  echo "verified sidecar path is missing or is not a mainnet datadir: $sidecar_dir" >&2
  exit 1
fi

target="$USB_ROOT/chain-sidecar/mainnet"
manifest_dir="$USB_ROOT/manifests"
rsync_args=(
  -a
  --delete
  --numeric-ids
  --one-file-system
  --partial
  --partial-dir=.rsync-partial
  "--exclude=/network.key*"
  "--exclude=/bdageth/nodekey*"
  "--exclude=/bdageth/LOCK"
  "--exclude=/bdageth/chaindata/LOCK"
  "--exclude=/bdageth/nodes*"
  "--exclude=/keystore*"
  "--exclude=/bdageth/keystore*"
  "--exclude=/peerstore*"
  "--exclude=/nodes*"
  "--exclude=/transactions.rlp"
  "--exclude=/bdageth/transactions.rlp"
  "--exclude=/.rsync-partial"
  "--exclude=/LOCK"
  "--exclude=/BdagChain/LOCK"
  "--exclude=*.ipc"
  "--exclude=*.sock"
)
if [[ -n "$BWLIMIT" && "$BWLIMIT" != "0" ]]; then
  rsync_args+=(--bwlimit "$BWLIMIT")
fi
if [[ "$EXECUTE" != "1" ]]; then
  rsync_args+=(--dry-run --itemize-changes)
fi

echo "source=$sidecar_dir"
echo "target=$target"
echo "execute=$EXECUTE"
echo "bwlimit=$BWLIMIT"

if [[ "$EXECUTE" == "1" ]]; then
  mkdir -p "$target" "$manifest_dir"
else
  echo "Dry run. Add --execute to update the USB holder."
  if [[ ! -d "$target" ]]; then
    echo "Target does not exist yet, so rsync itemization is skipped until --execute creates it."
    exit 0
  fi
fi

rsync "${rsync_args[@]}" "$sidecar_dir/" "$target/"

if [[ "$EXECUTE" == "1" ]]; then
  cp -a "$STATUS_FILE" "$manifest_dir/rawdatadir-sidecar-safe-status.json"
  cat > "$USB_ROOT/README-BLOCKDAG-CHAIN-SIDECAR.txt" <<EOF
BlockDAG portable chain sidecar

Updated: $(date -Is)
Source sidecar: $sidecar_dir
Portable chain data: chain-sidecar/mainnet
Safe-status manifest: manifests/rawdatadir-sidecar-safe-status.json

Use this drive with:
  ops/usb-sidecar-restore-from-drive.sh --usb-root "$USB_ROOT" --target /path/to/stack/data/node/mainnet --dry-run
  ops/usb-sidecar-restore-from-drive.sh --usb-root "$USB_ROOT" --target /path/to/stack/data/node/mainnet --execute
EOF
fi
