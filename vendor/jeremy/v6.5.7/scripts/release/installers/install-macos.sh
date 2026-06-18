#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export BDAG_INSTALL_OS=macos
: "${BDAG_SNAPSHOT_DOWNLOADER:=aria2c}"
: "${BDAG_INSTALL_ARIA2:=1}"
: "${BDAG_BROWSER_SNAPSHOT_FALLBACK:=1}"
export BDAG_SNAPSHOT_DOWNLOADER
export BDAG_INSTALL_ARIA2
export BDAG_BROWSER_SNAPSHOT_FALLBACK
exec bash "$SCRIPT_DIR/install-unix-common.sh" "$@"
