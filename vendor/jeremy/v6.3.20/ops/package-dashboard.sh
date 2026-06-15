#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="${1:-$PROJECT_ROOT/ops/runtime}"
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/bdag-dashboard-portable-$STAMP.tar.gz"

tar -czf "$OUT" \
  --exclude='./.git' \
  --exclude='./data' \
  --exclude='./data-restore' \
  --exclude='./ops/runtime' \
  --exclude='./ops/runtime-*' \
  --exclude='./ops/__pycache__' \
  --exclude='*.pyc' \
  -C "$PROJECT_ROOT" .

echo "$OUT"
