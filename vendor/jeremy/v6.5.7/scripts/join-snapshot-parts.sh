#!/bin/bash
# Reassemble chunks from snapshots/lfs-parts/ (see split-snapshot-for-lfs.sh).
#
# Usage: ./scripts/join-snapshot-parts.sh <stem> [output.bdsnap]
#   Example: ./scripts/join-snapshot-parts.sh latest snapshots/latest.bdsnap
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

STEM="${1:?Usage: $0 <stem> [output.bdsnap]}"
OUT="${2:-snapshots/${STEM}.bdsnap}"
CHUNK_DIR="snapshots/lfs-parts"

shopt -s nullglob
parts=( "$CHUNK_DIR/${STEM}".[0-9][0-9][0-9] )
shopt -u nullglob

if [[ ${#parts[@]} -eq 0 ]]; then
  echo "No chunks matching $CHUNK_DIR/${STEM}.???" >&2
  exit 1
fi

IFS=$'\n' sorted=( $(printf '%s\n' "${parts[@]}" | sort -V) )
unset IFS

mkdir -p "$(dirname "$OUT")"
cat "${sorted[@]}" > "$OUT"
echo "Wrote $OUT ($(numfmt --to=iec-i --suffix=B "$(stat -c%s "$OUT")" 2>/dev/null || stat -c%s "$OUT") bytes)"
