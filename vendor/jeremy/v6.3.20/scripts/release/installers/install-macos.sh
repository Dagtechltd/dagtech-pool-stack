#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export BDAG_INSTALL_OS=macos
exec bash "$SCRIPT_DIR/install-unix-common.sh" "$@"
