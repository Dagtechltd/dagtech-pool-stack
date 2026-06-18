#!/bin/bash
# Assemble split LFS parts (snapshots/lfs-parts/<stem>.000, .001, …) into
# snapshots/latest.bdsnap for Docker build and release tarball.
# Used by .github/workflows/build-*.yml after Git LFS checkout.
#
# Optional: SNAPSHOT_LFS_STEM=chunk — if set, use that stem; else auto-detect
# the single *.000 file under snapshots/lfs-parts/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f snapshots/latest.bdsnap ]] && [[ $(stat -c%s snapshots/latest.bdsnap) -ge 1024 ]]; then
  echo "ci-assemble: using existing snapshots/latest.bdsnap ($(stat -c%s snapshots/latest.bdsnap) bytes)"
  exit 0
fi

STEM="${SNAPSHOT_LFS_STEM:-}"
if [[ -z "$STEM" ]]; then
  shopt -s nullglob
  first=(snapshots/lfs-parts/*.000)
  shopt -u nullglob
  if [[ ${#first[@]} -eq 0 ]]; then
    echo "ci-assemble: no snapshots/lfs-parts/*.000 and no valid latest.bdsnap; build will use no-snapshot marker"
    exit 0
  fi
  if [[ ${#first[@]} -gt 1 ]]; then
    echo "ci-assemble: multiple *.000 in snapshots/lfs-parts; set SNAPSHOT_LFS_STEM explicitly" >&2
    exit 1
  fi
  base=$(basename "${first[0]}")
  STEM="${base%.000}"
fi

if [[ ! -f "snapshots/lfs-parts/${STEM}.000" ]]; then
  echo "ci-assemble: missing snapshots/lfs-parts/${STEM}.000" >&2
  exit 1
fi

chmod +x ./scripts/join-snapshot-parts.sh
./scripts/join-snapshot-parts.sh "$STEM" snapshots/latest.bdsnap
echo "ci-assemble: wrote snapshots/latest.bdsnap ($(stat -c%s snapshots/latest.bdsnap) bytes)"
