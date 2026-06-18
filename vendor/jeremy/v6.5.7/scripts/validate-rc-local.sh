#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
TMP_SOURCE="$TMP_ROOT/source"
TMP_RUNTIME="$TMP_ROOT/runtime"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

copy_source_tree() {
  mkdir -p "$TMP_SOURCE"
  python3 - "$ROOT" "$TMP_SOURCE" <<'PY'
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(sys.argv[1])
target = Path(sys.argv[2])
raw = subprocess.check_output(
    ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
)
for item in raw.split(b"\0"):
    if not item:
        continue
    rel = item.decode()
    source = root / rel
    if not source.exists() and not source.is_symlink():
        raise SystemExit(f"tracked source file is missing: {rel}")
    destination = target / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(source.readlink())
    else:
        shutil.copy2(source, destination)
PY
}

copy_source_tree
cd "$TMP_SOURCE"
python3 scripts/secret-scan-tracked-files.py
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q ops scripts
BDAG_RUNTIME_DIR="$TMP_RUNTIME/runtime" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/tests -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 python3 scripts/release_readiness_check_test.py
python3 scripts/check-doc-consistency.py
find "$TMP_SOURCE/ops" "$TMP_SOURCE/scripts" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$TMP_SOURCE/ops" "$TMP_SOURCE/scripts" -name '*.pyc' -delete 2>/dev/null || true
bash scripts/validate-release-build.sh .
