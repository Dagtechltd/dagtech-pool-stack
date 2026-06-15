#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
USB_ROOT=""
TARGET="${BDAG_NODE_MAINNET_DIR:-$PROJECT_ROOT/data/node/mainnet}"
EXECUTE=0
BWLIMIT="${BDAG_USB_RSYNC_BWLIMIT:-0}"
BACKUP_EXISTING=1

usage() {
  cat <<'USAGE'
Usage:
  ops/usb-sidecar-restore-from-drive.sh --usb-root /path/to/usb/blockdag-portable [--dry-run]
  ops/usb-sidecar-restore-from-drive.sh --usb-root /path/to/usb/blockdag-portable --target /path/to/stack/data/node/mainnet --execute

Seed or restore local chain data from a portable USB raw-datadir sidecar using
rsync deltas. The default is dry-run. Stop the node/pool before --execute:
  docker compose stop pool node

The script preserves the existing target by moving it aside before the first
execute copy unless --no-backup is set. Reruns update only changed files.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --usb-root) USB_ROOT="${2:-}"; shift 2 ;;
    --target) TARGET="${2:-}"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --dry-run) EXECUTE=0; shift ;;
    --bwlimit) BWLIMIT="${2:-}"; shift 2 ;;
    --no-backup) BACKUP_EXISTING=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$USB_ROOT" ]]; then
  echo "--usb-root is required" >&2
  usage >&2
  exit 2
fi
if [[ "$TARGET" != /* ]]; then
  TARGET="$PROJECT_ROOT/$TARGET"
fi
source_dir="$USB_ROOT/chain-sidecar/mainnet"
manifest="$USB_ROOT/manifests/rawdatadir-sidecar-safe-status.json"

if [[ ! -d "$source_dir/BdagChain" ]]; then
  echo "USB source does not look like a mainnet datadir: $source_dir" >&2
  exit 1
fi
if [[ "$TARGET" == "$source_dir" ]]; then
  echo "target equals source; refusing restore" >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required" >&2
  exit 1
fi

if [[ -f "$manifest" ]]; then
  python3 - "$manifest" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not payload.get("safe") or not payload.get("usable") or payload.get("unsafe_path_count", 1) != 0:
    raise SystemExit("USB sidecar manifest is not marked safe/usable")
PY
else
  echo "warning: no USB safe-status manifest found at $manifest" >&2
fi

rsync_args=(
  -a
  --delete
  --numeric-ids
  --one-file-system
  --partial
  --partial-dir=.rsync-partial
)
if [[ -n "$BWLIMIT" && "$BWLIMIT" != "0" ]]; then
  rsync_args+=(--bwlimit "$BWLIMIT")
fi
if [[ "$EXECUTE" != "1" ]]; then
  rsync_args+=(--dry-run --itemize-changes)
fi

echo "source=$source_dir"
echo "target=$TARGET"
echo "execute=$EXECUTE"
echo "backup_existing=$BACKUP_EXISTING"
echo "bwlimit=$BWLIMIT"

if [[ "$EXECUTE" == "1" ]]; then
  if [[ "$BACKUP_EXISTING" == "1" && -d "$TARGET" && ! -e "$TARGET/.usb-sidecar-restore-in-progress" ]]; then
    backup="${TARGET}.before-usb-restore-$(date -u +%Y%m%dT%H%M%SZ)"
    echo "moving existing target to $backup"
    mv "$TARGET" "$backup"
  fi
  mkdir -p "$TARGET"
  : > "$TARGET/.usb-sidecar-restore-in-progress"
else
  echo "Dry run. Add --execute after stopping pool/node to copy from USB."
fi

rsync "${rsync_args[@]}" "$source_dir/" "$TARGET/"

if [[ "$EXECUTE" == "1" ]]; then
  rm -f "$TARGET/.usb-sidecar-restore-in-progress"
  echo "restore copy complete"
  echo "Next:"
  echo "  docker compose up -d --no-deps postgres node dashboard"
  echo "  leave pool stopped until dashboard catch-up/readiness gates clear"
fi
