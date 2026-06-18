#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/bdag-low-io-build.sh [command...]

Runs a Docker build command with low CPU and idle I/O priority while forcing
TMPDIR/TMP/TEMP to a disk-backed build scratch directory. If no command is
provided, it runs: docker compose build

Environment:
  BDAG_BUILD_TMPDIR   Build scratch directory. Defaults to ./.build-tmp.
  BDAG_BUILD_NICE     nice value. Defaults to 19.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

env_value() {
  local key="$1" file="${2:-$repo_root/.env}" line
  [[ -f "$file" ]] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 0
  line="${line#*=}"
  line="${line%$'\r'}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s\n' "$line"
}

build_tmp="${BDAG_BUILD_TMPDIR:-$(env_value BDAG_BUILD_TMPDIR)}"
if [[ -z "$build_tmp" ]]; then
  build_tmp="$repo_root/.build-tmp"
fi
if [[ "$build_tmp" != /* ]]; then
  build_tmp="$repo_root/$build_tmp"
fi
mkdir -p "$build_tmp"

export TMPDIR="$build_tmp"
export TMP="$build_tmp"
export TEMP="$build_tmp"
export DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"

command_args=("$@")
if [[ ${#command_args[@]} -eq 0 ]]; then
  command_args=(docker compose build)
fi

prefix=()
if command -v ionice >/dev/null 2>&1; then
  prefix+=(ionice -c 3)
fi
if command -v nice >/dev/null 2>&1; then
  prefix+=(nice -n "${BDAG_BUILD_NICE:-19}")
fi

exec "${prefix[@]}" "${command_args[@]}"
