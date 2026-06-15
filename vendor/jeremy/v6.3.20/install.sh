#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OS_NAME=$(uname -s 2>/dev/null || echo unknown)
ARCH_NAME=$(uname -m 2>/dev/null || echo unknown)

case "$ARCH_NAME" in
  x86_64|amd64) BDAG_INSTALL_ARCH=amd64 ;;
  arm64|aarch64) BDAG_INSTALL_ARCH=arm64 ;;
  *)
    echo "Unsupported CPU architecture: $ARCH_NAME" >&2
    exit 1
    ;;
esac
export BDAG_INSTALL_ARCH

case "$OS_NAME" in
  Linux)
    export BDAG_INSTALL_OS=linux
    exec sh "$SCRIPT_DIR/installers/install-linux.sh" "$@"
    ;;
  Darwin)
    echo "macOS is not supported in this release yet. Only Linux is currently supported." >&2
    exit 1
    ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "Windows is not supported in this release yet. Only Linux is currently supported." >&2
    exit 1
    ;;
  *)
    echo "Unsupported operating system: $OS_NAME" >&2
    exit 1
    ;;
esac
