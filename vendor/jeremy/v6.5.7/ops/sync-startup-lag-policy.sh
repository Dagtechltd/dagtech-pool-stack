#!/usr/bin/env bash

# Shared seed/startup acceptance policy for chain pre-sync. A recently copied
# seed should be close enough to start, then normal P2P sync should catch the
# tail instead of redoing the copy loop.

bdag_sync_nonnegative_int_or_default() {
  local value="${1:-}"
  local default="${2-0}"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  printf '%s\n' "$default"
}

bdag_sync_read_nonnegative_int_file() {
  local path="${1:-}"
  local value=""
  if [[ -n "$path" && -r "$path" ]]; then
    read -r value < "$path" || value=""
  fi
  bdag_sync_nonnegative_int_or_default "$value" 0
}

bdag_sync_lag_threshold_blocks() {
  local floor multiplier copy_seconds_file copy_seconds copy_minutes duration_lag
  floor="$(bdag_sync_nonnegative_int_or_default "${1:-}" 4000)"
  multiplier="$(bdag_sync_nonnegative_int_or_default "${2:-}" 4)"
  copy_seconds_file="${3:-}"
  copy_seconds="$(bdag_sync_nonnegative_int_or_default "${4:-}" "")"
  if [[ -z "$copy_seconds" ]]; then
    copy_seconds="$(bdag_sync_read_nonnegative_int_file "$copy_seconds_file")"
  fi

  copy_minutes=$(((copy_seconds + 59) / 60))
  duration_lag=$((copy_minutes * multiplier))
  if (( duration_lag > floor )); then
    printf '%s\n' "$duration_lag"
  else
    printf '%s\n' "$floor"
  fi
}

bdag_sync_min_tip_for_target() {
  local target_tip="${1:-}"
  local explicit_min_tip="${2:-}"
  local floor="${3:-}"
  local multiplier="${4:-}"
  local copy_seconds_file="${5:-}"
  local copy_seconds="${6:-}"
  local lag

  if [[ "$explicit_min_tip" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$explicit_min_tip"
    return 0
  fi
  if [[ ! "$target_tip" =~ ^[0-9]+$ ]]; then
    printf '0\n'
    return 0
  fi

  lag="$(bdag_sync_lag_threshold_blocks "$floor" "$multiplier" "$copy_seconds_file" "$copy_seconds")"
  if (( target_tip > lag )); then
    printf '%s\n' $((target_tip - lag))
  else
    printf '0\n'
  fi
}

bdag_sync_record_copy_seconds() {
  local path="${1:-}"
  local seconds="${2:-}"
  if [[ -z "$path" || ! "$seconds" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$seconds" > "$path"
}
