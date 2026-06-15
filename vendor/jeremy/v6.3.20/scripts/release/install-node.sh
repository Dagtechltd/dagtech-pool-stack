#!/usr/bin/env sh
# Installs a standalone BlockDAG node (no pool, dashboard, or ASIC services).
#
# Usage:
#   ./install-node.sh             installer prompts for archive vs non-archive
#   ./install-node.sh --archive   archive node (full history, no pruning)
#   ./install-node.sh --no-archive  non-archive node (pruned chain data)
#
# This is a shortcut for the standalone-node deployment; the installer still
# runs its two-step selection, with step 1 (deployment) preselected to "node".
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Step 1 (deployment) is fixed to a standalone node here.
BDAG_DEPLOY_KIND=node

for arg in "$@"; do
  case "$arg" in
    --archive) BDAG_CHAIN_MODE=archive ;;
    --no-archive) BDAG_CHAIN_MODE=non-archive ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

export BDAG_DEPLOY_KIND
[ -n "${BDAG_CHAIN_MODE:-}" ] && export BDAG_CHAIN_MODE

OS_NAME=$(uname -s 2>/dev/null || echo unknown)
case "$OS_NAME" in
  Linux)
    exec sh "$SCRIPT_DIR/installers/install-linux.sh"
    ;;
  Darwin)
    exec sh "$SCRIPT_DIR/installers/install-macos.sh"
    ;;
  MINGW*|MSYS*|CYGWIN*)
    if command -v powershell.exe >/dev/null 2>&1; then
      exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$SCRIPT_DIR/install.ps1"
    fi
    echo "Windows detected, but powershell.exe was not found. Run install.cmd or install.ps1 from Windows." >&2
    exit 1
    ;;
  *)
    echo "Unsupported operating system: $OS_NAME" >&2
    exit 1
    ;;
esac
